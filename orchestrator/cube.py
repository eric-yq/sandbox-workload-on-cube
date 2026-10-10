"""CubeSandbox control-plane client.

CubeSandbox (TencentCloud/CubeSandbox) exposes an E2B-compatible HTTP API
served by CubeAPI. We talk to it directly with httpx rather than through
the `cubesandbox` / `e2b` SDK because the benchmark needs precise control
over connection pooling and per-call timing, and because the SDKs add
their own retry behaviour that would smear the latency distribution we
are trying to measure. The SDK is also synchronous in the places that
matter (`Sandbox.clone` fans out with a ThreadPoolExecutor), which caps
the concurrency this suite has to drive.

Four properties of this API shape drive the design here:

1. **The data plane is a separate process on a separate port.** CubeAPI
   (:3000) handles the control plane; CubeProxy (:80) routes traffic into
   sandboxes. Unlike the AgentENV topology, control-plane calls and
   in-sandbox requests do not contend for one process - see
   `EndpointResolver`.

2. **There are three ways to address a port inside a sandbox**, all
   implemented by CubeProxy: a path prefix, a Host header, and - because
   CubeProxy resolves same-host callers to the sandbox's own IP - a
   direct dial that takes the proxy off the path entirely. The sandbox IP
   is not in any API response; it lives in the Redis routing map that
   CubeMaster writes and CubeProxy reads. See `resolve_sandbox_ip`.

3. **There is no `fork`.** AgentENV could clone a running sandbox into N
   children in one call. Cube's equivalent is two steps: snapshot the
   parent (`POST /sandboxes/{id}/snapshots`), then create N sandboxes
   from the returned snapshot ID - snapshots and templates share one ID
   namespace, so the snapshot ID goes in the `templateID` field. Cube
   adds `rollback`, which AgentENV had no equivalent for: reset a
   sandbox to a snapshot in place, keeping its ID.

4. **Neither `/health` nor any metric reports live sandbox counts or
   create failures.** `GET /health` returns `sandboxes: 0` hardcoded
   (CubeAPI/src/handlers/health.rs). Node inventory lives in a different
   service (CubeOps, :3010) with different auth, and no component
   exports a create-success/failure counter - so this suite counts
   create failures itself (see `SandboxProvider` in rollout.py).

Reference: https://github.com/TencentCloud/CubeSandbox (v0.7.x)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Wire constants, all verified against the CubeSandbox sources
# ---------------------------------------------------------------------
# CubeProxy enforces a per-sandbox traffic token only when the sandbox is
# marked non-public (`AllowPublicTraffic == "false"`, i.e. created with
# `secure: true`). It accepts either spelling; we send both so the same
# code works whether or not the deployment restricts traffic.
# (CubeProxy/lua/sandbox_backend.lua: enforce_traffic_token)
HDR_TRAFFIC_TOKEN_E2B = "e2b-traffic-access-token"
HDR_TRAFFIC_TOKEN_CUBE = "cube-traffic-access-token"

# Redis hash written by CubeMaster and read by CubeProxy to route traffic.
# Fields we care about: SandboxIP (guest IP, dialable from the host
# network namespace), HostIP, and one entry per exposed container port
# mapping it to a host port for cross-node callers.
# (CubeProxy/lua/redis_keys.lua)
REDIS_KEY_SANDBOX_PROXY = "cube:v1:shared:sandbox:proxy:{sid}"
REDIS_KEY_SANDBOX_PROXY_LEGACY = "bypass_host_proxy:{sid}"

# Marker written into every sandbox this suite creates so teardown can
# reap what it leaked without touching unrelated sandboxes.
BENCH_METADATA_KEY = "arl_bench"
BENCH_METADATA = {BENCH_METADATA_KEY: "1"}

# CubeAPI route timeout budgets (CubeAPI/src/routes.rs). Ours must be
# larger or we would time out client-side before the server answers and
# then double-count the operation.
CTL_TIMEOUT_DEFAULT = 40.0      # server: 30s
CTL_TIMEOUT_LIFECYCLE = 150.0   # server: 120s (pause / resume / connect)
CTL_TIMEOUT_SNAPSHOT = 300.0    # server: 240s (snapshot create, template delete)

# `DELETE /sandboxes/{id}` answers 503 + Retry-After while another
# lifecycle operation holds the sandbox lock, and 409 when a paused
# sandbox cannot be admitted. Both are explicitly retriable, and at high
# concurrency they are normal rather than exceptional - so retry instead
# of leaking the sandbox (a leaked sandbox holds its scheduler quota and
# would skew the next measurement point).
DELETE_MAX_ATTEMPTS = 4
DELETE_RETRY_CAP_SEC = 5.0


class CubeError(RuntimeError):
    """Control-plane call failed."""


class TemplateNotLocalError(CubeError):
    """The template exists in the DB but is not materialised on the node.

    Its own type because it is the one control-plane failure with a
    specific, known remedy (re-provision the templates) and because it is
    indistinguishable from a healthy deployment until you try to create:
    `GET /templates` still reports READY. See the message raised in
    `create()` for why.
    """


@dataclass
class SandboxRef:
    """A live sandbox plus everything needed to reach services inside it."""

    sandbox_id: str
    template_id: str = ""
    # Only populated in direct-IP mode (looked up from the Redis routing map).
    sandbox_ip: Optional[str] = None
    # Only set when the deployment restricts public traffic.
    traffic_token: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)

    def __repr__(self) -> str:  # keep log lines short at c=1024
        return f"<Sandbox {self.sandbox_id[:12]}>"


@dataclass
class SnapshotRef:
    """A snapshot. Doubles as a template ID when creating sandboxes."""

    snapshot_id: str
    names: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"<Snapshot {self.snapshot_id[:12]}>"


# ---------------------------------------------------------------------
# Endpoint resolution
# ---------------------------------------------------------------------
class EndpointResolver:
    """Turns (sandbox, port, path) into (url, headers) for httpx.

    Three transports, all first-class in CubeProxy:

    * ``path`` (default) - ``{proxy}/sandbox/<sandbox-id>/<port><path>``.
      CubeProxy strips the prefix before forwarding
      (CubeProxy/lua/path_rewrite_phase.lua). Chosen as the default
      because it needs no Host rewriting and no DNS, so it works against
      a bare IP on a freshly installed node.
    * ``host`` - ``{proxy}{path}`` with ``Host: <port>-<sandbox-id>.<domain>``.
      This is the E2B-compatible form and what `getHost()` hands to SDK
      users. CubeProxy only regex-matches the leading
      ``<digits>-<id>`` of the Host header
      (CubeProxy/lua/rewrite_phase.lua), so the domain suffix is
      cosmetic and no DNS record has to exist.
    * ``direct`` - ``http://<sandbox-ip>:<port><path>``, no proxy at all.
      Legitimate rather than a hack: CubeProxy itself resolves a
      same-host caller to exactly this address. It removes the shared
      OpenResty worker pool from the measured path, which matters at high
      concurrency, but it also bypasses the auto-resume gate, so a paused
      sandbox will refuse the connection instead of being woken.
    """

    def __init__(
        self,
        proxy_url: str,
        style: str = "path",
        sandbox_domain: str = "cube.app",
    ) -> None:
        self.proxy_url = (proxy_url or "").rstrip("/")
        self.style = style if style in ("path", "host", "direct") else "path"
        self.sandbox_domain = sandbox_domain or "cube.app"

    def resolve(self, ref: SandboxRef, port: int, path: str) -> tuple[str, dict[str, str]]:
        path = path if path.startswith("/") else f"/{path}"
        headers: dict[str, str] = {}
        if ref.traffic_token:
            headers[HDR_TRAFFIC_TOKEN_E2B] = ref.traffic_token
            headers[HDR_TRAFFIC_TOKEN_CUBE] = ref.traffic_token

        if self.style == "direct" and ref.sandbox_ip:
            return f"http://{ref.sandbox_ip}:{port}{path}", headers
        if self.style == "host":
            headers["Host"] = f"{port}-{ref.sandbox_id}.{self.sandbox_domain}"
            return f"{self.proxy_url}{path}", headers
        # `path` style, and the fallback when direct mode has no IP.
        return f"{self.proxy_url}/sandbox/{ref.sandbox_id}/{port}{path}", headers

    def describe(self) -> str:
        if self.style == "direct":
            return "direct-ip (proxy bypassed)"
        return f"cubeproxy/{self.style}"


# ---------------------------------------------------------------------
# Direct-IP lookup (Redis routing map)
# ---------------------------------------------------------------------
class SandboxIpResolver:
    """Reads the sandbox IP out of CubeMaster's Redis routing map.

    Cube's sandbox API returns no routable address - `Sandbox` and
    `SandboxDetail` carry `sandboxID`, `domain` and the access tokens,
    nothing dialable. But CubeProxy has to know where to send traffic, so
    CubeMaster publishes a per-sandbox hash containing `SandboxIP`, and
    CubeProxy dials exactly that when the caller is on the same host as
    the sandbox. Reading the same hash is therefore not a workaround - it
    is the same source of truth the proxy uses.

    Optional: only constructed when the transport is `direct`. A missing
    or unreachable Redis degrades to proxy routing rather than failing.
    """

    def __init__(
        self,
        host: str,
        port: int = 6379,
        password: str = "",
        db: int = 0,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.db = db
        self._redis: Any = None
        self._unavailable = False
        self._warned = False

    async def _client(self):
        if self._redis is not None or self._unavailable:
            return self._redis
        try:
            import redis.asyncio as aioredis  # lazy: only needed in direct mode
        except ImportError:
            self._unavailable = True
            log.warning(
                "CUBE_TRANSPORT=direct needs the `redis` package to read the "
                "sandbox routing map; falling back to CubeProxy. "
                "Install it with: .venv/bin/pip install redis"
            )
            return None
        try:
            self._redis = aioredis.Redis(
                host=self.host, port=self.port, db=self.db,
                password=self.password or None,
                decode_responses=True,
                socket_timeout=2.0, socket_connect_timeout=2.0,
                max_connections=8,
            )
            await self._redis.ping()
        except Exception as e:
            self._unavailable = True
            self._redis = None
            log.warning(
                "cannot reach the CubeSandbox Redis at %s:%d (%r); direct-IP "
                "transport falls back to CubeProxy. Check CUBE_REDIS_HOST / "
                "CUBE_REDIS_PASSWORD against deploy/one-click .env "
                "(CUBE_SANDBOX_REDIS_PASSWORD).",
                self.host, self.port, e,
            )
        return self._redis

    async def lookup(self, sandbox_id: str) -> Optional[str]:
        red = await self._client()
        if red is None:
            return None
        for key in (
            REDIS_KEY_SANDBOX_PROXY.format(sid=sandbox_id),
            REDIS_KEY_SANDBOX_PROXY_LEGACY.format(sid=sandbox_id),
        ):
            try:
                data = await red.hgetall(key)
            except Exception:
                return None
            if data:
                ip = data.get("SandboxIP") or data.get("sandbox_ip")
                if ip:
                    return ip
        if not self._warned:
            self._warned = True
            log.warning(
                "no SandboxIP in the routing map for %s; this sandbox will use "
                "CubeProxy instead. (Route keys checked: %s)",
                sandbox_id[:12], REDIS_KEY_SANDBOX_PROXY.format(sid="<id>"),
            )
        return None

    async def aclose(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass


# ---------------------------------------------------------------------
# Control-plane client
# ---------------------------------------------------------------------
class CubeClient:
    """Thin async wrapper over the CubeSandbox sandbox / template APIs.

    One instance per process (the load generator runs in several spawned
    child processes, each of which builds its own). `aclose()` must be
    called or httpx will complain about an unclosed client.
    """

    def __init__(
        self,
        api_url: str,
        api_key: str = "",
        *,
        proxy_url: str = "",
        transport: str = "path",
        sandbox_domain: str = "cube.app",
        ops_url: str = "",
        redis_host: str = "",
        redis_port: int = 6379,
        redis_password: str = "",
        sandbox_timeout_sec: int = 1800,
        max_connections: int = 256,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.ops_url = (ops_url or "").rstrip("/")
        self.sandbox_timeout_sec = sandbox_timeout_sec
        self.resolver = EndpointResolver(
            proxy_url or self.api_url, transport, sandbox_domain,
        )

        # CubeAPI accepts either `Authorization: Bearer` (preferred by the
        # middleware) or `X-API-Key`, and with neither `CUBE_API_KEY` nor
        # `AUTH_CALLBACK_URL` configured it runs unauthenticated - which
        # is the one-click default. Sending both spellings costs nothing
        # and works in all three modes.
        # (CubeAPI/src/middleware/auth.rs: extract_credential)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key

        self._ctl = httpx.AsyncClient(
            base_url=self.api_url,
            headers=headers,
            timeout=httpx.Timeout(CTL_TIMEOUT_DEFAULT, connect=10.0),
            limits=httpx.Limits(max_connections=max_connections),
        )
        # Separate client for CubeOps: different port, different auth, and
        # a stalled node query must never block a create or delete.
        self._ops: Optional[httpx.AsyncClient] = None
        if self.ops_url:
            self._ops = httpx.AsyncClient(
                base_url=self.ops_url,
                timeout=httpx.Timeout(8.0, connect=2.0),
                limits=httpx.Limits(max_connections=4),
            )

        self._ip_resolver: Optional[SandboxIpResolver] = None
        if transport == "direct" and redis_host:
            self._ip_resolver = SandboxIpResolver(
                redis_host, redis_port, redis_password,
            )

        self._warned_no_node = False

    # ---------------- lifecycle ----------------
    async def aclose(self) -> None:
        await self._ctl.aclose()
        if self._ops is not None:
            await self._ops.aclose()
        if self._ip_resolver is not None:
            await self._ip_resolver.aclose()

    async def health(self) -> bool:
        try:
            r = await self._ctl.get("/health")
            return r.status_code in (200, 204)
        except Exception:
            return False

    async def wait_healthy(self, timeout_sec: float = 120.0) -> None:
        deadline = time.monotonic() + timeout_sec
        last = "no response"
        while time.monotonic() < deadline:
            try:
                r = await self._ctl.get("/health")
                if r.status_code in (200, 204):
                    return
                last = f"HTTP {r.status_code}"
            except Exception as e:
                last = repr(e)
            await asyncio.sleep(1)
        raise CubeError(
            f"CubeAPI at {self.api_url} not healthy after {timeout_sec:.0f}s "
            f"(last: {last}). Check `systemctl status cube-sandbox-cube-api` "
            f"and `journalctl -u cube-sandbox-cube-api`."
        )

    # ---------------- templates ----------------
    async def list_templates(self) -> list[dict]:
        try:
            r = await self._ctl.get("/templates")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    async def resolve_template(self, name_or_id: str) -> str:
        """Map a template alias to its `tpl-` ID.

        Cube accepts an alias wherever a template ID is accepted, so
        strictly this is optional - but resolving up front turns a
        misspelled template into one clear error at warmup instead of
        `concurrency` identical failures per second during measurement.
        """
        if not name_or_id:
            raise CubeError("empty template name")
        if name_or_id.startswith(("tpl-", "snap-")):
            return name_or_id

        # The dedicated alias route is the cheap, exact answer.
        try:
            r = await self._ctl.get(f"/templates/aliases/{name_or_id}")
            if r.status_code == 200:
                data = r.json() or {}
                tid = data.get("templateID") or data.get("templateId")
                if tid:
                    return tid
        except Exception:
            pass

        templates = await self.list_templates()
        known: list[str] = []
        for t in templates:
            tid = t.get("templateID") or t.get("templateId") or ""
            aliases = t.get("aliases") or t.get("names") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            known.extend(aliases)
            if tid == name_or_id or name_or_id in aliases:
                return tid
        log.warning(
            "template %r not found in listing (known aliases: %s); passing "
            "through as-is and letting create be the judge",
            name_or_id, ", ".join(sorted(known)) or "<none>",
        )
        return name_or_id

    async def template_status(self, template_id: str) -> tuple[str, str]:
        """(status, last_error) for a template. Status is e.g. READY."""
        r = await self._ctl.get(f"/templates/{template_id}")
        if r.status_code != 200:
            raise CubeError(
                f"template {template_id} status failed: HTTP {r.status_code}: "
                f"{r.text[:200]}"
            )
        data = r.json() or {}
        return str(data.get("status") or ""), str(data.get("lastError") or "")

    async def delete_template(self, template_id: str, *, sync: bool = True) -> None:
        """Delete a template or snapshot (they share one ID namespace)."""
        r = await self._ctl.delete(
            f"/templates/{template_id}",
            params={"sync": "true"} if sync else None,
            timeout=CTL_TIMEOUT_SNAPSHOT,
        )
        if r.status_code not in (200, 202, 204, 404):
            raise CubeError(
                f"delete template {template_id} failed: HTTP {r.status_code}: "
                f"{r.text[:200]}"
            )

    # ---------------- sandboxes ----------------
    async def create(
        self,
        template_id: str,
        *,
        timeout_sec: Optional[int] = None,
        env_vars: Optional[dict[str, str]] = None,
        metadata: Optional[dict[str, str]] = None,
        auto_pause: bool = False,
        auto_resume: bool = False,
    ) -> SandboxRef:
        """Create one sandbox from a template or snapshot ID.

        `template_id` accepts a snapshot ID too: Cube has no separate
        "create from snapshot" call, snapshots and templates share one ID
        namespace and one field. That is what makes the `clone` strategy
        in rollout.py possible.

        `timeout_sec` is an *idle* TTL in seconds (E2B uses milliseconds;
        Cube uses seconds). Always send it explicitly: the shipped
        cluster default is `default_timeout_insec: -1`, i.e. no
        reclamation at all, so a leaked sandbox would live forever and
        hold its scheduler quota for the rest of the suite.

        `lifecycle.onTimeout` defaults to `kill`, which is what we want -
        an expired sandbox should be destroyed, not parked as a paused
        tombstone that still counts against `mvm_num`.
        """
        body: dict[str, Any] = {
            "templateID": template_id,
            "timeout": int(
                timeout_sec if timeout_sec is not None else self.sandbox_timeout_sec
            ),
            "lifecycle": {
                "onTimeout": "pause" if auto_pause else "kill",
                "autoResume": bool(auto_resume),
            },
        }
        if env_vars:
            body["envVars"] = env_vars
        if metadata:
            body["metadata"] = metadata

        r = await self._ctl.post("/sandboxes", json=body)
        if r.status_code not in (200, 201):
            body_text = r.text[:300]
            if "not available locally" in body_text:
                raise TemplateNotLocalError(
                    f"template {template_id} exists but is not materialised on "
                    f"this node, so no sandbox can be created from it.\n"
                    f"        Cubelet mounts its state directory as tmpfs "
                    f"(`mounted state tmpfs /data/cubelet/state`), and the "
                    f"node-local image registry lives there. A Cubelet restart "
                    f"or host reboot therefore wipes it, while the template "
                    f"definition survives in MySQL and keeps reporting "
                    f"status=READY - so the template list looks healthy and "
                    f"only create fails.\n"
                    f"        Fix: re-run `bash scripts/provision-templates.sh` "
                    f"(it deletes and rebuilds each alias, ~4 min for all 7; the "
                    f"container image is already in the registry, so this only "
                    f"re-imports it).\n"
                    f"        Server said: HTTP {r.status_code}: {body_text}"
                )
            raise CubeError(
                f"create sandbox failed: HTTP {r.status_code}: {body_text}"
            )
        data = r.json() or {}
        sid = data.get("sandboxID") or data.get("sandboxId")
        if not sid:
            raise CubeError(f"create sandbox returned no sandboxID: {data}")
        ref = SandboxRef(
            sandbox_id=sid,
            template_id=data.get("templateID") or template_id,
            traffic_token=data.get("trafficAccessToken") or None,
        )
        if self._ip_resolver is not None:
            ref.sandbox_ip = await self._ip_resolver.lookup(sid)
        return ref

    async def get(self, sandbox_id: str) -> dict:
        r = await self._ctl.get(f"/sandboxes/{sandbox_id}")
        if r.status_code != 200:
            raise CubeError(
                f"get {sandbox_id} failed: HTTP {r.status_code}: {r.text[:200]}"
            )
        return r.json() or {}

    async def delete(self, sandbox_id: str) -> None:
        """Delete a sandbox, retrying the documented transient failures.

        Cube's delete is synchronous and serialised per sandbox by a
        lifecycle lock, so at high churn it legitimately answers 503 with
        a `Retry-After` while a pause/resume/delete is already in flight,
        or 409 when a paused sandbox cannot be admitted. Treating either
        as fatal would leak the sandbox - and a leaked sandbox keeps its
        CPU/memory quota reserved, quietly shifting the next measurement
        point.
        """
        last = ""
        for attempt in range(DELETE_MAX_ATTEMPTS):
            r = await self._ctl.delete(f"/sandboxes/{sandbox_id}")
            # 404 means it already went away (TTL) - not an error for us,
            # we only care that it is gone.
            if r.status_code in (200, 202, 204, 404):
                return
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in (409, 503) or attempt == DELETE_MAX_ATTEMPTS - 1:
                break
            try:
                delay = float(r.headers.get("Retry-After", "2"))
            except ValueError:
                delay = 2.0
            await asyncio.sleep(min(delay, DELETE_RETRY_CAP_SEC))
        raise CubeError(f"delete {sandbox_id} failed: {last}")

    async def pause(self, sandbox_id: str) -> None:
        r = await self._ctl.post(
            f"/sandboxes/{sandbox_id}/pause", timeout=CTL_TIMEOUT_LIFECYCLE,
        )
        if r.status_code not in (200, 204):
            raise CubeError(
                f"pause {sandbox_id} failed: HTTP {r.status_code}: {r.text[:200]}"
            )

    async def resume(self, sandbox_id: str, timeout_sec: Optional[int] = None) -> None:
        """Resume a paused sandbox.

        `POST /sandboxes/{id}/connect` is the non-deprecated path
        (`/resume` is marked deprecated in the OpenAPI spec) and it
        preserves the sandbox's existing idle timeout. Fall back to
        `/resume` for older servers.
        """
        body = {
            "timeout": int(
                timeout_sec if timeout_sec is not None else self.sandbox_timeout_sec
            )
        }
        r = await self._ctl.post(
            f"/sandboxes/{sandbox_id}/connect", json=body,
            timeout=CTL_TIMEOUT_LIFECYCLE,
        )
        if r.status_code in (200, 201, 204):
            return
        r = await self._ctl.post(
            f"/sandboxes/{sandbox_id}/resume", json=body,
            timeout=CTL_TIMEOUT_LIFECYCLE,
        )
        if r.status_code not in (200, 201, 204):
            raise CubeError(
                f"resume {sandbox_id} failed: HTTP {r.status_code}: {r.text[:200]}"
            )

    async def set_timeout(self, sandbox_id: str, timeout_sec: int) -> None:
        r = await self._ctl.post(
            f"/sandboxes/{sandbox_id}/timeout", json={"timeout": int(timeout_sec)},
        )
        if r.status_code not in (200, 204):
            raise CubeError(
                f"set timeout on {sandbox_id} failed: HTTP {r.status_code}: "
                f"{r.text[:200]}"
            )

    # ---------------- snapshot / clone / rollback ----------------
    async def snapshot(self, sandbox_id: str, name: str = "") -> SnapshotRef:
        """Checkpoint a running sandbox (filesystem + memory).

        The parent keeps running; internally Cube pauses it, snapshots
        and resumes, and the documented pause window is under 100ms. The
        *first* snapshot of a sandbox is materially slower than later
        ones because the soft-dirty reset is deferred into it, so warm up
        before measuring.
        """
        body: dict[str, Any] = {"name": name} if name else {}
        r = await self._ctl.post(
            f"/sandboxes/{sandbox_id}/snapshots", json=body,
            timeout=CTL_TIMEOUT_SNAPSHOT,
        )
        if r.status_code not in (200, 201):
            raise CubeError(
                f"snapshot {sandbox_id} failed: HTTP {r.status_code}: {r.text[:300]}"
            )
        data = r.json() or {}
        snap = data.get("snapshotID") or data.get("snapshotId")
        if not snap:
            raise CubeError(f"snapshot returned no snapshotID: {data}")
        names = data.get("names") or []
        return SnapshotRef(snapshot_id=snap, names=list(names))

    async def clone(
        self,
        sandbox_id: str,
        count: int = 1,
        *,
        timeout_sec: Optional[int] = None,
        metadata: Optional[dict[str, str]] = None,
        snapshot: Optional[SnapshotRef] = None,
    ) -> tuple[list[SandboxRef], SnapshotRef, float]:
        """Derive `count` children that inherit a sandbox's live state.

        This is Cube's answer to AgentENV's `fork`, and the shape is
        different in a way that matters for benchmarking: there is no
        server-side fan-out. `POST /sandboxes/{id}/snapshots` once, then
        `count` independent `POST /sandboxes` with the snapshot ID in
        `templateID`. So unlike a batched fork, per-child cost here is a
        full create round trip and scales with `count` - which is why we
        issue them concurrently rather than in the SDK's serial loop.

        Pass `snapshot` to reuse an existing checkpoint instead of taking
        a fresh one; returns (children, snapshot, snapshot_ms) so a
        caller can separate checkpoint cost from create cost.
        """
        t0 = time.perf_counter()
        snap = snapshot if snapshot is not None else await self.snapshot(sandbox_id)
        snapshot_ms = (time.perf_counter() - t0) * 1000.0

        results = await asyncio.gather(
            *[
                self.create(
                    snap.snapshot_id,
                    timeout_sec=timeout_sec,
                    metadata=metadata,
                )
                for _ in range(max(1, count))
            ],
            return_exceptions=True,
        )
        children = [r for r in results if isinstance(r, SandboxRef)]
        if not children:
            errs = [repr(r)[:120] for r in results]
            raise CubeError(f"clone produced no children: {errs}")
        if len(children) < count:
            log.warning("clone: only %d/%d children came up", len(children), count)
        return children, snap, snapshot_ms

    async def rollback(self, sandbox_id: str, snapshot_id: str) -> None:
        """Reset a sandbox to a snapshot in place, keeping its ID.

        No AgentENV equivalent. It is what makes a *reusable* sandbox pool
        state-isolated: hand the same sandbox to the next rollout after
        resetting its filesystem and memory, instead of paying a
        create/delete pair. The VM restarts, so any connection held to a
        service inside it is dropped and must be re-established.
        """
        r = await self._ctl.post(
            f"/sandboxes/{sandbox_id}/rollback",
            json={"snapshotID": snapshot_id},
            timeout=CTL_TIMEOUT_SNAPSHOT,
        )
        if r.status_code not in (200, 201, 204):
            raise CubeError(
                f"rollback {sandbox_id} -> {snapshot_id} failed: "
                f"HTTP {r.status_code}: {r.text[:300]}"
            )

    async def delete_snapshot(self, snapshot_id: str) -> None:
        """Snapshots are deleted through the template route by design."""
        await self.delete_template(snapshot_id, sync=False)

    # ---------------- listing / reaping ----------------
    async def list_sandboxes(
        self,
        *,
        metadata: Optional[dict[str, str]] = None,
        state: Optional[str] = None,
        page_limit: int = 500,
        max_pages: int = 40,
    ) -> list[dict]:
        """List sandboxes, following pagination.

        Two traps in Cube's implementation that this works around:

        * The `metadata` filter is applied **client-side inside CubeAPI**,
          after it has fetched one `limit`-bounded page from CubeMaster.
          So a small `limit` silently hides matches that live further
          down the list. We therefore ask for large pages and follow the
          `x-next-token` cursor rather than trusting one call.
        * `GET /sandboxes` (v1) is hardcoded to 200 rows with no state
          filter, so we only ever use `/v2/sandboxes`.
        """
        out: list[dict] = []
        params: dict[str, Any] = {"limit": page_limit}
        if metadata:
            params["metadata"] = "&".join(f"{k}={v}" for k, v in metadata.items())
        if state:
            params["state"] = state

        token = ""
        for _ in range(max_pages):
            q = dict(params)
            if token:
                q["nextToken"] = token
            try:
                r = await self._ctl.get("/v2/sandboxes", params=q)
                if r.status_code != 200:
                    break
                data = r.json()
                if not isinstance(data, list):
                    break
                out.extend(data)
                token = r.headers.get("x-next-token") or ""
                if not token or not data:
                    break
            except Exception:
                break
        return out

    async def delete_all(self, metadata_key: str = BENCH_METADATA_KEY) -> int:
        """Best-effort cleanup of sandboxes this suite created.

        Called between concurrency levels and at teardown. Filtering is
        done here rather than server-side: Cube's `state` filter silently
        ignores anything but `running`/`paused` and returns everything, so
        relying on it to narrow a destructive operation would be unsafe.
        """
        killed = 0
        sandboxes = await self.list_sandboxes(
            metadata={metadata_key: "1"} if metadata_key else None,
        )
        victims = []
        for sb in sandboxes:
            meta = sb.get("metadata") or {}
            if metadata_key and metadata_key not in meta:
                continue
            sid = sb.get("sandboxID") or sb.get("sandboxId")
            if sid:
                victims.append(sid)

        async def _kill(sid: str) -> bool:
            try:
                await self.delete(sid)
                return True
            except Exception:
                return False

        # Deleting hundreds of sandboxes serially would take longer than
        # the cooldown window it runs in.
        results = await asyncio.gather(
            *[_kill(s) for s in victims], return_exceptions=True,
        )
        for ok in results:
            if ok is True:
                killed += 1
        return killed

    # ---------------- node observability ----------------
    async def node_metrics(self) -> dict:
        """Per-node capacity and live sandbox count, from CubeOps.

        Cube splits this away from the sandbox API: `GET /health` on
        CubeAPI reports `sandboxes: 0` unconditionally (hardcoded), and
        there is no `/nodes` route there at all - a CubeAPI unit test
        asserts it 404s. The node registry lives in CubeOps on :3010.
        We use its `/internal/v1` surface because it needs no credentials,
        where `/api/v1` requires a JWT from a login round trip.

        Note that every numeric field is `omitempty`, so a genuinely idle
        node omits `mvm_num` rather than reporting 0 - hence the explicit
        `.get(..., 0)` defaults.
        """
        if self._ops is None:
            return {}
        try:
            r = await self._ops.get("/internal/v1/nodes")
            if r.status_code != 200:
                if not self._warned_no_node:
                    self._warned_no_node = True
                    log.warning(
                        "GET %s/internal/v1/nodes returned HTTP %s; node-level "
                        "metrics (live sandbox count, allocated CPU/memory, "
                        "node health) will be absent from results. "
                        "Control-plane CPU sampling still works.",
                        self.ops_url, r.status_code,
                    )
                return {}
            nodes = r.json()
            if not isinstance(nodes, list) or not nodes:
                return {}
            node = nodes[0]
            return {
                "node_id": node.get("InstanceID") or node.get("IP"),
                "node_ip": node.get("IP"),
                "instance_type": node.get("InstanceType"),
                "healthy": node.get("Healthy"),
                "unhealthy_reason": node.get("UnhealthyReason") or "",
                "scheduling_disabled": node.get("SchedulingDisabled"),
                # Live microVMs on this node. The authoritative count -
                # our own process scan cannot see paused sandboxes, whose
                # VM is shut down but which still exist and still consume
                # scheduler quota.
                "sandbox_count": node.get("mvm_num", 0),
                "max_sandboxes": node.get("MaxMvmLimit", 0),
                "creates_in_flight": node.get("RealTimeCreateNum", 0),
                "allocated_cpu_milli": node.get("QuotaCpuUsage", 0),
                "allocated_mem_mb": node.get("QuotaMemUsage", 0),
                "quota_cpu_milli": node.get("QuotaCpu", 0),
                "quota_mem_mb": node.get("QuotaMem", 0),
                "cpu_total": node.get("CpuTotal", 0),
                "mem_mb_total": node.get("MemMBTotal", 0),
                "cpu_util": node.get("CpuUtil", 0.0),
                "mem_used_mb": node.get("MemUsage", 0),
                "nic_queues": node.get("nic_queues", 0),
                "versions": node.get("Versions") or [],
            }
        except Exception:
            return {}

    # ---------------- data plane ----------------
    def endpoint(self, ref: SandboxRef, port: int, path: str) -> tuple[str, dict[str, str]]:
        return self.resolver.resolve(ref, port, path)

    async def wait_port(
        self,
        ref: SandboxRef,
        port: int,
        path: str = "/healthz",
        timeout_sec: float = 180.0,
        expect_ok_field: bool = False,
    ) -> None:
        """Poll a service inside the sandbox until it answers.

        Cube guarantees the *probed* port is serving by the time create
        returns - the template snapshot is only taken once that probe
        answered 2xx, and the same contract is re-established on resume.
        It says nothing about other exposed ports, and we deliberately
        keep measuring rather than trusting the guarantee: this is the
        difference between "the API returned" and "a trainer could
        dispatch its first tool call", and if the guarantee ever slips
        we want it to show up as latency instead of as a burst of
        first-request failures.
        """
        url, headers = self.endpoint(ref, port, path)
        deadline = time.monotonic() + timeout_sec
        last = "no response"
        async with httpx.AsyncClient(timeout=10.0) as c:
            while time.monotonic() < deadline:
                try:
                    r = await c.get(url, headers=headers)
                    if r.status_code == 200:
                        if not expect_ok_field:
                            return
                        try:
                            if (r.json() or {}).get("ok", True):
                                return
                            last = f"ok=false: {r.text[:160]}"
                        except Exception:
                            return
                    else:
                        last = f"HTTP {r.status_code}: {r.text[:160]}"
                except Exception as e:
                    last = repr(e)
                await asyncio.sleep(0.5)
        raise CubeError(
            f"service on port {port} in {ref} not ready after {timeout_sec:.0f}s "
            f"(last: {last}); endpoint={url} via {self.resolver.describe()}"
        )


def client_from_setup(setup: dict, *, max_connections: int = 256) -> CubeClient:
    """Build a client from the picklable bundle in Config.cube_setup().

    Load-generator children are spawned, not forked, so they receive
    plain dicts rather than the Config object.
    """
    return CubeClient(
        api_url=setup["api_url"],
        api_key=setup.get("api_key", ""),
        proxy_url=setup.get("proxy_url", ""),
        transport=setup.get("transport", "path"),
        sandbox_domain=setup.get("sandbox_domain", "cube.app"),
        ops_url=setup.get("ops_url", ""),
        redis_host=setup.get("redis_host", ""),
        redis_port=int(setup.get("redis_port", 6379)),
        redis_password=setup.get("redis_password", ""),
        sandbox_timeout_sec=int(setup.get("sandbox_timeout_sec", 1800)),
        max_connections=max_connections,
    )


def client_from_config(cfg, *, max_connections: int = 16) -> CubeClient:
    """Build a client straight from a Config (parent-process callers)."""
    return CubeClient(
        api_url=cfg.cube_api_url,
        api_key=cfg.cube_api_key,
        proxy_url=cfg.cube_proxy_url,
        transport=cfg.cube_transport,
        sandbox_domain=cfg.cube_sandbox_domain,
        ops_url=cfg.cube_ops_url,
        redis_host=cfg.cube_redis_host,
        redis_port=cfg.cube_redis_port,
        redis_password=cfg.cube_redis_password,
        sandbox_timeout_sec=cfg.cube_sandbox_timeout_sec,
        max_connections=max_connections,
    )
