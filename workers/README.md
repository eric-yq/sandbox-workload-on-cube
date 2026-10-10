# workers/

**There is one image, not one per benchmark.**

The per-benchmark directories here (`b1-codeexec/`, `b3-mock-api/`, …) hold
*source only* — an `app.py`, or in B4's case a static site. They are not
independently buildable images and have no Dockerfile of their own. Only
`arl-sandbox/` is built.

Earlier versions of this suite did ship one Dockerfile per worker, because
under docker-compose each was a separate long-lived container. That topology
is gone: the sandbox runtime is CubeSandbox, and a rollout now gets its own
microVM. Those Dockerfiles were left behind as dead files that read like
per-benchmark images, so they were removed.

## The actual mapping

```
workers/arl-sandbox/Dockerfile          ← the ONE image (~2 GB)
    │   COPYs each b*/app.py + b4-webarena-static/html + readyz.py
    ▼
<registry>/arl-sandbox:v2-arm64          ← one push
    │   scripts/provision-templates.sh creates 7 templates from it,
    │   differing ONLY in entrypoint args + cpu/memory + probe port
    ▼
arl-b1  /app/start.sh b1                        2 vCPU / 1024 MiB  probe :8001
arl-b3  /app/start.sh b3                        2 vCPU / 1024 MiB  probe :8003
arl-b4  /app/start.sh b4 static                 2 vCPU / 2048 MiB  probe :8010
arl-b5  /app/start.sh b5                        2 vCPU / 3072 MiB  probe :8005
arl-b7  /app/start.sh b7                        2 vCPU / 1024 MiB  probe :8007
arl-b8  /app/start.sh b3                        2 vCPU / 1024 MiB  probe :8003
arl-b9  /app/start.sh b1 b3 b4 b5 b7 static     4 vCPU / 4096 MiB  probe :8010
    │
    ▼
one sandbox per rollout, created on demand
```

So a template is not "a benchmark's image". It is a *snapshot of this image
with a chosen subset of services already running*. Note `arl-b8` runs the b3
service purely because B8 measures microVM lifecycle and just needs something
cheap to answer a readiness probe.

`start.sh` takes the service list as arguments and backgrounds a uvicorn (or
nginx) per requested service. Ports: b1 8001, b3 8003, b4 8004, b5 8005,
b7 8007, static 80.

## The probe port, and why two templates have an extra service

Cube decides when to freeze a template's snapshot by polling **one** HTTP
endpoint (`probePort` + `probePath`) until it returns 2xx. Whatever is running
at that moment is what every sandbox resumes with.

For the five single-service templates that is simply the service's own port.
But `arl-b4` runs Playwright *and* nginx, and `arl-b9` runs all five stacks
plus nginx — and probing any one of those would let the snapshot be frozen
while the others were still starting. The expensive ones would lose that race:
DuckDB's TPC-H `dbgen` and Chromium's launch. Every rollout would then re-pay
that startup cost, intermittently, as a first-request error rather than as a
build failure.

So those two templates additionally run `readyz.py` on **:8010**, which returns
2xx only once every service it was asked about answers, and that is what they
probe. Single-service templates do not start it: at c=1024 an extra Python
interpreter per sandbox would be a measurable and pointless tax on exactly the
memory numbers this suite reports.

## Why one image instead of six

Two benchmarks force it. B4 needs its target SPA reachable inside the same
microVM, and B9 needs all five stacks inside one microVM — otherwise the
shared-worker contention that per-rollout isolation exists to remove simply
reappears as a shared service. Once B4 and B9 need a combined image, keeping
four more near-identical thin ones only adds build and provisioning paths
without changing what is measured.

The size objection does not apply to this runtime. Cube converts this image
into a microVM rootfs once, at template-build time, and every sandbox then
restores from a per-node memory snapshot through reflink copy-on-write
(`FICLONE`). An untouched page is shared, not copied, so only the pages a
rollout actually writes cost real memory or real disk. A fat rootfs with a
narrow working set is the shape this runtime is built for — which is why the
documented per-sandbox overhead is single-digit MB more or less regardless of
how large the template is.

The flip side, and the reason `scripts/ensure-cube.sh` refuses to continue
without it: reflink means `/data/cubelet` **must** be XFS created with
`-m reflink=1` (or btrfs). Ubuntu defaults to ext4, on which Cubelet will not
start at all.

## Changing what a template runs

Edit the `templates_spec` table in `scripts/provision-templates.sh` and
re-provision. No image rebuild is needed unless you change `app.py`:

```bash
bash scripts/provision-templates.sh B5      # one template
sudo bash scripts/setup-host.sh --skip-build  # all templates, no rebuild
```

Per-sandbox CPU and memory come from `SANDBOX_VCPU_*` / `SANDBOX_MEM_MB_*` in
`.env`. They can only be set at template-build time — `POST /sandboxes` has no
CPU or memory field, so the shape is a property of the template, not of the
sandbox. Note the unit conversion: `.env` is written in whole vCPU and
`provision-templates.sh` converts to the **millicores** Cube's `cpu` field
takes (2 vCPU → `2000`).

Scale-out here is "more sandboxes", not "bigger sandboxes" — which is the point
of the per-rollout model, and why 2 vCPU is the right size for a single-stack
template even on a 192-vCPU host.
