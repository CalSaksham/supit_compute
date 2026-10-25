from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .session import LiveSession

ROOT = Path(__file__).resolve().parents[2]
PORT = 8787
session: LiveSession | None = None


async def boot():
    try:
        await session.startup()
        print(f"\nSUPIT live is ready.\n  Demo:    http://127.0.0.1:{PORT}/demo.html  (or flip the Live toggle on the regular site)\n  Gateway: http://127.0.0.1:{PORT}/v1/chat/completions\n", flush=True)
    except Exception as e:
        session.phase = "error"
        session.error = f"{type(e).__name__}: {e}"
        print("SUPIT live failed to start:", session.error, flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global session
    session = LiveSession(ROOT)
    task = asyncio.create_task(boot())
    yield
    task.cancel()
    await session.shutdown()


app = FastAPI(title="SUPIT live", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origin_regex=r"https?://(127\.0\.0\.1|localhost)(:\d+)?$", allow_methods=["*"], allow_headers=["*"])


def ready() -> LiveSession:
    if session is None or session.phase != "running":
        phase = session.phase if session else "starting"
        message = getattr(session, "error", None) or {"starting": "Starting the three local inference servers…", "calibrating": "Measuring Lambda's speed bar…"}.get(phase, phase)
        raise HTTPException(503, detail={"phase": phase, "message": message})
    return session


@app.get("/api/state")
async def state():
    return ready().snapshot()


@app.get("/api/events")
async def events(request: Request):
    s = ready()

    async def stream():
        while not await request.is_disconnected():
            yield f"event: state\ndata: {json.dumps(s.snapshot())}\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/demo/action")
async def demo_action(body: dict):
    s = ready()
    try:
        await s.action(body.get("action", ""), body.get("demo_accounting"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    return s.snapshot()


@app.get("/api/evidence")
async def evidence():
    return {"entries": ready().evidence.entries[-300:]}


@app.get("/api/evidence/verify")
async def evidence_verify():
    return ready().evidence.verify()


@app.get("/api/claim")
async def claim():
    s = ready()
    statement = s.evidence.sign({**s.claim(), "contract_hash": s.contract_hash, "label": "LIVE STATEMENT · measured on this Mac · no payment instruction"})
    s.evidence.record("claim_exported", {"title": "Claim statement exported", "total_claim": statement["total_claim"], "contract_hash": s.contract_hash})
    return statement


@app.get("/api/telemetry")
async def telemetry():
    return ready().telemetry()


@app.get("/api/certificate/verify")
async def certificate_verify():
    s = ready()
    c = s.certificate
    if "signature" not in c:
        return {"valid": False, "reason": "No certificate has been issued in this session."}
    valid = s.evidence.verify_doc(c)
    return {"valid": valid, "reason": "The certificate's Ed25519 signature matches this Mac's live signing key." if valid else "The certificate signature did not verify."}


@app.get("/api/contract.yaml")
async def contract_yaml():
    return PlainTextResponse(ready().contract_yaml, media_type="text/yaml")


@app.post("/v1/chat/completions")
async def chat_completions(body: dict):
    """OpenAI-compatible entry point: point any app here and the gateway routes, fails over and measures."""
    s = ready()
    rec, raw = await s.gateway.request(body.get("messages", []), int(body.get("max_tokens") or 128), source="external")
    if not rec["ok"]:
        raise HTTPException(503, "Every backend failed.")
    usage = {"prompt_tokens": raw.get("prompt_eval_count", 0), "completion_tokens": raw.get("eval_count", 0)}
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(time.time()), "model": s.contract["workload"]["model"],
        "choices": [{"index": 0, "message": {"role": "assistant", "content": raw["message"]["content"]}, "finish_reason": "stop"}],
        "usage": usage,
        "supit": {"served_by": s.backends[rec["backend"]].name, "latency_ms": round(rec["latency_ms"]), "retried_from": [s.backends[a["backend"]].name for a in rec["attempts"]],
                  "tokens_per_sec": round(rec["decode_tps"], 1) if rec.get("decode_tps") else None},
    }


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
