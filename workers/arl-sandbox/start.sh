#!/bin/sh
# Launch the subset of benchmark services a given CubeSandbox template needs.
#
#   /app/start.sh b1                        # B1 template
#   /app/start.sh b4 static                 # B4 template (SPA must be local)
#   /app/start.sh b1 b3 b4 b5 b7 static     # B9 template
#
# This is the template's entrypoint (`command` / `args` on
# POST /templates), so it runs once inside the temporary microVM Cube boots
# to build the template. Cube then polls the template's HTTP probe and
# freezes the snapshot once it answers 2xx - so the snapshot captures these
# services already running. Every sandbox created from that template
# resumes with its stack live: no per-rollout startup, and for B5 no
# per-rollout dbgen, because the generated TPC-H tables are inside the
# snapshotted memory.
#
# When more than one service is requested we also start readyz.py, which
# answers 2xx only once all of them do. Cube's template build probes a
# single port, so without it the snapshot could be frozen while DuckDB or
# Chromium were still starting - see readyz.py for the full reasoning.
#
# Each service is backgrounded and we `wait` at the end so the script stays
# alive as long as any of them does.

set -eu

SERVICES="${*:-}"
if [ -z "${SERVICES}" ]; then
  echo "[start] no services requested; nothing to do" >&2
  exit 2
fi

echo "[start] launching services: ${SERVICES}"

want() {
  for s in ${SERVICES}; do
    [ "${s}" = "$1" ] && return 0
  done
  return 1
}

if want static; then
  # nginx serves the WebArena-style SPA on :80 for B4 / B9.
  #
  # Validate the config before backgrounding it. nginx is started with `&`,
  # so a startup failure is otherwise completely silent: the script keeps
  # running, port 80 never opens, and the only symptom is a template build
  # that times out on its readiness probe. That is exactly how the IPv6
  # `listen [::]:80` failure presented before nginx-spa.conf fixed it, and
  # it cost far more time to find than this check costs to run.
  if ! nginx -t 2>&1; then
    echo "[start] FATAL: nginx config test failed - port 80 will not open." >&2
    echo "[start]        B4/B9 need the SPA in-sandbox; not starting it is" >&2
    echo "[start]        not a degraded mode, it is a broken template." >&2
    exit 3
  fi
  echo "[start] nginx on :80 (static SPA)"
  nginx -g 'daemon off;' &
fi

if want b1; then
  echo "[start] b1 code-exec on :8001 (${B1_UVICORN_WORKERS} worker/s)"
  cd /app/b1 && uvicorn app:app --host 0.0.0.0 --port 8001 \
      --workers "${B1_UVICORN_WORKERS}" &
fi

if want b3; then
  echo "[start] b3 mock-api on :8003 (${B3_UVICORN_WORKERS} worker/s)"
  cd /app/b3 && uvicorn app:app --host 0.0.0.0 --port 8003 \
      --workers "${B3_UVICORN_WORKERS}" &
fi

if want b4; then
  # The B4 app derives its /ports response from B4_NUM_PORTS and listens on
  # B4_PORT_BASE + i. Keep this at 1: W=2 would claim 8005, which is B5's.
  B4_NUM_PORTS="${B4_UVICORN_WORKERS}"
  export B4_NUM_PORTS
  export TARGET_URL="${TARGET_URL:-http://127.0.0.1:80}"
  i=0
  while [ "${i}" -lt "${B4_UVICORN_WORKERS}" ]; do
    port=$((B4_PORT_BASE + i))
    echo "[start] b4 playwright on :${port} (MAX_CONTEXTS=${MAX_CONTEXTS})"
    (cd /app/b4 && uvicorn app:app --host 0.0.0.0 --port "${port}" --workers 1) &
    i=$((i + 1))
  done
fi

if want b5; then
  # Single uvicorn worker: DuckDB's in-memory database cannot be shared
  # across processes. Concurrency inside one sandbox comes from
  # asyncio.to_thread plus DuckDB's own executor - and across sandboxes
  # from there being many sandboxes.
  echo "[start] b5 duckdb on :8005 (TPCH_SF=${TPCH_SF})"
  cd /app/b5 && uvicorn app:app --host 0.0.0.0 --port 8005 --workers 1 &
fi

if want b7; then
  echo "[start] b7 textgame on :8007 (${B7_UVICORN_WORKERS} worker/s)"
  cd /app/b7 && uvicorn app:app --host 0.0.0.0 --port 8007 \
      --workers "${B7_UVICORN_WORKERS}" &
fi

# Aggregate readiness gate, for templates that run more than one service.
# Single-service templates are probed directly on their own port, so they
# do not pay for an extra Python interpreter in every one of the ~1024
# sandboxes a high-concurrency sweep keeps alive.
NSERVICES=0
for _s in ${SERVICES}; do
  NSERVICES=$((NSERVICES + 1))
done
if [ "${NSERVICES}" -gt 1 ] || [ "${READYZ_FORCE:-0}" = "1" ]; then
  echo "[start] readyz on :${READYZ_PORT} gating on: ${SERVICES}"
  READYZ_SERVICES="${SERVICES}" python3 /app/readyz.py &
fi

wait
