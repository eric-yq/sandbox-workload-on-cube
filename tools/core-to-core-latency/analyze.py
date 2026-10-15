#!/usr/bin/env python3
"""
analyze.py - summarise the matrices collected by run.sh.

    python3 analyze.py <outdir>

For every machine found in <outdir> it prints the latency distribution (SMT
siblings split out, read from the recorded sibling map rather than assumed),
a block-median heatmap that exposes the fabric structure, and - when the
Rust tool was run too - a per-pair agreement check between the methods.
Finally it compares the machines against each other, per method.

Only the standard library is used.
"""
import csv
import glob
import os
import statistics as st
import sys


# ---------------------------------------------------------------- loading
def load_c(path):
    """This repo's c2c.c: a,b,one_way_ns."""
    pairs = {}
    n = 0
    with open(path) as f:
        for r in csv.DictReader(f):
            a, b = int(r["a"]), int(r["b"])
            pairs[(min(a, b), max(a, b))] = float(r["one_way_ns"])
            n = max(n, a + 1, b + 1)
    return pairs, n


def load_tool(path, n):
    """
    core-to-core-latency --csv emits a LOWER-TRIANGULAR matrix: data row i
    carries columns 0..i-1. When stderr was merged into the same file the
    pretty table is interleaved, so rows are identified by comma count.
    """
    rows = [ln.rstrip("\n").split(",") for ln in open(path) if ln.count(",") >= 5]
    pairs = {}
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            cell = cell.strip()
            if not cell or j >= i:
                continue
            try:
                pairs[(j, i)] = float(cell)
            except ValueError:
                pass
    return pairs


def load_siblings(path, n):
    """cpu -> set of its SMT siblings (excluding itself)."""
    sib = {c: set() for c in range(n)}
    if not os.path.exists(path):
        return sib
    for ln in open(path):
        parts = ln.split()
        if len(parts) != 2:
            continue
        try:
            cpu = int(parts[0])
        except ValueError:
            continue
        peers = set()
        for tok in parts[1].split(","):
            tok = tok.strip()
            if "-" in tok:
                lo, hi = tok.split("-")[:2]
                peers.update(range(int(lo), int(hi) + 1))
            elif tok:
                peers.add(int(tok))
        peers.discard(cpu)
        if cpu in sib:
            sib[cpu] = peers
    return sib


# ---------------------------------------------------------------- stats
def q(v, p):
    v = sorted(v)
    if not v:
        return float("nan")
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def line(label, v):
    if not v:
        print("  %-32s (none)" % label)
        return
    print("  %-32s n=%-5d min=%6.1f  p25=%6.1f  med=%6.1f  p75=%6.1f  max=%6.1f"
          % (label, len(v), min(v), q(v, .25), st.median(v), q(v, .75), max(v)))


def split(pairs, sib):
    same, diff = [], []
    for (a, b), v in pairs.items():
        (same if b in sib.get(a, ()) else diff).append(v)
    return same, diff


def heat(pairs, n, block, label):
    nb = max(1, n // block)
    print("\n  %s (block median, %d-core blocks)" % (label, block))
    print("      " + "".join("%6d" % (j * block) for j in range(nb)))
    for i in range(nb):
        cells = []
        for j in range(nb):
            v = [pairs[(min(a, b), max(a, b))]
                 for a in range(i * block, min((i + 1) * block, n))
                 for b in range(j * block, min((j + 1) * block, n))
                 if a != b and (min(a, b), max(a, b)) in pairs]
            cells.append("%6.0f" % st.median(v) if v else "     -")
        print("%6d%s" % (i * block, "".join(cells)))


def pearson(xy):
    xs = [x for x, _ in xy]
    ys = [y for _, y in xy]
    mx, my = st.mean(xs), st.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in xy)
    den = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** .5
    return num / den if den else float("nan")


# ---------------------------------------------------------------- main
def main(outdir):
    labels = sorted(os.path.basename(p)[:-len(".c2c.csv")]
                    for p in glob.glob(os.path.join(outdir, "*.c2c.csv")))
    if not labels:
        print("no *.c2c.csv found in %s" % outdir)
        return 1

    machines = {}
    for lab in labels:
        c, n = load_c(os.path.join(outdir, lab + ".c2c.csv"))
        sib = load_siblings(os.path.join(outdir, lab + ".siblings.txt"), n)
        m = {"n": n, "sib": sib, "methods": {"c-pingpong": c}}
        for b, name in ((1, "tool-b1 CAS 1 line"), (2, "tool-b2 2 lines")):
            p = os.path.join(outdir, "%s.tool-b%d.csv" % (lab, b))
            if os.path.exists(p):
                t = load_tool(p, n)
                if t:
                    m["methods"][name] = t
        machines[lab] = m

    for lab, m in machines.items():
        print("=" * 88)
        meta = os.path.join(outdir, lab + ".meta.txt")
        print(lab + ("   (%s)" % open(meta).readline().strip() if os.path.exists(meta) else ""))
        print("=" * 88)
        smt = any(m["sib"].get(c) for c in range(m["n"]))
        print("  cpus=%d  SMT=%s" % (m["n"], "yes" if smt else "no"))
        print("\n  one-way latency, ns")
        for name, pairs in m["methods"].items():
            same, diff = split(pairs, m["sib"])
            if smt:
                line(name + "  [SMT sibling]", same)
            line(name + ("  [diff core]" if smt else "  [all pairs]"), diff)

        for name, pairs in m["methods"].items():
            heat(pairs, m["n"], 12, name)

        if len(m["methods"]) > 1:
            print("\n  per-pair agreement between methods (different physical cores)")
            names = list(m["methods"])
            base = names[0]
            for other in names[1:]:
                xy = [(m["methods"][base][k], m["methods"][other][k])
                      for k in m["methods"][base]
                      if k in m["methods"][other] and k[1] not in m["sib"].get(k[0], ())]
                if len(xy) < 10:
                    continue
                d = [y - x for x, y in xy]
                print("    %-22s vs %-22s n=%-5d r=%+.3f  delta med=%+.1f ns p95=%+.1f ns"
                      % (other, base, len(xy), pearson(xy), st.median(d), q(d, .95)))
        print()

    if len(machines) > 1:
        print("=" * 88)
        print("machine comparison - median over DIFFERENT-physical-core pairs, ns")
        print("=" * 88)
        all_methods = []
        for m in machines.values():
            for name in m["methods"]:
                if name not in all_methods:
                    all_methods.append(name)
        labs = list(machines)
        ratios = []
        print("  %-24s%s" % ("method", "".join("%14s" % l[:13] for l in labs)))
        for name in all_methods:
            cells, meds = [], []
            for l in labs:
                mm = machines[l]
                if name in mm["methods"]:
                    _, diff = split(mm["methods"][name], mm["sib"])
                    v = st.median(diff)
                    meds.append(v)
                    cells.append("%14.1f" % v)
                else:
                    meds.append(None)
                    cells.append("%14s" % "-")
            print("  %-24s%s" % (name, "".join(cells)))
            if len(meds) == 2 and all(x is not None for x in meds):
                ratios.append((name, meds[0], meds[1]))

        if ratios:
            print()
            print("  ratio, per method:")
            for name, a, b in ratios:
                verdict = ("%s slower" % labs[1]) if b > a else ("%s FASTER" % labs[1])
                print("    %-24s %s / %s = %.2fx   (%s)"
                      % (name, labs[1], labs[0], b / a, verdict))
            signs = {b > a for _, a, b in ratios}
            if len(signs) > 1:
                print()
                print("  The ranking FLIPS between methods. That is a real property of the")
                print("  two fabrics, not noise - see README.md, 'why the ranking flips'.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
