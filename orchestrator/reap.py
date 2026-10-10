"""Delete every sandbox this benchmark created, and nothing else.

Used by the run scripts' exit traps, and safe to run by hand:

    .venv/bin/python orchestrator/reap.py

Why it exists as its own entry point: CubeSandbox ships with no
cluster-wide idle timeout (`default_timeout_insec: -1`), so a sandbox
leaked by an interrupted run is not collected by anything. It keeps its
scheduler quota reserved, which silently shifts whatever measurement runs
next - and at c=1024 that is a large amount of quota.

Selection is by the `arl_bench` metadata marker the orchestrator stamps on
every sandbox it creates, so this can never touch an unrelated workload on
the same node. Note that Cube applies the metadata filter *client-side
inside CubeAPI*, after fetching a page bounded by `limit`, so the filtering
here does not rely on the server having narrowed anything - see
`CubeClient.list_sandboxes`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import CFG  # noqa: E402
from cube import client_from_config  # noqa: E402


async def main() -> int:
    client = client_from_config(CFG, max_connections=16)
    try:
        n = await client.delete_all()
        print(f"reaped {n} sandbox(es) tagged arl_bench" if n
              else "no leftover benchmark sandboxes")
        return 0
    except Exception as e:
        print(f"reap failed: {e!r}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
