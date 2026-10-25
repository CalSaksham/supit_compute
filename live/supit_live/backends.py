from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import httpx

# Provider-side degradation: the model runner is paused for most of every cycle, like a hard power cap.
# The HTTP server stays responsive, so health checks pass and only measured speed reveals it.
THROTTLE_ON_S, THROTTLE_OFF_S = 0.04, 0.22


class BackendError(Exception):
    """The server answered, but without a usable completion (for example a GPU compute error)."""


class Backend:
    """One provider endpoint, served by its own `ollama serve` process."""

    def __init__(self, id_: str, name: str, role: str, port: int, model: str, log_dir: Path):
        self.id, self.name, self.role, self.port, self.model = id_, name, role, port, model
        self.url = f"http://127.0.0.1:{port}"
        self.log_path = log_dir / f"ollama-{id_}.log"
        self.proc: subprocess.Popen | None = None
        self.status = "stopped"  # stopped | starting | up | down
        self.changed_at = time.time()
        self.fail_streak = 0
        self.faulty = False
        self._throttle: asyncio.Task | None = None

    @property
    def pid(self):
        return self.proc.pid if self.proc and self.proc.poll() is None else None

    @property
    def slow(self) -> bool:
        return self._throttle is not None and not self._throttle.done()

    def _free_port(self):
        out = subprocess.run(["lsof", "-ti", f"tcp:{self.port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
        for pid in out:
            name = subprocess.run(["ps", "-o", "comm=", "-p", pid], capture_output=True, text=True).stdout
            if "ollama" in name.lower():
                os.kill(int(pid), signal.SIGKILL)

    async def start(self, client: httpx.AsyncClient):
        self.status = "starting"
        self.changed_at = time.time()
        self._free_port()
        env = {**os.environ, "OLLAMA_HOST": f"127.0.0.1:{self.port}", "OLLAMA_KEEP_ALIVE": "-1", "OLLAMA_NUM_PARALLEL": "4", "OLLAMA_CONTEXT_LENGTH": "2048"}
        log = self.log_path.open("ab")
        self.proc = subprocess.Popen([shutil.which("ollama") or "ollama", "serve"], env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(60):
            if await self.health(client):
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError(f"{self.name} (ollama on port {self.port}) did not start; see {self.log_path}")
        await self.chat(client, [{"role": "user", "content": "Reply with OK."}], 1, timeout=90)
        self.fail_streak = 0
        self.faulty = False

    def kill(self):
        """Hard crash: SIGKILL the whole process group, including the model runner."""
        self.stop_throttle()
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait(timeout=5)
        self.proc = None

    def runner_pids(self) -> list[int]:
        if not self.pid:
            return []
        out = subprocess.run(["pgrep", "-P", str(self.pid)], capture_output=True, text=True).stdout.split()
        return [int(p) for p in out]

    def _signal_runners(self, sig):
        for p in self.runner_pids():
            try:
                os.kill(p, sig)
            except ProcessLookupError:
                pass

    def start_throttle(self):
        if self.slow:
            return

        async def cycle():
            try:
                while True:
                    self._signal_runners(signal.SIGSTOP)
                    await asyncio.sleep(THROTTLE_OFF_S)
                    self._signal_runners(signal.SIGCONT)
                    await asyncio.sleep(THROTTLE_ON_S)
            finally:
                self._signal_runners(signal.SIGCONT)
        self._throttle = asyncio.create_task(cycle())

    def stop_throttle(self):
        if self._throttle:
            self._throttle.cancel()
            self._throttle = None
        self._signal_runners(signal.SIGCONT)

    async def health(self, client: httpx.AsyncClient) -> bool:
        try:
            r = await client.get(self.url + "/api/version", timeout=0.8)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def chat(self, client: httpx.AsyncClient, messages, num_predict: int, timeout: float = 45) -> dict:
        r = await client.post(self.url + "/api/chat", json={"model": self.model, "messages": messages, "stream": False, "options": {"num_predict": num_predict}}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if data.get("error") or not data.get("done") or not data.get("eval_count"):
            raise BackendError(data.get("error") or "Empty completion: the model produced no tokens")
        return data

    async def model_info(self, client: httpx.AsyncClient) -> dict:
        tags = (await client.get(self.url + "/api/tags", timeout=5)).json().get("models", [])
        tag = next((m for m in tags if m["name"] == self.model or m["model"] == self.model), None)
        show = (await client.post(self.url + "/api/show", json={"model": self.model}, timeout=10)).json()
        return {"tag": tag, "details": show.get("details", {})}

    def listeners(self) -> list[int]:
        out = subprocess.run(["lsof", "-ti", f"tcp:{self.port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
        return [int(p) for p in out]
