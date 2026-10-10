#!/usr/bin/env python3
"""Extract the longer-episode (32-step) B1 confirmation points, both arches."""
import json, glob

REPO = "/home/ubuntu/sandbox-workload-on-cube"

def g(d, *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur

def newest_longep(arch, inst):
    # the long run is the newest codeexec dir (timestamp ~005xxx) with episode_steps=32
    dirs = sorted(glob.glob(f"{REPO}/results/{arch}/codeexec_result_{inst}.metal-24xl_20260929-*"))
    out = {}
    for d in dirs:
        for f in sorted(glob.glob(d + "/c*.json")):
            with open(f) as fh:
                j = json.load(fh)
            if g(j, "extra", "episode_steps") == 32:
                out[j["concurrency"]] = j
    return out

lines = []
def pr(s=""): lines.append(s)

pr("LONGER-EPISODE B1 (episode_steps=32, DURATION=90s) — realistic RL rollout")
pr("="*96)
c8 = newest_longep("aarch64", "c8g")
c7 = newest_longep("x86_64", "c7i")
hdr = f"{'c':>4} | {'thru c8g':>9} {'thru c7i':>9} {'ratio':>6} | {'crP50 8g':>8} {'crP50 7i':>8} | {'stepP50 8g':>10} {'stepP50 7i':>10} | {'prov% 8g':>8} {'prov% 7i':>8} | {'cpcore 8g':>9} {'cpcore 7i':>9} | {'sat':>4} | {'$/1k 8g':>8} {'$/1k 7i':>8} | {'cost adv':>8}"
pr(hdr); pr("-"*len(hdr))
for c in sorted(set(c8)&set(c7)):
    a, b = c8[c], c7[c]
    ta = g(a,"throughput","executions_per_sec"); tb = g(b,"throughput","executions_per_sec")
    cra = g(a,"latency_ms","sandbox","sandbox_create","p50"); crb = g(b,"latency_ms","sandbox","sandbox_create","p50")
    sa = g(a,"latency_ms","sandbox","steps","p50"); sb = g(b,"latency_ms","sandbox","steps","p50")
    pa = g(a,"extra","sandbox","provisioning_share_p50"); pb = g(b,"extra","sandbox","provisioning_share_p50")
    cpa = g(a,"resource","control_plane","control_plane_cores_p95"); cpb = g(b,"resource","control_plane","control_plane_cores_p95")
    sata = g(a,"resource","control_plane","control_plane_saturated"); satb = g(b,"resource","control_plane","control_plane_saturated")
    ka = g(a,"cost","cost_per_1k_units_usd"); kb = g(b,"cost","cost_per_1k_units_usd")
    costadv = (kb-ka)/kb*100 if ka and kb else float('nan')  # positive = c8g cheaper per unit
    pr(f"{c:>4} | {ta:>9.1f} {tb:>9.1f} {ta/tb:>5.2f}x | {cra:>8.1f} {crb:>8.1f} | {sa:>10.1f} {sb:>10.1f} | {pa*100:>7.1f}% {pb*100:>7.1f}% | {cpa:>9.1f} {cpb:>9.1f} | {('Y' if sata else 'n')+'/'+('Y' if satb else 'n'):>4} | {ka:>8.4f} {kb:>8.4f} | {costadv:>7.1f}%")
pr()
pr("prov% = provisioning_share_p50 (fraction of rollout wall time spent creating the sandbox).")
pr("cost adv = how much cheaper c8g is per 1k units (positive = c8g cheaper).")
txt="\n".join(lines)
open("/tmp/long_analysis.txt","w").write(txt+"\n")
print(txt)
