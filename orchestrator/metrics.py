"""Latency / throughput / resource sampling and aggregation.

The orchestrator now runs as a host process (not a container), partly
because Firecracker caps a single microVM at 32 vCPU so a containerised
load generator could never drive a 192-vCPU box, and partly because these
numbers are cleaner when read directly from the host.

CPU is reported from four viewpoints, because on this architecture any of
them can be the binding constraint:

  - `cpu_util_avg`          the host as a whole: is the machine busy?
  - `orch_cpu_util_avg`     the orchestrator process: is the *load
                            generator* the bottleneck? If this is pegged
                            while host CPU is low, the result is a client
                            artefact.
  - `control_plane.cube_proxy_cores_*`
                            CubeProxy, the OpenResty data-plane router.
                            Every in-sandbox request traverses it unless
                            `CUBE_TRANSPORT=direct`, so this is the guard
                            rail that says whether we measured sandboxes
                            or measured the proxy.
  - `control_plane.sandbox_fleet_cores_*`
                            the microVM fleet, i.e. where the CPU should
                            actually be going.

Two things about CubeSandbox make this accounting different from the
AgentENV version of this suite, and both are load-bearing:

* **There is no single control-plane cgroup.** AgentENV ran as one
  systemd service whose cgroup contained the API server, the block-device
  daemon and every microVM, so one `cpu.stat` read covered the lot. Cube
  splits into five native systemd units plus containerised CubeProxy, and
  none of the units declares `Slice=` or `CPUAccounting=`, so each has to
  be read separately and summed.
* **The sandboxes are not in those cgroups.** Cube pre-creates a pool of
  numerically indexed cgroups at `/sys/fs/cgroup/cube_sandbox{,_v2}` and
  assigns one per sandbox, so the fleet is accounted at the cgroup-v2
  parent and never mixes into a component's number.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

import numpy as np
import psutil

log = logging.getLogger(__name__)


@dataclass
class LatencySink:
    """Thread-safe-ish recorder for per-request latencies (ms)."""

    samples: list[float] = field(default_factory=list)
    errors: int = 0

    def record(self, latency_ms: float) -> None:
        self.samples.append(latency_ms)

    def fail(self) -> None:
        self.errors += 1

    def summary(self) -> dict:
        if not self.samples:
            return {
                "count": 0,
                "errors": self.errors,
                "p50": None, "p95": None, "p99": None,
                "mean": None, "max": None,
            }
        arr = np.array(self.samples)
        return {
            "count": int(arr.size),
            "errors": self.errors,
            "p50": float(np.percentile(arr, 50)),
            "p95": float(np.percentile(arr, 95)),
            "p99": float(np.percentile(arr, 99)),
            "mean": float(arr.mean()),
            "max": float(arr.max()),
        }


# /proc/stat is shared with the host kernel even inside containers
# (unless explicitly remounted). On hosts that bind-mount /proc/host
# we prefer that for unambiguous host-level CPU stats.
_PROC_STAT = "/proc/host/stat" if os.path.exists("/proc/host/stat") else "/proc/stat"
_NCPU = os.cpu_count() or 1


def _read_cpu_totals() -> tuple[int, int]:
    """Return (idle_jiffies, total_jiffies) from /proc/stat aggregate line."""
    with open(_PROC_STAT) as f:
        first = f.readline().split()
    # cpu  user nice system idle iowait irq softirq steal guest guest_nice
    vals = [int(x) for x in first[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    total = sum(vals)
    return idle, total


_CGROUP_ROOT = "/sys/fs/cgroup"

# Why cgroups rather than per-process sampling: `cpu.stat/usage_usec` is a
# *cumulative* counter, so a delta over the sampling interval captures
# every microVM that lived and died in between. Per-process sampling
# cannot - sandboxes here are created and destroyed in tens of
# milliseconds, so processes appear and vanish between two 1-second
# psutil samples and their CPU is simply lost. (Measured on the AgentENV
# version of this suite: per-process sampling reported 0.00 cores of VMM
# time across a run that created 40 sandboxes.)
#
# Cube's control plane is five native systemd units. Each `ExecStart` is a
# wrapper shell script that `exec`s the real binary, so the unit cgroup is
# the reliable handle - not the process name.
_CUBE_UNIT_CGROUPS: dict[str, tuple[str, ...]] = {
    # E2B-compatible REST API. Control plane only: sandbox traffic does
    # not pass through it, which is the structural difference from
    # AgentENV's single Axum process.
    "cube_api": ("cube-sandbox-cube-api.service",),
    # Cluster orchestrator / scheduler.
    "cube_master": ("cube-sandbox-cubemaster.service",),
    # Node agent. Embeds the network runtime and CubeVS (they are linked
    # in, not separate daemons), so their CPU lands here.
    #
    # Cubelet is the one unit whose systemd cgroup is the wrong place to
    # look: it moves its own process into /cube_sandbox/cubelet at
    # startup, leaving the unit cgroup with no processes and only the few
    # milliseconds it burned before relocating. Sampling the unit reported
    # cubelet at a flat 0.0 cores while the real cgroup had accumulated
    # 171s. _CUBELET_CGROUPS is tried first; the unit stays as a fallback
    # in case a future release stops relocating.
    "cubelet": ("cube-sandbox-cubelet.service",),
    # Node registry / ops API.
    "cube_ops": ("cube-sandbox-cubeops.service",),
    # Template builds. Idle during measurement; non-zero here means a
    # build is running and the run is contaminated.
    "template_center": ("cube-sandbox-cube-templatecenter.service",),
}

# The microVM fleet. Cube pre-creates a pool of numerically indexed
# cgroups and hands one to each sandbox, reusing slots after deletion, so
# the sandbox ID never appears in the path and counting directories would
# wildly over-count live sandboxes. The cgroup-v2 parent aggregates its
# children's CPU, so two reads cover the whole fleet.
# (Cubelet/plugins/cube/internals/cgroup/handle/interface.go)
_SANDBOX_CGROUPS = (
    f"{_CGROUP_ROOT}/cube_sandbox/sandbox",
    f"{_CGROUP_ROOT}/cube_sandbox_v2/sandbox",
)

# Where Cubelet actually accounts itself, checked before its systemd unit.
# Sibling of the sandbox pool under the same parent, so sampling both
# /cube_sandbox/cubelet and /cube_sandbox/sandbox double-counts nothing.
_CUBELET_CGROUPS = (
    f"{_CGROUP_ROOT}/cube_sandbox/cubelet",
    f"{_CGROUP_ROOT}/cube_sandbox_v2/cubelet",
)

# One process per sandbox, and it is the shim - Cube links the VMM in as a
# library and runs the microVM in-process (CubeShim/shim/src/hypervisor/
# cube_hypervisor.rs calls VmmInstance::new, never spawn). So there is no
# separate hypervisor process to look for.
#
# `containerd-shim-cube-rs` is 23 characters, and /proc/<pid>/comm is
# capped at 15, so the kernel truncates it to exactly `containerd-shim` -
# which collides with an ordinary `containerd-shim-runc-v2`. The comm is
# only used as a cheap prefilter; identity is confirmed from cmdline.
#
# Matched as a prefix rather than for equality so that either form works:
# /proc/<pid>/comm gives the truncated `containerd-shim`, while psutil
# hands back the full `containerd-shim-cube-rs` (Process.name() notices a
# 15-character comm and substitutes basename(cmdline[0])). _count_sandbox_
# shims reads /proc and sees the truncated form; see the note there for
# why it no longer uses psutil at all.
_SHIM_COMM_PREFIX = "containerd-shim"
_SHIM_CMDLINE_MARK = "containerd-shim-cube-rs"

# CubeProxy runs as a Docker container with host networking, so its
# OpenResty master and workers show up in the host PID namespace under a
# Docker cgroup scope rather than a cube-* unit. These are the comms to
# look for when locating that scope.
_PROXY_COMMS = ("nginx", "openresty")


# ---------------------------------------------------------------------
# Disk I/O
# ---------------------------------------------------------------------
# Everything Cubelet writes lives under /data/cubelet - image cache,
# unpacked rootfs, CoW volumes, snapshot packs, shim logs - and none of
# the four target *.metal-48xl instance types has instance store, so it is
# all EBS. That volume additionally has to be XFS with reflink enabled,
# because the cubecow storage backend implements snapshots with FICLONE;
# scripts/ensure-cube.sh refuses to proceed otherwise.
#
# The number that bites is gp3's *baseline* of 3,000 IOPS, not its ceiling: a
# single gp3 volume can be provisioned to 80,000 IOPS / 2,000 MiB/s, which
# comfortably covers this workload (~40,000 IOPS projected at c=1024), but an
# unprovisioned volume runs out around c=64.
#
# The load is IOPS-shaped rather than throughput-shaped: reads are largely
# served from the host page cache after warmup (sandboxes created from one
# snapshot share a single memory ublk device by reference count), while
# writes are dominated by per-sandbox lifecycle churn - a writable
# overlaybd upper, a Firecracker work dir and a serial-log dir created and
# destroyed for every rollout.
#
# `util` is the number that matters and is computed the way iostat's %util
# is: the fraction of wall time the device had I/O in flight. Without this
# a disk-bound result is indistinguishable from a CPU-bound one.
_SECTOR_BYTES = 512

# Virtual / non-backing devices, excluded so the numbers describe the real
# volume rather than a layer stacked on top of it. Counting a virtual
# device is wrong twice over: its I/O is the same I/O that either lands on
# the real volume or is served from the host page cache, so IOPS and MB/s
# get double-counted; and because `util` takes the peak across devices, one
# briefly-busy virtual device fakes saturation for the whole run.
#
# `nbd*` is the one that matters on Cube: the optional CubeS3lvol backend
# exports remote CoW volumes over NVMe/TCP, and `dm-*`/`loop*` can appear
# for volume plugins. `ublk*` is kept from the AgentENV build of this
# suite - harmless, and it costs nothing to stay robust if a deployment
# introduces one.
_SKIP_DEV_PREFIXES = ("loop", "ram", "dm-", "sr", "zram", "md", "ublk", "nbd")


def _whole_block_devices() -> list[str]:
    """Real backing block devices.

    /sys/block lists only whole devices, which avoids double-counting a
    partition against its parent (nvme0n1p1 vs nvme0n1). On these instances
    that leaves the EBS volume(s), e.g. nvme0n1 - which is the thing the
    question "is gp3 my bottleneck?" is actually about.
    """
    try:
        names = os.listdir("/sys/block")
    except OSError:
        return []
    return sorted(
        n for n in names if not n.startswith(_SKIP_DEV_PREFIXES)
    )


class _DiskCounters(NamedTuple):
    read_ios: int
    read_sectors: int
    read_ms: int
    write_ios: int
    write_sectors: int
    write_ms: int
    io_ticks_ms: int      # time with at least one I/O in flight
    weighted_ms: int      # sum of in-flight time, i.e. queue-depth integral


def _read_diskstats(devices: set[str]) -> dict[str, _DiskCounters]:
    """Parse /proc/diskstats for the given whole devices."""
    out: dict[str, _DiskCounters] = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                p = line.split()
                if len(p) < 14:
                    continue
                if p[2] not in devices:
                    continue
                out[p[2]] = _DiskCounters(
                    read_ios=int(p[3]),
                    read_sectors=int(p[5]),
                    read_ms=int(p[6]),
                    write_ios=int(p[7]),
                    write_sectors=int(p[9]),
                    write_ms=int(p[10]),
                    io_ticks_ms=int(p[12]),
                    weighted_ms=int(p[13]),
                )
    except OSError:
        pass
    return out


def _cgroup_cpu_usec(cgroup: str) -> Optional[int]:
    """Cumulative CPU microseconds for a cgroup and everything under it."""
    try:
        with open(os.path.join(cgroup, "cpu.stat")) as f:
            for line in f:
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
    except Exception:
        pass
    return None


def _cgroup_memory_bytes(cgroup: str) -> Optional[int]:
    try:
        with open(os.path.join(cgroup, "memory.current")) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _cgroup_has_procs(cgroup: str) -> bool:
    """True if this cgroup currently holds at least one process.

    Used to tell a relocated daemon's real cgroup from its now-empty
    systemd unit cgroup, which still exists and still has a readable
    cpu.stat - just one that stopped advancing.
    """
    try:
        with open(os.path.join(cgroup, "cgroup.procs")) as f:
            return any(line.strip() for line in f)
    except Exception:
        return False


def _find_unit_cgroup(units: tuple[str, ...]) -> Optional[str]:
    """Locate a systemd unit's cgroup, tolerating slice nesting."""
    for unit in units:
        for path in (
            f"{_CGROUP_ROOT}/system.slice/{unit}",
            f"{_CGROUP_ROOT}/{unit}",
        ):
            if os.path.exists(os.path.join(path, "cpu.stat")):
                return path
    return None


def _find_proxy_cgroup() -> Optional[str]:
    """Locate CubeProxy's cgroup by way of one of its nginx processes.

    CubeProxy is a Docker container with `network_mode: host`, so it has
    no cube-* systemd unit and its cgroup path contains a container ID we
    cannot predict. Rather than shelling out to `docker inspect` on every
    sample, find one OpenResty process and read its own cgroup - which is
    the container scope, and therefore covers the master and every worker.
    """
    for proc in psutil.process_iter(attrs=["pid", "name"]):
        try:
            if (proc.info.get("name") or "") not in _PROXY_COMMS:
                continue
            pid = proc.info["pid"]
            with open(f"/proc/{pid}/cgroup") as f:
                for line in f:
                    # cgroup v2: "0::/system.slice/docker-<id>.scope"
                    rel = line.rstrip("\n").split(":", 2)[-1]
                    if not rel or rel == "/":
                        continue
                    path = f"{_CGROUP_ROOT}{rel}"
                    if os.path.exists(os.path.join(path, "cpu.stat")):
                        return path
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return None


def _count_sandbox_shims() -> int:
    """Live microVMs, counted as `containerd-shim-cube-rs` processes.

    One shim process per sandbox, hosting the VMM in-process.

    Scans /proc directly instead of going through psutil, which measured
    a flat zero here. Verified side by side while a B3 load ran: a raw
    scan saw 2.5 shims on average (max 4) at the same instants that
    `psutil.process_iter()` reported none, and the same psutil call
    returned the correct 4 once the sandboxes were held steady instead of
    churning. Whatever the mechanism inside process_iter's caching, it
    does not reliably observe processes that live ~100ms, and a `fresh`
    rollout creates and destroys one in well under a second - exactly the
    case this metric exists to measure. Reading /proc costs one small read
    per pid and has no cache to go stale.

    A *paused* sandbox has its microVM shut down, so it does not appear
    here even though it still exists and still consumes scheduler quota;
    `control_plane.sandbox_count_max` (from CubeOps) is the count that
    includes them.

    Two-stage match: /proc/<pid>/comm is the kernel's 15-character
    truncation, so the shim always shows there as exactly
    `containerd-shim` - which an ordinary `containerd-shim-runc-v2`
    shares. Only those pay the second read, where the full cmdline
    decides.
    """
    n = 0
    try:
        pids = os.listdir("/proc")
    except OSError:
        return 0
    mark = _SHIM_CMDLINE_MARK.encode()
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                if not f.read().strip().startswith(_SHIM_COMM_PREFIX):
                    continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if mark in f.read():
                    n += 1
        except OSError:
            # Exited between listdir and read, or not ours to inspect.
            continue
    return n


class ResourceSampler:
    """Background task: sample CPU + memory at a fixed interval.

    When given a `cfg`, it additionally samples the CubeSandbox control
    plane (per-component cgroup CPU plus CubeOps' node accounting) so a
    result file records whether the sandbox fleet or the control plane was
    the limiting factor.
    """

    def __init__(self, interval: float = 1.0, cfg=None) -> None:
        self.interval = interval
        self.cfg = cfg
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.cpu_host: list[float] = []      # host-wide %
        self.cpu_orch: list[float] = []      # this process %
        self.mem_gb: list[float] = []
        self.ctx_switches_per_s: list[float] = []
        self._last_ctx = psutil.cpu_stats().ctx_switches
        self._last_t = time.monotonic()
        self._last_idle, self._last_total = _read_cpu_totals()
        self._proc = psutil.Process()
        # Prime per-process counter: first call returns 0.0.
        self._proc.cpu_percent(interval=None)

        # Control-plane sampling. Expressed in *cores consumed* rather
        # than percent: these are multi-threaded Rust and Go services, so
        # "100%" is meaningless - what matters is how many of the host's
        # cores they eat relative to the sandboxes.
        self.cp_cores: dict[str, list[float]] = {
            role: [] for role in (*_CUBE_UNIT_CGROUPS, "cube_proxy", "sandbox_fleet")
        }
        self.cp_mem_gb: dict[str, list[float]] = {
            "sandbox_fleet": [], "cubelet": [],
        }
        self.vm_count: list[int] = []

        # Disk I/O. Always sampled (cheap: one /proc/diskstats read per
        # second) because a disk-bound data point is otherwise
        # indistinguishable from a CPU-bound one.
        override = os.getenv("CUBE_DISK_DEVICES", "").strip()
        self._disk_devices: set[str] = (
            {d.strip() for d in override.split(",") if d.strip()}
            if override else set(_whole_block_devices())
        )
        self.disk_read_mbps: list[float] = []
        self.disk_write_mbps: list[float] = []
        self.disk_iops: list[float] = []
        self.disk_util: list[float] = []
        self.disk_await_ms: list[float] = []
        self.disk_aqu: list[float] = []
        self._last_disk = _read_diskstats(self._disk_devices)
        self._last_disk_t = time.monotonic()

        # role -> cgroup path, resolved once. CubeProxy's path contains a
        # container ID so it is rediscovered if the container restarts.
        self._cgroups: dict[str, str] = {}
        self._last_cg_usec: dict[str, int] = {}
        self._last_cg_t: float = time.monotonic()
        self._node_samples: list[dict] = []
        self._create_fails_noted = 0
        self._cube = None
        if cfg is not None:
            self._discover_cgroups()
            self._prime_cgroup_counters()
            try:
                from cube import client_from_config  # local import: avoid cycle
                self._cube = client_from_config(cfg, max_connections=4)
            except Exception:
                self._cube = None

    # ---------------- cgroup discovery ----------------
    def _discover_cgroups(self) -> None:
        for role, units in _CUBE_UNIT_CGROUPS.items():
            path = _find_unit_cgroup(units)
            if path:
                self._cgroups[role] = path
        # Cubelet relocates itself out of its unit cgroup, so prefer the
        # cgroup it actually lives in and only keep the unit as a fallback.
        # Verified by the presence of a live process rather than by path
        # existence: the unit cgroup exists either way, and picking it
        # yields a flat zero.
        for path in _CUBELET_CGROUPS:
            if _cgroup_has_procs(path):
                self._cgroups["cubelet"] = path
                break
        proxy = _find_proxy_cgroup()
        if proxy:
            self._cgroups["cube_proxy"] = proxy
        # The sandbox pool is two sibling trees; keep whichever exist.
        pools = [p for p in _SANDBOX_CGROUPS
                 if os.path.exists(os.path.join(p, "cpu.stat"))]
        if pools:
            self._cgroups["sandbox_fleet"] = ",".join(pools)
        if not self._cgroups:
            log.warning(
                "no CubeSandbox cgroups found under %s; control-plane CPU will "
                "be absent from results. Expected units like %s - check "
                "`systemctl status cube-sandbox-cube-api`.",
                _CGROUP_ROOT, next(iter(_CUBE_UNIT_CGROUPS.values()))[0],
            )

    def _role_usec(self, role: str) -> Optional[int]:
        """Cumulative CPU usec for a role, summing multi-path roles."""
        paths = self._cgroups.get(role)
        if not paths:
            return None
        total = 0
        seen = False
        for path in paths.split(","):
            usec = _cgroup_cpu_usec(path)
            if usec is not None:
                total += usec
                seen = True
        return total if seen else None

    def _prime_cgroup_counters(self) -> None:
        self._last_cg_usec = {}
        for role in self._cgroups:
            usec = self._role_usec(role)
            if usec is not None:
                self._last_cg_usec[role] = usec
        self._last_cg_t = time.monotonic()

    def note_create_fails(self, n: int) -> None:
        """Fold in sandbox-create failures counted by the load generator.

        Cube exposes no create-success/failure counter anywhere - not on
        CubeAPI, not in Cubelet's Prometheus surface, not in CubeOps - so
        the only place this can be known is the client that issued the
        calls. It matters because a run that failed to create sandboxes
        looks deceptively healthy: throughput drops but nothing else does.
        """
        self._create_fails_noted += max(0, int(n))

    def reset(self) -> None:
        """Discard samples collected so far without stopping the loop.

        Use this after a warmup phase: the sampler keeps running but the
        recorded series only covers the actual measurement window.
        """
        self.cpu_host.clear()
        self.cpu_orch.clear()
        self.mem_gb.clear()
        self.ctx_switches_per_s.clear()
        for series in self.cp_cores.values():
            series.clear()
        for series in self.cp_mem_gb.values():
            series.clear()
        self.vm_count.clear()
        self.disk_read_mbps.clear()
        self.disk_write_mbps.clear()
        self.disk_iops.clear()
        self.disk_util.clear()
        self.disk_await_ms.clear()
        self.disk_aqu.clear()
        self._last_disk = _read_diskstats(self._disk_devices)
        self._last_disk_t = time.monotonic()
        self._node_samples.clear()
        # Refresh delta counters so the next sample isn't artificially
        # large (catching up on whatever happened during warmup).
        self._last_ctx = psutil.cpu_stats().ctx_switches
        self._last_t = time.monotonic()
        self._last_idle, self._last_total = _read_cpu_totals()
        self._proc.cpu_percent(interval=None)
        if self.cfg is not None:
            # CubeProxy may have restarted during warmup, which would
            # leave a dead container scope cached.
            if "cube_proxy" not in self._cgroups or not os.path.exists(
                os.path.join(self._cgroups["cube_proxy"], "cpu.stat")
            ):
                proxy = _find_proxy_cgroup()
                if proxy:
                    self._cgroups["cube_proxy"] = proxy
            self._prime_cgroup_counters()

    def _sample_host_cpu(self) -> float:
        idle, total = _read_cpu_totals()
        d_idle = idle - self._last_idle
        d_total = total - self._last_total
        self._last_idle, self._last_total = idle, total
        if d_total <= 0:
            return 0.0
        return 100.0 * (1.0 - d_idle / d_total)

    def _sample_disk(self) -> None:
        """Aggregate IOPS / MB/s, and peak per-device utilisation."""
        now = _read_diskstats(self._disk_devices)
        t = time.monotonic()
        dt = t - self._last_disk_t
        if dt <= 0 or not now or not self._last_disk:
            self._last_disk, self._last_disk_t = now, t
            return

        r_ios = w_ios = 0
        r_sect = w_sect = 0
        svc_ms = 0
        util_peak = 0.0
        aqu = 0.0
        for dev, cur in now.items():
            prev = self._last_disk.get(dev)
            if prev is None:
                continue
            r_ios += max(0, cur.read_ios - prev.read_ios)
            r_sect += max(0, cur.read_sectors - prev.read_sectors)
            w_ios += max(0, cur.write_ios - prev.write_ios)
            w_sect += max(0, cur.write_sectors - prev.write_sectors)
            svc_ms += max(0, cur.read_ms - prev.read_ms) + \
                max(0, cur.write_ms - prev.write_ms)
            # One saturated device bounds the whole run, so take the peak
            # across devices rather than the mean.
            util_peak = max(
                util_peak,
                max(0, cur.io_ticks_ms - prev.io_ticks_ms) / (dt * 1000.0),
            )
            aqu += max(0, cur.weighted_ms - prev.weighted_ms) / (dt * 1000.0)

        ios = r_ios + w_ios
        self.disk_read_mbps.append(r_sect * _SECTOR_BYTES / dt / 1e6)
        self.disk_write_mbps.append(w_sect * _SECTOR_BYTES / dt / 1e6)
        self.disk_iops.append(ios / dt)
        self.disk_util.append(min(util_peak, 1.0))
        # Mean service time per I/O, and mean queue depth. For EBS these are
        # the signals that actually indicate throttling; see _disk_block().
        self.disk_await_ms.append((svc_ms / ios) if ios else 0.0)
        self.disk_aqu.append(aqu)
        self._last_disk, self._last_disk_t = now, t

    def _disk_block(self) -> dict:
        """Disk load plus an explicit saturation verdict."""
        if not self.disk_util:
            return {}
        out = {
            "devices": sorted(self._disk_devices),
            "read_mbps_avg": float(np.mean(self.disk_read_mbps)),
            "write_mbps_avg": float(np.mean(self.disk_write_mbps)),
            "write_mbps_p95": float(np.percentile(self.disk_write_mbps, 95)),
            "iops_avg": float(np.mean(self.disk_iops)),
            "iops_p95": float(np.percentile(self.disk_iops, 95)),
            "util_avg": float(np.mean(self.disk_util)),
            "util_p95": float(np.percentile(self.disk_util, 95)),
            "await_ms_avg": float(np.mean(self.disk_await_ms)),
            "await_ms_p95": float(np.percentile(self.disk_await_ms, 95)),
            "queue_depth_avg": float(np.mean(self.disk_aqu)),
            "queue_depth_p95": float(np.percentile(self.disk_aqu, 95)),
        }
        # Verdict.
        #
        # `util` alone is NOT a saturation signal for EBS. It only says the
        # device had at least one I/O in flight; a network-attached volume
        # serving many requests in parallel can sit at high util while nowhere
        # near its provisioned limit. Measured on a gp3 volume provisioned for
        # 10,000 IOPS: this workload showed 43-56% util at just ~740 IOPS, 7%
        # of the limit. Treating util>90% as "saturated" would cry wolf.
        #
        # What does indicate throttling is service time rising while
        # throughput stops rising - EBS queues rather than rejecting. So
        # require both a busy device and elevated latency. gp3 normally serves
        # in ~1ms; sustained double-digit await means the volume is the
        # constraint, not the workload.
        out["disk_saturated"] = bool(
            out["util_p95"] > 0.90 and out["await_ms_p95"] > 10.0
        )
        # Softer hint: worth a look even if not conclusive.
        out["disk_pressure"] = bool(
            out["await_ms_p95"] > 5.0 or out["queue_depth_p95"] > 8.0
        )
        return out

    def _sample_control_plane(self) -> None:
        """Cores used by each Cube component and by the sandbox fleet.

        All from cumulative cgroup counters, so nothing is lost to
        processes that lived and died between two samples - which is the
        normal case here, since a `fresh`-strategy rollout creates and
        destroys a shim in well under a second.
        """
        if not self._cgroups:
            return
        now = time.monotonic()
        dt = max(now - self._last_cg_t, 1e-6)
        for role in self._cgroups:
            usec = self._role_usec(role)
            if usec is None:
                continue
            prev = self._last_cg_usec.get(role)
            if prev is not None:
                # Slot reuse means the sandbox pool's counter can be reset
                # under us; clamp instead of reporting a negative core count.
                self.cp_cores.setdefault(role, []).append(
                    max(0.0, (usec - prev) / 1e6 / dt)
                )
            self._last_cg_usec[role] = usec
        self._last_cg_t = now

        # Memory is tracked for the sandbox pool *and* for Cubelet, because
        # on Cube the pool cgroup alone understates what a sandbox costs.
        # Cubelet is the process that maps guest memory and does the rootfs
        # I/O, so much of what a sandbox consumes is charged to Cubelet
        # rather than to the sandbox's own cgroup - measured with a single
        # 1 GiB sandbox live: pool 6.3 GB, Cubelet 16.6 GB. Reporting only
        # the pool would understate the memory ceiling, which is the number
        # that decides how many rollouts fit on a host.
        for role in ("sandbox_fleet", "cubelet"):
            paths = self._cgroups.get(role)
            if not paths:
                continue
            total = 0
            for path in paths.split(","):
                mem = _cgroup_memory_bytes(path)
                if mem is not None:
                    total += mem
            if total:
                self.cp_mem_gb.setdefault(role, []).append(total / (1024 ** 3))

        self.vm_count.append(_count_sandbox_shims())

    async def _loop(self) -> None:
        tick = 0
        while not self._stop.is_set():
            self.cpu_host.append(self._sample_host_cpu())
            self._sample_disk()
            if self.cfg is not None:
                self._sample_control_plane()
            # CubeOps aggregates node state from heartbeats, so a 1s poll
            # would mostly re-read the same snapshot. Every 5s is enough to
            # catch a capacity ceiling or an unhealthy node.
            if self._cube is not None and tick % 5 == 0:
                try:
                    snap = await self._cube.node_metrics()
                    if snap:
                        self._node_samples.append(snap)
                except Exception:
                    pass
            tick += 1
            # `cpu_percent(None)` returns process CPU% normalised to 100%
            # per core; divide by ncpu to get fraction-of-machine.
            self.cpu_orch.append(
                self._proc.cpu_percent(interval=None) / max(_NCPU, 1)
            )
            mem = psutil.virtual_memory()
            self.mem_gb.append((mem.total - mem.available) / (1024**3))
            now = time.monotonic()
            ctx = psutil.cpu_stats().ctx_switches
            dt = max(now - self._last_t, 1e-6)
            self.ctx_switches_per_s.append((ctx - self._last_ctx) / dt)
            self._last_ctx, self._last_t = ctx, now
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    # Components that make up "the control plane" for the saturation
    # verdict. The sandbox fleet is excluded by construction - that is the
    # workload, and it is supposed to be eating the machine.
    _CONTROL_ROLES = ("cube_proxy", "cube_api", "cube_master", "cubelet", "cube_ops")

    def _control_plane_block(self) -> dict:
        """Control-plane load plus an explicit saturation verdict."""
        if not any(self.cp_cores.values()) and not self._node_samples:
            return {}
        out: dict = {"cgroups": dict(self._cgroups)}
        for role, series in self.cp_cores.items():
            if not series:
                continue
            out[f"{role}_cores_avg"] = float(np.mean(series))
            out[f"{role}_cores_p95"] = float(np.percentile(series, 95))
        for role, series in self.cp_mem_gb.items():
            if series:
                out[f"{role}_mem_gb_max"] = float(np.max(series))

        control_cores = sum(
            out.get(f"{role}_cores_p95", 0.0) for role in self._CONTROL_ROLES
        )
        out["control_plane_cores_p95"] = control_cores

        if self.vm_count:
            # Direct read on live microVMs from the shim process count.
            # Excludes paused sandboxes, whose VM is shut down - the node
            # API count below includes them.
            out["sandbox_shims_max"] = int(max(self.vm_count))
            out["sandbox_shims_avg"] = float(np.mean(self.vm_count))

        if self._node_samples:
            last = self._node_samples[-1]
            out["node_id"] = last.get("node_id")
            out["node_healthy"] = last.get("healthy")
            out["node_unhealthy_reason"] = last.get("unhealthy_reason") or None
            out["node_instance_type"] = last.get("instance_type")
            out["max_sandboxes"] = last.get("max_sandboxes")
            for key in ("sandbox_count", "allocated_cpu_milli", "allocated_mem_mb",
                        "creates_in_flight"):
                vals = [s.get(key) for s in self._node_samples if s.get(key) is not None]
                if vals:
                    out[f"{key}_max"] = max(vals)
                    out[f"{key}_avg"] = float(np.mean(vals))
            if out.get("allocated_mem_mb_max") is not None:
                out["allocated_mem_gb_max"] = out["allocated_mem_mb_max"] / 1024.0

        # Cube publishes no create-success/failure counter, so this is the
        # tally the load generator handed us. Reported unconditionally: a
        # literal zero is a meaningful result, and distinguishing it from
        # "not measured" matters when reading a suspicious throughput dip.
        out["create_fails"] = int(self._create_fails_noted)

        # Verdict. CubeProxy carries every in-sandbox request (unless the
        # run used direct transport), and the remaining components sit on
        # the create/delete path, so once they collectively eat a large
        # share of the host the measurement is about the control plane
        # rather than the sandboxes. 25% is a deliberately conservative
        # trip wire: past it, treat the number as suspect, not as a result.
        share = control_cores / max(_NCPU, 1)
        out["control_plane_host_share_p95"] = round(share, 4)
        out["cube_proxy_host_share_p95"] = round(
            out.get("cube_proxy_cores_p95", 0.0) / max(_NCPU, 1), 4
        )
        # A node that hit its sandbox ceiling did not measure the
        # concurrency level we asked for, which is the same class of
        # invalidation as a saturated proxy.
        at_capacity = bool(
            out.get("max_sandboxes")
            and out.get("sandbox_count_max")
            and out["sandbox_count_max"] >= out["max_sandboxes"]
        )
        out["node_at_sandbox_capacity"] = at_capacity
        out["control_plane_saturated"] = bool(
            share > 0.25 or out["create_fails"] > 0 or at_capacity
        )
        return out

    async def stop(self) -> dict:
        self._stop.set()
        if self._task:
            await self._task
        if self._cube is not None:
            try:
                await self._cube.aclose()
            except Exception:
                pass
        control_plane = self._control_plane_block()
        disk = self._disk_block()
        return {
            # Host-level CPU - use this to answer "is the machine busy?"
            "cpu_util_avg": float(np.mean(self.cpu_host)) if self.cpu_host else 0.0,
            "cpu_util_p95": float(np.percentile(self.cpu_host, 95)) if self.cpu_host else 0.0,
            # Orchestrator-process CPU - use this to spot client-side
            # bottlenecks. If this is near 100% but cpu_util_avg is low,
            # the load generator is the limiting factor, not the worker.
            "orch_cpu_util_avg": float(np.mean(self.cpu_orch)) if self.cpu_orch else 0.0,
            "orch_cpu_util_p95": float(np.percentile(self.cpu_orch, 95)) if self.cpu_orch else 0.0,
            "mem_peak_gb": float(np.max(self.mem_gb)) if self.mem_gb else 0.0,
            "ctx_switch_per_sec_avg": float(np.mean(self.ctx_switches_per_s)) if self.ctx_switches_per_s else 0.0,
            "host_ncpu": _NCPU,
            # CubeSandbox control plane: was CubeProxy / CubeMaster /
            # Cubelet the limiting factor rather than the sandbox fleet?
            "control_plane": control_plane,
            # EBS: is the volume the limiting factor? These instances have
            # no instance store, so all sandbox I/O is EBS.
            "disk": disk,
        }
