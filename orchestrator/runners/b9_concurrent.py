"""B9 - End-to-end concurrent rollouts, one microVM per rollout.

This is the archetype the whole suite exists to measure: many concurrent
sessions against one sandbox tier, where every step is sampled from a
weighted task mix:

    B3 (tool-call step)          weight 0.50
    B1 (python code execution)   weight 0.20
    B5 (DuckDB TPC-H query)      weight 0.10
    B4 (browser action batch)    weight 0.10
    B7 (text-game episode)       weight 0.10

The signal versus running B1/B3/B4/B5/B7 in isolation is **long-tail
amplification**: one slow step drags the whole rollout's wall time,
exactly like a synchronous RL batch waiting on its slowest environment.
B4 contributes a disproportionate tail because its step latency is one to
two orders of magnitude above the others, so it dominates p99 even at 10%
weight.

Two archetypes, one mechanism
-----------------------------
The same task mix models both workloads that run on a sandbox tier, and
`BENCH_MODE` says which one a run is claiming to be:

* `rl` (default) - a trainer dispatching short rollouts, each in its own
  fresh sandbox. `CUBE_STRATEGY=fresh`, 10-30 steps, no think time.
  Sandbox churn is part of the workload, so create latency belongs in
  the headline. Expect provisioning to be ~15% of rollout wall time.
* `hosting` - a long-lived pool, one agent session per sandbox.
  `CUBE_STRATEGY=pool`, hundreds of steps, non-zero `THINK_TIME_MS`.
  Provisioning is a one-off fixture (reported separately as
  `extra.pool_fill`) and state carry-over between steps is the product
  rather than contamination.

The trap in the hosting shape is the think-time ceiling. A closed-loop
session cannot step faster than `1/think_time`, so once sessions x
think-time saturates before the *host* does, throughput collapses to
`concurrency / think_time` - identical on any two machines, at a few
percent CPU. `run_one` logs that ceiling next to the measured rate so a
think-time-bound run is recognisable rather than publishable. The way out
is more sessions per host, which is why the small 1 vCPU B9 template
exists: the node admits `QuotaCpu / template_cpu` sandboxes, so a 4 vCPU
template caps a 192k-millicore node at 48.

Why one sandbox holds all five stacks
-------------------------------------
Under docker-compose a B9 rollout fanned out across five shared worker
containers. That is not what "one sandbox per rollout" means, and it
reintroduces exactly the cross-rollout contention the per-rollout model
exists to remove. So the B9 template runs all five services plus the
static SPA inside a single microVM: the rollout's environment is entirely
its own.

The obvious objection is image size - Chromium plus DuckDB plus the TPC-H
dataset is a fat rootfs. That is the wrong intuition here. Cube builds
each template into a memory snapshot on local disk and restores sandboxes
from it through reflink copy-on-write (FICLONE), so an untouched page is
shared rather than copied: only the pages a rollout actually writes cost
real memory or real disk. A fat template with a narrow working set is
precisely the shape this runtime is designed for - which is why the
reported per-sandbox overhead is single-digit MB regardless of how large
the template is.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from config import Config
from metrics import ResourceSampler
from rollout import (
    PORT_B1,
    PORT_B3,
    PORT_B4_BASE,
    PORT_B5,
    PORT_B7,
    PORT_STATIC,
    build_provider,
    rollout_result,
    warmup_template,
)
from .b1_corpus import load_corpus
from .b3_trajectories import build_trajectories as build_b3_trajectories
from .b4_trajectories import build_trajectories as build_b4_trajectories
from .base import (
    BenchmarkResult,
    Runner,
    cost_block,
    fold_create_fails,
    load_gen_block,
    pool_fill_block,
    sandbox_extra_block,
    sandbox_latency_block,
    schedule_sampler_reset,
    slo_block,
)
from .multiproc import drive_load_mp

log = logging.getLogger(__name__)

# Task mix - weights determine sampling probability per step.
# B4 / B7 deliberately get a smaller share than their wall time would
# suggest, otherwise their slow steps monopolise every rollout and the
# faster sandboxes (B1/B3/B5) never get exercised.
TASK_MIX = [
    ("B3", 0.50),   # ~5-15ms per step
    ("B1", 0.20),   # ~50-80ms per code exec
    ("B5", 0.10),   # ~50ms-5s per TPC-H query
    ("B4", 0.10),   # ~1-3s per browser action batch
    ("B7", 0.10),   # ~5-50ms per episode
]

# Session length in steps, overridable via B9_EPISODE_MIN_STEPS /
# B9_EPISODE_MAX_STEPS. These defaults are an RL training mini-episode.
#
# This is the single axis that separates the two archetypes B9 can model,
# so it has to be settable: at ~20 steps a `fresh` rollout spends ~15% of
# its wall time on create+release, and B9's headline throughput is then
# substantially a measure of the create path. A hosted agent session is
# hundreds of steps long, which amortises provisioning to near zero and
# leaves the step work as the signal. Running one shape and reporting it
# as the other is the mistake these two knobs exist to prevent.
DEFAULT_EPISODE_MIN_STEPS = 10
DEFAULT_EPISODE_MAX_STEPS = 30

TPCH_QUERIES = list(range(1, 23))

# B7 episode parameters - mirror b7_textgame.py.
B7_EPISODE_MIN = 30
B7_EPISODE_MAX = 50

B4_TARGET_URL = f"http://127.0.0.1:{PORT_STATIC}"


async def _b9_factory(*, setup: dict, seed_base: int, concurrency: int):
    """Child-process work factory. Must stay module-level to be picklable."""
    setup = dict(setup)
    setup["seed_base"] = seed_base
    # Readiness is probed on B3 (the cheapest service to answer); the
    # template's readyCmd already gated snapshot capture on all five
    # being up, including B5's dbgen.
    client, provider, timer = await build_provider(
        setup, concurrency=concurrency, ready_port=PORT_B3,
    )

    ep_min = int(setup.get("episode_min_steps", DEFAULT_EPISODE_MIN_STEPS))
    ep_max = int(setup.get("episode_max_steps", DEFAULT_EPISODE_MAX_STEPS))

    b1_corpus = setup["b1_corpus"]
    b3_trajs = setup["b3_trajectories"]
    b4_trajs = setup["b4_trajectories"]
    names = setup["task_names"]
    cum = setup["task_cum"]
    cum_total = setup["task_total"]

    n_b1, n_b3, n_b4 = len(b1_corpus), len(b3_trajs), len(b4_trajs)
    n_q = len(TPCH_QUERIES)

    # One HTTP client for everything: all five services now live in the
    # same sandbox behind the same transport, so the per-downstream
    # tuning the container topology needed no longer applies. The timeout
    # is sized for the slowest member of the mix (B5 under load).
    http = httpx.AsyncClient(
        timeout=900.0,
        limits=httpx.Limits(max_connections=max(64, concurrency * 4)),
    )
    rng = random.Random(seed_base)

    def _pick_task() -> str:
        x = rng.random() * cum_total
        for nm, c in zip(names, cum):
            if x < c:
                return nm
        return names[-1]

    async def _do_b1(ref) -> float:
        code = b1_corpus[rng.randrange(n_b1)]
        url, headers = client.endpoint(ref, PORT_B1, "/run")
        t0 = time.perf_counter()
        r = await http.post(url, headers=headers, json={"code": code, "timeout": 10})
        r.raise_for_status()
        return (time.perf_counter() - t0) * 1000.0

    async def _do_b3(ref) -> float:
        steps = b3_trajs[rng.randrange(n_b3)][1]
        t0 = time.perf_counter()
        for method, path, body in steps:
            url, headers = client.endpoint(ref, PORT_B3, path)
            if method == "GET":
                r = await http.get(url, headers=headers, params=body)
            else:
                r = await http.post(url, headers=headers, json=body)
            r.raise_for_status()
        return (time.perf_counter() - t0) * 1000.0

    async def _do_b4(ref) -> float:
        steps = b4_trajs[rng.randrange(n_b4)][1]
        url, headers = client.endpoint(ref, PORT_B4_BASE, "/task")
        t0 = time.perf_counter()
        r = await http.post(url, headers=headers, json={
            "target_url": B4_TARGET_URL,
            "steps": [
                {"action": s["action"], "args": s.get("args", {})}
                for s in steps
            ],
        })
        r.raise_for_status()
        return (time.perf_counter() - t0) * 1000.0

    async def _do_b5(ref) -> float:
        q = TPCH_QUERIES[rng.randrange(n_q)]
        url, headers = client.endpoint(ref, PORT_B5, "/query")
        t0 = time.perf_counter()
        r = await http.post(url, headers=headers, json={"q": q})
        r.raise_for_status()
        return (time.perf_counter() - t0) * 1000.0

    async def _do_b7(ref) -> float:
        steps = rng.randint(B7_EPISODE_MIN, B7_EPISODE_MAX)
        url, headers = client.endpoint(ref, PORT_B7, "/episode")
        t0 = time.perf_counter()
        r = await http.post(url, headers=headers, json={
            "seed": rng.randrange(1 << 30),
            "steps": steps,
            "batch": 1,
        })
        r.raise_for_status()
        return (time.perf_counter() - t0) * 1000.0

    dispatch = {
        "B1": _do_b1, "B3": _do_b3, "B4": _do_b4,
        "B5": _do_b5, "B7": _do_b7,
    }

    async def work_fn() -> dict:
        t_all = time.perf_counter()
        ref, create_ms = await provider.acquire()
        idle_ms = 0.0
        n_steps = rng.randint(ep_min, ep_max)
        counters = {
            "steps_total": 0,
            "ops_B1": 0, "ops_B3": 0, "ops_B4": 0, "ops_B5": 0, "ops_B7": 0,
        }
        # Per-task latency for every step in this rollout, so the parent
        # can build a real distribution and show which stack owns the tail.
        per_task: dict[str, list[float]] = {
            "task_B1": [], "task_B3": [], "task_B4": [],
            "task_B5": [], "task_B7": [],
        }
        try:
            t_steps = time.perf_counter()
            for k in range(n_steps):
                task = _pick_task()
                lat = await dispatch[task](ref)
                counters[f"ops_{task}"] += 1
                counters["steps_total"] += 1
                per_task[f"task_{task}"].append(lat)
                if k < n_steps - 1:
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
            counters=counters,
            channels={k: v for k, v in per_task.items() if v},
        )

    async def teardown():
        await http.aclose()
        await provider.stop()
        await client.aclose()

    # Third element: fixture measurements taken before the clock started.
    # `provider.start()` already ran inside build_provider, so fill_stats
    # is populated here for the pre-creating strategies and None for
    # `fresh` (where every create is already counted per rollout).
    return work_fn, teardown, {"pool_fill": provider.fill_stats}


class B9Runner(Runner):
    name = "B9"
    workload = "concurrent"

    def __init__(self) -> None:
        b1_corpus, _ = load_corpus()
        b3_trajs, _ = build_b3_trajectories(target_total=200, seed=93)
        b4_trajs, _ = build_b4_trajectories(target_total=80, seed=94)
        # Pre-compute cumulative weights so the child process doesn't
        # recompute them on every step.
        names = [t for t, _ in TASK_MIX]
        cum: list[float] = []
        s = 0.0
        for _, w in TASK_MIX:
            s += w
            cum.append(s)
        self._setup = {
            "b1_corpus": b1_corpus,
            "b3_trajectories": [
                (t.template, [(m, p, b) for (m, p, b) in t.steps])
                for t in b3_trajs
            ],
            "b4_trajectories": [(t.template, list(t.steps)) for t in b4_trajs],
            "task_names": names,
            "task_cum": cum,
            "task_total": s,
        }
        self._template_id = ""

    async def warmup(self, cfg: Config) -> None:
        # Boot one sandbox and confirm every service in the fat template
        # answers, so a missing stack fails here rather than as a stream
        # of errors once the clock is running.
        self._template_id = await warmup_template(
            cfg, cfg.template_b9, port=PORT_B3, label=self.name,
        )
        await self._verify_all_services(cfg)

    async def _verify_all_services(self, cfg: Config) -> None:
        from cube import client_from_config
        client = client_from_config(cfg, max_connections=8)
        checks = [
            ("B1", PORT_B1, False),
            ("B3", PORT_B3, False),
            ("B4", PORT_B4_BASE, True),
            ("B5", PORT_B5, True),
            ("B7", PORT_B7, True),
        ]
        ref = None
        try:
            ref = await client.create(self._template_id or cfg.template_b9)
            for label, port, expect_ok in checks:
                await client.wait_port(
                    ref, port, "/healthz", timeout_sec=300.0,
                    expect_ok_field=expect_ok,
                )
                log.info("[B9] %s service ready on port %d", label, port)
        finally:
            if ref is not None:
                try:
                    await client.delete(ref.sandbox_id)
                except Exception:
                    pass
            await client.aclose()

    async def run_one(self, cfg: Config, instance: dict, concurrency: int) -> BenchmarkResult:
        sampler = ResourceSampler(cfg=cfg)
        sampler.start()
        duration = cfg.b9_duration_sec or cfg.duration_sec
        reset_task = schedule_sampler_reset(sampler, cfg.warmup_sec, name=self.name)

        ep_min, ep_max = cfg.b9_episode_range()
        setup = dict(self._setup)
        setup["cube"] = cfg.cube_setup()
        setup["template_id"] = self._template_id or cfg.template_b9
        setup["episode_min_steps"] = ep_min
        setup["episode_max_steps"] = ep_max

        log.info("[B9] mode=%s warmup=%ds + measure=%ds @ %d live sandboxes, "
                 "%d-%d steps/session, strategy=%s, think=%.0fms",
                 cfg.bench_mode, cfg.warmup_sec, duration, concurrency,
                 ep_min, ep_max, cfg.cube_strategy, cfg.think_time_ms)
        if cfg.think_time_ms > 0:
            # A closed-loop session spends think_time between steps, so
            # its step rate cannot exceed 1/think_time no matter how fast
            # the host is. When that ceiling binds, throughput is pure
            # arithmetic (concurrency / think_time) and comes out
            # identical on any two machines - which looks like a result
            # and is not one.
            ceiling = concurrency / (cfg.think_time_ms / 1000.0)
            log.info("[B9] think-time step-rate ceiling at this level: "
                     "%.1f steps/s (%d sessions / %.0fms). If the measured "
                     "rate lands near it, the run is think-time bound and "
                     "says nothing about the host; lower THINK_TIME_MS or "
                     "raise concurrency.", ceiling, concurrency, cfg.think_time_ms)

        try:
            mp_result = await drive_load_mp(
                work_factory=_b9_factory,
                setup=setup,
                concurrency=concurrency,
                duration_sec=duration,
                warmup_sec=cfg.warmup_sec,
                nprocs=cfg.client_nprocs or None,
                nprocs_max=cfg.client_nprocs_max,
                seed_base=0xB9 ^ concurrency,
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
        counters = mp_result["counters"]
        steps_total = int(counters.get("steps_total", 0))
        rollouts_per_sec = rollouts / duration
        steps_per_sec = steps_total / duration

        per_task = {}
        for tname in ("B1", "B3", "B4", "B5", "B7"):
            ch = mp_result["channels"].get(f"task_{tname}")
            if ch:
                per_task[tname] = {
                    "ops": int(counters.get(f"ops_{tname}", 0)),
                    **{k: ch.get(k) for k in ("count", "p50", "p95", "p99", "mean", "max")},
                }

        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=duration,
            throughput={
                # Under `hosting` a "rollout" is a whole agent session,
                # so sessions/sec is a function of session length and
                # think time rather than of the machine. `steps_per_sec`
                # is the unit that stays comparable across both shapes,
                # and `headline` names which one to read.
                "headline": "steps_per_sec" if cfg.is_hosting() else "rollouts_per_sec",
                "rollouts_per_sec": rollouts_per_sec,
                "steps_per_sec": steps_per_sec,
                "total_rollouts": rollouts,
                "total_steps": steps_total,
            },
            latency_ms={
                "rollout": mp_result["latency_ms"],
                "per_task": per_task,
                "sandbox": sandbox_latency_block(mp_result["channels"]),
            },
            resource=res,
            cost=cost_block(
                rollouts_per_sec, duration, instance["instance_type"], cfg,
                unit="session" if cfg.is_hosting() else "rollout",
                secondary_units={"step": steps_per_sec},
            ),
            extra={
                "mode": cfg.bench_mode,
                "task_mix": dict(TASK_MIX),
                "episode_steps_range": [ep_min, ep_max],
                "avg_steps_per_rollout": round(steps_total / rollouts, 2) if rollouts else 0.0,
                "all_stacks_in_one_sandbox": True,
                "ops_breakdown": {
                    t: int(counters.get(f"ops_{t}", 0))
                    for t in ("B1", "B3", "B4", "B5", "B7")
                },
                "sandbox": sandbox_extra_block(cfg, mp_result),
                "pool_fill": pool_fill_block(mp_result),
                "slo": slo_block(cfg, per_task, concurrency, steps_per_sec),
                "load_gen": load_gen_block(mp_result),
            },
        )
