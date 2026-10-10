"""End-to-end smoke test: one sandbox, one rollout step, teardown.

Run by scripts/setup-host.sh as its last step, and useful on its own when
something looks wrong:

    .venv/bin/python orchestrator/smoke_test.py
    .venv/bin/python orchestrator/smoke_test.py B9
    .venv/bin/python orchestrator/smoke_test.py --all

It exercises the entire path a benchmark depends on - CubeAPI health,
template alias resolution, sandbox create, readiness probe, CubeProxy
routing, a real workload request, delete - so a misconfiguration surfaces
in a few seconds instead of thirty minutes into a sweep. It also prints
the observed create/delete latencies, which is the fastest sanity check on
whether snapshot restore is behaving (expect tens of milliseconds, not
seconds), and exercises snapshot/clone/rollback because those primitives
back the `clone` and `rollback` acquisition strategies.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

import httpx  # noqa: E402

from config import CFG  # noqa: E402
from cube import client_from_config  # noqa: E402
from instance_meta import detect_instance  # noqa: E402
from rollout import (  # noqa: E402
    BENCH_METADATA,
    PORT_B1,
    PORT_B3,
    PORT_B4_BASE,
    PORT_B5,
    PORT_B7,
)

# bid -> (port, path, request body or None for a GET)
CHECKS = {
    "B1": (PORT_B1, "/run", {"code": "print(1)", "timeout": 10}),
    "B3": (PORT_B3, "/search", None),
    "B4": (PORT_B4_BASE, "/healthz", None),
    "B5": (PORT_B5, "/query", {"q": 6}),
    "B7": (PORT_B7, "/episode", {"seed": 1, "steps": 30, "batch": 1}),
    "B8": (PORT_B3, "/healthz", None),
    "B9": (PORT_B3, "/healthz", None),
}


async def check(bid: str) -> bool:
    template = CFG.template_for(bid)
    port, path, body = CHECKS[bid]

    client = client_from_config(CFG, max_connections=8)
    # Short TTL: a smoke-test sandbox that leaks should evict itself
    # quickly rather than hold node quota for an hour. Cube ships with no
    # cluster-wide idle timeout at all, so this has to be explicit.
    client.sandbox_timeout_sec = 120
    ref = None
    try:
        tid = await client.resolve_template(template)
        t0 = time.perf_counter()
        ref = await client.create(tid, metadata=dict(BENCH_METADATA))
        create_ms = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        await client.wait_port(ref, port, "/healthz", timeout_sec=180.0)
        ready_ms = (time.perf_counter() - t0) * 1000.0

        url, headers = client.endpoint(ref, port, path)
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=120.0) as http:
            if body is None:
                r = await http.get(url, headers=headers)
            else:
                r = await http.post(url, headers=headers, json=body)
            r.raise_for_status()
        step_ms = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        await client.delete(ref.sandbox_id)
        ref = None
        delete_ms = (time.perf_counter() - t0) * 1000.0

        print(
            f"  [ok]   {bid:<3} template={template:<8} "
            f"create={create_ms:7.1f}ms ready={ready_ms:6.1f}ms "
            f"step={step_ms:7.1f}ms delete={delete_ms:6.1f}ms"
        )
        return True
    except Exception as e:
        print(f"  [FAIL] {bid:<3} template={template:<8} {type(e).__name__}: {e}")
        return False
    finally:
        if ref is not None:
            try:
                await client.delete(ref.sandbox_id)
            except Exception:
                pass
        await client.aclose()


async def check_state_primitives(bid: str = "B3") -> bool:
    """snapshot -> clone -> rollback, the basis of two strategies.

    Worth its own check because these are the calls that have no AgentENV
    or container equivalent, so a deployment can be perfectly healthy for
    `fresh` while `clone` or `rollback` fails - and that failure would
    otherwise surface only once a sweep is already running.
    """
    client = client_from_config(CFG, max_connections=8)
    client.sandbox_timeout_sec = 300
    parent = None
    snap = None
    children: list = []
    try:
        tid = await client.resolve_template(CFG.template_for(bid))
        parent = await client.create(tid, metadata=dict(BENCH_METADATA))
        await client.wait_port(parent, CHECKS[bid][0], "/healthz", timeout_sec=180.0)

        # Cube defers the soft-dirty page-table reset into a sandbox's
        # first snapshot, so take one throwaway before timing.
        warm = await client.snapshot(parent.sandbox_id)
        t0 = time.perf_counter()
        snap = await client.snapshot(parent.sandbox_id)
        snap_ms = (time.perf_counter() - t0) * 1000.0
        await client.delete_snapshot(warm.snapshot_id)

        t0 = time.perf_counter()
        children, _, _ = await client.clone(
            parent.sandbox_id, count=2,
            metadata=dict(BENCH_METADATA), snapshot=snap,
        )
        clone_ms = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        await client.rollback(parent.sandbox_id, snap.snapshot_id)
        rollback_ms = (time.perf_counter() - t0) * 1000.0

        print(
            f"  [ok]   state  snapshot={snap_ms:7.1f}ms "
            f"clone(2)={clone_ms:7.1f}ms ({clone_ms / 2:.1f}ms/child) "
            f"rollback={rollback_ms:7.1f}ms"
        )
        return True
    except Exception as e:
        print(f"  [FAIL] state  {type(e).__name__}: {e}")
        return False
    finally:
        # Order matters, and getting it wrong is how this check used to
        # leak a snapshot on every run: Cube refuses to delete a snapshot
        # that a live sandbox still derives from, and `parent` has been
        # rolled back *onto* `snap` by the time we get here. So every
        # dependent - the clone children and the parent - has to go first.
        for c in children:
            try:
                await client.delete(c.sandbox_id)
            except Exception as e:
                print(f"  [warn] leaked child {c.sandbox_id}: {e!r}")
        if parent is not None:
            try:
                await client.delete(parent.sandbox_id)
            except Exception as e:
                print(f"  [warn] leaked parent {parent.sandbox_id}: {e!r}")
        if snap is not None:
            try:
                # sync=true so a refusal surfaces here rather than being
                # accepted as 202 and failing silently afterwards.
                await client.delete_template(snap.snapshot_id, sync=True)
            except Exception as e:
                print(
                    f"  [warn] leaked snapshot {snap.snapshot_id}: {e!r}\n"
                    f"         delete it with: curl -X DELETE "
                    f"{CFG.cube_api_url}/templates/{snap.snapshot_id}?sync=true"
                )
        await client.aclose()


async def main() -> int:
    args = [a for a in sys.argv[1:]]
    if "--all" in args:
        which = list(CHECKS)
        args = [a for a in args if a != "--all"]
    else:
        which = [a.upper() for a in args] or ["B3"]
    for bid in which:
        if bid not in CHECKS:
            print(f"unknown benchmark id: {bid} (known: {', '.join(CHECKS)})")
            return 2

    instance = await detect_instance()
    print(
        f"host: {instance['instance_type']} {instance['arch']} "
        f"kernel={instance['kernel']} "
        f"{instance.get('vcpus')} vCPU / {instance.get('physical_cores')} cores "
        f"(smt={instance.get('smt')})"
    )
    print(f"cube: api={CFG.cube_api_url} proxy={CFG.cube_proxy_url} "
          f"transport={CFG.transport_label()}")

    probe = client_from_config(CFG, max_connections=4)
    try:
        await probe.wait_healthy(30.0)
        node = await probe.node_metrics()
        if node:
            print(f"node: {node.get('node_id')} type={node.get('instance_type')} "
                  f"cpus={node.get('cpu_total')} "
                  f"mem={node.get('mem_mb_total')}MB "
                  f"sandboxes={node.get('sandbox_count')}/{node.get('max_sandboxes')} "
                  f"healthy={node.get('healthy')}")
        else:
            # Not fatal: only the node-level columns of the report depend
            # on it, and it is a different service with its own failure
            # modes. Say so rather than letting the gap go unexplained.
            print(f"node: CubeOps at {CFG.cube_ops_url} did not answer; "
                  f"node-level metrics will be missing from results")
    except Exception as e:
        print(f"CubeAPI is not reachable at {CFG.cube_api_url}: {e}")
        return 1
    finally:
        await probe.aclose()

    print("checks:")
    results = [await check(bid) for bid in which]
    results.append(await check_state_primitives(which[0]))

    if all(results):
        print("smoke test passed.")
        return 0
    print("smoke test FAILED. Check `cubemastercli tpl list` and "
          "`journalctl -u cube-sandbox-cube-api -n 80`.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
