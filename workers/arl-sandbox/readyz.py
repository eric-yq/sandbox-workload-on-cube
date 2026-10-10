#!/usr/bin/env python3
"""Aggregate readiness endpoint for multi-service templates.

Why this exists
---------------
CubeSandbox decides when to freeze a template's snapshot by polling **one**
HTTP endpoint until it answers 2xx (`--probe <port> --probe-path <path>`,
i.e. `probePort` / `probePath` on `POST /templates`). Everything running at
that moment is captured in the snapshot, and every sandbox created from
the template resumes with it already live.

That single-probe design is a problem for two of our templates:

* ``arl-b4`` runs the Playwright worker *and* nginx serving the SPA it
  drives.
* ``arl-b9`` runs all five workload services plus nginx, because a mixed
  rollout must not reach across sandboxes for its dependencies.

Probing any one of those ports would let the snapshot be taken while the
others are still starting - and the expensive ones are exactly the ones
that would lose: DuckDB's TPC-H `dbgen` and Chromium's launch. A sandbox
restored from such a snapshot would then pay that startup cost per
rollout, which is the whole thing the snapshot is supposed to avoid. Worse,
it would fail *intermittently*, as a first-request error rather than as a
build failure.

So: this process answers 2xx only once every service the template was
asked to start is answering, and it is what those templates probe.

Deliberately stdlib-only and only started when it is actually needed
(`start.sh` launches it for multi-service templates; single-service
templates probe their one service directly). Cube's per-sandbox memory
overhead is single-digit MB, so adding a Python interpreter to all ~1024
sandboxes of a single-service sweep would be a measurable and pointless
tax on the very numbers this suite reports.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# service name -> (port, path). Must stay in step with start.sh and with
# the PORT_* constants in orchestrator/rollout.py.
#
# nginx is probed at "/" because it serves a static SPA and has no
# /healthz. The workload services all expose /healthz; where they also
# return an {"ok": ...} field we honour it, which is what makes this gate
# on DuckDB having finished dbgen rather than merely on uvicorn listening.
PROBES: dict[str, tuple[int, str]] = {
    "b1": (8001, "/healthz"),
    "b3": (8003, "/healthz"),
    "b4": (8004, "/healthz"),
    "b5": (8005, "/healthz"),
    "b7": (8007, "/healthz"),
    "static": (80, "/"),
}

PORT = int(os.getenv("READYZ_PORT", "8010"))
# Services to require, as a space-separated list - the same argument list
# start.sh received.
SERVICES = [s for s in os.getenv("READYZ_SERVICES", "").split() if s in PROBES]
PROBE_TIMEOUT = float(os.getenv("READYZ_PROBE_TIMEOUT", "2"))
# Positive results are cached briefly so a template build (or a client
# health check) polling once a second does not fan out into six probes per
# poll. Short enough that a service dying is noticed within seconds.
CACHE_SEC = float(os.getenv("READYZ_CACHE_SEC", "2"))

_LOCK = threading.Lock()
_CACHE: tuple[float, bool, dict] = (0.0, False, {})


def _probe_one(name: str) -> tuple[bool, str]:
    port, path = PROBES[name]
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as r:
            if not (200 <= r.status < 300):
                return False, f"HTTP {r.status}"
            body = r.read(4096)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, type(e).__name__
    # An explicit ok=false means "listening but not usable" - B5 reports
    # that until DuckDB has loaded the tpch extension and generated data.
    try:
        doc = json.loads(body or b"{}")
        if isinstance(doc, dict) and doc.get("ok") is False:
            return False, "ok=false"
    except Exception:
        pass  # non-JSON (nginx serving HTML) is fine
    return True, "ok"


def _probe_all() -> tuple[bool, dict]:
    global _CACHE
    now = time.monotonic()
    with _LOCK:
        ts, ok, detail = _CACHE
        if ok and (now - ts) < CACHE_SEC:
            return ok, detail

    detail = {}
    all_ok = True
    for name in SERVICES:
        ok, why = _probe_one(name)
        detail[name] = why
        all_ok = all_ok and ok

    with _LOCK:
        _CACHE = (time.monotonic(), all_ok, detail)
    return all_ok, detail


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # silence per-request logs
        pass

    def _reply(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/", "/healthz", "/readyz", "/health"):
            ok, detail = _probe_all()
            # 503 rather than 200-with-ok=false: Cube's template probe
            # keys on the status code, so a body-level flag would let the
            # snapshot be taken too early.
            self._reply(
                200 if ok else 503,
                {"ok": ok, "services": detail, "required": SERVICES},
            )
            return
        self._reply(404, {"error": "not found"})


def main() -> int:
    if not SERVICES:
        # Nothing to aggregate. Exit non-zero rather than answering 200
        # for an empty set, which would make a misconfigured template
        # build succeed with an empty snapshot.
        print("[readyz] READYZ_SERVICES is empty; nothing to aggregate",
              file=sys.stderr)
        return 2
    print(f"[readyz] :{PORT} gating on {' '.join(SERVICES)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
