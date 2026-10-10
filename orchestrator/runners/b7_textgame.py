"""B7 - Text-game simulation (ALFWorld / TextWorld style), one microVM per rollout.

Workload unchanged: a hand-rolled minigrid world (8 rooms, 30 items, 6
goal templates) played for 30-50 steps per episode. As the original B7
design says, the signal is scheduling plus GIL plus tight Python state
loops, not task-completion accuracy, so a pure-Python simulator gives the
same signal without heavy native dependencies.

One episode is one rollout, which makes the mapping direct: each rollout
sandbox plays `B7_EPISODES_PER_ROLLOUT` episodes and is then discarded.

Note the batching semantics changed. The shared-container version packed
10 episodes into a single HTTP request specifically so that per-request
wall time rose above FastAPI dispatch noise. With a dedicated sandbox per
rollout that trick is no longer needed to get a measurable signal - the
sandbox lifecycle already dominates dispatch overhead - so the default is
one episode per rollout, which is the honest RL shape. Raising
B7_EPISODES_PER_ROLLOUT amortises provisioning over more episodes if you
want to isolate the simulator from the sandbox cost.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from config import Config
from metrics import ResourceSampler
from rollout import PORT_B7, build_provider, rollout_result, warmup_template
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

EPISODE_MIN = 30
EPISODE_MAX = 50


async def _b7_factory(*, setup: dict, seed_base: int, concurrency: int):
    """Child-process work factory. Must stay module-level to be picklable."""
    setup = dict(setup)
    setup["seed_base"] = seed_base
    client, provider, timer = await build_provider(
        setup, concurrency=concurrency,
        ready_port=PORT_B7, ready_expect_ok=True,
    )

    episodes_per_rollout = max(1, int(setup["episodes_per_rollout"]))

    http = httpx.AsyncClient(
        timeout=120.0,
        limits=httpx.Limits(max_connections=max(32, concurrency * 2)),
    )
    rng = random.Random(seed_base)

    async def work_fn() -> dict:
        t_all = time.perf_counter()
        ref, create_ms = await provider.acquire()
        idle_ms = 0.0
        steps_total = 0
        reward_total = 0.0
        episodes = 0
        try:
            t_steps = time.perf_counter()
            for k in range(episodes_per_rollout):
                steps = rng.randint(EPISODE_MIN, EPISODE_MAX)
                url, headers = client.endpoint(ref, PORT_B7, "/episode")
                r = await http.post(url, headers=headers, json={
                    "seed": rng.randrange(1 << 30),
                    "steps": steps,
                    "batch": 1,
                })
                r.raise_for_status()
                data = r.json()
                steps_total += int(data.get("steps_done", 0))
                reward_total += float(data.get("total_reward", 0.0))
                episodes += 1
                if k < episodes_per_rollout - 1:
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
            counters={
                "episodes": episodes,
                "steps_total": steps_total,
                "reward_total": reward_total,
            },
        )

    async def teardown():
        await http.aclose()
        await provider.stop()
        await client.aclose()

    return work_fn, teardown


class B7Runner(Runner):
    name = "B7"
    workload = "textgame"

    def __init__(self) -> None:
        self._template_id = ""

    async def warmup(self, cfg: Config) -> None:
        self._template_id = await warmup_template(
            cfg, cfg.template_b7, port=PORT_B7,
            expect_ok_field=True, label=self.name,
        )

    async def run_one(self, cfg: Config, instance: dict, concurrency: int) -> BenchmarkResult:
        sampler = ResourceSampler(cfg=cfg)
        sampler.start()
        reset_task = schedule_sampler_reset(sampler, cfg.warmup_sec, name=self.name)

        setup = {
            "cube": cfg.cube_setup(),
            "template_id": self._template_id or cfg.template_b7,
            "episodes_per_rollout": cfg.b7_episodes_per_rollout,
        }

        log.info("[B7] warmup=%ds + measure=%ds @ %d live sandboxes, %d episode(s)/rollout",
                 cfg.warmup_sec, cfg.duration_sec, concurrency,
                 cfg.b7_episodes_per_rollout)

        try:
            mp_result = await drive_load_mp(
                work_factory=_b7_factory,
                setup=setup,
                concurrency=concurrency,
                duration_sec=cfg.duration_sec,
                warmup_sec=cfg.warmup_sec,
                nprocs=cfg.client_nprocs or None,
                nprocs_max=cfg.client_nprocs_max,
                seed_base=0xB7 ^ concurrency,
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
        episodes = int(counters.get("episodes", 0))
        steps_total = int(counters.get("steps_total", 0))
        reward_total = float(counters.get("reward_total", 0.0))
        episodes_per_sec = episodes / cfg.duration_sec

        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=cfg.duration_sec,
            throughput={
                "episodes_per_sec": episodes_per_sec,
                "steps_per_sec": steps_total / cfg.duration_sec,
                "rollouts_per_sec": rollouts / cfg.duration_sec,
                "total_episodes": episodes,
                "total_steps": steps_total,
            },
            latency_ms={
                "rollout": mp_result["latency_ms"],
                "sandbox": sandbox_latency_block(mp_result["channels"]),
            },
            resource=res,
            cost=cost_block(
                episodes_per_sec, cfg.duration_sec, instance["instance_type"], cfg,
            ),
            extra={
                "episode_steps_range": [EPISODE_MIN, EPISODE_MAX],
                "episodes_per_rollout": cfg.b7_episodes_per_rollout,
                "avg_steps_per_episode": round(steps_total / episodes, 2) if episodes else 0.0,
                "avg_reward_per_episode": round(reward_total / episodes, 4) if episodes else 0.0,
                "world_rooms": 8,
                "world_items": 30,
                "goal_templates": 6,
                "sandbox": sandbox_extra_block(cfg, mp_result),
                "load_gen": load_gen_block(mp_result),
            },
        )
