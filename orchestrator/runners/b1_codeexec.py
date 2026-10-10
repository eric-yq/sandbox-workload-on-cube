"""B1 - Python code execution, one microVM per rollout.

Workload is unchanged from the Docker version: a corpus of HumanEval +
MBPP-sanitized canonical solutions with their assert-style tests, each a
self-contained program that exits 0 on success. Every snippet is POSTed
to the b1 service's /run, which forks a fresh `prlimit ... python3 -c`
subprocess. That mirrors how a real Code-RL sandbox verifies an
LLM-generated patch.

What changed is the isolation boundary. Previously one op was one HTTP
request to a shared worker container. A single code execution is however
an environment *step*, not a rollout - a Code-RL trajectory looks like
"write patch -> run tests -> read the failure -> revise". So here one
rollout is `B1_EPISODE_STEPS` executions inside one dedicated sandbox
that lives for the whole episode.

Sizing note: with K=1 the sandbox create (Cube advertises sub-60ms) would
be the same order as the ~80ms code execution and the benchmark would
mostly measure the provisioning path instead of the workload. K=8 keeps
provisioning at a realistic fraction of rollout wall time. The
`sandbox_create` channel reports that fraction explicitly.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from config import Config
from metrics import ResourceSampler
from rollout import PORT_B1, build_provider, rollout_result, warmup_template
from .b1_corpus import load_corpus
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

# Per-snippet timeout (seconds). A handful of HumanEval tests touch
# brute-force / large-input edge cases, so 5s is too tight; 10s is
# conservative but still bounds the long tail.
SNIPPET_TIMEOUT = 10


async def _b1_factory(*, setup: dict, seed_base: int, concurrency: int):
    """Child-process work factory. Must stay module-level to be picklable."""
    setup = dict(setup)
    setup["seed_base"] = seed_base
    client, provider, timer = await build_provider(
        setup, concurrency=concurrency, ready_port=PORT_B1,
    )

    corpus = setup["corpus"]
    episode_steps = int(setup["episode_steps"])
    n = len(corpus)

    http = httpx.AsyncClient(
        timeout=SNIPPET_TIMEOUT * 6,
        limits=httpx.Limits(max_connections=max(32, concurrency * 2)),
    )
    rng = random.Random(seed_base)
    state = {"i": 0}

    async def work_fn() -> dict:
        t_all = time.perf_counter()
        ref, create_ms = await provider.acquire()
        idle_ms = 0.0
        passes = 0
        steps_done = 0
        try:
            t_steps = time.perf_counter()
            for k in range(episode_steps):
                i = state["i"]
                state["i"] += 1
                # Mix the child's seed into the walk so two children do
                # not synchronise on the same expensive HumanEval problem.
                code = corpus[(rng.randrange(n) + i) % n]

                url, headers = client.endpoint(ref, PORT_B1, "/run")
                r = await http.post(
                    url, headers=headers,
                    json={"code": code, "timeout": SNIPPET_TIMEOUT},
                )
                r.raise_for_status()
                steps_done += 1
                try:
                    if r.json().get("exit_code") == 0:
                        passes += 1
                except Exception:
                    pass

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
            counters={"passes": passes, "steps_total": steps_done},
        )

    async def teardown():
        await http.aclose()
        await provider.stop()
        await client.aclose()

    return work_fn, teardown


class B1Runner(Runner):
    name = "B1"
    workload = "codeexec"

    def __init__(self) -> None:
        self._corpus, self._breakdown = load_corpus()
        # Shuffle once per process so different concurrency runs see the
        # same sequence (comparability) while workers within a run do not
        # synchronise on the same problem at the same wall time.
        random.Random(42).shuffle(self._corpus)
        self._template_id = ""

    async def warmup(self, cfg: Config) -> None:
        self._template_id = await warmup_template(
            cfg, cfg.template_b1, port=PORT_B1, label=self.name,
        )

    async def run_one(self, cfg: Config, instance: dict, concurrency: int) -> BenchmarkResult:
        sampler = ResourceSampler(cfg=cfg)
        sampler.start()
        reset_task = schedule_sampler_reset(sampler, cfg.warmup_sec, name=self.name)

        setup = {
            "cube": cfg.cube_setup(),
            "template_id": self._template_id or cfg.template_b1,
            "corpus": self._corpus,
            "episode_steps": cfg.b1_episode_steps,
        }

        log.info("[B1] warmup=%ds + measure=%ds @ %d live sandboxes, K=%d steps/rollout",
                 cfg.warmup_sec, cfg.duration_sec, concurrency, cfg.b1_episode_steps)

        try:
            mp_result = await drive_load_mp(
                work_factory=_b1_factory,
                setup=setup,
                concurrency=concurrency,
                duration_sec=cfg.duration_sec,
                warmup_sec=cfg.warmup_sec,
                nprocs=cfg.client_nprocs or None,
                nprocs_max=cfg.client_nprocs_max,
                seed_base=0xB1 ^ concurrency,
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
        execs = int(counters.get("steps_total", 0))
        passes = int(counters.get("passes", 0))

        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=cfg.duration_sec,
            throughput={
                "executions_per_sec": execs / cfg.duration_sec,
                "rollouts_per_sec": rollouts / cfg.duration_sec,
                "total_executions": execs,
                "total_rollouts": rollouts,
            },
            latency_ms={
                "rollout": mp_result["latency_ms"],
                "sandbox": sandbox_latency_block(mp_result["channels"]),
            },
            resource=res,
            cost=cost_block(
                execs / cfg.duration_sec, cfg.duration_sec,
                instance["instance_type"], cfg,
            ),
            extra={
                "corpus_size": len(self._corpus),
                "corpus_breakdown": self._breakdown,
                "snippet_timeout_sec": SNIPPET_TIMEOUT,
                "episode_steps": cfg.b1_episode_steps,
                "pass_rate": round(passes / execs, 4) if execs else 0.0,
                "sandbox": sandbox_extra_block(cfg, mp_result),
                "load_gen": load_gen_block(mp_result),
            },
        )
