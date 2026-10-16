"""Base classes for benchmark runners."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from config import Config

log = logging.getLogger(__name__)


@dataclass
class BenchmarkResult:
    benchmark: str
    instance: dict[str, Any]
    concurrency: int
    duration_sec: float
    throughput: dict[str, float] = field(default_factory=dict)
    latency_ms: dict[str, Any] = field(default_factory=dict)
    resource: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S"))

    def to_dict(self) -> dict:
        return {
            "benchmark": self.benchmark,
            "instance": self.instance,
            "concurrency": self.concurrency,
            "duration_sec": self.duration_sec,
            "throughput": self.throughput,
            "latency_ms": self.latency_ms,
            "resource": self.resource,
            "cost": self.cost,
            "extra": self.extra,
            "timestamp": self.timestamp,
        }


# vCPU implied by an instance size suffix (AWS standard sizes, plus the
# bare-metal SKUs this benchmark targets). `metal-48xl` is the one that
# matters here: the previous version of this table stopped at `48xlarge`,
# so every metal instance silently priced at $0/h and every
# cost_per_1k_units_usd came out as null.
SIZE_TO_VCPUS = {
    "large":       2,  "xlarge":      4,
    "2xlarge":     8,  "4xlarge":    16,  "8xlarge":    32,
    "12xlarge":   48,  "16xlarge":   64,  "24xlarge":   96,
    "48xlarge":  192,
    "metal-16xl": 64,  "metal-24xl": 96,  "metal-48xl": 192,
}

# A bare `.metal` suffix carries no vCPU count of its own - c7g.metal is 64
# vCPU while i4i.metal is 128 - so it deliberately has no entry above. Such
# an instance must be priced by exact match in INSTANCE_PRICES or
# DEFAULT_INSTANCE_PRICES; the linear fallback cannot guess it.


def hourly_price(instance_type: str, cfg: Config) -> float:
    """$/h for the instance under test, used for cost-per-unit maths.

    Resolution order:

    1. Exact instance-type match against `INSTANCE_PRICES` from .env,
       then the built-in table in config.py. This is the accurate path
       and the only one used for the four metal SKUs we benchmark -
       real on-demand pricing is *not* perfectly linear across sizes
       (m9g.metal-48xl runs ~3% above 12x m9g.4xlarge), so extrapolating
       would quietly bias every cost number.
    2. Legacy fallback: scale a .4xlarge anchor linearly by vCPU. Kept
       so results recorded by the Docker-era version of this suite stay
       reproducible.

    Returns 0.0 when the instance is unknown, which makes `cost_block`
    emit a null cost rather than a fabricated one.
    """
    t = (instance_type or "").strip().lower()
    if not t:
        return 0.0

    exact = cfg.price_for(t)
    if exact > 0:
        return exact

    parts = t.split(".")
    if len(parts) < 2:
        return 0.0
    family, size = parts[0], parts[1]
    vcpus = SIZE_TO_VCPUS.get(size)
    if vcpus is None:
        return 0.0

    # Legacy two-anchor model. Note this deliberately does NOT try to
    # separate m8g from m9g - they share one anchor and differ by ~9% in
    # reality, which is exactly why path 1 above should be used instead.
    base_4xl = None
    if family.startswith(("c7i", "m7i", "m8i")):
        base_4xl = cfg.price_c7i_4xl
    elif family.startswith(("c8g", "m8g", "m9g")):
        base_4xl = cfg.price_c8g_4xl
    if not base_4xl:
        return 0.0
    log.warning(
        "no exact price for %s; falling back to linear %s anchor scaling. "
        "Add it to INSTANCE_PRICES for accurate cost numbers.", t, family,
    )
    return base_4xl * (vcpus / 16.0)


def cost_block(
    throughput_per_sec: float,
    duration: float,
    instance_type: str,
    cfg: Config,
    *,
    unit: str = "rollout",
    secondary_units: dict[str, float] | None = None,
) -> dict:
    """Cost per unit of work, charging the whole instance for the run.

    Both per-1k and per-1M are reported: per-1k reads naturally for the
    slower archetypes (browser trajectories), per-1M for the fast ones
    (tool calls) and is what the write-up tables use.

    `unit` names what one unit is, because the answer differs by
    archetype and the bare name `units` has been actively misleading: for
    an RL rollout a unit is one episode, but for a hosted agent session a
    "rollout" is the entire session, so cost-per-unit then varies with
    session length and think time rather than with the machine. Pass
    `secondary_units={"step": steps_per_sec}` to get the per-step cost
    alongside it - that is the figure that stays comparable across both
    shapes.
    """
    price_h = hourly_price(instance_type, cfg)
    cost_run = price_h * (duration / 3600.0)

    def _per(rate: float) -> float | None:
        total = max(rate * duration, 1e-9)
        return (cost_run / total) if price_h > 0 else None

    per_unit = _per(throughput_per_sec)
    out: dict[str, Any] = {
        "instance_hourly_usd": price_h,
        "unit": unit,
        "cost_per_1k_units_usd": per_unit * 1000.0 if per_unit is not None else None,
        "cost_per_1m_units_usd": per_unit * 1_000_000.0 if per_unit is not None else None,
    }
    for name, rate in (secondary_units or {}).items():
        per = _per(float(rate or 0.0))
        out[f"cost_per_1k_{name}s_usd"] = per * 1000.0 if per is not None else None
        out[f"cost_per_1m_{name}s_usd"] = per * 1_000_000.0 if per is not None else None
    return out


def load_gen_block(mp_result: dict) -> dict:
    """Client-side load generator shape, for the result blob."""
    return {
        "nprocs": mp_result.get("nprocs"),
        "concurrency_per_proc": mp_result.get("concurrency_per_proc"),
    }


# Named channels every rollout-based runner emits, in report order.
SANDBOX_CHANNELS = (
    "sandbox_create",
    "steps",
    "sandbox_release",
    "idle_pause_overhead",
)


def sandbox_latency_block(channels: dict) -> dict:
    """Per-phase rollout latency, pulled out of the named channels.

    `sandbox_create` covers create-until-first-request-accepted, so it is
    the number that answers "how long before the trainer can dispatch a
    tool call". `steps` is the workload proper.
    """
    return {k: channels[k] for k in SANDBOX_CHANNELS if channels.get(k)}


def sandbox_extra_block(cfg: Config, mp_result: dict) -> dict:
    """How much of a rollout was environment provisioning, not work.

    This is the headline question when swapping a container sandbox tier
    for a microVM one, so it belongs in every result file rather than
    being derivable only by hand from the channel percentiles.
    """
    channels = mp_result.get("channels", {})
    rollout_p50 = (mp_result.get("latency_ms") or {}).get("p50") or 0.0
    create_p50 = (channels.get("sandbox_create") or {}).get("p50") or 0.0
    release_p50 = (channels.get("sandbox_release") or {}).get("p50") or 0.0
    overhead = create_p50 + release_p50
    reused = cfg.cube_strategy == "pool"
    return {
        "mode": cfg.bench_mode,
        "strategy": cfg.cube_strategy,
        "transport": cfg.transport_label(),
        "one_sandbox_per_rollout": not reused,
        # Whether sandbox state survives from one rollout to the next.
        # `rollback` reuses a sandbox but restores it from a snapshot
        # first; `pool` hands the same dirty sandbox straight back out.
        #
        # Whether that is a defect depends entirely on the archetype, so
        # it is reported as a fact and interpreted by `mode`. Under `rl`
        # it is contamination between training episodes and makes the
        # throughput a reference ceiling rather than a headline. Under
        # `hosting` it is the thing being sold: one long-lived agent
        # session whose files, installed packages and interpreter
        # namespace persist across steps. Same mechanism, opposite
        # meaning.
        "state_reset_between_rollouts": not reused,
        "session_reuse": reused,
        "sandbox_timeout_sec": cfg.cube_sandbox_timeout_sec,
        "provisioning_share_p50": round(overhead / rollout_p50, 4) if rollout_p50 else None,
        "think_time_ms": cfg.think_time_ms,
        "think_time_jitter_ms": cfg.think_time_jitter_ms,
        "think_time_pause": cfg.think_time_pause,
    }


def pool_fill_block(mp_result: dict) -> dict | None:
    """How long it took to stand the sandbox fleet up, and at what rate.

    Pool/clone/rollback pre-create their sandboxes in
    `SandboxProvider.start()`, deliberately outside the measured window -
    for a hosting tier that cost is amortised over a session lasting
    hours, so charging it to the first step would be wrong.

    But "outside the window" had meant "discarded", and fleet stand-up
    time is itself a product metric: it is the scale-out latency when a
    hosting tier has to add capacity, and it is exactly where the 4 KiB
    arm64 create path shows up. So it is reported separately instead.

    Aggregated across load-generator children: each child fills its own
    slice of the pool concurrently, so `wall_ms` is the max (when the
    last child finished) while the counts and failures sum.
    """
    fixtures = [f for f in (mp_result.get("fixtures") or []) if f]
    fills = [f.get("pool_fill") for f in fixtures if f.get("pool_fill")]
    if not fills:
        return None

    requested = sum(int(f.get("requested") or 0) for f in fills)
    created = sum(int(f.get("created") or 0) for f in fills)
    failed = sum(int(f.get("failed") or 0) for f in fills)
    wall_ms = max((float(f.get("wall_ms") or 0.0) for f in fills), default=0.0)
    per_create = [p for f in fills for p in (f.get("create_ms") or [])]

    out: dict[str, Any] = {
        "strategy": fills[0].get("strategy"),
        "requested": requested,
        "created": created,
        "failed": failed,
        "wall_ms": round(wall_ms, 2),
        "creates_per_sec": round(created / (wall_ms / 1000.0), 2) if wall_ms > 0 else None,
    }
    if per_create:
        import numpy as np

        a = np.array(per_create)
        out["create_ms"] = {
            "count": int(a.size),
            "p50": float(np.percentile(a, 50)),
            "p95": float(np.percentile(a, 95)),
            "p99": float(np.percentile(a, 99)),
            "mean": float(a.mean()),
            "max": float(a.max()),
        }
    return out


def slo_block(cfg: Config, per_task: dict, sessions: int, steps_per_sec: float) -> dict:
    """The observed step latency this concurrency level delivered.

    Deliberately only *records*; the pass/fail verdict needs the whole
    sweep (the relative budget is anchored on the lowest concurrency
    point), so it is computed at roll-up time in main.py. Keeping the
    judgement out of here means a single result file is never stale with
    respect to a budget change.
    """
    metric = cfg.slo_metric if cfg.slo_metric in ("p50", "p95", "p99") else "p95"
    observed = {
        stack: v.get(metric)
        for stack, v in (per_task or {}).items()
        if isinstance(v, dict) and v.get(metric) is not None
    }
    return {
        "metric": metric,
        "sessions": sessions,
        "steps_per_sec": steps_per_sec,
        "steps_per_sec_per_session": round(steps_per_sec / sessions, 4) if sessions else None,
        "observed_step_ms": observed,
        "budget_ms": {k: v for k, v in (cfg.slo_step_p95_ms or {}).items()},
        "degrade_factor": cfg.slo_degrade_factor,
    }


def fold_create_fails(resource: dict, mp_result: dict) -> dict:
    """Record sandbox-create failures counted by the load generator.

    CubeSandbox exports no create-success/failure counter anywhere:
    CubeAPI's `/health` reports a hardcoded `sandboxes: 0`, Cubelet's
    Prometheus surface only has an in-flight create gauge, and CubeOps has
    no cumulative counter either. The client that issued the calls is the
    only place this can be known, so the count travels up through the
    rollout counters and is folded into the resource block here.

    It also forces the saturation verdict: a level that could not create
    the sandboxes it asked for is invalid no matter how idle the CPU
    looked, and without this it would read as a merely disappointing
    throughput number.
    """
    fails = int((mp_result.get("counters") or {}).get("sandbox_create_fails", 0) or 0)
    cp = resource.setdefault("control_plane", {})
    cp["create_fails"] = int(cp.get("create_fails", 0) or 0) + fails
    if cp["create_fails"] > 0:
        cp["control_plane_saturated"] = True
    return resource


class Runner:
    """Subclasses implement `run_one(cfg, instance, concurrency)`.

    The base orchestrator will call this once per concurrency level.
    """

    name: str = "BASE"
    workload: str = "base"

    async def warmup(self, cfg: Config) -> None:  # noqa: D401
        """Override to perform service-readiness checks before measurement."""
        await asyncio.sleep(1)

    async def run_one(
        self, cfg: Config, instance: dict, concurrency: int
    ) -> BenchmarkResult:
        raise NotImplementedError


def schedule_sampler_reset(
    sampler, warmup_sec: float, name: str = "?",
) -> "asyncio.Task | None":
    """Reset `sampler` after `warmup_sec` seconds so its averages reflect
    the steady-state phase only. No-op when warmup_sec <= 0.

    Returns the scheduled Task (caller should keep a reference so the
    task isn't garbage-collected before it fires) or None.
    """
    if warmup_sec <= 0:
        return None

    async def _reset():
        await asyncio.sleep(warmup_sec)
        sampler.reset()
        log.info("[%s] warmup window done; resource sampler reset", name)

    return asyncio.create_task(_reset())
