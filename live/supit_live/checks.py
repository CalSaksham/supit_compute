from __future__ import annotations

import asyncio
import random
import statistics
import time
import uuid

import httpx

from .util import iso, median, percentile, sha256

BENCH_PROMPTS = [
    "Explain in three sentences why GPUs are good at matrix multiplication.",
    "Describe how a load balancer decides where to send a request.",
    "Write a short paragraph about the history of the telescope.",
    "List four tips for writing clear documentation.",
]
LONG_CONTEXT = ("SUPIT measures every GPU deal: delivery, speed, uptime and credits. " * 60).strip()


async def _decode_runs(s, backend, n: int, num_predict: int = 64, timeout: float = 90, give_up_below: float | None = None):
    tps, lat = [], []
    for i in range(n):
        t0 = time.time()
        r = await backend.chat(s.client, [{"role": "user", "content": BENCH_PROMPTS[i % len(BENCH_PROMPTS)]}], num_predict, timeout=timeout)
        lat.append((time.time() - t0) * 1000)
        if r.get("eval_duration"):
            tps.append(r["eval_count"] / (r["eval_duration"] / 1e9))
        if give_up_below and len(tps) >= 2 and median(tps) < give_up_below:
            break
    return tps, lat


async def _prefill(s, backend, timeout: float = 120):
    # A unique prefix defeats Ollama's prompt cache so the full context is processed.
    prompt = f"[{uuid.uuid4()}] {LONG_CONTEXT}\nSummarize the text above in five words."
    r = await backend.chat(s.client, [{"role": "user", "content": prompt}], 1, timeout=timeout)
    return r["prompt_eval_count"] / (r["prompt_eval_duration"] / 1e9), r["prompt_eval_count"]


async def _weights_gb(s, backend):
    info = await backend.model_info(s.client)
    return (info["tag"] or {}).get("size", 0) / 1e9, info


async def calibrate(s) -> dict:
    """The bake-off bar: Lambda's measured speed with no other traffic."""
    lam = s.backends["lambda"]
    await _decode_runs(s, lam, 1)
    tps, lat = await _decode_runs(s, lam, 5)
    prefill, ctx = await _prefill(s, lam)
    gb, info = await _weights_gb(s, lam)
    decode = median(tps)
    return {
        "measured_at": iso(), "backend": "lambda", "model": lam.model, "digest": (info["tag"] or {}).get("digest", ""),
        "decode_tokens_per_sec": round(decode, 2), "prefill_tokens_per_sec": round(prefill, 1), "prefill_context_tokens": ctx,
        "p95_latency_ms_64_tokens": round(percentile(lat, 0.95)), "weights_gb": round(gb, 3),
        "effective_bandwidth_gbps": round(decode * gb, 1), "host": s.host,
    }


def _check(name, detail):
    return {"name": name, "detail": detail, "status": "WAITING"}


async def _pause_load(s):
    s.loadgen.running.clear()
    await asyncio.sleep(2.0)


async def run_handover(s):
    c = s.contract
    h = c["handover"]
    lam = s.backends["lambda"]
    bar = s.bar
    ratio_min = float(str(h["compute_vs_bar"]).replace(">=", ""))
    bw_min = float(str(h["memory_bandwidth_vs_bar"]).replace(">=", ""))
    cert = {"status": "RUNNING", "started_at": iso(), "checks": [
        _check("Inventory", "Model, version and host match the contract"),
        _check("Hardware diagnostics", "NVIDIA DCGM diagnostics"),
        _check("Compute speed", "Prompt processing speed against the bake-off bar"),
        _check("Memory bandwidth", "Effective bandwidth (decode speed × weights) against the bar"),
        _check("Inference speed", "Output tokens/s and p95 latency against the contract floor"),
        _check("Burn-in", f"{h['burn_in_seconds']} s of sustained load: no errors, stable speed"),
        _check("Basic security", "Only the expected process listens; model unchanged"),
    ]}
    s.certificate = cert
    await _pause_load(s)
    measured: dict = {}

    async def step(i, fn):
        chk = cert["checks"][i]
        chk["status"] = "RUNNING"
        try:
            status, detail, data = await fn()
        except Exception as e:
            status, detail, data = "FAIL", f"Check could not complete: {type(e).__name__}: {e}", {}
        chk.update(status=status, detail=detail, measured=data)
        s.evidence.record("handover_check", {"check": chk["name"], "status": status, "detail": detail, "measured": data, "contract_hash": s.contract_hash})

    async def inventory():
        if lam.status != "up":
            return "FAIL", "Lambda is not answering, so nothing can be inventoried.", {}
        gb, info = await _weights_gb(s, lam)
        d, tag = info["details"], info["tag"] or {}
        digest = tag.get("digest", "")
        measured["digest"] = digest
        ok = digest.startswith(str(c["workload"]["model_digest"])) and lam.model == c["workload"]["model"]
        detail = f"{lam.model} · {d.get('parameter_size', '?')} · {d.get('quantization_level', '?')} · digest {digest[:12]} · {s.host['chip']}, {s.host['memory_gb']} GB · Ollama {s.ollama_version}"
        return ("PASS" if ok else "FAIL"), detail + ("" if ok else " · does not match the contract"), {"digest": digest, "weights_gb": round(gb, 3), **d}

    async def diagnostics():
        return "SKIP", "Skipped: NVIDIA DCGM needs an NVIDIA GPU. Recorded as not applicable on Apple silicon; runs on real H100s.", {}

    async def compute():
        expected = bar["prefill_context_tokens"] / bar["prefill_tokens_per_sec"]
        limit = max(10.0, expected * 8)
        try:
            tps, ctx = await _prefill(s, lam, timeout=limit)
        except httpx.TimeoutException:
            return "FAIL", f"No answer within {limit:.0f} s for {bar['prefill_context_tokens']} prompt tokens (the bar does it in {expected:.1f} s)", {"timeout_s": limit}
        r = tps / bar["prefill_tokens_per_sec"]
        return ("PASS" if r >= ratio_min else "FAIL"), f"{tps:,.0f} prompt tokens/s on {ctx} tokens · {r:.0%} of bar (needs ≥ {ratio_min:.0%})", {"prefill_tokens_per_sec": round(tps, 1), "ratio": round(r, 3)}

    async def inference():
        tps, lat = await _decode_runs(s, lam, 5, timeout=30, give_up_below=s.floor / 2)
        med, p95 = median(tps), percentile(lat, 0.95)
        measured["decode"] = med
        ok = len(tps) == 5 and med >= s.floor and p95 <= s.p95_target
        stopped = "" if len(tps) == 5 else " · stopped early, far below the floor"
        return ("PASS" if ok else "FAIL"), f"{med:.1f} tokens/s (floor {s.floor:g}) · p95 {p95:,.0f} ms (target {s.p95_target:,.0f}) · {len(tps)} requests{stopped}", {"tokens_per_sec": round(med, 2), "p95_ms": round(p95), "requests": len(tps)}

    async def bandwidth():
        if "decode" not in measured:
            tps, _ = await _decode_runs(s, lam, 3, timeout=30, give_up_below=s.floor / 2)
            measured["decode"] = median(tps)
        gb = bar["weights_gb"]
        bw = measured["decode"] * gb
        r = bw / bar["effective_bandwidth_gbps"]
        return ("PASS" if r >= bw_min else "FAIL"), f"≈ {bw:,.0f} GB/s effective ({measured['decode']:.1f} tokens/s × {gb:.1f} GB weights) · {r:.0%} of bar", {"effective_gbps": round(bw, 1), "ratio": round(r, 3)}

    async def burn_in():
        end = time.time() + float(h["burn_in_seconds"])
        tps, errors, count = [], 0, 0

        async def worker():
            nonlocal errors, count
            while time.time() < end:
                try:
                    r = await lam.chat(s.client, [{"role": "user", "content": random.choice(BENCH_PROMPTS)}], 48, timeout=max(1.0, end - time.time() + 5))
                    count += 1
                    if r.get("eval_duration"):
                        tps.append(r["eval_count"] / (r["eval_duration"] / 1e9))
                except Exception:
                    errors += 1
        await asyncio.gather(worker(), worker())
        med = median(tps) or 0
        cv = statistics.pstdev(tps) / med if len(tps) > 1 and med else 0
        ok = errors == 0 and count > 0 and cv < 0.25 and med >= s.floor
        return ("PASS" if ok else "FAIL"), f"{count} requests over {h['burn_in_seconds']} s, 2 streams · {errors} errors · median {med:.1f} tokens/s · variation {cv:.0%}", {"requests": count, "errors": errors, "tokens_per_sec": round(med, 2), "cv": round(cv, 3)}

    async def security():
        listeners = lam.listeners()
        _, info = await _weights_gb(s, lam)
        same = (info["tag"] or {}).get("digest") == measured.get("digest")
        ok = listeners == [lam.pid] and same
        return ("PASS" if ok else "FAIL"), f"Port {lam.port}: {len(listeners)} listener (pid {', '.join(map(str, listeners)) or 'none'}, expected {lam.pid}) · model digest {'unchanged' if same else 'CHANGED'} · gateway stores timings, not prompts", {"listeners": listeners, "expected_pid": lam.pid, "digest_unchanged": same}

    try:
        for i, fn in enumerate([inventory, diagnostics, compute, bandwidth, inference, burn_in, security]):
            await step(i, fn)
    finally:
        s.loadgen.running.set()
    statuses = [chk["status"] for chk in cert["checks"]]
    overall = "PASS" if all(x in ("PASS", "SKIP") for x in statuses) else "FAIL"
    signed = s.evidence.sign({
        "status": overall, "issued_at": iso(), "contract_hash": s.contract_hash, "synthetic": False,
        "label": "LIVE CERTIFICATE · Measured on this Mac. Local Ollama server standing in for an 8 × H100 node.",
        "environment": {"host": s.host, "ollama_version": s.ollama_version, "backend": "lambda", "port": lam.port},
        "bar": bar, "checks": cert["checks"],
    })
    s.certificate = signed
    passed = statuses.count("PASS")
    failed = [chk["name"] for chk in cert["checks"] if chk["status"] == "FAIL"]
    entry = s.evidence.record("handover_certificate", {"title": f"Handover {'passed' if overall == 'PASS' else 'failed'}", "status": overall, "certificate_hash": sha256(signed), "contract_hash": s.contract_hash})
    s.incidents.insert(0, {"id": entry["id"], "timestamp": entry["timestamp"], "severity": "info" if overall == "PASS" else "warning",
                           "title": f"Handover {'passed' if overall == 'PASS' else 'failed'}",
                           "description": f"{passed} checks passed, {statuses.count('SKIP')} skipped (NVIDIA only)" + (f", failed: {', '.join(failed)}." if failed else ". Certificate signed by this Mac's Ed25519 key."),
                           "attribution": "Measured handover test", "injected": False, "detected": True, "evidence_id": entry["id"]})


async def run_bakeoff(s):
    await _pause_load(s)
    rows = []
    try:
        for k in ("lambda", "coreweave"):
            b = s.backends[k]
            if b.status != "up":
                rows.append({"provider": b.name, "id": k, "error": "offline"})
                continue
            tps, lat = await _decode_runs(s, b, 4)
            med = median(tps)
            price = s.prices[k]
            rows.append({"provider": b.name, "id": k, "price_per_gpu_hour": price, "tokens_per_sec": round(med, 2), "p95_ms": round(percentile(lat, 0.95)),
                         "cost_per_million_tokens": round(price / (med * 3600) * 1e6, 2)})
    finally:
        s.loadgen.running.set()
    s.bakeoff = {"measured_at": iso(), "rows": rows, "prompts": len(BENCH_PROMPTS), "output_tokens_per_request": 64}
    entry = s.evidence.record("bakeoff_result", {"title": "Bake-off measured", **s.bakeoff, "contract_hash": s.contract_hash})
    s.incidents.insert(0, {"id": entry["id"], "timestamp": entry["timestamp"], "severity": "info", "title": "Bake-off measured",
                           "description": " · ".join(f"{r['provider']}: {r['tokens_per_sec']} tokens/s, ${r['cost_per_million_tokens']}/M tokens" for r in rows if "tokens_per_sec" in r),
                           "attribution": "Measured bake-off", "injected": False, "detected": True, "evidence_id": entry["id"]})
