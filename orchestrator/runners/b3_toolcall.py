"""B3 - Tool/API call trajectories (tau-bench style), one microVM per rollout.

The workload is unchanged: trajectories sampled from a pre-materialised
pool built from 7 templates inspired by tau-bench retail tasks
(browse_only, buy_simple, buy_with_compare, refund_flow, inventory_heavy,
checkout_loop, profile_admin), averaging ~11.7 HTTP calls each.

B3 was already rollout-shaped - one op has always been one full
trajectory - so this is the archetype where the mapping onto a
per-rollout microVM is exact: one trajectory, one sandbox.

It is also where per-rollout isolation fixes a genuine fidelity bug. The
trajectory bodies never send a `user` field, so under the shared-container
topology every one of the 200 trajectories fell through to
`user="alice"` and contended on a single `threading.Lock` inside one
mock-API process. Nominally independent rollouts were serialising on each
other's cart state. Each rollout now gets a private mock API, which is
what a real tool-use RL environment looks like - and it means B3 numbers
are NOT comparable with the shared-container ones.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from config import Config
from metrics import ResourceSampler
from rollout import PORT_B3, build_provider, rollout_result, warmup_template
from .b3_trajectories import build_trajectories
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


async def _b3_factory(*, setup: dict, seed_base: int, concurrency: int):
    """Child-process work factory. Must stay module-level to be picklable."""
    setup = dict(setup)
    setup["seed_base"] = seed_base
    client, provider, timer = await build_provider(
        setup, concurrency=concurrency, ready_port=PORT_B3,
    )

    trajectories = setup["trajectories"]  # list[(template, steps)]
    n = len(trajectories)

    http = httpx.AsyncClient(
        timeout=60.0,
        limits=httpx.Limits(
            max_connections=max(64, concurrency * 4),
            max_keepalive_connections=max(64, concurrency * 4),
        ),
    )
    rng = random.Random(seed_base)

    async def work_fn() -> dict:
        t_all = time.perf_counter()
        ref, create_ms = await provider.acquire()
        idle_ms = 0.0
        calls = 0
        try:
            template, steps = trajectories[rng.randrange(n)]
            t_steps = time.perf_counter()
            for k, (method, path, body) in enumerate(steps):
                url, headers = client.endpoint(ref, PORT_B3, path)
                if method == "GET":
                    r = await http.get(url, headers=headers, params=body)
                else:
                    r = await http.post(url, headers=headers, json=body)
                r.raise_for_status()
                calls += 1
                if k < len(steps) - 1:
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
            counters={"tool_calls": calls},
        )

    async def teardown():
        await http.aclose()
        await provider.stop()
        await client.aclose()

    return work_fn, teardown


class B3Runner(Runner):
    name = "B3"
    workload = "toolcall"

    def __init__(self) -> None:
        self._trajectories, self._breakdown = build_trajectories(
            target_total=200, seed=42,
        )
        # Flatten the Trajectory dataclass to plain tuples so it pickles
        # without dragging the dataclass module across processes.
        self._traj_flat = [
            (t.template, [(m, p, b) for (m, p, b) in t.steps])
            for t in self._trajectories
        ]
        self._template_id = ""

    async def warmup(self, cfg: Config) -> None:
        self._template_id = await warmup_template(
            cfg, cfg.template_b3, port=PORT_B3, label=self.name,
        )

    async def run_one(self, cfg: Config, instance: dict, concurrency: int) -> BenchmarkResult:
        sampler = ResourceSampler(cfg=cfg)
        sampler.start()
        reset_task = schedule_sampler_reset(sampler, cfg.warmup_sec, name=self.name)

        setup = {
            "cube": cfg.cube_setup(),
            "template_id": self._template_id or cfg.template_b3,
            "trajectories": self._traj_flat,
        }

        log.info("[B3] warmup=%ds + measure=%ds @ %d live sandboxes",
                 cfg.warmup_sec, cfg.duration_sec, concurrency)

        try:
            mp_result = await drive_load_mp(
                work_factory=_b3_factory,
                setup=setup,
                concurrency=concurrency,
                duration_sec=cfg.duration_sec,
                warmup_sec=cfg.warmup_sec,
                nprocs=cfg.client_nprocs or None,
                nprocs_max=cfg.client_nprocs_max,
                seed_base=0xB3 ^ concurrency,
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
        calls = int(mp_result["counters"].get("tool_calls", 0))
        traj_per_sec = rollouts / cfg.duration_sec
        avg_steps = (
            sum(len(t[1]) for t in self._traj_flat) / len(self._traj_flat)
            if self._traj_flat else 0.0
        )

        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=cfg.duration_sec,
            throughput={
                "trajectories_per_sec": traj_per_sec,
                "tool_calls_per_sec": calls / cfg.duration_sec,
                "total_trajectories": rollouts,
                "total_tool_calls": calls,
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
                "avg_steps_per_trajectory": round(avg_steps, 2),
                "per_rollout_state_isolation": True,
                "sandbox": sandbox_extra_block(cfg, mp_result),
                "load_gen": load_gen_block(mp_result),
            },
        )
