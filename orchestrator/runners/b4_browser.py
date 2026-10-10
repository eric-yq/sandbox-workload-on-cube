"""B4 - Browser automation via Playwright + headless Chromium, one microVM per rollout.

Workload unchanged: trajectories sampled from 8 templates inspired by
WebArena shopping tasks, driven against a realistic static e-commerce SPA.
The worker reports per-step success so we can still compute a selector
miss rate.

Two structural changes come with the microVM mapping:

* **The SPA moves inside the sandbox.** Under docker-compose the static
  site was a separate nginx container on a shared bridge network. A
  per-rollout sandbox has to be self-contained, so its template runs
  nginx alongside the Playwright worker and `target_url` is plain
  `http://127.0.0.1:80`. The rollout no longer depends on anything
  outside its own VM, which is both more faithful to a real web-agent RL
  environment and removes a shared service that would otherwise have
  become a cross-rollout bottleneck.

* **The SO_REUSEPORT workaround is gone.** The old topology ran many
  single-worker uvicorn processes on adjacent ports inside one container,
  and the client had to round-robin explicitly because the aarch64 kernel
  hashed connections so unevenly that one PID took 35% of requests while
  another took 1%. With one Chromium master per sandbox there is no
  shared listening socket to hash over, and load spreading is simply a
  function of how many sandboxes are live. That entire class of bug does
  not exist here.

B4 stays the most resource-hungry archetype - each sandbox needs a
Chromium master plus renderer processes - so its concurrency sweep tops
out lower than B1/B3/B7 and it gets more memory per sandbox.
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
    PORT_B4_BASE,
    PORT_STATIC,
    build_provider,
    rollout_result,
    warmup_template,
)
from .b4_trajectories import build_trajectories
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

# nginx serves the SPA inside the same sandbox.
TARGET_URL = f"http://127.0.0.1:{PORT_STATIC}"


async def _b4_factory(*, setup: dict, seed_base: int, concurrency: int):
    """Child-process work factory. Must stay module-level to be picklable."""
    setup = dict(setup)
    setup["seed_base"] = seed_base
    client, provider, timer = await build_provider(
        setup, concurrency=concurrency,
        ready_port=PORT_B4_BASE, ready_expect_ok=True,
    )

    trajectories = setup["trajectories"]  # list[(template, steps)]
    n = len(trajectories)

    http = httpx.AsyncClient(
        # A browser trajectory is seconds, and at high sandbox counts
        # Chromium startup queues behind the CPU.
        timeout=300.0,
        limits=httpx.Limits(max_connections=max(32, concurrency * 2)),
    )
    rng = random.Random(seed_base)

    async def work_fn() -> dict:
        t_all = time.perf_counter()
        ref, create_ms = await provider.acquire()
        idle_ms = 0.0
        actions = steps_done = steps_failed = 0
        try:
            _template, steps = trajectories[rng.randrange(n)]
            payload = {
                "target_url": TARGET_URL,
                "steps": [
                    {"action": s["action"], "args": s.get("args", {})}
                    for s in steps
                ],
            }
            url, headers = client.endpoint(ref, PORT_B4_BASE, "/task")
            t_steps = time.perf_counter()
            r = await http.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            steps_ms = (time.perf_counter() - t_steps) * 1000.0
            actions = int(data.get("actions", 0))
            steps_done = int(data.get("steps_done", 0))
            steps_failed = int(data.get("steps_failed", 0))
            # Think time applies between environment steps; the worker
            # executes a whole trajectory in one call, so the only place
            # it can be injected is after it.
            if timer.enabled:
                idle_ms += await timer.wait(client, ref)
        finally:
            release_ms = await provider.release(ref)

        return rollout_result(
            wall_ms=(time.perf_counter() - t_all) * 1000.0,
            create_ms=create_ms,
            steps_ms=steps_ms,
            release_ms=release_ms,
            idle_overhead_ms=idle_ms,
            counters={
                "actions": actions,
                "steps_done": steps_done,
                "steps_failed": steps_failed,
            },
        )

    async def teardown():
        await http.aclose()
        await provider.stop()
        await client.aclose()

    return work_fn, teardown


class B4Runner(Runner):
    name = "B4"
    workload = "browser"

    def __init__(self) -> None:
        self._trajectories, self._breakdown = build_trajectories(
            target_total=80, seed=42,
        )
        # Flatten to plain tuples so the pool pickles cleanly into the
        # spawned load-generator children.
        self._traj_flat = [(t.template, list(t.steps)) for t in self._trajectories]
        self._template_id = ""

    async def warmup(self, cfg: Config) -> None:
        self._template_id = await warmup_template(
            cfg, cfg.template_b4, port=PORT_B4_BASE,
            expect_ok_field=True, label=self.name,
        )

    async def run_one(self, cfg: Config, instance: dict, concurrency: int) -> BenchmarkResult:
        sampler = ResourceSampler(cfg=cfg)
        sampler.start()
        reset_task = schedule_sampler_reset(sampler, cfg.warmup_sec, name=self.name)

        setup = {
            "cube": cfg.cube_setup(),
            "template_id": self._template_id or cfg.template_b4,
            "trajectories": self._traj_flat,
        }

        log.info("[B4] warmup=%ds + measure=%ds @ %d live sandboxes",
                 cfg.warmup_sec, cfg.duration_sec, concurrency)

        try:
            mp_result = await drive_load_mp(
                work_factory=_b4_factory,
                setup=setup,
                concurrency=concurrency,
                duration_sec=cfg.duration_sec,
                warmup_sec=cfg.warmup_sec,
                nprocs=cfg.client_nprocs or None,
                nprocs_max=cfg.client_nprocs_max,
                seed_base=0xB4 ^ concurrency,
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
        actions = int(counters.get("actions", 0))
        steps_failed = int(counters.get("steps_failed", 0))
        traj_per_sec = rollouts / cfg.duration_sec

        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=cfg.duration_sec,
            throughput={
                "trajectories_per_sec": traj_per_sec,
                "actions_per_sec": actions / cfg.duration_sec,
                "total_trajectories": rollouts,
                "total_actions": actions,
            },
            latency_ms={
                "trajectory": mp_result["latency_ms"],
                "sandbox": sandbox_latency_block(mp_result["channels"]),
            },
            resource=res,
            cost=cost_block(
                traj_per_sec, cfg.duration_sec, instance["instance_type"], cfg,
            ),
            extra={
                "trajectory_pool_size": len(self._traj_flat),
                "trajectory_breakdown": self._breakdown,
                "selector_miss_rate": round(steps_failed / actions, 4) if actions else 0.0,
                "steps_done_total": int(counters.get("steps_done", 0)),
                "steps_failed_total": steps_failed,
                "target_url": TARGET_URL,
                "spa_colocated_in_sandbox": True,
                "sandbox": sandbox_extra_block(cfg, mp_result),
                "load_gen": load_gen_block(mp_result),
            },
        )
