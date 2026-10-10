"""B5 - Analytical SQL on DuckDB (TPC-H), one microVM per rollout.

Workload unchanged: the official 22 TPC-H queries against DuckDB with the
`tpch` extension at the configured scale factor, cycled in a
deterministic shuffled order so heavy queries (Q11/Q15/Q17/Q20) land
evenly.

A single query is an environment *step*, not a rollout - a text-to-SQL RL
trajectory is "write SQL -> run it -> read the error or the wrong answer
-> revise". So one rollout is `B5_EPISODE_STEPS` queries against one
dedicated DuckDB instance.

Two consequences worth knowing before reading the numbers:

* Each rollout gets its own DuckDB. The shared-container topology ran a
  single `--workers 1` uvicorn serving all concurrency through
  `asyncio.to_thread` plus DuckDB's internal thread pool, which is what
  produced the c=32 dip in the earlier results. That contention is gone,
  so the scaling curve is genuinely different and not comparable.
* dbgen runs during the *template build*, so the snapshot already
  contains the generated tables in memory. A new rollout sandbox resumes
  with TPC-H loaded rather than spending ~30s regenerating it, and
  because sandboxes from one snapshot share a memory ublk device by
  reference count, the unmodified dataset pages are shared across all of
  them instead of being duplicated per VM.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from config import Config
from metrics import ResourceSampler
from rollout import PORT_B5, build_provider, rollout_result, warmup_template
from .base import (
    BenchmarkResult,
    Runner,
    cost_block,
    fold_create_fails,
    load_gen_block,
    sandbox_extra_block,
    sandbox_latency_block,
    schedule_sampler_reset,
)
from .multiproc import drive_load_mp

log = logging.getLogger(__name__)

# All 22 official TPC-H queries.
TPCH_QUERIES = list(range(1, 23))


async def _b5_factory(*, setup: dict, seed_base: int, concurrency: int):
    """Child-process work factory. Must stay module-level to be picklable."""
    setup = dict(setup)
    setup["seed_base"] = seed_base
    client, provider, timer = await build_provider(
        setup, concurrency=concurrency,
        ready_port=PORT_B5, ready_expect_ok=True,
    )

    mix = setup["mix"]
    episode_steps = int(setup["episode_steps"])

    http = httpx.AsyncClient(
        # Heavy queries can take many seconds once several hundred
        # sandboxes are competing for cores.
        timeout=900.0,
        limits=httpx.Limits(max_connections=max(32, concurrency * 2)),
    )
    rng = random.Random(seed_base)
    state = {"i": rng.randrange(len(mix))}

    async def work_fn() -> dict:
        t_all = time.perf_counter()
        ref, create_ms = await provider.acquire()
        idle_ms = 0.0
        queries = 0
        # Per-query latency goes into its own named channel so the report
        # can still show the Q01..Q22 breakdown.
        per_q: dict[str, list[float]] = {}
        try:
            t_steps = time.perf_counter()
            for k in range(episode_steps):
                i = state["i"]
                state["i"] += 1
                q = mix[i % len(mix)]
                url, headers = client.endpoint(ref, PORT_B5, "/query")
                t_q = time.perf_counter()
                r = await http.post(url, headers=headers, json={"q": q})
                r.raise_for_status()
                per_q.setdefault(f"q{q:02d}", []).append(
                    (time.perf_counter() - t_q) * 1000.0
                )
                queries += 1
                if k < episode_steps - 1:
                    idle_ms += await timer.wait(client, ref)
            steps_ms = (time.perf_counter() - t_steps) * 1000.0
        finally:
            release_ms = await provider.release(ref)

        return rollout_result(
            wall_ms=(time.perf_counter() - t_all) * 1000.0,
            create_ms=create_ms,
            steps_ms=steps_ms,
            release_ms=release_ms,
            idle_overhead_ms=idle_ms,
            counters={"queries": queries},
            channels=per_q,
        )

    async def teardown():
        await http.aclose()
        await provider.stop()
        await client.aclose()

    return work_fn, teardown


class B5Runner(Runner):
    name = "B5"
    workload = "sqlexec"

    def __init__(self) -> None:
        # Deterministic shuffle so every concurrency level sees the same
        # query stream and results stay comparable.
        self._mix = TPCH_QUERIES.copy()
        random.Random(42).shuffle(self._mix)
        self._template_id = ""
        self._worker_info: dict = {}

    async def warmup(self, cfg: Config) -> None:
        self._template_id = await warmup_template(
            cfg, cfg.template_b5, port=PORT_B5,
            expect_ok_field=True, label=self.name,
        )
        # Grab the engine's own view (sf, version, dbgen time, row counts)
        # once, from a throwaway sandbox, for reproducibility metadata.
        self._worker_info = await self._probe_info(cfg)

    async def _probe_info(self, cfg: Config) -> dict:
        from cube import client_from_config
        client = client_from_config(cfg, max_connections=8)
        ref = None
        try:
            ref = await client.create(self._template_id or cfg.template_b5)
            await client.wait_port(ref, PORT_B5, "/healthz", expect_ok_field=True)
            url, headers = client.endpoint(ref, PORT_B5, "/info")
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.get(url, headers=headers)
                if r.status_code == 200:
                    info = r.json()
                    log.info("[B5] engine: %s", info)
                    return info
        except Exception as e:
            log.warning("[B5] could not read /info: %s", e)
        finally:
            if ref is not None:
                try:
                    await client.delete(ref.sandbox_id)
                except Exception:
                    pass
            await client.aclose()
        return {}

    async def run_one(self, cfg: Config, instance: dict, concurrency: int) -> BenchmarkResult:
        sampler = ResourceSampler(cfg=cfg)
        sampler.start()
        reset_task = schedule_sampler_reset(sampler, cfg.warmup_sec, name=self.name)

        setup = {
            "cube": cfg.cube_setup(),
            "template_id": self._template_id or cfg.template_b5,
            "mix": self._mix,
            "episode_steps": cfg.b5_episode_steps,
        }

        log.info("[B5] warmup=%ds + measure=%ds @ %d live sandboxes, K=%d queries/rollout",
                 cfg.warmup_sec, cfg.duration_sec, concurrency, cfg.b5_episode_steps)

        try:
            mp_result = await drive_load_mp(
                work_factory=_b5_factory,
                setup=setup,
                concurrency=concurrency,
                duration_sec=cfg.duration_sec,
                warmup_sec=cfg.warmup_sec,
                nprocs=cfg.client_nprocs or None,
                nprocs_max=cfg.client_nprocs_max,
                seed_base=0xB5 ^ concurrency,
            )
        finally:
            if reset_task is not None and not reset_task.done():
                reset_task.cancel()
                try:
                    await reset_task
                except (asyncio.CancelledError, Exception):
                    pass
            res = await sampler.stop()

        # Cube has no create-failure counter; the load generator's tally
        # is the only source. See base.fold_create_fails.
        res = fold_create_fails(res, mp_result)

        rollouts = mp_result["completed"]
        queries = int(mp_result["counters"].get("queries", 0))
        qps = queries / cfg.duration_sec

        # Rebuild the Q01..Q22 table from the named channels.
        per_query = {}
        for q in TPCH_QUERIES:
            ch = mp_result["channels"].get(f"q{q:02d}")
            if ch and ch.get("count"):
                per_query[f"Q{q:02d}"] = ch

        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=cfg.duration_sec,
            throughput={
                "queries_per_sec": qps,
                "rollouts_per_sec": rollouts / cfg.duration_sec,
                "total_queries": queries,
                "total_rollouts": rollouts,
            },
            latency_ms={
                "trajectory": mp_result["latency_ms"],
                "per_query": per_query,
                "sandbox": sandbox_latency_block(mp_result["channels"]),
            },
            resource=res,
            cost=cost_block(qps, cfg.duration_sec, instance["instance_type"], cfg),
            extra={
                "engine": "duckdb",
                "tpch_sf": self._worker_info.get("tpch_sf", cfg.tpch_sf),
                "duckdb_version": self._worker_info.get("duckdb_version"),
                "duckdb_threads": self._worker_info.get("duckdb_threads"),
                "dbgen_sec": self._worker_info.get("dbgen_sec"),
                "row_counts": self._worker_info.get("row_counts"),
                "dbgen_in_snapshot": True,
                "query_pool": TPCH_QUERIES,
                "episode_steps": cfg.b5_episode_steps,
                "sandbox": sandbox_extra_block(cfg, mp_result),
                "load_gen": load_gen_block(mp_result),
            },
        )
