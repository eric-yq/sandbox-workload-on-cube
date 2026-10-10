"""Rollout -> sandbox lifecycle.

The model this whole suite is built on: **one rollout, one microVM.**

Three layers of nesting in Agentic-RL training:

    training step (one gradient update)
     └── rollout x (bsz x G)        <- a batch of trajectories, independent
          └── env step x T          <- strictly serial, stateful
               └── one tool call / code exec / browser action

A rollout is the natural isolation boundary and an env step is not: the
steps inside one rollout must share a cart / filesystem / browser
session, while rollouts must be fully isolated from each other. So a
concurrency level here means "simultaneously live rollout sandboxes",
not "in-flight HTTP requests against a shared worker".

Four ways to hand a sandbox to a rollout, selected by `CUBE_STRATEGY`:

``fresh`` (default)
    create -> run one rollout -> delete. Maximum isolation, and what an
    RL trainer that trusts nothing does.
``pool``
    Pre-create `concurrency` sandboxes and reuse them across rollouts
    **without resetting guest state**. Trades isolation away for an upper
    bound on achievable throughput - a reference line, not a headline
    number.
``clone``
    Keep one warm golden sandbox, snapshot it once, and create every
    rollout's sandbox from that snapshot. This is the GRPO shape: G
    candidate rollouts all starting from one identical state. Note the
    asymmetry with the AgentENV version of this suite, which used a
    single batched `fork` call - Cube has no server-side fan-out, so each
    child is a full create round trip and we pipeline them instead.
``rollback``
    Cube-only, and the interesting one. Pre-create a pool, snapshot each
    member once, then reset a sandbox in place with
    `POST /sandboxes/{id}/rollback` when a rollout hands it back. That
    buys pool-like throughput *without* giving up state isolation, which
    neither containers nor AgentENV could offer.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

from cube import (
    BENCH_METADATA,
    BENCH_METADATA_KEY,  # noqa: F401  (re-exported for callers)
    CubeClient,
    CubeError,
    SandboxRef,
    SnapshotRef,
    client_from_config,
    client_from_setup,
)

log = logging.getLogger(__name__)

# Service ports inside a sandbox. Unchanged from the Docker and AgentENV
# topologies - only the transport in front of them changed. nginx serves
# the static SPA on port 80 (see PORT_STATIC users in the B4/B9 runners).
PORT_B1 = 8001
PORT_B3 = 8003
PORT_B4_BASE = 8004
PORT_B5 = 8005
PORT_B7 = 8007
PORT_STATIC = 80

# Aggregate readiness gate (workers/arl-sandbox/readyz.py), used as the
# template probe port for the two templates that run more than one service
# (arl-b4, arl-b9). Cube freezes a template's snapshot as soon as a single
# HTTP probe answers 2xx, so those templates need one endpoint that speaks
# for all of their services; single-service templates are probed directly
# on their own port and do not run this. See scripts/provision-templates.sh.
#
# Note the runners still probe their *workload* port rather than this one:
# Cube's readiness contract covers the probed port, and the number we want
# is "when could a trainer dispatch its first tool call".
PORT_READY = 8010

VALID_STRATEGIES = ("fresh", "pool", "clone", "rollback")

# Create failures have to be counted here because Cube exports no such
# counter anywhere: CubeAPI's /health reports a hardcoded sandboxes=0, the
# Cubelet scheduler gauges only expose in-flight creates, and no component
# has a cumulative create-success/failure metric. The count is drained
# into the next rollout's counters by `rollout_result`, which keeps the
# six workload runners from each having to thread it through by hand.
# One provider per child process, so a module-level tally is unambiguous.
_PENDING_CREATE_FAILS = 0


def _note_create_fail() -> None:
    global _PENDING_CREATE_FAILS
    _PENDING_CREATE_FAILS += 1


def _drain_create_fails() -> int:
    global _PENDING_CREATE_FAILS
    n, _PENDING_CREATE_FAILS = _PENDING_CREATE_FAILS, 0
    return n


class SandboxProvider:
    """Hands out one sandbox per rollout according to the strategy.

    Lives inside a load-generator child process; each child builds its
    own. Not safe to share across processes.
    """

    def __init__(
        self,
        client: CubeClient,
        template_id: str,
        *,
        strategy: str = "fresh",
        pool_size: int = 0,
        concurrency: int = 1,
        timeout_sec: Optional[int] = None,
        ready_port: Optional[int] = None,
        ready_path: str = "/healthz",
        ready_expect_ok: bool = False,
        clone_batch: int = 16,
    ) -> None:
        self.client = client
        self.template_id = template_id
        self.strategy = strategy if strategy in VALID_STRATEGIES else "fresh"
        self.concurrency = max(1, concurrency)
        self.pool_size = pool_size or self.concurrency
        self.timeout_sec = timeout_sec
        self.ready_port = ready_port
        self.ready_path = ready_path
        self.ready_expect_ok = ready_expect_ok

        self._pool: Optional[asyncio.Queue[SandboxRef]] = None
        self._golden: Optional[SandboxRef] = None
        self._snapshot: Optional[SnapshotRef] = None
        # rollback strategy: sandbox_id -> its own reset point.
        self._reset_points: dict[str, str] = {}
        self._all: list[SandboxRef] = []
        self._clone_lock = asyncio.Lock()
        self._clone_ready: list[SandboxRef] = []
        # Children are created in concurrent batches rather than one per
        # rollout. The snapshot is taken once at start(), so a refill is
        # `clone_batch` independent creates issued together - which is both
        # the GRPO fan-out shape and the only way to amortise per-create
        # round-trip latency, since Cube has no batched clone call.
        self._clone_batch = max(1, min(clone_batch, 64))
        self._started = False

    # ---------------- setup / teardown ----------------
    async def start(self) -> None:
        """Pre-create whatever the strategy needs before measurement.

        Deliberately done outside the measured window: for `pool`,
        `clone` and `rollback` the up-front cost is a fixture, not part of
        any rollout.
        """
        if self._started:
            return
        self._started = True

        if self.strategy in ("pool", "rollback"):
            self._pool = asyncio.Queue()
            refs = await asyncio.gather(
                *[self._create() for _ in range(self.pool_size)],
                return_exceptions=True,
            )
            ok = 0
            for r in refs:
                if isinstance(r, SandboxRef):
                    self._pool.put_nowait(r)
                    ok += 1
            if ok == 0:
                raise CubeError(
                    f"{self.strategy} strategy: failed to create any of "
                    f"{self.pool_size} sandboxes"
                )
            if ok < self.pool_size:
                log.warning("%s strategy: only %d/%d sandboxes came up",
                            self.strategy, ok, self.pool_size)

            if self.strategy == "rollback":
                # One reset point per pool member, captured while the
                # sandbox is pristine. Snapshotting is serialised per
                # sandbox anyway, and the *first* snapshot of a sandbox is
                # the expensive one (Cube defers the soft-dirty reset into
                # it), so taking them all here keeps that cost out of the
                # measured window entirely.
                members = list(self._pool._queue)  # type: ignore[attr-defined]
                snaps = await asyncio.gather(
                    *[self.client.snapshot(m.sandbox_id) for m in members],
                    return_exceptions=True,
                )
                for member, snap in zip(members, snaps):
                    if isinstance(snap, SnapshotRef):
                        self._reset_points[member.sandbox_id] = snap.snapshot_id
                    else:
                        log.warning(
                            "rollback strategy: no reset point for %s (%r); it "
                            "will be reused without a state reset",
                            member, snap,
                        )
                log.info("rollback strategy: %d/%d reset points captured",
                         len(self._reset_points), len(members))

        elif self.strategy == "clone":
            # One golden sandbox, snapshotted once. Every rollout's
            # sandbox is then created from that snapshot ID - Cube reuses
            # the `templateID` field for snapshots, so a create from a
            # snapshot is the same call as a create from a template.
            self._golden = await self._create()
            self._snapshot = await self.client.snapshot(
                self._golden.sandbox_id, name="",
            )
            log.info("clone strategy: golden %s -> snapshot %s",
                     self._golden, self._snapshot)

    async def stop(self) -> None:
        """Delete every sandbox and snapshot this provider is holding."""
        victims: list[SandboxRef] = []
        if self._pool is not None:
            while not self._pool.empty():
                try:
                    victims.append(self._pool.get_nowait())
                except asyncio.QueueEmpty:
                    break
        if self._golden is not None:
            victims.append(self._golden)
            self._golden = None
        # Pre-cloned children that no rollout ever claimed.
        victims.extend(self._clone_ready)
        self._clone_ready.clear()
        # Anything acquired-but-never-released (a rollout that died mid
        # flight) is tracked in _all.
        victims.extend(self._all)
        self._all.clear()

        seen: set[str] = set()

        async def _kill(ref: SandboxRef) -> None:
            if ref.sandbox_id in seen:
                return
            seen.add(ref.sandbox_id)
            try:
                await self.client.delete(ref.sandbox_id)
            except Exception:
                pass

        # Sandboxes first, snapshots second, and not concurrently: Cube
        # refuses to delete a snapshot that a live sandbox still derives
        # from (clone children carry it as their templateID, and a
        # rolled-back member is bound to it), so deleting in the other
        # order silently leaves the snapshot behind.
        await asyncio.gather(*[_kill(v) for v in victims], return_exceptions=True)

        # Snapshots outlive their sandbox and cost persistent storage, so
        # they have to be deleted explicitly - `with`-style cleanup in the
        # SDK does not do it either.
        stale = list(self._reset_points.values())
        self._reset_points.clear()
        if self._snapshot is not None:
            stale.append(self._snapshot.snapshot_id)
            self._snapshot = None
        await self._delete_snapshots(stale)

    async def _delete_snapshots(self, snapshot_ids: list[str]) -> int:
        """Delete snapshots, reporting what failed. Returns the count freed.

        Worth its own method because a silent failure here is invisible
        and unbounded: an orphaned snapshot is a full filesystem+memory
        checkpoint that nothing reclaims. `reap.py` cannot help - it
        selects sandboxes by the `arl_bench` metadata marker, and
        snapshots carry no metadata and no alias.

        Deletes synchronously so the server's answer means something. The
        fire-and-forget form returns 202 and can still fail afterwards,
        which is fine for latency but useless for verifying cleanup, and
        this only ever runs outside a measured window.
        """
        if not snapshot_ids:
            return 0
        results = await asyncio.gather(
            *[
                self.client.delete_template(s, sync=True)
                for s in snapshot_ids
            ],
            return_exceptions=True,
        )
        freed, failed = 0, []
        for sid, res in zip(snapshot_ids, results):
            if isinstance(res, BaseException):
                failed.append((sid, res))
            else:
                freed += 1
        if failed:
            for sid, err in failed:
                log.warning("snapshot %s NOT deleted: %r", sid, err)
            log.warning(
                "%d of %d snapshot(s) survived teardown. They hold disk for "
                "the rest of the suite; list them with "
                "`curl -s $CUBE_API_URL/templates | grep snap-` and delete with "
                "`curl -X DELETE $CUBE_API_URL/templates/<id>?sync=true`",
                len(failed), len(snapshot_ids),
            )
        return freed

    # ---------------- internals ----------------
    async def _create(self, template_id: Optional[str] = None) -> SandboxRef:
        ref = await self.client.create(
            template_id or self.template_id,
            timeout_sec=self.timeout_sec,
            metadata=dict(BENCH_METADATA),
        )
        await self._ensure_ready(ref)
        return ref

    async def _ensure_ready(self, ref: SandboxRef) -> None:
        """Block until the sandbox can actually accept work.

        Deliberately counted inside `sandbox_create`: the number an RL
        trainer cares about is not "when did the API return" but "when
        could I dispatch the first tool call". Cube promises the *probed*
        port is already serving when create returns - the template
        snapshot was only taken once that probe answered 2xx - so this is
        normally a single sub-millisecond round trip. We keep measuring it
        anyway, because if the guarantee ever slips we want it to show up
        as latency rather than as a burst of first-request failures.
        """
        if self.ready_port is None:
            return
        await self.client.wait_port(
            ref, self.ready_port, self.ready_path,
            timeout_sec=180.0, expect_ok_field=self.ready_expect_ok,
        )

    # ---------------- acquire / release ----------------
    async def acquire(self) -> tuple[SandboxRef, float]:
        """Get a sandbox for one rollout. Returns (ref, create_ms)."""
        t0 = time.perf_counter()
        try:
            if self.strategy == "fresh":
                ref = await self._create()
            elif self.strategy in ("pool", "rollback"):
                assert self._pool is not None
                # Blocking on an empty pool surfaces exhaustion as create
                # latency, which is the honest place for it.
                ref = await self._pool.get()
                if self.strategy == "rollback":
                    # The VM restarted during the reset at release time,
                    # so re-establish readiness before handing it over.
                    await self._ensure_ready(ref)
            else:  # clone
                ref = await self._acquire_clone()
                await self._ensure_ready(ref)
        except Exception:
            _note_create_fail()
            raise
        self._all.append(ref)
        return ref, (time.perf_counter() - t0) * 1000.0

    async def _acquire_clone(self) -> SandboxRef:
        """Take a pre-cloned child, refilling in one concurrent batch.

        The lock serialises only the *refill*, not every rollout. The
        snapshot is reused across refills - re-snapshotting the golden
        sandbox per batch would measure checkpoint cost rather than
        clone-create cost, and the golden sandbox is idle so its state
        does not drift.
        """
        if self._golden is None or self._snapshot is None:
            raise CubeError("clone strategy used before start()")
        while True:
            if self._clone_ready:
                return self._clone_ready.pop()
            async with self._clone_lock:
                # Another waiter may have refilled while we queued.
                if self._clone_ready:
                    continue
                children, _, _ = await self.client.clone(
                    self._golden.sandbox_id,
                    count=self._clone_batch,
                    timeout_sec=self.timeout_sec,
                    metadata=dict(BENCH_METADATA),
                    snapshot=self._snapshot,
                )
                # Readiness is probed per child by the caller, after this
                # returns, so a slow child does not hold the refill lock.
                self._clone_ready.extend(children)

    async def release(self, ref: SandboxRef) -> float:
        """Give the sandbox back. Returns release_ms."""
        t0 = time.perf_counter()
        try:
            if self.strategy == "pool":
                assert self._pool is not None
                self._pool.put_nowait(ref)
            elif self.strategy == "rollback":
                assert self._pool is not None
                reset_to = self._reset_points.get(ref.sandbox_id)
                if reset_to:
                    try:
                        await self.client.rollback(ref.sandbox_id, reset_to)
                    except Exception as e:
                        # A member that cannot be reset is worse than one
                        # fewer member: it would silently serve dirty
                        # state to the next rollout. Drop it.
                        log.warning("rollback of %s failed (%s); retiring it", ref, e)
                        self._reset_points.pop(ref.sandbox_id, None)
                        try:
                            await self.client.delete(ref.sandbox_id)
                        except Exception:
                            pass
                        # The reset point is now unreachable from
                        # _reset_points, so stop() can no longer see it.
                        # Delete it here or it is orphaned for good: a
                        # snapshot survives its sandbox and costs
                        # persistent storage, and nothing in Cube reclaims
                        # it (reap.py only handles sandboxes, and snapshots
                        # carry no metadata to select on).
                        await self._delete_snapshots([reset_to])
                        return (time.perf_counter() - t0) * 1000.0
                self._pool.put_nowait(ref)
            else:  # fresh, clone
                await self.client.delete(ref.sandbox_id)
        finally:
            try:
                self._all.remove(ref)
            except ValueError:
                pass
        return (time.perf_counter() - t0) * 1000.0


class ThinkTimer:
    """Simulated policy-generation delay between environment steps.

    A real rollout spends most of its wall time waiting for the policy to
    emit the next action; the environment is idle. Back-to-back stepping
    (think_time_ms = 0, the default) measures the sandbox capacity
    ceiling, which is what the Docker-era numbers measured too.

    Turning think time on is what exercises the actual pitch of a microVM
    sandbox tier - that an idle environment can release its CPU and
    memory and come back quickly. With `pause=True` we pause the sandbox
    for the idle window, so the comparison becomes "how many concurrent
    rollouts fit in this much RAM when the idle ones are parked", which
    containers cannot do at all.
    """

    def __init__(
        self,
        mean_ms: float = 0.0,
        jitter_ms: float = 0.0,
        pause: bool = False,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.mean_ms = max(0.0, mean_ms)
        self.jitter_ms = max(0.0, jitter_ms)
        self.pause = pause and self.mean_ms > 0
        self.rng = rng or random.Random()

    @property
    def enabled(self) -> bool:
        return self.mean_ms > 0

    def _delay_sec(self) -> float:
        d = self.mean_ms
        if self.jitter_ms:
            d += self.rng.uniform(-self.jitter_ms, self.jitter_ms)
        return max(0.0, d) / 1000.0

    async def wait(self, client: CubeClient, ref: SandboxRef) -> float:
        """Sleep for one think interval. Returns paused overhead in ms."""
        if not self.enabled:
            return 0.0
        delay = self._delay_sec()
        if not self.pause:
            await asyncio.sleep(delay)
            return 0.0
        t0 = time.perf_counter()
        await client.pause(ref.sandbox_id)
        overhead_pause = (time.perf_counter() - t0) * 1000.0
        await asyncio.sleep(delay)
        t1 = time.perf_counter()
        await client.resume(ref.sandbox_id)
        return overhead_pause + (time.perf_counter() - t1) * 1000.0


# ---------------------------------------------------------------------
# Shared bootstrap used by every runner's child-process work factory
# ---------------------------------------------------------------------
async def build_provider(
    setup: dict,
    *,
    concurrency: int,
    ready_port: Optional[int] = None,
    ready_path: str = "/healthz",
    ready_expect_ok: bool = False,
) -> tuple[CubeClient, SandboxProvider, ThinkTimer]:
    """Construct the per-child Cube client, provider and think timer.

    `setup` is the picklable bundle from `Config.cube_setup()` plus a
    `template_id` key added by the runner.
    """
    cube = setup["cube"]
    client = client_from_setup(
        cube,
        # Each in-flight rollout needs at least one control-plane
        # connection (create/delete) plus its data-plane requests.
        max_connections=max(32, concurrency * 4),
    )
    provider = SandboxProvider(
        client,
        setup["template_id"],
        strategy=cube.get("strategy", "fresh"),
        pool_size=int(cube.get("pool_size", 0) or 0),
        concurrency=concurrency,
        timeout_sec=int(cube.get("sandbox_timeout_sec", 3600)),
        ready_port=ready_port,
        ready_path=ready_path,
        ready_expect_ok=ready_expect_ok,
        clone_batch=int(cube.get("clone_batch", 16)),
    )
    await provider.start()
    timer = ThinkTimer(
        mean_ms=float(cube.get("think_time_ms", 0.0)),
        jitter_ms=float(cube.get("think_time_jitter_ms", 0.0)),
        pause=bool(cube.get("think_time_pause", False)),
        rng=random.Random(setup.get("seed_base", 0)),
    )
    return client, provider, timer


def rollout_result(
    *,
    wall_ms: float,
    create_ms: float,
    steps_ms: float,
    release_ms: float,
    idle_overhead_ms: float = 0.0,
    counters: Optional[dict] = None,
    channels: Optional[dict] = None,
) -> dict:
    """Assemble the dict `multiproc.drive_load_mp` expects.

    The three lifecycle phases go into named channels so the parent can
    build a real distribution for each; `main.py` renders them as the
    "rollout lifecycle" section of the report.
    """
    ch = {
        "sandbox_create": create_ms,
        "steps": steps_ms,
        "sandbox_release": release_ms,
    }
    if idle_overhead_ms:
        ch["idle_pause_overhead"] = idle_overhead_ms
    if channels:
        ch.update(channels)

    cnt = dict(counters or {})
    fails = _drain_create_fails()
    if fails:
        cnt["sandbox_create_fails"] = cnt.get("sandbox_create_fails", 0) + fails
    return {
        "wall_ms": wall_ms,
        "counters": cnt,
        "channels": ch,
    }


async def warmup_template(
    cfg,
    template_name: str,
    *,
    port: int,
    path: str = "/healthz",
    expect_ok_field: bool = False,
    label: str = "?",
) -> str:
    """Validate the whole path before any measurement starts.

    Checks CubeAPI is healthy, resolves the template alias, boots one
    sandbox, reaches the service inside it through the configured
    transport and tears it down. A failure here is a configuration
    problem; the alternative is discovering it as `concurrency` identical
    errors per second once the clock is running.

    Returns the resolved template ID.
    """
    client = client_from_config(cfg, max_connections=16)
    try:
        await client.wait_healthy(120.0)
        template_id = await client.resolve_template(template_name)
        log.info("[%s] template %r -> %s (transport: %s)",
                 label, template_name, template_id[:16], client.resolver.describe())

        t0 = time.perf_counter()
        ref = await client.create(template_id, metadata=dict(BENCH_METADATA))
        create_ms = (time.perf_counter() - t0) * 1000.0
        try:
            await client.wait_port(
                ref, port, path, timeout_sec=180.0, expect_ok_field=expect_ok_field,
            )
            log.info("[%s] smoke test ok: sandbox up in %.0f ms, port %d reachable",
                     label, create_ms, port)
        finally:
            await client.delete(ref.sandbox_id)
        return template_id
    finally:
        await client.aclose()
