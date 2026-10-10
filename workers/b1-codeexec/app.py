"""B1 worker - executes Python snippets in a fresh subprocess.

POST /run {code: str, timeout: int} -> {exit_code, stdout, stderr, wall_ms}

Each request spawns `python -c <code>` with resource limits applied
via the `prlimit` wrapper (which sets rlimits BEFORE execve, then
exec's the target). This mirrors how a real Agentic-RL sandbox runs
LLM-generated code (short-lived subprocess, isolated stdout/stderr,
hard timeout).

Why prlimit and not asyncio's `preexec_fn`? Passing `preexec_fn=...`
forces CPython onto its slow synchronous fork path with internal
locking around the fork helper - measurable as a hard throughput
cliff at moderate concurrency (~c=32 on amd64, ~c=64 on aarch64).
prlimit lets us stay on the lock-free fork+exec fast path.
"""

from __future__ import annotations

import asyncio
import os
import time

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI()


# Per-child resource limits. Adjustable via env so smoke tests can
# loosen them if a snippet legitimately needs more memory.
RSS_BYTES = int(os.getenv("B1_RSS_BYTES", str(256 * 1024 * 1024)))   # 256 MB
CPU_SECS  = int(os.getenv("B1_CPU_SECS",  "30"))                      # 30 s


class RunRequest(BaseModel):
    code: str = Field(..., max_length=64_000)
    timeout: int = Field(5, ge=1, le=60)


class RunResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    wall_ms: float
    timed_out: bool = False


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest) -> RunResponse:
    t0 = time.perf_counter()

    # `prlimit --as=N --cpu=N --core=0 -- python3 -c CODE` sets rlimits
    # on itself, then execve's into python3 - so the target inherits
    # the limits without us needing preexec_fn (which kills throughput
    # under high concurrency due to CPython's slow-fork lock).
    proc = await asyncio.create_subprocess_exec(
        "prlimit",
        f"--as={RSS_BYTES}",
        f"--cpu={CPU_SECS}",
        "--core=0",
        "--",
        "python3", "-c", req.code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=req.timeout)
        rc = proc.returncode if proc.returncode is not None else -1
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        try:
            out, err = await proc.communicate()
        except Exception:
            out, err = b"", b""
        rc = -9

    wall_ms = (time.perf_counter() - t0) * 1000.0
    return RunResponse(
        exit_code=rc,
        stdout=out[:4096].decode("utf-8", errors="replace"),
        stderr=err[:4096].decode("utf-8", errors="replace"),
        wall_ms=wall_ms,
        timed_out=timed_out,
    )
