"""Detect EC2 instance metadata via IMDSv2 (with sane fallback).

Beyond the instance type we also record CPU *topology*, because the
four instances this suite compares split into two very different
shapes at the same nominal vCPU count:

    m7i.metal-48xl / m8i.metal-48xl : 96 physical cores + SMT = 192 vCPU
    m8g.metal-48xl / m9g.metal-48xl : 192 physical cores, no SMT

For a workload that fans out into hundreds of single-vCPU microVMs that
distinction drives the result, so every per-vCPU number in the report
has to be read against it. Without `physical_cores` in the output it is
invisible.
"""

from __future__ import annotations

import glob
import os
import platform
import socket

import httpx

IMDS = "http://169.254.169.254"


def _cpu_topology() -> dict:
    """Physical core count / threads-per-core from sysfs.

    Uses `thread_siblings_list`, which is the authoritative view of
    which logical CPUs share a physical core and works identically on
    x86_64 and aarch64. Falls back to reporting logical CPUs only.
    """
    ncpu = os.cpu_count() or 1
    info: dict = {
        "vcpus": ncpu,
        "physical_cores": None,
        "threads_per_core": None,
        "smt": None,
    }
    try:
        groups: set[frozenset[int]] = set()
        for path in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/topology/thread_siblings_list"):
            with open(path) as f:
                raw = f.read().strip()
            sibs: set[int] = set()
            # Format is a mix of ranges and lists, e.g. "0,96" or "0-1".
            for part in raw.split(","):
                part = part.strip()
                if not part:
                    continue
                if "-" in part:
                    lo, _, hi = part.partition("-")
                    sibs.update(range(int(lo), int(hi) + 1))
                else:
                    sibs.add(int(part))
            if sibs:
                groups.add(frozenset(sibs))
        if groups:
            cores = len(groups)
            info["physical_cores"] = cores
            info["threads_per_core"] = round(ncpu / cores, 2)
            info["smt"] = cores < ncpu
    except Exception:
        pass
    return info


def _cpu_model() -> str:
    """Best-effort CPU model string, x86 and aarch64."""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                key, _, val = line.partition(":")
                key = key.strip().lower()
                # x86 exposes "model name"; aarch64 exposes
                # "CPU implementer" / "CPU part" instead.
                if key in ("model name", "cpu part"):
                    return val.strip()
    except Exception:
        pass
    return platform.processor() or "unknown"


async def detect_instance() -> dict:
    """Return instance identity + CPU topology.

    Falls back to local hostname / uname when not running on EC2.
    """
    info: dict = {
        "instance_type": "unknown",
        "instance_id": socket.gethostname(),
        "region": "unknown",
        "az": "unknown",
        "arch": platform.machine(),
        "kernel": platform.release(),
        "cpu_model": _cpu_model(),
    }
    info.update(_cpu_topology())

    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            tok = (
                await c.put(
                    f"{IMDS}/latest/api/token",
                    headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
                )
            ).text
            h = {"X-aws-ec2-metadata-token": tok}
            info["instance_type"] = (await c.get(f"{IMDS}/latest/meta-data/instance-type", headers=h)).text
            info["instance_id"] = (await c.get(f"{IMDS}/latest/meta-data/instance-id", headers=h)).text
            info["az"] = (await c.get(f"{IMDS}/latest/meta-data/placement/availability-zone", headers=h)).text
            info["region"] = info["az"][:-1] if info["az"] else "unknown"
    except Exception:
        # Not on EC2 or IMDS unreachable - keep fallback values
        pass

    return info
