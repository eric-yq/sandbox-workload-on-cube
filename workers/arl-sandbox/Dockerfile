# Unified rollout-sandbox image for the CubeSandbox benchmark.
#
# One image, seven CubeSandbox templates. Each template differs only in the
# entrypoint args it is built with, which select the services to launch
# (`command` + `args` on POST /templates):
#
#   arl-b1  ->  /app/start.sh b1
#   arl-b3  ->  /app/start.sh b3
#   arl-b4  ->  /app/start.sh b4 static      (Playwright needs the SPA local)
#   arl-b5  ->  /app/start.sh b5
#   arl-b7  ->  /app/start.sh b7
#   arl-b9  ->  /app/start.sh b1 b3 b4 b5 b7 static
#   arl-b8  ->  /app/start.sh b3             (cheapest service; B8 only
#                                             measures VM lifecycle)
#
# Templates that launch more than one service also get readyz.py on :8010,
# because Cube gates the template snapshot on a single HTTP probe and one
# service answering says nothing about the others. See readyz.py.
#
# Why one fat image instead of six thin ones
# ------------------------------------------
# A per-rollout sandbox has to be self-contained: B4 needs its target SPA
# in-VM, and B9 needs all five stacks in-VM, otherwise the "shared worker
# container" contention we are trying to eliminate simply reappears as a
# shared service. Once B4 and B9 need a combined image, keeping four more
# near-identical thin ones only adds build and provisioning paths.
#
# The size objection does not apply to this runtime. Cube converts this
# image into a microVM rootfs once, at template-build time, and every
# sandbox then restores from a per-node memory snapshot through reflink
# copy-on-write (FICLONE). An untouched page is shared, not copied, so only
# the pages a rollout actually writes cost real memory or real disk - which
# is why the reported per-sandbox overhead is single-digit MB regardless of
# how large the template is. A fat rootfs plus a narrow working set is the
# shape this runtime is built for.
#
# Build context is the REPOSITORY ROOT (the worker sources live in sibling
# directories):
#   docker buildx build -f workers/arl-sandbox/Dockerfile .
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

# util-linux  -> prlimit, used by the B1 code-exec sandbox. It exists so
#                we avoid asyncio's preexec_fn path, which forces CPython's
#                locked fork helper and caps B1 throughput.
# nginx       -> serves the static WebArena-style SPA for B4/B9.
# procps/curl -> readiness probes and debugging inside the guest.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl procps iproute2 \
        util-linux \
        nginx \
        fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY workers/arl-sandbox/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
 && playwright install --with-deps chromium

# Each worker app keeps its own directory so `app:app` resolves to the
# right module without renaming anything. The application sources are
# byte-for-byte the ones the docker-compose topology used.
COPY workers/b1-codeexec/app.py        /app/b1/app.py
COPY workers/b3-mock-api/app.py        /app/b3/app.py
COPY workers/b4-playwright/app.py      /app/b4/app.py
COPY workers/b5-sql-runner/app.py      /app/b5/app.py
COPY workers/b7-textgame/app.py        /app/b7/app.py

# Static SPA for B4 / B9.
COPY workers/b4-webarena-static/html/  /usr/share/nginx/html/

# Replace Debian's stock site, which binds `listen [::]:80` and therefore
# makes nginx abort inside a Cube microVM (the guest kernel has no IPv6):
#     nginx: [emerg] socket() [::]:80 failed
#            (97: Address family not supported by protocol)
# Our config is IPv4-only. Also send nginx's errors to stderr so a future
# config problem shows up in the guest console instead of silently leaving
# port 80 closed. See nginx-spa.conf.
COPY workers/arl-sandbox/nginx-spa.conf /etc/nginx/conf.d/spa.conf
RUN rm -f /etc/nginx/sites-enabled/default \
 && sed -i 's|^\s*error_log .*|error_log /dev/stderr warn;|' /etc/nginx/nginx.conf \
 && nginx -t

# Bake the DuckDB tpch extension so a sandbox needs no outbound network.
# Sandboxes have internet access by default, but relying on it here would
# make every B5 rollout's first query depend on extensions.duckdb.org.
RUN python -c "import duckdb; \
con = duckdb.connect(); \
con.execute('INSTALL tpch'); \
con.execute('LOAD tpch'); \
print('[build] tpch extension installed and loaded ok')"

COPY workers/arl-sandbox/start.sh /app/start.sh
COPY workers/arl-sandbox/readyz.py /app/readyz.py
RUN chmod +x /app/start.sh /app/readyz.py

# Service defaults. These are sized for a *small* VM (2-4 vCPU), which is
# the whole point of the per-rollout model: parallelism comes from running
# many sandboxes, not from many workers inside one.
#
# B4_UVICORN_WORKERS=1 is load-bearing, not a tuning choice. The B4 worker
# listens on B4_PORT_BASE..B4_PORT_BASE+W-1, so W=2 would claim 8004 and
# 8005 - and 8005 is B5's port. One Chromium master per sandbox is also
# the correct shape here.
ENV B1_UVICORN_WORKERS=1 \
    B3_UVICORN_WORKERS=1 \
    B7_UVICORN_WORKERS=1 \
    B4_UVICORN_WORKERS=1 \
    B4_PORT_BASE=8004 \
    MAX_CONTEXTS=1 \
    TPCH_SF=1.0 \
    DUCKDB_THREADS=0 \
    DUCKDB_MEM_LIMIT= \
    READYZ_PORT=8010 \
    PYTHONUNBUFFERED=1

# Cube needs every port it may proxy into declared as an exposedPort at
# template-build time (scripts/provision-templates.sh passes them), and the
# image documents the same set here. 8010 is the aggregate readiness gate
# used as the probe port for multi-service templates.
EXPOSE 80 8001 8003 8004 8005 8007 8010

# Default to the full set; templates override this via startCmd.
CMD ["/app/start.sh", "b1", "b3", "b4", "b5", "b7", "static"]
