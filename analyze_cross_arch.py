#!/usr/bin/env python3
"""Cross-arch comparison: c8g (aarch64) vs c7i (x86_64) on CubeSandbox v0.7.2.
Walks the 20260929 result trees and prints an honest per-benchmark table.
"""
import json, glob, os

REPO = "/home/ubuntu/sandbox-workload-on-cube"
BENCHES = [("B1", "codeexec"), ("B3", "toolcall"), ("B4", "browser"), ("B8", "sandboxlife")]
ARCHES = [("c8g", "aarch64"), ("c7i", "x86_64")]
STAMP = "20260929"


def load_points(prefix, inst, arch):
    """Return {concurrency: point_dict} for one benchmark on one arch."""
    pat = f"{REPO}/results/{arch}/{prefix}_result_{inst}.metal-24xl_{STAMP}-*/c*.json"
    out = {}
    for f in sorted(glob.glob(pat)):
        with open(f) as fh:
            d = json.load(fh)
        out[d["concurrency"]] = d
    return out


def prim_thru(d):
    t = d["throughput"]
    k = next(iter(t))  # primary unit is first key
    return k, t[k]


def g(d, *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


lines = []
def pr(s=""):
    lines.append(s)


pr("=" * 100)
pr("CROSS-ARCH: c8g.metal-24xl (Graviton4, aarch64) vs c7i.metal-24xl (Intel, x86_64)")
pr("CubeSandbox v0.7.2, identical tarball install. Prices: c8g $3.82848/hr, c7i $4.284/hr.")
pr("=" * 100)

for bid, prefix in BENCHES:
    c8 = load_points(prefix, "c8g", "aarch64")
    c7 = load_points(prefix, "c7i", "x86_64")
    concs = sorted(set(c8) | set(c7))
    pr()
    pr(f"### {bid} ({prefix}) ###")
    # header
    hdr = (f"{'conc':>5} | {'unit':<22} | {'c8g thru':>10} {'c7i thru':>10} {'c8g/c7i':>8} | "
           f"{'crP50 c8g':>9} {'crP50 c7i':>9} | {'crP95 c8g':>9} {'crP95 c7i':>9} | "
           f"{'cp_core c8g':>11} {'cp_core c7i':>11} | {'sat c8g/c7i':>12} | "
           f"{'$/1k c8g':>9} {'$/1k c7i':>9}")
    pr(hdr)
    pr("-" * len(hdr))
    for c in concs:
        a = c8.get(c); b = c7.get(c)
        if not a or not b:
            pr(f"{c:>5} | (missing on one arch)")
            continue
        ua, ta = prim_thru(a); ub, tb = prim_thru(b)
        ratio = ta / tb if tb else float('nan')
        cra = g(a, "latency_ms", "sandbox", "sandbox_create", "p50")
        crb = g(b, "latency_ms", "sandbox", "sandbox_create", "p50")
        cra95 = g(a, "latency_ms", "sandbox", "sandbox_create", "p95")
        crb95 = g(b, "latency_ms", "sandbox", "sandbox_create", "p95")
        cpa = g(a, "resource", "control_plane", "control_plane_cores_p95")
        cpb = g(b, "resource", "control_plane", "control_plane_cores_p95")
        sata = g(a, "resource", "control_plane", "control_plane_saturated")
        satb = g(b, "resource", "control_plane", "control_plane_saturated")
        fa = g(a, "resource", "control_plane", "create_fails")
        fb = g(b, "resource", "control_plane", "create_fails")
        ka = g(a, "cost", "cost_per_1k_units_usd")
        kb = g(b, "cost", "cost_per_1k_units_usd")

        def fnum(x, w=9, p=1):
            return f"{x:>{w}.{p}f}" if isinstance(x, (int, float)) else f"{'-':>{w}}"

        satstr = f"{'Y' if sata else 'n'}/{'Y' if satb else 'n'}"
        pr(f"{c:>5} | {ua:<22} | {fnum(ta,10,1)} {fnum(tb,10,1)} {ratio:>7.2f}x | "
           f"{fnum(cra)} {fnum(crb)} | {fnum(cra95)} {fnum(crb95)} | "
           f"{fnum(cpa,11,1)} {fnum(cpb,11,1)} | {satstr:>12} | "
           f"{fnum(ka,9,4)} {fnum(kb,9,4)}")
    # note create fails on saturated points
    for c in concs:
        a = c8.get(c); b = c7.get(c)
        if a and g(a, "resource","control_plane","create_fails"):
            pr(f"      note: c8g c={c} create_fails={g(a,'resource','control_plane','create_fails')}")
        if b and g(b, "resource","control_plane","create_fails"):
            pr(f"      note: c7i c={c} create_fails={g(b,'resource','control_plane','create_fails')}")

pr()
pr("=" * 100)
pr("Legend: crP50/P95 = sandbox_create latency ms; cp_core = control_plane_cores_p95 (host cores burned by")
pr("        CubeSandbox control plane); sat = control_plane_saturated (Y = point is suspect / not a clean")
pr("        workload measure); $/1k = cost_per_1k_units_usd (lower better).")
pr("=" * 100)

txt = "\n".join(lines)
with open("/tmp/cross_arch.txt", "w") as f:
    f.write(txt + "\n")
print(txt)
