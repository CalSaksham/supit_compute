from __future__ import annotations

import asyncio
import json
import math
import subprocess
import time
from pathlib import Path

import httpx
import yaml

from . import checks
from .backends import Backend
from .evidence import EvidenceLog
from .traffic import Gateway, LoadGenerator, Metrics
from .util import clock, iso, median, percentile, sha256, threshold

SERIES_KEYS = {"lambda": "a", "coreweave": "b", "together": "serverless"}
RECOVERY_HOLD_S = 4


def host_info() -> dict:
    def sysctl(key):
        return subprocess.run(["sysctl", "-n", key], capture_output=True, text=True).stdout.strip()
    mem = sysctl("hw.memsize")
    return {"chip": sysctl("machdep.cpu.brand_string") or "Unknown", "memory_gb": round(int(mem) / 2**30) if mem.isdigit() else None}


class LiveSession:
    def __init__(self, root: Path):
        self.root = root
        self.data = root / "live" / "data"
        (self.data / "logs").mkdir(parents=True, exist_ok=True)
        contract_path = root / "live" / "contract.live.yaml"
        self.contract_yaml = contract_path.read_text()
        self.contract = yaml.safe_load(self.contract_yaml)
        self.contract_hash = sha256(self.contract)
        perf = self.contract["performance"]
        self.floor = threshold(perf["output_tokens_per_sec"], 35)
        self.p95_target = threshold(perf["p95_latency_ms"], 2000)
        self.grace = float(perf.get("window_seconds", 20))
        model = self.contract["workload"]["model"]
        logs = self.data / "logs"
        self.backends = {
            "lambda": Backend("lambda", "Lambda", "primary", 11501, model, logs),
            "coreweave": Backend("coreweave", "CoreWeave", "secondary", 11502, model, logs),
            "together": Backend("together", "Together AI serverless", "serverless", 11503, model, logs),
        }
        self.prices = {"lambda": self.contract["price_per_gpu_hour"], "coreweave": self.contract["demo_backup_pricing"]["secondary_per_gpu_hour"], "together": self.contract["demo_backup_pricing"]["serverless_per_million_tokens"]}
        self.client = httpx.AsyncClient(limits=httpx.Limits(max_connections=64, max_keepalive_connections=16))
        self.evidence = EvidenceLog(self.data)
        self.metrics = Metrics()
        self.gateway = Gateway(self.backends, self.metrics, self.client)
        self.gateway.on_faulty = self._restart_faulty
        self.loadgen = LoadGenerator(self.gateway, workers=3, num_predict=48)
        self.host = host_info()
        self.ollama_version = None
        bar_path = self.data / "bar.json"
        self.bar = json.loads(bar_path.read_text()) if bar_path.exists() else None
        self.tasks: list[asyncio.Task] = []
        self.busy: str | None = None
        self.phase = "starting"
        self._new_session()

    # ---------- lifecycle ----------

    def _new_session(self):
        self.started = time.time()
        self.accounting = False
        self.intervals: list[dict] = []
        self.incidents: list[dict] = []
        self.certificate = {"status": "PENDING", "checks": []}
        self.bakeoff = None
        self.injected = {"outage_at": None, "slow_at": None}
        self.prev_status = {k: b.status for k, b in self.backends.items()}
        self.slow_pending_since = None
        self.slow_pending_tps = None
        self.slow_open: dict | None = None
        self.above_since = None
        self.outage_open: dict | None = None
        self.metrics.reset()

    async def startup(self):
        await asyncio.gather(*(b.start(self.client) for b in self.backends.values()))
        for b in self.backends.values():
            self.gateway.set_status(b, "up")
        self.gateway.recovered_at = 0
        self.ollama_version = (await self.client.get(self.backends["lambda"].url + "/api/version")).json().get("version")
        if not self.bar:
            self.phase = "calibrating"
            self.bar = await checks.calibrate(self)
            (self.data / "bar.json").write_text(json.dumps(self.bar, indent=2))
            self.evidence.record("bar_measured", self.bar)
        self.prev_status = {k: b.status for k, b in self.backends.items()}
        self.evidence.record("session_started", {
            "mode": "live", "contract_hash": self.contract_hash, "host": self.host, "ollama_version": self.ollama_version,
            "backends": {k: {"name": b.name, "port": b.port, "pid": b.pid} for k, b in self.backends.items()},
            "description": "Live session started on this Mac. Three local Ollama servers stand in for Lambda, CoreWeave and Together AI.",
        })
        self.started = time.time()
        self.metrics.reset()
        self.loadgen.start()
        self.tasks += [asyncio.create_task(self.gateway.health_loop()), asyncio.create_task(self._detect_loop())]
        self.phase = "running"

    async def shutdown(self):
        for t in self.tasks + self.loadgen.tasks:
            t.cancel()
        for b in self.backends.values():
            b.kill()
        await self.client.aclose()

    # ---------- incidents & evidence ----------

    def _incident(self, type_: str, title: str, description: str, severity: str, injected: bool, payload: dict) -> dict:
        entry = self.evidence.record(type_, {"title": title, "description": description, "severity": severity, "cause_injected": injected, "contract_hash": self.contract_hash, **payload})
        incident = {"id": entry["id"], "timestamp": entry["timestamp"], "title": title, "description": description, "severity": severity,
                    "attribution": "Detected by the gateway · cause injected for the demo" if injected else "Detected by the gateway · provider fault",
                    "injected": injected, "detected": True, "evidence_id": entry["id"]}
        self.incidents.insert(0, incident)
        return entry

    def _info(self, title: str, description: str, payload: dict | None = None):
        entry = self.evidence.record("operator_action", {"title": title, "description": description, **(payload or {})})
        self.incidents.insert(0, {"id": entry["id"], "timestamp": entry["timestamp"], "title": title, "description": description, "severity": "info",
                                  "attribution": "Operator action on this Mac", "injected": True, "detected": False, "evidence_id": entry["id"]})

    # ---------- detection ----------

    def _traffic_window(self):
        rs = self.metrics.recent(30)
        if not rs:
            return
        lat = [r["latency_ms"] for r in rs]
        by = {k: sum(1 for r in rs if r["ok"] and r["backend"] == k) for k in self.backends}
        self.evidence.record("traffic_window", {
            "title": f"30 s of traffic: {len(rs)} requests, {sum(1 for r in rs if not r['ok'])} failed",
            "window_s": 30, "requests": len(rs), "failed": sum(1 for r in rs if not r["ok"]), "served_by": by,
            "p95_ms": round(percentile(lat, 0.95)), "lambda_tokens_per_sec": median([r["decode_tps"] for r in rs if r["backend"] == "lambda" and r.get("decode_tps")]),
            "contract_hash": self.contract_hash,
        })

    async def _detect_loop(self):
        last_window = time.time()
        while True:
            try:
                if time.time() - last_window >= 30:
                    last_window = time.time()
                    self._traffic_window()
                self._detect()
            except Exception as e:  # keep detecting even if one tick fails
                print("detect error:", repr(e))
            await asyncio.sleep(0.25)

    def lambda_speed(self):
        """Median of Lambda's last 4 completed requests, or a lower in-flight bound if a request is taking too long."""
        L = self.backends["lambda"]
        rs = [r for r in self.metrics.recent(20, backend="lambda") if r.get("decode_tps") and r["t_end"] >= L.changed_at][:4]
        measured = median([r["decode_tps"] for r in rs]) if len(rs) >= 2 else None
        bound = self.gateway.inflight_bound("lambda")
        if bound is not None and (measured is None or bound < measured):
            return bound, True
        return measured, False

    def _detect(self):
        now = time.time()
        L = self.backends["lambda"]
        prev = self.prev_status["lambda"]
        if prev == "up" and L.status != "up" and not self.outage_open:
            self._open_outage(L.changed_at)
        elif L.status == "up" and self.outage_open:
            self._close_outage(L.changed_at)
        for k, b in self.backends.items():
            if k != "lambda" and self.prev_status[k] == "up" and b.status == "down":
                self._incident("incident_detected", f"{b.name} became unreachable", "Health checks failed. The gateway stopped routing to it.", "warning", False, {"provider": k})
        self.prev_status = {k: b.status for k, b in self.backends.items()}

        if L.status != "up":
            self.slow_pending_since = None
            if self.slow_open:
                self._close_slow(L.changed_at, superseded=True)
            return
        tps, bound = self.lambda_speed()
        if tps is None:
            return
        basis = (f"A {self.loadgen.num_predict}-token answer is still running after {self.loadgen.num_predict / tps + 1:.1f} s, so Lambda is producing at most {tps:.1f} tokens/s"
                 if bound else f"Measured {tps:.1f} tokens/s (median of Lambda's last 4 real requests)")
        if tps < self.floor:
            self.above_since = None
            if not self.slow_pending_since and not self.slow_open:
                self.slow_pending_since = now
                self.slow_pending_tps = tps
                self._incident("incident_detected", "Lambda is below the speed floor",
                               f"{basis}, against the {self.floor:g} floor. Confirming over the {self.grace:g}-second contract window before credits start.",
                               "warning", self.backends["lambda"].slow, {"provider": "lambda", "kind": "slow_pending", "tokens_per_sec": round(tps, 2), "floor": self.floor})
            elif self.slow_pending_since and now - self.slow_pending_since >= self.grace:
                entry = self._incident("incident_detected", "Confirmed: Lambda is below the speed floor",
                                       f"Still {tps:.1f} tokens/s after {self.grace:g} s. Slow credits now accrue at half price for every GPU serving the model."
                                       + (" Cause: Lambda's model server is being throttled to 15% duty (demo)." if L.slow else ""),
                                       "warning", L.slow, {"provider": "lambda", "kind": "slow_confirmed", "tokens_per_sec": round(tps, 2), "floor": self.floor})
                self.slow_open = {"kind": "slow", "start": now, "end": None, "evidence_id": entry["id"], "injected": L.slow, "tps": tps}
                self.intervals.append(self.slow_open)
                self.slow_pending_since = None
        else:
            if self.slow_pending_since:
                self.slow_pending_since = None
            if self.slow_open:
                self.above_since = self.above_since or now
                if now - self.above_since >= RECOVERY_HOLD_S:
                    self._close_slow(self.above_since, tps=tps)

    def _open_outage(self, t: float):
        inj = self.injected["outage_at"]
        injected = bool(inj and t - inj < 60)
        lag = f" {(t - inj) * 1000:.0f} ms after the process was killed." if injected else ""
        entry = self._incident("incident_detected", "Lambda went offline. Your traffic stayed online.",
                               f"Lambda stopped answering at {clock(t)}.{lag} The gateway moved traffic to CoreWeave and Together AI and retried in-flight requests.",
                               "critical", injected, {"provider": "lambda", "kind": "outage", "detected_at": iso(t), "injected_at": iso(inj) if injected else None,
                                                      "detection_ms": round((t - inj) * 1000) if injected else None})
        self.outage_open = {"kind": "outage", "start": t, "end": None, "evidence_id": entry["id"], "injected": injected}
        self.intervals.append(self.outage_open)

    def _close_outage(self, t: float):
        iv = self.outage_open
        iv["end"] = t
        rs = self.metrics.between(iv["start"], t)
        retried = sum(1 for r in rs if any(a["backend"] == "lambda" for a in r["attempts"]))
        failed = sum(1 for r in rs if not r["ok"])
        by = {k: sum(1 for r in rs if r["ok"] and r["backend"] == k) for k in self.backends}
        self._incident("incident_resolved", "Lambda recovered",
                       f"Down for {t - iv['start']:.1f} s. Backups served {by['coreweave'] + by['together']} requests ({by['coreweave']} CoreWeave, {by['together']} Together AI); "
                       f"{retried} in-flight requests were retried; {failed} failed. Traffic ramps back to Lambda over 10 seconds.",
                       "info", iv["injected"], {"provider": "lambda", "kind": "outage_resolved", "downtime_s": round(t - iv["start"], 2), "served_by": by, "retried": retried, "failed": failed})
        self.outage_open = None

    def _close_slow(self, t: float, tps: float | None = None, superseded: bool = False):
        iv = self.slow_open
        iv["end"] = t
        if not superseded:
            self._incident("incident_resolved", "Lambda is back above the speed floor",
                           f"Recovered to {tps:.1f} tokens/s. Below the floor for {t - iv['start']:.1f} s after the contract window.",
                           "info", iv["injected"], {"provider": "lambda", "kind": "slow_resolved", "slow_s": round(t - iv["start"], 2), "tokens_per_sec": round(tps, 2)})
        self.slow_open = None
        self.above_since = None

    # ---------- actions ----------

    async def action(self, kind: str, accounting: bool | None = None):
        L = self.backends["lambda"]
        if accounting is not None and bool(accounting) != self.accounting:
            self.accounting = bool(accounting)
            self.evidence.record("demo_accounting_changed", {"enabled": self.accounting, "description": "Count demo-injected faults in the claim illustration."})
        if kind == "accounting":
            return
        if kind == "slow":
            if L.status != "up":
                raise ValueError("Lambda is offline. Restore it first.")
            if not L.slow:
                L.start_throttle()
                self.injected["slow_at"] = time.time()
                self._info("Demo: Lambda is being throttled", "Operator capped Lambda's model runner to 15% duty, like a GPU power cap. Health checks still pass; the gateway is not told and has to notice from measured speed.",
                           {"fault": "slow", "method": "SIGSTOP/SIGCONT duty cycle on Lambda's model runner (40 ms on, 220 ms off)"})
        elif kind == "outage":
            if L.pid is None:
                raise ValueError("Lambda is already offline.")
            pid = L.pid
            self.injected["outage_at"] = time.time()
            L.kill()
            self._info("Demo: Lambda's server was killed", f"Operator sent SIGKILL to the Lambda Ollama process group (pid {pid}). The gateway is not told; it has to notice.",
                       {"fault": "outage", "pid": pid})
        elif kind == "restore":
            was_slow = L.slow
            L.stop_throttle()
            if L.pid is None:
                self._info("Demo: restarting Lambda", "Operator restarted the Lambda Ollama server. Traffic returns once it answers health checks.")
                asyncio.create_task(self._restart(L))
            elif was_slow:
                self._info("Demo: Lambda's throttle removed", "Operator removed the cap on Lambda's model runner. It runs at full speed again.")
        elif kind == "accept":
            self._start_job("handover", checks.run_handover(self))
        elif kind == "bakeoff":
            self._start_job("bakeoff", checks.run_bakeoff(self))
        elif kind == "reset":
            L.stop_throttle()
            if L.pid is None:
                asyncio.create_task(self._restart(L))
            self._new_session()
            self.evidence.record("session_reset", {"description": "New live session. Prior evidence stays in the hash chain.", "contract_hash": self.contract_hash})

    async def _restart(self, b: Backend):
        try:
            await b.start(self.client)
            self.gateway.set_status(b, "up")
        except Exception as e:
            b.status = "down"
            self._info(f"{b.name} failed to restart", str(e))

    async def _restart_faulty(self, b: Backend):
        self._incident("incident_detected", f"{b.name}'s server stopped producing tokens",
                       f"Its process was running but every completion came back empty (see {b.log_path.name}). SUPIT routed around it and restarted it.",
                       "critical" if b.id == "lambda" else "warning", False, {"provider": b.id, "kind": "server_fault"})
        b.kill()
        await self._restart(b)

    def _start_job(self, name: str, coro):
        if self.busy:
            coro.close()
            raise ValueError(f"The {self.busy} is still running.")
        self.busy = name

        async def wrapper():
            try:
                await coro
            finally:
                self.busy = None
                self.loadgen.running.set()
        self.tasks.append(asyncio.create_task(wrapper()))

    # ---------- claim & state ----------

    def _counted(self):
        return [iv for iv in self.intervals if iv["start"] >= self.started and (self.accounting or not iv["injected"])]

    def claim(self) -> dict:
        now = time.time()
        c, bp = self.contract, self.contract["demo_backup_pricing"]
        gpus, price, share = c["cluster"]["gpus"], c["price_per_gpu_hour"], c["holdback"]["share_of_invoice"]
        duration = now - self.started
        counted = self._counted()
        outages = [(iv["start"], iv["end"] or now) for iv in counted if iv["kind"] == "outage"]
        down_s = sum(e - s for s, e in outages)
        slow_s = 0.0
        for iv in counted:
            if iv["kind"] == "slow":
                s, e = iv["start"], iv["end"] or now
                overlap = sum(max(0, min(e, oe) - max(s, os_)) for os_, oe in outages)
                slow_s += max(0, e - s - overlap)
        down, slow = down_s / 60 * gpus, slow_s / 60 * gpus
        rescued = [r for s, e in outages for r in self.metrics.between(s, e) if r["ok"]]
        cw = sum(1 for r in rescued if r["backend"] == "coreweave")
        tg = [r for r in rescued if r["backend"] == "together"]
        tg_tokens = sum(r.get("output_tokens", 0) for r in tg)
        cw_share = cw / (cw + len(tg)) if (cw + len(tg)) else 0.8
        invoice = duration / 3600 * gpus * price
        holdback = invoice * share
        downcredit, slowcredit = down * price / 60, slow * price / 120
        rescue = down * cw_share * (bp["secondary_per_gpu_hour"] - price) / 60 + tg_tokens * bp["serverless_per_million_tokens"] / 1e6
        total = downcredit + slowcredit + rescue
        applied = min(holdback, total)
        return {
            "duration_seconds": duration, "contracted_gpu_minutes": duration / 60 * gpus,
            "downtime_gpu_minutes": down, "slow_gpu_minutes": slow, "credits": downcredit + slowcredit,
            "downtime_credit": downcredit, "slow_credit": slowcredit, "rescue_cost": rescue,
            "invoice": invoice, "holdback": holdback, "total_claim": total, "holdback_applied": applied, "provider_owed": invoice - applied,
            "projected_monthly_claim": total * 2592000 / duration if duration else 0,
            "evidence_ids": [iv["evidence_id"] for iv in counted], "demo_accounting": self.accounting,
            "period_start": iso(self.started), "period_end": iso(now), "synthetic": False,
            "measured": {"rescue_requests_coreweave": cw, "rescue_requests_together": len(tg), "serverless_output_tokens": tg_tokens, "downtime_s": down_s, "slow_s": slow_s},
        }

    def _gpus(self, outage: bool, slow: bool, pending: bool):
        t = time.time()
        out = []
        for i in range(8):
            s = slow and i == 3
            out.append({
                "id": i, "name": f"GPU {i}", "status": "offline" if outage else "pending" if s and pending else "slow" if s else "healthy",
                "clock_mhz": 0 if outage else 1147 if s else round(1830 + 18 * math.sin(t / 5 + i)),
                "temp_c": 0 if outage else round(61 + 3 * math.sin(t / 9 + i)),
                "power_w": 0 if outage else 195 if s else round(530 + 22 * math.sin(t / 7 + i)),
                "power_limit_w": 200 if s else 700, "speed_ratio": 0 if outage else 0.4 if s else 0.98,
                "reason": "Simulated tile. Lambda's real server is offline." if outage else "Simulated tile. Lambda's real server is measured below the speed floor." if s else "Simulated tile. Lambda's real server is within the speed floor.",
            })
        return out

    def snapshot(self) -> dict:
        now = time.time()
        L = self.backends["lambda"]
        outage = L.status != "up"
        slow = bool(self.slow_pending_since or self.slow_open)
        session = self.metrics.session()
        failed = sum(1 for r in session if not r["ok"])
        recent = self.metrics.recent(30)
        lat = [r["latency_ms"] for r in recent]
        tps, bound = self.lambda_speed() if not outage else (None, False)
        last10 = self.metrics.recent(10)
        weights = self.gateway.weights()
        served = [r for r in last10 if r["ok"] and self.backends[r["backend"]].status == "up" and r["t_end"] >= self.backends[r["backend"]].changed_at]
        share = {k: (sum(1 for r in served if r["backend"] == k) / len(served) * 100) if served else weights[k] * 100 for k in self.backends}
        stats = {k: self.gateway.backend_stats(k) for k in self.backends}

        def provider_status(k, b):
            if b.status != "up":
                return "offline"
            if k == "lambda" and slow:
                return "degraded"
            if k == "together" and weights[k] == 0:
                return "standby"
            return "healthy"

        return {
            "mode": "live", "phase": self.phase, "started_at": iso(self.started),
            "contract": self.contract, "contract_hash": self.contract_hash,
            "gpus": self._gpus(outage, slow, bool(self.slow_pending_since)),
            "providers": [{"id": k, "name": b.name, "status": provider_status(k, b), "weight": share[k], "price": self.prices[k]} for k, b in self.backends.items()],
            "traffic": {
                "rps": len(self.metrics.recent(10, ok=None)) / 10,
                "tokens_per_sec_per_gpu": tps, "success_rate": 100 * (1 - failed / len(session)) if session else 100,
                "p50_ms": percentile(lat, 0.5) or 0, "p95_ms": percentile(lat, 0.95) or 0,
                "total_requests": len(session), "failed_requests": failed,
                "failovers": sum(1 for r in session if r["failover"]),
                "series": self.metrics.series(SERIES_KEYS),
            },
            "incidents": self.incidents[:100],
            "certificate": self.certificate,
            "claim": self.claim(),
            "evidence_count": len(self.evidence.entries),
            "demo_accounting": self.accounting,
            "faults": {"slow": slow, "slow_pending": bool(self.slow_pending_since), "outage": outage},
            "live": {
                "host": self.host, "ollama_version": self.ollama_version, "floor": self.floor, "p95_target": self.p95_target, "grace_s": self.grace,
                "bar": self.bar, "busy": self.busy, "bakeoff": self.bakeoff, "load_paused": not self.loadgen.running.is_set(),
                "injected": {"slow": L.slow, "outage": L.pid is None},
                "backends": [{"id": k, "name": b.name, "port": b.port, "pid": b.pid, "status": b.status, **stats[k]} for k, b in self.backends.items()],
                "slow_pending_s": round(now - self.slow_pending_since, 1) if self.slow_pending_since else None,
                "speed_bound": bound,
                "slow_tps": median([iv["tps"] for iv in self._counted() if iv["kind"] == "slow"]),
            },
        }

    def telemetry(self) -> dict:
        recent = list(self.metrics.records)[-25:]
        return {
            "host": self.host, "ollama_version": self.ollama_version, "bar": self.bar,
            "backends": [{"id": k, "name": b.name, "url": b.url, "pid": b.pid, "status": b.status, "throttle": "15% duty (demo)" if b.slow else "none", **self.gateway.backend_stats(k)} for k, b in self.backends.items()],
            "recent_requests": [{"time": clock(r["t_end"]), "backend": r["backend"], "ok": r["ok"], "latency_ms": round(r["latency_ms"]),
                                 "tokens_per_sec": round(r["decode_tps"], 1) if r.get("decode_tps") else None, "output_tokens": r.get("output_tokens"),
                                 "retried_from": [a["backend"] for a in r["attempts"]] or None, "source": r["source"]} for r in reversed(recent)],
        }
