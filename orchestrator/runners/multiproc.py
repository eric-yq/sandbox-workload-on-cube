"""Multi-process load driver for HTTP-bound benchmarks.

A single asyncio event loop tops out around 50-100k req/s (with
uvloop) because all socket reads, JSON parsing and httpx coroutine
machinery is single-threaded. For B3/B7/B9 where each request is
sub-millisecond, that's the binding constraint - the orchestrator
maxes out one CPU long before the worker container does.

This module spawns N child processes (defaults to one per vCPU),
each running its own asyncio loop with `concurrency / N` workers.
Each child returns a partial result; the parent aggregates.

Usage from a runner:

    from .multiproc import drive_load_mp
    summary = await drive_load_mp(
        request_factory=_make_traj_request,
        concurrency=concurrency,
        duration_sec=cfg.duration_sec,
        nprocs=cfg.client_nprocs or auto,
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing as mp
import os
import time
from typing import Any, Awaitable, Callable

import numpy as np

log = logging.getLogger(__name__)


def _default_nprocs() -> int:
    """One client process per vCPU available to this process."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except Exception:
        return os.cpu_count() or 1


# ---------------------------------------------------------------
# Child-process entry point
# ---------------------------------------------------------------

def _child_main(
    *,
    setup_pickled: bytes,
    work_factory_pickled: bytes,
    concurrency: int,
    duration_sec: float,
    warmup_sec: float,
    seed_base: int,
    out_q: mp.Queue,
) -> None:
    """Run an asyncio event loop with `concurrency` workers.

    Skips counting / latency recording for the first `warmup_sec`
    seconds. The total wall budget is `warmup_sec + duration_sec`.
    Sends the result dict back via `out_q`.
    """
    # Use uvloop in children too.
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass

    import pickle
    setup = pickle.loads(setup_pickled)
    work_factory = pickle.loads(work_factory_pickled)

    async def _run() -> dict:
        # The factory builds an async work_fn that captures any HTTP
        # client / state needed; it can also return a teardown coroutine.
        work_fn, teardown = await work_factory(
            setup=setup, seed_base=seed_base, concurrency=concurrency,
        )
        latencies: list[float] = []
        # Optional named channels - runners use these for per-sub-task
        # latency distributions (B9 splits B1/B3/B5; B5 splits Q01..Q22).
        channels: dict[str, list[float]] = {}
        counters: dict[str, float] = {}
        completed = 0
        errors = 0
        measure_start = time.monotonic() + warmup_sec
        end_at = measure_start + duration_sec

        async def worker():
            nonlocal completed, errors
            while time.monotonic() < end_at:
                try:
                    result = await work_fn()
                    if time.monotonic() < measure_start:
                        continue
                    if result is None:
                        continue
                    # Accept either a plain latency float, or a dict
                    # {"wall_ms": float, "counters": {...},
                    #  "channels": {name: float, ...}} for runners
                    # that need to aggregate sub-task latency / counters.
                    if isinstance(result, dict):
                        lat = result.get("wall_ms")
                        if lat is not None:
                            latencies.append(lat)
                            completed += 1
                        for k, v in result.get("counters", {}).items():
                            counters[k] = counters.get(k, 0) + v
                        for k, v in result.get("channels", {}).items():
                            # v can be a single float or a list of floats
                            ch = channels.setdefault(k, [])
                            if isinstance(v, (list, tuple)):
                                ch.extend(v)
                            else:
                                ch.append(v)
                    else:
                        latencies.append(result)
                        completed += 1
                except Exception:
                    if time.monotonic() < measure_start:
                        continue
                    errors += 1

        tasks = [asyncio.create_task(worker()) for _ in range(concurrency)]
        await asyncio.gather(*tasks, return_exceptions=True)
        if teardown:
            try:
                await teardown()
            except Exception:
                pass

        return {
            "completed": completed,
            "errors": errors,
            "latencies_ms": latencies,
            "channels": channels,
            "counters": counters,
        }

    try:
        result = asyncio.run(_run())
        out_q.put(result)
    except Exception as e:
        out_q.put({
            "completed": 0, "errors": 0,
            "latencies_ms": [], "channels": {}, "counters": {},
            "child_error": repr(e),
        })


# ---------------------------------------------------------------
# Parent-side helpers
# ---------------------------------------------------------------

async def drive_load_mp(
    *,
    work_factory: Callable[..., Awaitable],
    setup: Any,
    concurrency: int,
    duration_sec: float,
    nprocs: int | None = None,
    nprocs_max: int = 0,
    seed_base: int = 0,
    warmup_sec: float = 0.0,
) -> dict:
    """Spawn `nprocs` child processes, each driving `concurrency/nprocs`
    workers. Aggregate their results.

    Each child runs `warmup_sec` seconds of unmeasured load before the
    counted `duration_sec` window, so latency / throughput numbers
    reflect the steady state.

    `work_factory` is an async function `(setup, seed_base, concurrency) ->
    (work_fn, teardown)` where `work_fn()` returns a latency in ms per
    completed unit of work. It must be picklable (use module-level
    functions, not closures).

    Returns: dict with completed/errors/latencies_ms aggregated, plus
    per-child diagnostic info.
    """
    import pickle

    nprocs = nprocs or _default_nprocs()
    # On a 192-vCPU metal host the auto value would spawn 192 children,
    # each with its own httpx pool and its own Cube control-plane
    # client. That is far more connection state than the load needs and
    # it competes with the sandboxes for CPU, so cap it.
    if nprocs_max and nprocs > nprocs_max:
        log.info("capping load-gen processes %d -> %d (CLIENT_NPROCS_MAX)",
                 nprocs, nprocs_max)
        nprocs = nprocs_max
    nprocs = max(1, min(nprocs, concurrency))

    # Distribute concurrency as evenly as possible.
    per_proc = [concurrency // nprocs] * nprocs
    for i in range(concurrency % nprocs):
        per_proc[i] += 1

    setup_blob = pickle.dumps(setup)
    factory_blob = pickle.dumps(work_factory)

    ctx = mp.get_context("spawn")
    out_q: mp.Queue = ctx.Queue()
    procs: list[mp.Process] = []
    for i, c in enumerate(per_proc):
        if c <= 0:
            continue
        p = ctx.Process(
            target=_child_main,
            kwargs=dict(
                setup_pickled=setup_blob,
                work_factory_pickled=factory_blob,
                concurrency=c,
                duration_sec=duration_sec,
                warmup_sec=warmup_sec,
                seed_base=seed_base + i * 1_000_003,
                out_q=out_q,
            ),
            daemon=True,
        )
        p.start()
        procs.append(p)

    # Drain the queue without blocking the asyncio loop.
    loop = asyncio.get_event_loop()
    results: list[dict] = []
    for _ in procs:
        res = await loop.run_in_executor(None, out_q.get)
        results.append(res)
    for p in procs:
        p.join(timeout=10)

    total_completed = sum(r.get("completed", 0) for r in results)
    total_errors = sum(r.get("errors", 0) for r in results)
    all_lat: list[float] = []
    counters_total: dict[str, float] = {}
    channels_total: dict[str, list[float]] = {}
    for r in results:
        all_lat.extend(r.get("latencies_ms", []))
        for k, v in r.get("counters", {}).items():
            counters_total[k] = counters_total.get(k, 0) + v
        for k, vs in r.get("channels", {}).items():
            channels_total.setdefault(k, []).extend(vs)

    def _summarise(arr_in: list[float]) -> dict:
        if not arr_in:
            return {"count": 0, "p50": None, "p95": None, "p99": None,
                    "mean": None, "max": None}
        a = np.array(arr_in)
        return {
            "count": int(a.size),
            "p50": float(np.percentile(a, 50)),
            "p95": float(np.percentile(a, 95)),
            "p99": float(np.percentile(a, 99)),
            "mean": float(a.mean()),
            "max": float(a.max()),
        }

    if all_lat:
        arr = np.array(all_lat)
        latency_summary = {
            "count": int(arr.size),
            "errors": total_errors,
            "p50": float(np.percentile(arr, 50)),
            "p95": float(np.percentile(arr, 95)),
            "p99": float(np.percentile(arr, 99)),
            "mean": float(arr.mean()),
            "max": float(arr.max()),
        }
    else:
        latency_summary = {
            "count": 0, "errors": total_errors,
            "p50": None, "p95": None, "p99": None,
            "mean": None, "max": None,
        }

    channel_summary = {k: _summarise(v) for k, v in channels_total.items()}

    return {
        "completed": total_completed,
        "errors": total_errors,
        "latency_ms": latency_summary,
        "channels": channel_summary,
        "counters": counters_total,
        "nprocs": len(procs),
        "concurrency_per_proc": per_proc,
        "per_child": [
            {"completed": r.get("completed", 0),
             "errors": r.get("errors", 0),
             "child_error": r.get("child_error")}
            for r in results
        ],
    }
