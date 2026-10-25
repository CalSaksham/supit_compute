from __future__ import annotations

import asyncio
import itertools
import random
import time
from collections import defaultdict, deque

import httpx

from .backends import BackendError
from .util import median, percentile

SERIES_SECONDS = 300
CONNECTION_ERRORS = (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)

PROMPTS = [
    "Summarize this support ticket in one sentence: 'My invoice shows two charges for the same month and I need a refund.'",
    "Classify the sentiment of this review as positive, neutral or negative: 'Setup took ten minutes and it just works.'",
    "Extract the company name and amount: 'Acme Corp approved a purchase order for $48,000 on Friday.'",
    "Write a two-sentence product description for a noise-cancelling headset for call centers.",
    "Translate to Spanish: 'Your order has shipped and will arrive on Thursday.'",
    "Give three bullet points on why caching reduces inference cost.",
    "Rewrite this more politely: 'Send me the report now.'",
    "What is the capital of Australia? Answer in one sentence with one fact about it.",
    "Suggest a subject line for an email announcing a new pricing tier.",
    "Explain what p95 latency means to a non-technical founder in two sentences.",
    "Turn this into a SQL WHERE clause: customers in California who signed up after January 2025.",
    "List two risks of relying on a single GPU cloud provider.",
    "Draft a one-line reply confirming a meeting on Tuesday at 3pm.",
    "Name the main ingredient of guacamole and one way to keep it from browning.",
    "Convert 72 degrees Fahrenheit to Celsius and show the formula.",
    "Write a haiku about a data center at night.",
]


class Metrics:
    def __init__(self):
        self.records: deque = deque(maxlen=50000)
        self.session_start = time.time()

    def add(self, rec: dict):
        self.records.append(rec)

    def reset(self):
        self.session_start = time.time()

    def recent(self, seconds: float, backend: str | None = None, ok: bool | None = True, sources=("load", "external")):
        since = time.time() - seconds
        out = []
        for r in reversed(self.records):
            if r["t_end"] < since:
                break
            if r["source"] in sources and (backend is None or r["backend"] == backend) and (ok is None or r["ok"] == ok):
                out.append(r)
        return out

    def between(self, start: float, end: float, sources=("load", "external")):
        return [r for r in self.records if start <= r["t_end"] <= end and r["source"] in sources]

    def session(self):
        rs = [r for r in self.records if r["t_end"] >= self.session_start and r["source"] in ("load", "external")]
        return rs

    def series(self, keys: dict[str, str]):
        """Per-second points for the last 5 minutes: requests/sec per backend (5 s smoothing) and p95 (20 s window)."""
        now = int(time.time())
        start = max(now - SERIES_SECONDS - 25, int(self.session_start))
        counts: dict[int, dict] = defaultdict(lambda: defaultdict(int))
        lat: dict[int, list] = defaultdict(list)
        for r in reversed(self.records):
            if r["t_end"] < start:
                break
            if r["source"] not in ("load", "external"):
                continue
            sec = int(r["t_end"])
            if r["ok"]:
                counts[sec][keys.get(r["backend"], r["backend"])] += 1
                lat[sec].append(r["latency_ms"])
        points = []
        for sec in range(max(now - SERIES_SECONDS, int(self.session_start)), now):
            point = {"time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(sec)) + "Z"}
            for k in keys.values():
                point[k] = sum(counts[s][k] for s in range(sec - 4, sec + 1)) / 5
            window = [v for s in range(sec - 19, sec + 1) for v in lat[s]]
            point["p95"] = percentile(window, 0.95) or 0
            points.append(point)
        return points


class Gateway:
    """Weighted routing with health checks and transparent retry on connection failure."""

    ORDER = ("lambda", "coreweave", "together")

    def __init__(self, backends: dict, metrics: Metrics, client: httpx.AsyncClient):
        self.backends, self.metrics, self.client = backends, metrics, client
        self.recovered_at = 0.0
        self.inflight: dict[int, tuple[str, float, int]] = {}
        self._seq = itertools.count()
        self.on_faulty = None  # async callback(backend) when a server keeps failing real completions

    def inflight_bound(self, key: str, min_elapsed: float = 4.0, prefill_allowance: float = 1.0):
        """Upper bound on output speed from requests still waiting: n tokens can't have streamed faster than n / elapsed."""
        now = time.time()
        bounds = [n / (now - t0 - prefill_allowance) for k, t0, n in self.inflight.values() if k == key and now - t0 >= min_elapsed]
        return min(bounds) if bounds else None

    def set_status(self, b, status: str):
        if b.status != status:
            b.status = status
            b.changed_at = time.time()
            if b.id == "lambda" and status == "up":
                self.recovered_at = b.changed_at

    def weights(self) -> dict[str, float]:
        up = {k for k, b in self.backends.items() if b.status == "up"}
        w = {k: 0.0 for k in self.ORDER}
        if "lambda" in up:
            w["lambda"] = 0.8 * min(1.0, (time.time() - self.recovered_at) / 10) if self.recovered_at else 0.8
            rest = 1 - w["lambda"]
            w["coreweave" if "coreweave" in up else "together" if "together" in up else "lambda"] += rest
        elif "coreweave" in up and "together" in up:
            w["coreweave"], w["together"] = 0.8, 0.2
        elif up:
            w[next(k for k in self.ORDER if k in up)] = 1.0
        return w

    async def request(self, messages, num_predict: int, source: str = "load"):
        w = self.weights()
        live = [k for k, v in w.items() if v > 0]
        first = random.choices(live, weights=[w[k] for k in live])[0] if live else self.ORDER[0]
        candidates = [first] + [k for k in self.ORDER if k != first and self.backends[k].status == "up"]
        t0, attempts, raw = time.time(), [], None
        for k in candidates:
            b = self.backends[k]
            token = next(self._seq)
            self.inflight[token] = (k, time.time(), num_predict)
            try:
                raw = await b.chat(self.client, messages, num_predict)
                break
            except CONNECTION_ERRORS as e:
                attempts.append({"backend": k, "error": type(e).__name__, "at": time.time()})
                b.fail_streak += 1
                self.set_status(b, "down")
            except BackendError as e:
                attempts.append({"backend": k, "error": f"BackendError: {e}", "at": time.time()})
                b.faulty = True
                self.set_status(b, "down")
            except httpx.HTTPError as e:
                attempts.append({"backend": k, "error": type(e).__name__, "at": time.time()})
            finally:
                self.inflight.pop(token, None)
        t1 = time.time()
        ok = raw is not None
        rec = {
            "t_start": t0, "t_end": t1, "source": source, "ok": ok,
            "backend": k if ok else (candidates[-1] if candidates else first),
            "latency_ms": (t1 - t0) * 1000,
            "attempts": attempts,
            "failover": bool(attempts) and ok,
        }
        if ok:
            ev = raw.get("eval_count", 0)
            rec.update({
                "output_tokens": ev,
                "decode_tps": ev / (raw["eval_duration"] / 1e9) if raw.get("eval_duration") else None,
                "ttft_ms": (raw.get("load_duration", 0) + raw.get("prompt_eval_duration", 0)) / 1e6,
            })
        self.metrics.add(rec)
        return rec, raw

    async def _probe(self, b):
        """A faulty server must produce a real token before it gets traffic again."""
        try:
            await b.chat(self.client, [{"role": "user", "content": "Reply with OK."}], 1, timeout=10)
            b.faulty, b.fail_streak = False, 0
            self.set_status(b, "up")
        except Exception:
            b.fail_streak += 1
            if b.fail_streak >= 3 and self.on_faulty:
                b.fail_streak = 0
                await self.on_faulty(b)

    async def health_loop(self):
        last_probe: dict[str, float] = {}
        while True:
            for b in self.backends.values():
                if b.faulty and b.proc is not None and b.status != "starting":
                    if time.time() - last_probe.get(b.id, 0) >= 2:
                        last_probe[b.id] = time.time()
                        asyncio.create_task(self._probe(b))
                elif b.status in ("up", "down") and b.proc is not None:
                    ok = await b.health(self.client)
                    if ok:
                        b.fail_streak = 0
                        self.set_status(b, "up")
                    else:
                        b.fail_streak += 1
                        if b.fail_streak >= 2:
                            self.set_status(b, "down")
                elif b.status == "up" and b.proc is None:
                    self.set_status(b, "down")
            await asyncio.sleep(0.5)

    def backend_stats(self, key: str) -> dict:
        rs = self.metrics.recent(15, backend=key)
        tps = [r["decode_tps"] for r in rs if r.get("decode_tps")]
        return {
            "requests_15s": len(rs),
            "tokens_per_sec": median(tps),
            "p95_ms": percentile([r["latency_ms"] for r in rs], 0.95),
        }


class LoadGenerator:
    def __init__(self, gateway: Gateway, workers: int = 3, num_predict: int = 48):
        self.gateway, self.workers, self.num_predict = gateway, workers, num_predict
        self.running = asyncio.Event()
        self.running.set()
        self.tasks: list[asyncio.Task] = []

    def start(self):
        self.tasks = [asyncio.create_task(self._worker(i)) for i in range(self.workers)]

    async def _worker(self, i: int):
        await asyncio.sleep(i * 0.3)
        while True:
            await self.running.wait()
            await self.gateway.request([{"role": "user", "content": random.choice(PROMPTS)}], self.num_predict)
            await asyncio.sleep(random.uniform(0.05, 0.25))
