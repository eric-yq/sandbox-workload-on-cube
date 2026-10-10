"""B4 worker - Playwright headless Chromium service.

Multi-process + context-pool model:
  - Each uvicorn worker process owns its own Chromium browser.
  - Within a worker, we pre-create a pool of `MAX_CONTEXTS`
    long-lived BrowserContexts at startup. Per-request we borrow
    one, run the trajectory, then `clear_cookies()` + navigate
    back to `about:blank` before returning it.

Why pool instead of per-request context? Creating + tearing down a
BrowserContext spawns / kills a Chromium renderer process on every
request. At c=128 that's hundreds of fork()/wait() cycles per
second; aarch64 kernels in particular bottleneck on this fork
storm long before any V8 / Skia work is done. Pooling keeps the
renderer set fixed at WORKERS * MAX_CONTEXTS, so CPU utilisation
scales with concurrency instead of choking on fork overhead.

Knobs (env):
  - B4_UVICORN_WORKERS  : how many uvicorn processes (= chromium
                          masters). Auto-scaled by the launch script.
  - MAX_CONTEXTS        : per-process pool size (= renderer count
                          per Chromium master). Total in-flight
                          contexts = WORKERS * MAX_CONTEXTS.
  - ACTION_TIMEOUT_MS   : per-action timeout for click / fill. Small
                          value (default 500ms) so a missed selector
                          fails fast instead of dominating wall time.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
)
from pydantic import BaseModel


def _default_max_contexts() -> int:
    """Per-uvicorn-worker context-pool size.

    Default to 4. With client-side round-robin guaranteeing exactly
    `concurrency / WORKERS` connections per worker, a pool of 4 is
    enough for the standard c=32 sweep on a 4xlarge (8 workers x 4
    slots = 32 slots, perfectly matched). Higher concurrency (c=64,
    c=128) intentionally spills into the pool's queue - pool_wait
    rises but Chromium stays busy and CPU utilisation tracks load.

    Sizing too high is harmful: 16 contexts/worker x 8 workers = 128
    Chromium renderer processes on 16 vCPU thrashes the kernel
    scheduler and page-table walks (we measured CPU drop ~30% from
    over-subscription on aarch64).
    """
    return 4


MAX_CONTEXTS = int(os.getenv("MAX_CONTEXTS", "0")) or _default_max_contexts()
ACTION_TIMEOUT_MS = int(os.getenv("ACTION_TIMEOUT_MS", "500"))
# Goto timeout: must absorb worst-case Chromium IPC queueing at high
# concurrency. At c=128 with 4 slots/worker we get ~16 client
# connections per master, so a fresh goto can wait up to ~3-4
# trajectories' time before its own work starts. 10s gives 2x
# margin without masking real navigation hangs.
GOTO_TIMEOUT_MS = int(os.getenv("GOTO_TIMEOUT_MS", "10000"))

# When the launcher starts N independent uvicorn processes on
# adjacent ports starting at B4_PORT_BASE, this lets clients fetch
# the full list from any single instance and round-robin themselves.
PORT_BASE = int(os.getenv("B4_PORT_BASE", "8004"))
NUM_PORTS = int(os.getenv("B4_NUM_PORTS", "0"))  # set by launcher

_browser: Browser | None = None
_pw = None
# Async queue acts both as the pool and as a bounded semaphore: a
# request can only run when it can `get()` a context, and `put()`
# returns it. No separate Semaphore needed.
_pool: asyncio.Queue[tuple[BrowserContext, Page]] | None = None


async def _make_slot(browser: Browser) -> tuple[BrowserContext, Page]:
    ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
    page = await ctx.new_page()
    return ctx, page


async def _reset_slot(ctx: BrowserContext, page: Page) -> tuple[BrowserContext, Page]:
    """Clear per-trajectory state so the slot looks fresh next time.

    On error we drop the slot and create a new one (Chromium pages can
    end up wedged after navigation timeouts; better to recycle).
    """
    try:
        await ctx.clear_cookies()
        await page.goto("about:blank", wait_until="domcontentloaded",
                        timeout=GOTO_TIMEOUT_MS)
        return ctx, page
    except Exception:
        try:
            await ctx.close()
        except Exception:
            pass
        if _browser is None:
            raise
        return await _make_slot(_browser)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _browser, _pw, _pool
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-features=IsolateOrigins,site-per-process",
            # Skia / GPU compositing off so we don't accidentally
            # depend on hardware accel that's missing in containers.
            "--disable-software-rasterizer",
        ],
    )
    print(f"[b4] worker pid={os.getpid()} pool={MAX_CONTEXTS} "
          f"action_timeout={ACTION_TIMEOUT_MS}ms", flush=True)

    _pool = asyncio.Queue(maxsize=MAX_CONTEXTS)
    # Pre-create all slots up front so the first burst doesn't pay
    # the renderer-start tax.
    for _ in range(MAX_CONTEXTS):
        slot = await _make_slot(_browser)
        await _pool.put(slot)
    print(f"[b4] worker pid={os.getpid()} pool ready", flush=True)

    yield

    # Drain the pool on shutdown.
    while not _pool.empty():
        ctx, _page = _pool.get_nowait()
        try:
            await ctx.close()
        except Exception:
            pass
    await _browser.close()
    await _pw.stop()


app = FastAPI(lifespan=lifespan)


class Step(BaseModel):
    action: str
    args: dict[str, Any] = {}


class TaskRequest(BaseModel):
    target_url: str
    steps: list[Step]


class TaskResponse(BaseModel):
    wall_ms: float
    steps_done: int       # steps that ran without throwing
    steps_failed: int     # selector misses, type/click/wait timeouts
    actions: int          # total step count requested


@app.get("/healthz")
async def healthz() -> dict:
    pool_size = _pool.qsize() if _pool is not None else 0
    return {"ok": _browser is not None and _pool is not None,
            "pool_idle": pool_size,
            "pool_max": MAX_CONTEXTS,
            "pid": os.getpid(),
            "port_base": PORT_BASE,
            "num_ports": NUM_PORTS}


@app.get("/ports")
async def ports() -> dict:
    """List all worker ports the launcher started.

    Clients use this to discover the full set, then round-robin
    across them - bypassing the kernel's SO_REUSEPORT hash which
    is severely unbalanced on aarch64 with single-source-IP
    short-lived connections.
    """
    n = NUM_PORTS or 1
    return {"ports": [PORT_BASE + i for i in range(n)]}


@app.post("/task", response_model=TaskResponse)
async def task(req: TaskRequest) -> TaskResponse:
    if _browser is None or _pool is None:
        raise HTTPException(503, "browser not ready")
    t0 = time.perf_counter()
    done = 0
    failed = 0

    # Optional per-action timing instrumentation. Set B4_TRACE=1 in the
    # worker env (compose) and inspect `docker logs arl-b4-worker` to
    # see where time goes. KEEP OFF in production - even just the time
    # measurements add ~5us per action.
    trace = os.getenv("B4_TRACE", "0") == "1"
    pool_wait_t0 = time.perf_counter()
    ctx, page = await _pool.get()
    pool_wait_ms = (time.perf_counter() - pool_wait_t0) * 1000.0
    # Pool occupancy at the moment we acquired (idle slots after our get).
    pool_idle_after_get = _pool.qsize()

    action_times: list[tuple[str, float]] = []
    try:
        for step in req.steps:
            a = step.action
            args = step.args
            a_t0 = time.perf_counter()
            try:
                if a == "goto":
                    path = args.get("path", "/")
                    await page.goto(req.target_url + path,
                                    wait_until="domcontentloaded",
                                    timeout=GOTO_TIMEOUT_MS)
                elif a == "click":
                    sel = args.get("selector")
                    await page.click(sel, timeout=ACTION_TIMEOUT_MS)
                elif a == "scroll":
                    await page.evaluate(
                        f"window.scrollBy(0, {int(args.get('y', 400))})"
                    )
                elif a == "type":
                    sel = args.get("selector", "input")
                    text = args.get("text", "")
                    await page.fill(sel, text, timeout=ACTION_TIMEOUT_MS)
                elif a == "screenshot":
                    # Synthetic encode benchmark - keep PNG so Skia
                    # actually compresses (vs JPEG which is ~free).
                    await page.screenshot(full_page=False, type="png")
                else:
                    raise HTTPException(400, f"unknown action: {a}")
                done += 1
            except HTTPException:
                raise
            except Exception:
                # Selector misses / per-action timeouts: count + move on.
                failed += 1
            if trace:
                action_times.append(
                    (a, (time.perf_counter() - a_t0) * 1000.0)
                )
    finally:
        # Reset and return to pool. _reset_slot recreates on error so
        # we don't shrink the pool.
        try:
            ctx, page = await _reset_slot(ctx, page)
        except Exception:
            # Last-resort: if even slot recreation fails, close the
            # broken context and let the pool size shrink. The pool
            # will eventually refill via successful resets.
            try:
                await ctx.close()
            except Exception:
                pass
            ctx = page = None  # type: ignore
        if ctx is not None:
            await _pool.put((ctx, page))

    wall_ms = (time.perf_counter() - t0) * 1000.0
    if trace:
        # One line per trajectory; histogram by action type.
        per_kind: dict[str, list[float]] = {}
        for kind, ms in action_times:
            per_kind.setdefault(kind, []).append(ms)
        summary = " ".join(
            f"{k}={sum(v):.0f}ms/{len(v)}" for k, v in per_kind.items()
        )
        # Tag every trace line with the worker PID and the pool occupancy
        # observed right after acquiring a slot. This lets the operator
        # see, post-hoc, whether the kernel's SO_REUSEPORT is fanning
        # incoming connections evenly across worker processes - if a few
        # PIDs dominate the trace, load is imbalanced and bumping
        # MAX_CONTEXTS won't help; we'd need client-side worker-port
        # round-robin instead.
        print(f"[b4-trace] pid={os.getpid()} pool_idle={pool_idle_after_get}/{MAX_CONTEXTS} "
              f"wall={wall_ms:.0f}ms pool_wait={pool_wait_ms:.0f}ms "
              f"done={done} failed={failed} {summary}",
              flush=True)

    return TaskResponse(
        wall_ms=wall_ms,
        steps_done=done,
        steps_failed=failed,
        actions=len(req.steps),
    )
