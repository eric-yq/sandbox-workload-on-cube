#!/usr/bin/env python3
"""Offline self-check: drive the real Cube client against a fake CubeSandbox.

    .venv/bin/python scripts/selfcheck.py

Why this exists
---------------
Everything in `orchestrator/cube.py` is a claim about somebody else's HTTP
API: that create answers 201 with `sandboxID`, that a snapshot ID goes in
the `templateID` field, that delete's 503 carries `Retry-After`, that
CubeProxy routes `/sandbox/<id>/<port>/...`. Those claims were read out of
the CubeSandbox sources, but reading is not the same as exercising, and a
benchmark that silently mis-speaks the API produces numbers rather than
errors.

So this stands up a stdlib fake of CubeAPI, CubeProxy and CubeOps, points
the real client at it, and asserts the wire traffic in both directions:
what we send, and what we do with what comes back. It catches the whole
class of "wrong field name / wrong status code / wrong retry" bugs without
needing a KVM host.

What it does NOT do is validate the fake against the real thing. If the
fake and the client are wrong in the same way, both agree and this passes.
The fake's responses are therefore written to match the shapes in
CubeSandbox's `openapi.yml` and handler sources exactly, and every
endpoint is annotated with where its shape came from.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "orchestrator"))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

from cube import (  # noqa: E402
    BENCH_METADATA,
    CubeClient,
    CubeError,
    EndpointResolver,
    SandboxRef,
)
from rollout import (  # noqa: E402
    SandboxProvider,
    ThinkTimer,
    rollout_result,
)

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  [ok]   {label}")
    else:
        print(f"  [FAIL] {label}{(': ' + detail) if detail else ''}")
        FAILURES.append(label)


def eq(label: str, got, want) -> None:
    check(label, got == want, f"got {got!r}, want {want!r}")


# ---------------------------------------------------------------------
# Fake CubeSandbox
# ---------------------------------------------------------------------
class FakeCube:
    """In-memory CubeAPI + CubeProxy + CubeOps.

    Response shapes follow CubeSandbox's openapi.yml and handler sources:
      * POST /sandboxes            -> 201 Sandbox{templateID,sandboxID,clientID,envdVersion}
      * DELETE /sandboxes/{id}     -> 204, or 503 + Retry-After while locked
      * POST .../pause             -> 204
      * POST .../connect           -> 200 Sandbox
      * POST .../snapshots         -> 201 SnapshotInfo{snapshotID,names}
      * POST .../rollback          -> 200 RollbackResponse
      * GET  /v2/sandboxes         -> 200 [ListedSandbox], cursor in x-next-token
      * GET  /health               -> 200 {status, sandboxes:0}   (0 is hardcoded upstream)
      * POST /templates            -> 202 TemplateBuildJob
      * GET  /templates/{id}       -> 200 TemplateDetail{status,...}
      * GET  /templates/aliases/{a}-> 200 {templateID,...}
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sandboxes: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}
        self.next_id = 0
        # Recorded traffic, for assertions.
        self.requests: list[tuple[str, str, dict]] = []   # (method, path, body)
        self.proxy_hits: list[tuple[str, str, dict]] = []  # (path, host_header, headers)
        # Injectable behaviour.
        self.fail_creates = 0          # next N creates answer 500
        self.delete_503_once: set[str] = set()
        self.page_size = 2             # force pagination in list tests
        self.ready_after_hits = 0      # readiness probe fails this many times first

    # -------- CubeAPI --------
    def api(self, method: str, path: str, body: dict) -> tuple[int, dict, dict]:
        with self.lock:
            self.requests.append((method, path, body))
        m = lambda p: re.fullmatch(p, path)  # noqa: E731

        if path == "/health" and method == "GET":
            return 200, {"status": "ok", "sandboxes": 0}, {}

        if path == "/sandboxes" and method == "POST":
            with self.lock:
                if self.fail_creates > 0:
                    self.fail_creates -= 1
                    return 500, {"message": "injected create failure"}, {}
                self.next_id += 1
                sid = f"sbx{self.next_id:05d}-cafe"
                self.sandboxes[sid] = {
                    "sandboxID": sid,
                    "templateID": body.get("templateID", ""),
                    "clientID": "fake",
                    "envdVersion": "0.0.0",
                    "state": "running",
                    "cpuCount": 2,
                    "memoryMB": 1024,
                    "startedAt": "2026-01-01T00:00:00Z",
                    "metadata": body.get("metadata") or {},
                }
                return 201, dict(self.sandboxes[sid]), {}

        if (g := m(r"/sandboxes/([^/]+)")) and method == "GET":
            sid = g.group(1)
            with self.lock:
                if sid not in self.sandboxes:
                    return 404, {"message": "not found"}, {}
                return 200, dict(self.sandboxes[sid]), {}

        if (g := m(r"/sandboxes/([^/]+)")) and method == "DELETE":
            sid = g.group(1)
            with self.lock:
                if sid in self.delete_503_once:
                    self.delete_503_once.discard(sid)
                    return 503, {"message": "lifecycle op in progress"}, {"Retry-After": "1"}
                self.sandboxes.pop(sid, None)
            return 204, None, {}

        if (g := m(r"/sandboxes/([^/]+)/pause")) and method == "POST":
            with self.lock:
                if g.group(1) in self.sandboxes:
                    self.sandboxes[g.group(1)]["state"] = "paused"
            return 204, None, {}

        if (g := m(r"/sandboxes/([^/]+)/connect")) and method == "POST":
            sid = g.group(1)
            with self.lock:
                if sid not in self.sandboxes:
                    return 404, {"message": "not found"}, {}
                self.sandboxes[sid]["state"] = "running"
                return 200, dict(self.sandboxes[sid]), {}

        if (g := m(r"/sandboxes/([^/]+)/resume")) and method == "POST":
            # Deprecated path; the client only reaches it if connect failed.
            return 201, {"sandboxID": g.group(1), "templateID": "", "clientID": "f",
                         "envdVersion": "0"}, {}

        if (g := m(r"/sandboxes/([^/]+)/snapshots")) and method == "POST":
            sid = g.group(1)
            with self.lock:
                if sid not in self.sandboxes:
                    return 404, {"message": "not found"}, {}
                self.next_id += 1
                snap = f"snap{self.next_id:05d}-beef"
                self.snapshots[snap] = {"origin": sid}
                return 201, {"snapshotID": snap, "names": []}, {}

        if (g := m(r"/sandboxes/([^/]+)/rollback")) and method == "POST":
            sid = g.group(1)
            snap = (body or {}).get("snapshotID")
            with self.lock:
                if sid not in self.sandboxes:
                    return 404, {"message": "sandbox not found"}, {}
                if snap not in self.snapshots:
                    return 404, {"message": "snapshot not found"}, {}
            return 200, {"sandboxID": sid, "snapshotID": snap,
                         "operationID": "op1", "status": "success"}, {}

        if (g := m(r"/sandboxes/([^/]+)/timeout")) and method == "POST":
            return 204, None, {}

        if path.startswith("/v2/sandboxes") and method == "GET":
            # Cube filters metadata client-side inside CubeAPI *after* a
            # limit-bounded fetch, and paginates with the x-next-token
            # response header. Reproduce both so the client's paging is
            # actually exercised.
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(path).query)
            token = (q.get("nextToken") or [""])[0]
            meta_filter = (q.get("metadata") or [""])[0]
            with self.lock:
                ids = sorted(self.sandboxes)
            start = int(token) if token.isdigit() else 0
            page_ids = ids[start:start + self.page_size]
            rows = []
            for sid in page_ids:
                with self.lock:
                    row = dict(self.sandboxes.get(sid) or {})
                if not row:
                    continue
                if meta_filter:
                    meta = row.get("metadata") or {}
                    ok = True
                    for pair in meta_filter.split("&"):
                        if "=" in pair:
                            k, v = pair.split("=", 1)
                            if meta.get(k) != v:
                                ok = False
                    if not ok:
                        continue
                rows.append(row)
            nxt = str(start + self.page_size) if start + self.page_size < len(ids) else ""
            return 200, rows, ({"x-next-token": nxt} if nxt else {})

        if path == "/templates" and method == "GET":
            return 200, [{"templateID": "tpl-arl-b3-id", "aliases": ["arl-b3"],
                          "status": "READY"}], {}

        if path == "/templates" and method == "POST":
            return 202, {"jobID": "job1", "templateID": "tpl-new",
                         "status": "BUILDING", "phase": "pull", "progress": 0}, {}

        if (g := m(r"/templates/aliases/([^/]+)")) and method == "GET":
            if g.group(1) == "arl-b3":
                return 200, {"templateID": "tpl-arl-b3-id", "aliases": ["arl-b3"]}, {}
            return 404, {"message": "alias not found"}, {}

        if (g := m(r"/templates/([^/]+)")) and method == "GET":
            return 200, {"templateID": g.group(1), "status": "READY",
                         "public": True, "replicas": [], "aliases": []}, {}

        if m(r"/templates/([^/]+)") and method == "DELETE":
            return 204, None, {}

        return 404, {"message": f"fake: no route for {method} {path}"}, {}

    # -------- CubeProxy --------
    def proxy(self, path: str, headers: dict) -> tuple[int, dict, dict]:
        with self.lock:
            self.proxy_hits.append((path, headers.get("Host", ""), dict(headers)))
            if self.ready_after_hits > 0:
                self.ready_after_hits -= 1
                return 503, {"ok": False, "why": "not ready yet"}, {}
        return 200, {"ok": True, "path": path}, {}

    # -------- CubeOps --------
    def ops(self, path: str) -> tuple[int, dict, dict]:
        if path.startswith("/internal/v1/nodes"):
            # PascalCase with omitempty, and `mvm_num` / `nic_queues` in
            # snake_case - matching CubeOps' SchedulerNode struct tags.
            with self.lock:
                live = len(self.sandboxes)
            node = {
                "InstanceID": "node-1", "IP": "10.0.0.10",
                "InstanceType": "m8g.metal-48xl",
                "CpuTotal": 192, "MemMBTotal": 786432,
                "QuotaCpu": 192000, "QuotaMem": 786432,
                "QuotaCpuUsage": 2000 * live, "QuotaMemUsage": 1024 * live,
                "MaxMvmLimit": 3000,
                "Healthy": True, "SchedulingDisabled": False,
            }
            if live:
                node["mvm_num"] = live
            return 200, [node], {}
        return 404, {}, {}


def make_handler(fake: FakeCube, role: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _read_body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return {}

        def _dispatch(self, method: str):
            body = self._read_body() if method in ("POST", "PUT", "DELETE") else {}
            if role == "api":
                code, payload, extra = fake.api(method, self.path, body)
            elif role == "proxy":
                code, payload, extra = fake.proxy(self.path, dict(self.headers))
            else:
                code, payload, extra = fake.ops(self.path)
            raw = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(code)
            if raw:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def do_PUT(self):
            self._dispatch("PUT")

    return Handler


def serve(fake: FakeCube, role: str) -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fake, role))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ---------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------
def check_endpoint_resolver() -> None:
    print("\nendpoint resolution (no network):")
    ref = SandboxRef(sandbox_id="sbx1", sandbox_ip="192.168.7.9")

    r = EndpointResolver("http://proxy:80", "path")
    url, hdrs = r.resolve(ref, 8001, "/run")
    eq("path style URL", url, "http://proxy:80/sandbox/sbx1/8001/run")
    eq("path style sends no routing headers", hdrs, {})

    r = EndpointResolver("http://proxy:80", "host", "cube.app")
    url, hdrs = r.resolve(ref, 8003, "/search")
    eq("host style URL", url, "http://proxy:80/search")
    eq("host style Host header", hdrs.get("Host"), "8003-sbx1.cube.app")

    r = EndpointResolver("http://proxy:80", "direct")
    url, hdrs = r.resolve(ref, 8005, "/query")
    eq("direct style URL", url, "http://192.168.7.9:8005/query")

    # Direct mode with no IP must fall back rather than build a broken URL.
    bare = SandboxRef(sandbox_id="sbx2")
    url, _ = r.resolve(bare, 8005, "/query")
    eq("direct falls back to proxy when the IP is unknown",
       url, "http://proxy:80/sandbox/sbx2/8005/query")

    # Traffic token, only present when the deployment restricts traffic.
    tok = SandboxRef(sandbox_id="sbx3", traffic_token="t0k")
    _, hdrs = EndpointResolver("http://proxy:80", "path").resolve(tok, 80, "/")
    check("traffic token sent under both accepted header names",
          hdrs.get("e2b-traffic-access-token") == "t0k"
          and hdrs.get("cube-traffic-access-token") == "t0k",
          str(hdrs))

    # Leading slash is normalised; an unknown style degrades to `path`.
    r = EndpointResolver("http://proxy:80/", "nonsense")
    url, _ = r.resolve(ref, 80, "healthz")
    eq("unknown style degrades to path, and path is normalised",
       url, "http://proxy:80/sandbox/sbx1/80/healthz")


async def check_client(fake: FakeCube, api_port: int, proxy_port: int,
                       ops_port: int) -> None:
    print("\ncontrol plane (against the fake CubeAPI):")
    client = CubeClient(
        api_url=f"http://127.0.0.1:{api_port}",
        api_key="k",
        proxy_url=f"http://127.0.0.1:{proxy_port}",
        transport="path",
        ops_url=f"http://127.0.0.1:{ops_port}",
        sandbox_timeout_sec=1234,
    )
    try:
        check("health", await client.health())

        tid = await client.resolve_template("arl-b3")
        eq("template alias resolves to its tpl- ID", tid, "tpl-arl-b3-id")
        eq("an already-resolved ID is passed through without a lookup",
           await client.resolve_template("tpl-xyz"), "tpl-xyz")
        eq("a snapshot ID is accepted as a template source",
           await client.resolve_template("snap-xyz"), "snap-xyz")

        ref = await client.create(tid, metadata=dict(BENCH_METADATA))
        check("create returns a sandbox id", bool(ref.sandbox_id), repr(ref))

        body = [b for m, p, b in fake.requests if p == "/sandboxes" and m == "POST"][-1]
        eq("create sends templateID", body.get("templateID"), "tpl-arl-b3-id")
        eq("create sends an explicit idle TTL in seconds", body.get("timeout"), 1234)
        eq("create defaults to onTimeout=kill, not a paused tombstone",
           (body.get("lifecycle") or {}).get("onTimeout"), "kill")
        eq("create stamps the reap marker", body.get("metadata"), dict(BENCH_METADATA))

        # Readiness probe through CubeProxy, including a retry.
        fake.ready_after_hits = 2
        await client.wait_port(ref, 8003, "/healthz", timeout_sec=10.0)
        hits = [h for h in fake.proxy_hits if h[0].endswith("/healthz")]
        check("wait_port retries until the service answers", len(hits) >= 3,
              f"{len(hits)} proxy hits")
        eq("wait_port addresses the sandbox by path prefix",
           hits[-1][0], f"/sandbox/{ref.sandbox_id}/8003/healthz")

        # expect_ok_field must reject a 200 whose body says ok=false - this
        # is what makes B5's readiness gate on dbgen rather than on uvicorn
        # merely listening.
        fake.ready_after_hits = 1000  # never becomes ready within the deadline
        raised = False
        try:
            await client.wait_port(ref, 8003, "/healthz", timeout_sec=1.5,
                                   expect_ok_field=True)
        except CubeError:
            raised = True
        fake.ready_after_hits = 0
        check("wait_port surfaces a persistently not-ready service as an error",
              raised)

        print("\nstate primitives:")
        snap = await client.snapshot(ref.sandbox_id)
        check("snapshot returns a snapshotID", snap.snapshot_id.startswith("snap"),
              snap.snapshot_id)

        children, used, snap_ms = await client.clone(
            ref.sandbox_id, count=3, snapshot=snap, metadata=dict(BENCH_METADATA),
        )
        eq("clone produces the requested number of children", len(children), 3)
        eq("clone reuses the snapshot it was handed", used.snapshot_id, snap.snapshot_id)
        eq("reusing a snapshot costs no snapshot call", round(snap_ms), 0)
        creates = [b for m, p, b in fake.requests if p == "/sandboxes" and m == "POST"]
        eq("a clone child is created with the snapshot ID in templateID",
           creates[-1].get("templateID"), snap.snapshot_id)

        await client.rollback(ref.sandbox_id, snap.snapshot_id)
        check("rollback accepted", any(
            p.endswith("/rollback") for _, p, _ in fake.requests))

        await client.pause(ref.sandbox_id)
        await client.resume(ref.sandbox_id)
        paths = [p for _, p, _ in fake.requests]
        check("resume prefers /connect over the deprecated /resume",
              f"/sandboxes/{ref.sandbox_id}/connect" in paths
              and f"/sandboxes/{ref.sandbox_id}/resume" not in paths)

        print("\ndelete, listing and reaping:")
        # Delete must survive the documented transient 503.
        victim = children[0]
        fake.delete_503_once.add(victim.sandbox_id)
        await client.delete(victim.sandbox_id)
        check("delete retries a 503 + Retry-After instead of leaking the sandbox",
              victim.sandbox_id not in fake.sandboxes)

        # A 404 is success: the sandbox is gone, which is all we wanted.
        await client.delete("does-not-exist")
        check("delete treats 404 as already-gone", True)

        listed = await client.list_sandboxes(metadata={"arl_bench": "1"})
        with fake.lock:
            live = len(fake.sandboxes)
        eq("list follows x-next-token across pages", len(listed), live)

        # Reaping must only touch our own sandboxes.
        fake.sandboxes["stranger-1"] = {
            "sandboxID": "stranger-1", "templateID": "other",
            "clientID": "x", "envdVersion": "0", "state": "running",
            "cpuCount": 1, "memoryMB": 1, "startedAt": "now",
            "metadata": {"someone_else": "1"},
        }
        killed = await client.delete_all()
        eq("delete_all reaps only sandboxes carrying the bench marker",
           killed, live)
        check("an unrelated sandbox is left alone",
              "stranger-1" in fake.sandboxes, str(list(fake.sandboxes)))

        print("\nnode observability:")
        fake.sandboxes.pop("stranger-1", None)
        for _ in range(3):
            await client.create(tid, metadata=dict(BENCH_METADATA))
        node = await client.node_metrics()
        eq("node metrics report the live sandbox count", node.get("sandbox_count"), 3)
        eq("node metrics report the sandbox ceiling", node.get("max_sandboxes"), 3000)
        check("node metrics survive omitempty zero fields",
              node.get("allocated_cpu_milli") == 6000, str(node))
        await client.delete_all()
    finally:
        await client.aclose()


async def check_provider(fake: FakeCube, api_port: int, proxy_port: int) -> None:
    print("\nacquisition strategies:")
    setup_base = {
        "api_url": f"http://127.0.0.1:{api_port}",
        "api_key": "",
        "proxy_url": f"http://127.0.0.1:{proxy_port}",
        "transport": "path",
        "sandbox_timeout_sec": 600,
    }

    async def run_strategy(strategy: str, expect_reset: bool) -> None:
        from cube import client_from_setup
        client = client_from_setup(dict(setup_base), max_connections=16)
        provider = SandboxProvider(
            client, "tpl-arl-b3-id", strategy=strategy, concurrency=2,
            pool_size=2, ready_port=8003, clone_batch=2, timeout_sec=600,
        )
        try:
            await provider.start()
            seen: list[str] = []
            for _ in range(4):
                ref, create_ms = await provider.acquire()
                seen.append(ref.sandbox_id)
                check(f"{strategy}: acquire returns a timed sandbox",
                      bool(ref.sandbox_id) and create_ms >= 0.0)
                await provider.release(ref)
            if strategy in ("pool", "rollback"):
                check(f"{strategy}: sandboxes are reused across rollouts",
                      len(set(seen)) <= 2, str(sorted(set(seen))))
            else:
                check(f"{strategy}: every rollout gets its own sandbox",
                      len(set(seen)) == 4, str(sorted(set(seen))))
            if expect_reset and strategy == "rollback":
                rollbacks = [p for _, p, _ in fake.requests if p.endswith("/rollback")]
                check("rollback: state is reset when the sandbox is handed back",
                      len(rollbacks) >= 4, f"{len(rollbacks)} rollbacks")
        finally:
            await provider.stop()
            await client.aclose()

    before = len([p for _, p, _ in fake.requests if p.endswith("/rollback")])
    for strat, reset in (("fresh", False), ("pool", False),
                         ("clone", False), ("rollback", True)):
        with fake.lock:
            fake.requests = [r for r in fake.requests
                             if not r[1].endswith("/rollback")] if strat == "rollback" \
                else fake.requests
        await run_strategy(strat, reset)
    _ = before

    with fake.lock:
        leftover = list(fake.sandboxes)
    check("provider.stop() leaves no sandbox behind", not leftover, str(leftover))
    check("provider.stop() deletes the snapshots it created",
          any(p.startswith("/templates/snap") for _, p, _ in fake.requests),
          "no snapshot delete seen")


async def check_create_failure_accounting(fake: FakeCube, api_port: int,
                                          proxy_port: int) -> None:
    print("\ncreate-failure accounting (Cube exports no counter for this):")
    from cube import client_from_setup
    from runners.base import fold_create_fails

    client = client_from_setup({
        "api_url": f"http://127.0.0.1:{api_port}",
        "proxy_url": f"http://127.0.0.1:{proxy_port}",
        "transport": "path",
        "sandbox_timeout_sec": 600,
    }, max_connections=8)
    provider = SandboxProvider(
        client, "tpl-arl-b3-id", strategy="fresh", concurrency=1,
        ready_port=8003, timeout_sec=600,
    )
    try:
        await provider.start()
        fake.fail_creates = 2
        for _ in range(2):
            try:
                await provider.acquire()
            except CubeError:
                pass
        ref, create_ms = await provider.acquire()
        res = rollout_result(wall_ms=1.0, create_ms=create_ms, steps_ms=0.0,
                             release_ms=await provider.release(ref))
        eq("failed creates ride out on the next rollout's counters",
           res["counters"].get("sandbox_create_fails"), 2)

        mp = {"counters": res["counters"]}
        resource = fold_create_fails({}, mp)
        eq("failures land in the control-plane block",
           resource["control_plane"]["create_fails"], 2)
        check("a level that could not create sandboxes is marked saturated",
              resource["control_plane"]["control_plane_saturated"] is True)

        # And the tally must not double-count on the following rollout.
        ref, create_ms = await provider.acquire()
        res2 = rollout_result(wall_ms=1.0, create_ms=create_ms, steps_ms=0.0,
                              release_ms=await provider.release(ref))
        check("the tally is drained, not re-reported",
              "sandbox_create_fails" not in res2["counters"], str(res2["counters"]))
    finally:
        await provider.stop()
        await client.aclose()


async def check_think_timer(fake: FakeCube, api_port: int) -> None:
    print("\nthink time:")
    from cube import client_from_setup
    client = client_from_setup({"api_url": f"http://127.0.0.1:{api_port}",
                                "sandbox_timeout_sec": 600}, max_connections=4)
    try:
        ref = await client.create("tpl-arl-b3-id")
        t = ThinkTimer(mean_ms=0.0)
        eq("disabled think timer costs nothing", await t.wait(client, ref), 0.0)

        t = ThinkTimer(mean_ms=5.0, pause=True)
        overhead = await t.wait(client, ref)
        check("pausing think timer reports its pause+resume overhead",
              overhead > 0.0, str(overhead))
        paths = [p for _, p, _ in fake.requests]
        check("pausing think timer actually pauses and resumes the sandbox",
              f"/sandboxes/{ref.sandbox_id}/pause" in paths
              and f"/sandboxes/{ref.sandbox_id}/connect" in paths)
        await client.delete(ref.sandbox_id)
    finally:
        await client.aclose()


def check_rollout_result_shape() -> None:
    print("\nrollout result shape:")
    r = rollout_result(wall_ms=100.0, create_ms=10.0, steps_ms=80.0,
                       release_ms=5.0, idle_overhead_ms=2.0,
                       counters={"steps_total": 8}, channels={"q01": 3.0})
    eq("lifecycle phases are named channels",
       sorted(k for k in r["channels"] if k.startswith("sandbox")),
       ["sandbox_create", "sandbox_release"])
    check("idle pause overhead is only reported when non-zero",
          "idle_pause_overhead" in r["channels"])
    check("runner-specific channels are merged in", r["channels"].get("q01") == 3.0)
    r2 = rollout_result(wall_ms=1.0, create_ms=1.0, steps_ms=1.0, release_ms=1.0)
    check("no idle channel when think time is off",
          "idle_pause_overhead" not in r2["channels"])


def check_readyz() -> None:
    """The aggregate readiness gate that gates multi-service snapshots."""
    print("\nreadiness aggregator (workers/arl-sandbox/readyz.py):")
    import importlib.util
    import os

    # Two fake in-sandbox services: one healthy, one reporting ok=false.
    class SvcHandler(BaseHTTPRequestHandler):
        ok = True

        def log_message(self, *a):
            pass

        def do_GET(self):
            raw = json.dumps({"ok": type(self).ok}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    class HealthySvc(SvcHandler):
        ok = True

    class SickSvc(SvcHandler):
        ok = False

    s1 = ThreadingHTTPServer(("127.0.0.1", 0), HealthySvc)
    s2 = ThreadingHTTPServer(("127.0.0.1", 0), SickSvc)
    threading.Thread(target=s1.serve_forever, daemon=True).start()
    threading.Thread(target=s2.serve_forever, daemon=True).start()

    spec = importlib.util.spec_from_file_location(
        "readyz", ROOT / "workers" / "arl-sandbox" / "readyz.py")
    readyz = importlib.util.module_from_spec(spec)
    os.environ["READYZ_SERVICES"] = "b1 b3"
    os.environ["READYZ_CACHE_SEC"] = "0"
    spec.loader.exec_module(readyz)

    # Point its port map at the fakes.
    readyz.PROBES = {"b1": (s1.server_address[1], "/healthz"),
                     "b3": (s2.server_address[1], "/healthz")}
    readyz.SERVICES = ["b1", "b3"]
    readyz.CACHE_SEC = 0.0

    ok, detail = readyz._probe_all()
    check("readyz withholds readiness while any service reports ok=false",
          ok is False, str(detail))
    eq("readyz names the healthy service", detail.get("b1"), "ok")
    eq("readyz names why the sick service failed", detail.get("b3"), "ok=false")

    SickSvc.ok = True
    ok, detail = readyz._probe_all()
    check("readyz reports ready once every service answers", ok is True, str(detail))

    # And a missing service must not read as ready.
    readyz.PROBES["b9"] = (1, "/healthz")
    readyz.SERVICES = ["b1", "b9"]
    ok, detail = readyz._probe_all()
    check("readyz treats an unreachable service as not ready", ok is False, str(detail))

    s1.shutdown()
    s2.shutdown()


def check_no_agentenv_leftovers() -> None:
    print("\nno AgentENV wiring left behind:")
    stale = []
    for path in list((ROOT / "orchestrator").rglob("*.py")) + \
            list((ROOT / "scripts").glob("*")) + \
            list((ROOT / "workers").rglob("*.py")):
        if not path.is_file():
            continue
        # This file names the old symbols in order to look for them.
        if path.resolve() == Path(__file__).resolve():
            continue
        text = path.read_text(errors="ignore")
        # Comparative prose mentioning AgentENV is deliberate; live wiring
        # is not. Look for the things that would actually be executed.
        for pattern in (r"\baenv_setup\b", r"\bAenvClient\b", r"\bAENV_API_URL\b",
                        r"from aenv import", r"aenv_hook", r"ready_cmd\.py",
                        r"ensure-agentenv\.sh", r"x-agentenv-"):
            if re.search(pattern, text):
                stale.append(f"{path.relative_to(ROOT)}: {pattern}")
    check("no live AgentENV references remain", not stale, "; ".join(stale))

    for gone in ("orchestrator/aenv.py", "orchestrator/aenv_hook.py",
                 "scripts/ensure-agentenv.sh", "scripts/ready_cmd.py"):
        check(f"{gone} removed", not (ROOT / gone).exists())


def check_report_render() -> None:
    """Render the HTML report from a synthetic summary.

    Worth checking offline because the report reads result fields by name,
    and the control-plane section was rewritten for Cube's per-component
    accounting. A typo there surfaces only when a four-hour sweep finishes.
    """
    print("\nHTML report:")
    import tempfile
    import main as orch_main

    def run(bid, extra_latency=None, cp=None, **kw):
        d = {
            "benchmark": bid, "concurrency": 128, "duration_sec": 300,
            "throughput": kw.get("throughput", {"rollouts_per_sec": 41.2}),
            "latency_ms": extra_latency or {"p50": 10.0, "p95": 20.0, "p99": 30.0},
            "resource": {"cpu_util_avg": 91.0, "control_plane": cp or {},
                         "disk": kw.get("disk", {})},
            "cost": {"cost_per_1k_units_usd": 0.058},
            "extra": kw.get("extra", {}),
        }
        return d

    healthy_cp = {
        "cube_proxy_cores_p95": 6.4, "cube_api_cores_p95": 1.1,
        "cube_master_cores_p95": 0.9, "cubelet_cores_p95": 3.2,
        "control_plane_cores_p95": 11.6, "control_plane_host_share_p95": 0.06,
        "sandbox_fleet_cores_p95": 158.4, "sandbox_count_max": 128,
        "max_sandboxes": 3000, "create_fails": 0,
        "control_plane_saturated": False,
    }
    saturated_cp = dict(healthy_cp)
    saturated_cp.update({"control_plane_host_share_p95": 0.41,
                         "create_fails": 7, "control_plane_saturated": True})

    summary = {
        "generated_at": "2026-01-01T00:00:00+0000",
        "instance": {"instance_type": "m8g.metal-48xl", "arch": "aarch64",
                     "region": "us-east-1", "vcpus": 192, "physical_cores": 192,
                     "threads_per_core": 1, "smt": False, "cpu_model": "0xd4f",
                     "kernel": "6.8.0-aws"},
        "config": {"cube_strategy": "rollback", "transport": "cubeproxy/path"},
        "benchmarks": {
            "B1": [run("B1", throughput={"executions_per_sec": 828.0}, cp=healthy_cp,
                       extra={"sandbox": {"strategy": "rollback",
                                          "provisioning_share_p50": 0.31}},
                       disk={"devices": ["nvme1n1"], "iops_avg": 877.0,
                             "iops_p95": 1617.0, "read_mbps_avg": 0.0,
                             "write_mbps_avg": 24.6, "await_ms_p95": 1.37,
                             "queue_depth_p95": 2.2, "util_p95": 0.59,
                             "disk_saturated": False, "disk_pressure": False}),
                   run("B1", cp=saturated_cp)],
            "B5": [run("B5", extra_latency={
                "trajectory": {"p50": 1.0, "p99": 2.0},
                "per_query": {"Q01": {"count": 10, "p50": 203.0, "p95": 1.0,
                                      "p99": 1.0, "mean": 1.0, "max": 1.0}},
                "sandbox": {"sandbox_create": {"p50": 62.0, "p99": 210.0},
                            "steps": {"p50": 511.0}, "sandbox_release": {"p50": 35.0}},
            }, cp=healthy_cp, extra={"sandbox": {"strategy": "rollback",
                                                 "provisioning_share_p50": 0.19}})],
            "B8": [run("B8", extra_latency={
                "p50": 62.0, "p99": 210.0,
                "phases": {"create": {"count": 500, "errors": 0, "p50": 62.0,
                                      "p95": 90.0, "p99": 137.0, "mean": 67.0,
                                      "max": 300.0},
                           "clone_per_child": {"count": 60, "errors": 0,
                                               "p50": 70.0, "p95": 90.0,
                                               "p99": 99.0, "mean": 72.0,
                                               "max": 120.0}},
            }, cp=healthy_cp)],
            "B9": [run("B9", extra_latency={
                "rollout": {"p50": 2980.0, "p99": 11200.0},
                "per_task": {"B1": {"count": 683, "p50": 107.0, "p95": 1.0,
                                    "p99": 1.0, "mean": 1.0, "max": 1.0}},
            }, cp=healthy_cp, extra={"task_mix": {"B1": 0.2},
                                     "ops_breakdown": {"B1": 683},
                                     "avg_steps_per_rollout": 20.09,
                                     "sandbox": {"strategy": "rollback",
                                                 "provisioning_share_p50": 0.04}})],
        },
    }

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "report.html"
        orch_main.render_html(summary, out)
        html = out.read_text()

    check("report renders", len(html) > 2000, f"{len(html)} bytes")
    for needle, label in (
        ("on CubeSandbox", "report names the runtime"),
        ("CubeProxy cores", "control-plane table has a CubeProxy column"),
        ("CubeAPI cores", "control-plane table has a CubeAPI column"),
        ("fleet cores", "control-plane table separates the sandbox fleet"),
        ("create fails", "control-plane table shows create failures"),
        ("SATURATED", "a saturated point is flagged"),
        ("rollback", "the acquisition strategy is reported"),
        ("Q01", "B5's per-query table renders"),
        ("clone_per_child", "B8's per-phase table renders"),
        ("per-task latency", "B9's per-task table renders"),
        ("Rollout lifecycle", "the rollout lifecycle section renders"),
        ("nvme1n1", "the disk section renders"),
        ("828.00", "throughput uses the per-benchmark unit"),
    ):
        check(label, needle in html, f"{needle!r} missing")
    check("no unresolved format placeholders", "{" not in html.split("<style>")[0])


async def check_resource_sampler() -> None:
    """The sampler must degrade, not crash, where Cube is not installed.

    This host has no CubeSandbox, so every cgroup lookup misses. That is
    exactly the case where a naive implementation raises inside a
    background task and takes the run down with it, three hours in.
    """
    print("\nresource sampler (no CubeSandbox on this host):")
    from config import CFG
    from metrics import ResourceSampler

    sampler = ResourceSampler(interval=0.2, cfg=CFG)
    sampler.start()
    await asyncio.sleep(0.8)
    sampler.reset()
    await asyncio.sleep(0.5)
    res = await sampler.stop()

    check("sampler returns a resource block", isinstance(res, dict) and res)
    check("host CPU sampled", isinstance(res.get("cpu_util_avg"), float))
    check("host memory sampled", (res.get("mem_peak_gb") or 0) > 0)
    check("orchestrator CPU sampled", "orch_cpu_util_avg" in res)
    check("disk sampled", isinstance(res.get("disk"), dict))
    check("control plane degrades to empty rather than raising",
          isinstance(res.get("control_plane"), dict), str(res.get("control_plane"))[:120])
    eq("host ncpu reported", res.get("host_ncpu"), __import__("os").cpu_count())


async def main() -> int:
    fake = FakeCube()
    api_srv, api_port = serve(fake, "api")
    proxy_srv, proxy_port = serve(fake, "proxy")
    ops_srv, ops_port = serve(fake, "ops")
    print(f"fake CubeAPI :{api_port}  CubeProxy :{proxy_port}  CubeOps :{ops_port}")

    try:
        check_endpoint_resolver()
        await check_client(fake, api_port, proxy_port, ops_port)
        await check_provider(fake, api_port, proxy_port)
        await check_create_failure_accounting(fake, api_port, proxy_port)
        await check_think_timer(fake, api_port)
        check_rollout_result_shape()
        check_readyz()
        check_report_render()
        await check_resource_sampler()
        check_no_agentenv_leftovers()
    finally:
        for s in (api_srv, proxy_srv, ops_srv):
            s.shutdown()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}/{CHECKS} checks failed")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
