"""B8 - microVM lifecycle latency.

This sub-project measures the sandbox *supply* primitives themselves
rather than any workload, because in a per-rollout-microVM model the
supply path is on the critical path of every rollout. B1/B3/B5/B7/B9 each
report what fraction of their rollout wall time went to provisioning;
this one explains why that fraction is what it is.

What is measured, and why each one matters to an RL trainer:

``create``
    Create a sandbox from a template. This is a snapshot restore, not a
    boot, and it is the number that decides how fast a trainer can hand a
    fresh environment to a rollout. Cube advertises sub-60ms at low
    concurrency and sub-150ms p99 at 50 concurrent creates.
``pause`` / ``resume``
    Park an idle environment and bring it back. No container equivalent -
    this is what makes an environment that is waiting on policy
    generation cheap rather than merely idle-but-resident.
``snapshot``
    Checkpoint a running sandbox's memory and filesystem. Implemented
    with XFS reflink (FICLONE) plus soft-dirty incremental memory
    capture, so it should be roughly O(dirty pages) rather than O(image).
    Note the *first* snapshot of a sandbox is materially more expensive
    than later ones because Cube defers the soft-dirty reset into it, so
    this phase warms up before recording.
``clone_fanout`` / ``clone_per_child``
    Derive N children that all inherit one live state. This is the GRPO
    primitive: sample G candidate rollouts from an identical starting
    point. Cube has no batched fan-out call - `clone` is one snapshot plus
    N independent creates - so `clone_per_child` is the number that says
    whether cloning is actually cheaper than creating from a template, and
    it is expected to converge on `create` as N grows rather than beating
    it the way a single batched fork would.
``rollback``
    Reset a running sandbox to a snapshot in place, keeping its ID. The
    primitive behind the `rollback` acquisition strategy: it makes a
    reusable sandbox pool state-isolated, which is the one way to get
    pool-like throughput without leaking state between rollouts.

There is no cold-start phase. Cube has no per-sandbox equivalent of
AgentENV's `POST /sandboxes-cold`: converting an OCI image into a
bootable rootfs happens once, at template build time, so the cold path is
`scripts/provision-templates.sh` and not something a rollout can pay for.

Unlike the other benchmarks this one is trial-driven rather than
time-driven - `B8_TRIALS` samples per phase at the given concurrency -
because we want a fixed-N latency distribution, not a throughput figure.
"""

from __future__ import annotations

import asyncio
import logging
import time

from config import Config
from cube import CubeError, client_from_config
from metrics import LatencySink, ResourceSampler
from rollout import BENCH_METADATA, PORT_B3, warmup_template
from .base import BenchmarkResult, Runner, hourly_price

log = logging.getLogger(__name__)


async def _timed(coro) -> float:
    t0 = time.perf_counter()
    await coro
    return (time.perf_counter() - t0) * 1000.0


class B8Runner(Runner):
    name = "B8"
    workload = "sandboxlife"

    def __init__(self) -> None:
        self._template_id = ""

    async def warmup(self, cfg: Config) -> None:
        # B8's template runs one cheap service, so readiness is a
        # meaningful signal without adding startup cost to every trial -
        # and being single-service it is probed on its own port, with no
        # readiness aggregator in the sandbox.
        self._template_id = await warmup_template(
            cfg, cfg.template_b8, port=PORT_B3, label=self.name,
        )

    async def run_one(self, cfg: Config, instance: dict, concurrency: int) -> BenchmarkResult:
        template = self._template_id or cfg.template_b8
        trials = max(1, cfg.b8_trials)
        client = client_from_config(cfg, max_connections=max(32, concurrency * 4))

        sinks = {
            "create": LatencySink(),
            "delete": LatencySink(),
            "pause": LatencySink(),
            "resume": LatencySink(),
            "snapshot": LatencySink(),
            "clone_fanout": LatencySink(),
            "clone_per_child": LatencySink(),
            "rollback": LatencySink(),
        }

        sampler = ResourceSampler(cfg=cfg)
        sampler.start()
        sem = asyncio.Semaphore(concurrency)
        counters = {"clones": 0, "clone_children": 0, "rollbacks": 0}

        async def lifecycle_trial() -> None:
            """create -> pause -> resume -> delete on one sandbox."""
            async with sem:
                ref = None
                try:
                    t0 = time.perf_counter()
                    ref = await client.create(
                        template, metadata=dict(BENCH_METADATA),
                    )
                    sinks["create"].record((time.perf_counter() - t0) * 1000.0)
                except Exception as e:
                    sinks["create"].fail()
                    log.debug("create failed: %s", e)
                    return
                try:
                    sinks["pause"].record(await _timed(client.pause(ref.sandbox_id)))
                except Exception:
                    sinks["pause"].fail()
                try:
                    sinks["resume"].record(await _timed(client.resume(ref.sandbox_id)))
                except Exception:
                    sinks["resume"].fail()
                try:
                    sinks["delete"].record(await _timed(client.delete(ref.sandbox_id)))
                except Exception:
                    sinks["delete"].fail()

        await asyncio.gather(
            *[lifecycle_trial() for _ in range(trials)], return_exceptions=True
        )

        # ---- snapshot / clone / rollback phase ----
        # All three are parent-scoped: they act on one running sandbox, so
        # the meaningful measurement is a single golden parent exercised
        # repeatedly, not `concurrency` independent parents.
        clone_count = max(1, cfg.b8_clone_count)
        clone_trials = max(1, trials // clone_count)
        golden = None
        try:
            golden = await client.create(template, metadata=dict(BENCH_METADATA))

            # Warm up the snapshot path before recording. Cube defers the
            # soft-dirty page-table reset into the first snapshot of a
            # sandbox, so recording it would put a several-hundred-
            # millisecond outlier at the front of every distribution and
            # misrepresent the steady-state cost.
            warm_snap = None
            try:
                warm_snap = await client.snapshot(golden.sandbox_id)
            except Exception as e:
                log.warning("[B8] snapshot warmup failed: %s", e)

            # Every snapshot taken below, until we can prove it is gone.
            # This phase is the heaviest snapshot producer in the suite -
            # one per iteration, each a full filesystem+memory checkpoint -
            # and Cube reclaims none of them, so losing track of one costs
            # disk for the rest of the sweep.
            pending_snaps: list[str] = []

            async def _drop_snaps(ids: list[str]) -> list[str]:
                """Delete snapshots; return the ones that refused to go.

                Cube will not delete a snapshot that a live sandbox still
                derives from, and `golden` gets rolled back *onto* each
                snapshot here - so an in-loop delete is expected to fail
                and has to be retried once `golden` is gone. `sync=True`
                so the answer is authoritative instead of a 202 that fails
                later.
                """
                if not ids:
                    return []
                res = await asyncio.gather(
                    *[client.delete_template(s, sync=True) for s in ids],
                    return_exceptions=True,
                )
                return [
                    s for s, r in zip(ids, res) if isinstance(r, BaseException)
                ]

            for _ in range(clone_trials):
                snap = None
                try:
                    t0 = time.perf_counter()
                    snap = await client.snapshot(golden.sandbox_id)
                    sinks["snapshot"].record((time.perf_counter() - t0) * 1000.0)
                except Exception as e:
                    sinks["snapshot"].fail()
                    log.debug("snapshot failed: %s", e)
                    continue

                # Fan-out from that snapshot. Timed separately from the
                # snapshot so the two costs stay distinguishable - the
                # whole point of this phase is that Cube splits what
                # AgentENV did in one call into two very different halves.
                try:
                    t0 = time.perf_counter()
                    children, _, _ = await client.clone(
                        golden.sandbox_id, count=clone_count,
                        metadata=dict(BENCH_METADATA), snapshot=snap,
                    )
                    fanout_ms = (time.perf_counter() - t0) * 1000.0
                    sinks["clone_fanout"].record(fanout_ms)
                    counters["clones"] += 1
                    counters["clone_children"] += len(children)
                    if children:
                        sinks["clone_per_child"].record(fanout_ms / len(children))
                    await asyncio.gather(
                        *[client.delete(c.sandbox_id) for c in children],
                        return_exceptions=True,
                    )
                except Exception as e:
                    sinks["clone_fanout"].fail()
                    log.debug("clone failed: %s", e)

                # Rollback the parent to the snapshot we just took. In
                # place, so the golden sandbox keeps its ID and the next
                # iteration starts from a known state.
                try:
                    sinks["rollback"].record(
                        await _timed(client.rollback(golden.sandbox_id, snap.snapshot_id))
                    )
                    counters["rollbacks"] += 1
                except Exception as e:
                    sinks["rollback"].fail()
                    log.debug("rollback failed: %s", e)

                # Snapshots are billed storage and outlive their sandbox,
                # so each one has to be deleted explicitly. Deliberately
                # not timed: it is not on any rollout's critical path.
                # Try now to keep peak disk down - it succeeds for any
                # snapshot `golden` is no longer bound to - and carry the
                # rest to the retry after `golden` is deleted.
                pending_snaps.append(snap.snapshot_id)
                pending_snaps = await _drop_snaps(pending_snaps)

            if warm_snap is not None:
                pending_snaps.append(warm_snap.snapshot_id)
                warm_snap = None
        except CubeError as e:
            log.warning("[B8] snapshot/clone phase skipped: %s", e)
        finally:
            # golden holds a reference to whichever snapshot it was last
            # rolled back onto, so it has to go before the retry.
            if golden is not None:
                try:
                    await client.delete(golden.sandbox_id)
                except Exception as e:
                    log.warning("[B8] golden sandbox %s leaked: %r",
                                golden.sandbox_id, e)
            leaked = await _drop_snaps(pending_snaps)
            if leaked:
                log.warning(
                    "[B8] %d snapshot(s) could not be deleted and will hold "
                    "disk for the rest of the suite: %s. Remove with: "
                    "curl -X DELETE %s/templates/<id>?sync=true",
                    len(leaked), ", ".join(leaked), cfg.cube_api_url,
                )

        res = await sampler.stop()
        # This runner drives the control plane directly rather than through
        # the load generator, so it folds its own create failures in.
        cp = res.setdefault("control_plane", {})
        cp["create_fails"] = int(cp.get("create_fails", 0) or 0) + sinks["create"].errors
        if cp["create_fails"] > 0:
            cp["control_plane_saturated"] = True

        # Reap anything a failed trial left behind before the next level.
        try:
            leaked = await client.delete_all()
            if leaked:
                log.info("[B8] reaped %d leaked sandboxes", leaked)
        finally:
            await client.aclose()

        created = len(sinks["create"].samples)
        wall = sum(sinks["create"].samples) / 1000.0 if created else 0.0
        # Effective creates/sec at this concurrency, derived from the
        # measured p50 rather than a wall clock we did not fence.
        p50_create = sinks["create"].summary().get("p50") or 0.0
        creates_per_sec = (concurrency / (p50_create / 1000.0)) if p50_create else 0.0

        return BenchmarkResult(
            benchmark=self.name,
            instance=instance,
            concurrency=concurrency,
            duration_sec=wall,
            throughput={
                "sandbox_creates_per_sec": creates_per_sec,
                "total_creates": created,
                "total_clones": counters["clones"],
                "total_clone_children": counters["clone_children"],
                "total_rollbacks": counters["rollbacks"],
            },
            latency_ms={
                # `create` is the headline: time to hand a rollout a fresh
                # environment. Kept at the top level so the report's
                # generic p99 extraction picks it up.
                **sinks["create"].summary(),
                "phases": {k: v.summary() for k, v in sinks.items()},
            },
            resource=res,
            cost={
                "instance_hourly_usd": hourly_price(instance["instance_type"], cfg),
                # A lifecycle op is not a unit of useful work, so a
                # cost-per-op figure here would be meaningless. The cost
                # of provisioning shows up where it belongs: inside the
                # `sandbox_create` share of every other benchmark.
                "cost_per_1k_units_usd": None,
                "cost_per_1m_units_usd": None,
            },
            extra={
                "trials": trials,
                "clone_children_per_call": clone_count,
                "clone_batches": clone_trials,
                "template": template,
                "transport": cfg.transport_label(),
                "phases_measured": sorted(sinks.keys()),
                # Recorded so a reader does not have to know the runtime's
                # API shape to interpret clone_per_child.
                "clone_is_server_side_fanout": False,
                "cold_start_measured": False,
            },
        )
