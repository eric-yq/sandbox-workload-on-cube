"""Agentic-RL Sandbox Benchmark on CubeSandbox - main orchestrator.

Runs as a host process on a bare-metal instance, not in a container: it
needs all of the host's vCPUs to drive load, it reads host CPU stats and
the CubeSandbox component cgroups directly, and in `direct` transport it
dials sandbox IPs that only exist in the host network namespace.

Workflow:
  1. Detect instance identity + CPU topology
  2. For each runner not in SKIP:
       - warmup: resolve its template, boot one sandbox, smoke-test it
       - sweep its own concurrency list; each level runs `duration_sec` of
         rollouts, one dedicated microVM per rollout
       - reap leaked sandboxes, cooldown
  3. Aggregate all per-(benchmark, concurrency) JSON into summary.json
  4. Render the HTML report

Concurrency means "simultaneously live rollout sandboxes" here, not
in-flight HTTP requests against a shared worker. See
`orchestrator/rollout.py` for why the rollout is the isolation boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

# Use uvloop where available - the default asyncio event loop tops out
# around 50-70k req/s per process; uvloop typically doubles that and
# directly translates to higher CPU utilisation when many tiny HTTP
# requests are in flight (B3 / B7 / B9 workloads).
try:
    import uvloop  # type: ignore
    uvloop.install()
except ImportError:
    pass

# Configure logging BEFORE importing runners. The runners package
# instantiates Runner objects at import time (see runners/__init__.py),
# which triggers e.g. B1's corpus load and its INFO log line. If we
# configure logging after the import, those startup messages get
# discarded by the default last-resort handler.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# httpx logs every request at INFO regardless of status (e.g.
# `HTTP Request: POST ... "HTTP/1.1 200 OK"`). At benchmark concurrency
# this floods the log. Errors are surfaced via response.raise_for_status()
# in the runners themselves, so suppress httpx's own access lines.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from config import CFG, Config
from cube import client_from_config
from instance_meta import detect_instance
from runners import ALL_RUNNERS
from runners.base import BenchmarkResult, hourly_price
from s3_uploader import ResultStore

log = logging.getLogger("orchestrator")


def _extract_p99(latency: dict) -> float:
    """Pull a primary P99 number out of the polymorphic latency block.

    Different runners use different shapes:
      - B1/B3/B4: flat dict {p50, p95, p99, ...}
      - B5:       {"trajectory": {p99,...}, "per_query": {...}}
      - B9:       {"rollout":    {p99,...}, "per_task": {...}}
    """
    if not isinstance(latency, dict):
        return 0.0
    if "p99" in latency:
        return latency.get("p99") or 0.0
    for key in ("rollout", "trajectory"):
        sub = latency.get(key)
        if isinstance(sub, dict) and "p99" in sub:
            return sub.get("p99") or 0.0
    return 0.0


async def _reap_sandboxes(cfg: Config, label: str) -> None:
    """Delete any benchmark sandbox still alive between measurement points."""
    client = client_from_config(cfg, max_connections=16)
    try:
        n = await client.delete_all()
        if n:
            log.info("[%s] reaped %d leftover sandbox(es)", label, n)
    except Exception as e:
        log.debug("[%s] reap failed: %s", label, e)
    finally:
        await client.aclose()


async def run_benchmark(
    bid: str, cfg: Config, instance: dict, store: ResultStore
) -> list[dict]:
    runner = ALL_RUNNERS[bid]
    log.info("=== %s (%s) starting ===", bid, runner.workload)

    log.info("[%s] warmup ...", bid)
    try:
        await runner.warmup(cfg)
    except Exception as e:
        log.error("[%s] warmup failed: %s - skipping", bid, e)
        return []

    results: list[dict] = []
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = (
        f"{runner.workload}_result_{instance['instance_type']}_{ts}"
    )

    # Concurrency now means "simultaneously live rollout sandboxes", and
    # the memory cost of one sandbox differs by an order of magnitude
    # across benchmarks on a 768 GiB box - a B1 sandbox is ~512 MiB while
    # a B9 sandbox carries all five stacks. So every benchmark gets its
    # own sweep (see Config.concurrencies_for), rather than the old
    # global list with two special cases bolted on.
    concurrencies = cfg.concurrencies_for(bid)
    duration = cfg.duration_for(bid)

    for c in concurrencies:
        log.info("[%s] concurrency=%d duration=%ds", bid, c, duration)
        try:
            res: BenchmarkResult = await runner.run_one(cfg, instance, c)
        except Exception as e:
            log.error("[%s] run failed at c=%d: %s", bid, c, e)
            continue
        d = res.to_dict()
        results.append(d)

        rel = f"{instance['arch']}/{out_dir}/c{c:04d}.json"
        store.write_json(rel, d)
        log.info(
            "[%s] c=%d throughput=%s p99=%.1fms",
            bid, c,
            list(d["throughput"].items())[0] if d["throughput"] else "?",
            _extract_p99(d["latency_ms"]),
        )

        # Surface the two things that invalidate a data point, loudly and
        # at the moment they happen rather than only in the JSON.
        cp = (d.get("resource") or {}).get("control_plane") or {}
        if cp.get("control_plane_saturated"):
            reasons = []
            if (cp.get("control_plane_host_share_p95") or 0.0) > 0.25:
                reasons.append(
                    f"control plane used {cp.get('control_plane_cores_p95') or 0.0:.1f} "
                    f"cores ({100.0 * (cp.get('control_plane_host_share_p95') or 0.0):.0f}% "
                    f"of host), of which CubeProxy "
                    f"{cp.get('cube_proxy_cores_p95') or 0.0:.1f}"
                )
            if cp.get("create_fails"):
                reasons.append(f"{cp['create_fails']} sandbox creates failed")
            if cp.get("node_at_sandbox_capacity"):
                reasons.append(
                    f"node hit its sandbox ceiling "
                    f"({cp.get('sandbox_count_max')}/{cp.get('max_sandboxes')})"
                )
            log.warning(
                "[%s] c=%d CONTROL PLANE SATURATED: %s. This point measures "
                "CubeSandbox rather than the sandbox workload - treat it as "
                "suspect. CUBE_TRANSPORT=direct takes CubeProxy off the request "
                "path; node_max_mvm_num in CubeMaster/conf.yaml raises the "
                "sandbox ceiling.",
                bid, c, "; ".join(reasons) or "see resource.control_plane",
            )
        disk = (d.get("resource") or {}).get("disk") or {}
        if disk.get("disk_saturated"):
            log.warning(
                "[%s] c=%d DISK SATURATED: %s at %.1f ms await p95 (queue depth "
                "%.1f, %.0f IOPS p95, %.0f MB/s writes p95, %.0f%% util). These "
                "instances have no instance store, so this is the EBS volume "
                "behind /data/cubelet. A gp3 volume's *baseline* is only 3,000 "
                "IOPS - provision it (a single gp3 goes to 80,000 IOPS / "
                "2,000 MiB/s).",
                bid, c, ",".join(disk.get("devices", [])) or "?",
                disk.get("await_ms_p95") or 0.0,
                disk.get("queue_depth_p95") or 0.0,
                disk.get("iops_p95") or 0.0,
                disk.get("write_mbps_p95") or 0.0,
                100.0 * (disk.get("util_p95") or 0.0),
            )
        elif disk.get("disk_pressure"):
            log.info(
                "[%s] c=%d disk under pressure but not saturated: await p95 "
                "%.1f ms, queue depth p95 %.1f, %.0f IOPS p95. Worth watching if "
                "you raise concurrency.",
                bid, c, disk.get("await_ms_p95") or 0.0,
                disk.get("queue_depth_p95") or 0.0, disk.get("iops_p95") or 0.0,
            )

        sandbox_extra = (d.get("extra") or {}).get("sandbox") or {}
        share = sandbox_extra.get("provisioning_share_p50")
        if isinstance(share, (int, float)) and share > 0.5:
            log.warning(
                "[%s] c=%d sandbox provisioning is %.0f%% of rollout wall time. "
                "The episode is too short to amortise it; raise the per-benchmark "
                "episode length so the workload dominates.",
                bid, c, 100.0 * share,
            )

        # Reap anything the run leaked before the next level. A leaked
        # sandbox keeps its CPU/memory reservation, which would quietly
        # shift the next data point.
        await _reap_sandboxes(cfg, bid)

        if cfg.cooldown_sec > 0:
            log.info("[%s] cooldown %ds", bid, cfg.cooldown_sec)
            await asyncio.sleep(cfg.cooldown_sec)

    # Per-benchmark roll-up
    rollup = {
        "benchmark": bid,
        "workload": runner.workload,
        "instance": instance,
        "runs": results,
    }
    store.write_json(f"{instance['arch']}/{out_dir}/rollup.json", rollup)
    log.info("=== %s done, %d concurrency points ===", bid, len(results))
    return results


# Per-benchmark labels for the report. Each entry says which key to
# pull out of the throughput dict and how to spell it. We deliberately
# keep ops/s out of the label - "ops" hides what's actually being
# counted (a code-exec is very different from a 30-step rollout).
PRIMARY_UNITS = {
    "B1": {"tput_key": "executions_per_sec",      "unit": "exec/s",     "title": "Code Execution"},
    "B3": {"tput_key": "trajectories_per_sec",    "unit": "traj/s",     "title": "Tool Call"},
    "B4": {"tput_key": "trajectories_per_sec",    "unit": "traj/s",     "title": "Browser"},
    "B5": {"tput_key": "queries_per_sec",         "unit": "queries/s",  "title": "SQL Exec (TPC-H)"},
    "B7": {"tput_key": "episodes_per_sec",        "unit": "ep/s",       "title": "Text Game"},
    "B8": {"tput_key": "sandbox_creates_per_sec", "unit": "creates/s",  "title": "microVM Lifecycle"},
    "B9": {"tput_key": "rollouts_per_sec",        "unit": "rollouts/s", "title": "Concurrent Rollout"},
}


def _primary_throughput(bid: str, throughput: dict) -> tuple[float, str]:
    """Return (value, unit_label) for the row's primary throughput.

    Falls back to the first entry of `throughput` if `bid` isn't in
    the table or the expected key is missing - avoids silently zeroing
    out throughput when a runner is added without updating PRIMARY_UNITS.
    """
    if not isinstance(throughput, dict) or not throughput:
        return 0.0, "ops/s"
    info = PRIMARY_UNITS.get(bid)
    if info and info["tput_key"] in throughput:
        return throughput[info["tput_key"]] or 0.0, info["unit"]
    # Fallback: first numeric value, generic label.
    k, v = next(iter(throughput.items()))
    return (v or 0.0), "ops/s"


def render_html(summary: dict, out_path: Path) -> None:
    """Minimal report - main table + per-benchmark sub-task sections.

    Improvements over a flat ops/s view:
      1. Throughput cell shows the right unit per benchmark (exec/s,
         traj/s, queries/s, rollouts/s, ...) so the same number isn't
         mis-read across rows.
      2. B5 emits a per-query (Q01..Q22) latency table for each
         concurrency level. B9 emits a per-task (B1/B3/B4/B5/B7)
         latency table and ops breakdown for each concurrency level.
         Without these, the sub-task data the runners already collect
         is invisible from the report.
    """

    def cell(v, fmt=".2f"):
        if v is None:
            return "<td>-</td>"
        return f"<td>{v:{fmt}}</td>"

    # ----- main table -----
    main_rows: list[str] = []
    for bid, runs in summary["benchmarks"].items():
        title = PRIMARY_UNITS.get(bid, {}).get("title", "")
        bid_label = f"{bid}<br><span style='color:#888;font-size:.8em'>{title}</span>" if title else bid
        for r in runs:
            tput, unit = _primary_throughput(bid, r["throughput"])
            p99 = _extract_p99(r["latency_ms"])
            cpu_avg = r["resource"].get("cpu_util_avg")
            cost_1k = r["cost"].get("cost_per_1k_units_usd")
            main_rows.append(
                f"<tr><td style='text-align:left'>{bid_label}</td>"
                f"<td>{r['concurrency']}</td>"
                f"<td>{tput:,.2f} <span style='color:#888;font-size:.85em'>{unit}</span></td>"
                f"{cell(p99, '.1f')}"
                f"{cell(cpu_avg, '.1f')}"
                f"{cell(cost_1k, '.4f')}"
                "</tr>"
            )
    body_rows = "".join(main_rows)

    # ----- B5 per-query sub-table per concurrency -----
    def render_per_query_section(bid: str, runs: list[dict]) -> str:
        if not runs:
            return ""
        if not any(isinstance(r.get("latency_ms"), dict)
                   and r["latency_ms"].get("per_query") for r in runs):
            return ""
        out = [f"<h2>{bid} - per-query latency (TPC-H Q01..Q22)</h2>"]
        for r in runs:
            pq = (r.get("latency_ms") or {}).get("per_query") or {}
            if not pq:
                continue
            out.append(f"<h3 style='font-size:1rem;margin-top:1rem'>c={r['concurrency']}</h3>")
            out.append("<table>")
            out.append(
                "<tr><th style='text-align:left'>Query</th><th>count</th>"
                "<th>p50 (ms)</th><th>p95 (ms)</th><th>p99 (ms)</th>"
                "<th>mean (ms)</th><th>max (ms)</th></tr>"
            )
            # sort by query id (keys like Q01..Q22)
            for qkey in sorted(pq.keys()):
                row = pq[qkey] or {}
                out.append(
                    f"<tr><td style='text-align:left'>{qkey}</td>"
                    f"<td>{int(row.get('count', 0))}</td>"
                    f"{cell(row.get('p50'), '.1f')}"
                    f"{cell(row.get('p95'), '.1f')}"
                    f"{cell(row.get('p99'), '.1f')}"
                    f"{cell(row.get('mean'), '.1f')}"
                    f"{cell(row.get('max'), '.1f')}"
                    "</tr>"
                )
            out.append("</table>")
        return "\n".join(out)

    # ----- B9 per-task + ops breakdown sub-tables per concurrency -----
    def render_per_task_section(bid: str, runs: list[dict]) -> str:
        if not runs:
            return ""
        if not any(isinstance(r.get("latency_ms"), dict)
                   and r["latency_ms"].get("per_task") for r in runs):
            return ""
        out = [f"<h2>{bid} - per-task latency &amp; ops mix</h2>"]
        for r in runs:
            pt = (r.get("latency_ms") or {}).get("per_task") or {}
            if not pt:
                continue
            extra = r.get("extra") or {}
            ops_break = extra.get("ops_breakdown") or {}
            mix = extra.get("task_mix") or {}
            avg_steps = extra.get("avg_steps_per_rollout")

            header_bits = [f"c={r['concurrency']}"]
            if avg_steps is not None:
                header_bits.append(f"avg steps/rollout={avg_steps}")
            out.append(
                f"<h3 style='font-size:1rem;margin-top:1rem'>{' &middot; '.join(header_bits)}</h3>"
            )

            # per-task latency table (one row per sub-benchmark)
            out.append("<table>")
            out.append(
                "<tr><th style='text-align:left'>Sub-task</th>"
                "<th>weight</th><th>ops</th><th>count</th>"
                "<th>p50 (ms)</th><th>p95 (ms)</th><th>p99 (ms)</th>"
                "<th>mean (ms)</th><th>max (ms)</th></tr>"
            )
            for tkey in sorted(pt.keys()):
                row = pt[tkey] or {}
                w = mix.get(tkey)
                ops = ops_break.get(tkey, row.get("ops"))
                w_cell = f"{w:.2f}" if isinstance(w, (int, float)) else "-"
                ops_cell = f"{int(ops)}" if ops is not None else "-"
                out.append(
                    f"<tr><td style='text-align:left'>{tkey} "
                    f"<span style='color:#888;font-size:.85em'>"
                    f"{PRIMARY_UNITS.get(tkey, {}).get('title', '')}</span></td>"
                    f"<td>{w_cell}</td>"
                    f"<td>{ops_cell}</td>"
                    f"<td>{int(row.get('count', 0))}</td>"
                    f"{cell(row.get('p50'), '.1f')}"
                    f"{cell(row.get('p95'), '.1f')}"
                    f"{cell(row.get('p99'), '.1f')}"
                    f"{cell(row.get('mean'), '.1f')}"
                    f"{cell(row.get('max'), '.1f')}"
                    "</tr>"
                )
            out.append("</table>")
        return "\n".join(out)

    # ----- rollout lifecycle breakdown -----
    def render_sandbox_section() -> str:
        """How much of each rollout was provisioning rather than work.

        This is the question that decides whether a per-rollout microVM
        sandbox tier is viable, so it gets its own table rather than being
        buried in the JSON.
        """
        rows: list[str] = []
        for bid, runs in summary["benchmarks"].items():
            for r in runs:
                sb = (r.get("latency_ms") or {}).get("sandbox") or {}
                if not sb:
                    continue
                ex = (r.get("extra") or {}).get("sandbox") or {}
                create = sb.get("sandbox_create") or {}
                steps = sb.get("steps") or {}
                release = sb.get("sandbox_release") or {}
                share = ex.get("provisioning_share_p50")
                share_cell = f"{100.0 * share:.1f}%" if isinstance(share, (int, float)) else "-"
                rows.append(
                    f"<tr><td style='text-align:left'>{bid}</td>"
                    f"<td>{r['concurrency']}</td>"
                    f"<td style='text-align:left'>{ex.get('strategy', '-')}</td>"
                    f"{cell(create.get('p50'), '.1f')}{cell(create.get('p99'), '.1f')}"
                    f"{cell(steps.get('p50'), '.1f')}"
                    f"{cell(release.get('p50'), '.1f')}"
                    f"<td>{share_cell}</td>"
                    "</tr>"
                )
        if not rows:
            return ""
        return (
            "<h2>Rollout lifecycle - provisioning vs work</h2>"
            "<p class='meta'>One microVM per rollout. <code>create</code> is "
            "create-until-first-request-accepted, i.e. how long before a trainer "
            "can dispatch a tool call. A high provisioning share means the "
            "episode is too short to amortise the sandbox.</p>"
            "<table><tr><th style='text-align:left'>Benchmark</th><th>Concurrency</th>"
            "<th style='text-align:left'>Strategy</th>"
            "<th>create p50</th><th>create p99</th><th>steps p50</th>"
            "<th>release p50</th><th>provisioning share</th></tr>"
            + "".join(rows) + "</table>"
        )

    # ----- control-plane health -----
    def render_control_plane_section() -> str:
        """Whether we measured the sandboxes or measured CubeSandbox.

        This table is the guard rail on the whole result set: a saturated
        control plane makes a cross-architecture comparison meaningless,
        and silently so. CubeProxy is called out separately because unless
        the run used direct transport, every in-sandbox request traverses
        it, so it is the component most likely to become the ceiling.
        """
        rows: list[str] = []
        any_saturated = False
        for bid, runs in summary["benchmarks"].items():
            for r in runs:
                cp = (r.get("resource") or {}).get("control_plane") or {}
                if not cp:
                    continue
                sat = bool(cp.get("control_plane_saturated"))
                any_saturated = any_saturated or sat
                flag = ("<b style='color:#b00'>SATURATED</b>" if sat
                        else "<span style='color:#080'>ok</span>")
                share = cp.get("control_plane_host_share_p95")
                share_cell = (
                    f"<td>{100.0 * share:.1f}%</td>"
                    if isinstance(share, (int, float)) else "<td>-</td>"
                )
                sandboxes = cp.get("sandbox_count_max")
                ceiling = cp.get("max_sandboxes")
                sandbox_cell = (
                    f"<td>{sandboxes}{f' / {ceiling}' if ceiling else ''}</td>"
                    if sandboxes is not None else "<td>-</td>"
                )
                rows.append(
                    f"<tr><td style='text-align:left'>{bid}</td>"
                    f"<td>{r['concurrency']}</td>"
                    f"{cell(cp.get('cube_proxy_cores_p95'), '.2f')}"
                    f"{cell(cp.get('cube_api_cores_p95'), '.2f')}"
                    f"{cell(cp.get('cube_master_cores_p95'), '.2f')}"
                    f"{cell(cp.get('cubelet_cores_p95'), '.2f')}"
                    f"{share_cell}"
                    f"{cell(cp.get('sandbox_fleet_cores_p95'), '.2f')}"
                    f"{sandbox_cell}"
                    f"<td>{cp.get('create_fails', 0)}</td>"
                    f"<td>{flag}</td></tr>"
                )
        if not rows:
            return ""
        warning = ""
        if any_saturated:
            warning = (
                "<p style='color:#b00'><b>At least one point saturated the "
                "CubeSandbox control plane.</b> Those rows measure the platform "
                "rather than the sandbox workload. Re-run them with "
                "<code>CUBE_TRANSPORT=direct</code> to take CubeProxy off the "
                "request path, and check <code>create fails</code> and the "
                "sandbox ceiling before comparing architectures.</p>"
            )
        return (
            "<h2>Control plane</h2>"
            "<p class='meta'>CPU cores consumed by CubeSandbox itself, per "
            "component. <b>CubeProxy</b> routes every in-sandbox request unless "
            "the transport is <code>direct</code>; <b>CubeAPI</b>, "
            "<b>CubeMaster</b> and <b>Cubelet</b> sit on the create/delete path. "
            "<b>Fleet</b> is the microVMs themselves - that column is supposed to "
            "be large. <code>create fails</code> is counted by the load "
            "generator, because CubeSandbox exports no such counter.</p>"
            + warning
            + "<table><tr><th style='text-align:left'>Benchmark</th><th>Concurrency</th>"
            "<th>CubeProxy cores</th><th>CubeAPI cores</th>"
            "<th>CubeMaster cores</th><th>Cubelet cores</th>"
            "<th>ctl share of host</th><th>fleet cores</th>"
            "<th>sandboxes</th><th>create fails</th><th>verdict</th></tr>"
            + "".join(rows) + "</table>"
        )

    # ----- B8 lifecycle phase breakdown -----
    def render_phases_section(bid: str, runs: list[dict]) -> str:
        """Per-phase latency for the lifecycle benchmark (B8).

        create / pause / resume / fork are the primitives an RL trainer
        actually calls, and only `create` has a container equivalent - so
        showing them side by side is the point of B8.
        """
        if not any((r.get("latency_ms") or {}).get("phases") for r in runs):
            return ""
        out = [f"<h2>{bid} - microVM lifecycle phases</h2>",
               "<p class='meta'><code>pause</code>, <code>resume</code> and "
               "<code>fork</code> have no container equivalent: they are what "
               "makes an idle environment cheap and GRPO-style fan-out from a "
               "single state possible.</p>"]
        for r in runs:
            phases = (r.get("latency_ms") or {}).get("phases") or {}
            if not phases:
                continue
            out.append(f"<h3 style='font-size:1rem;margin-top:1rem'>c={r['concurrency']}</h3>")
            out.append("<table>")
            out.append(
                "<tr><th style='text-align:left'>Phase</th><th>count</th>"
                "<th>errors</th><th>p50 (ms)</th><th>p95 (ms)</th>"
                "<th>p99 (ms)</th><th>mean (ms)</th><th>max (ms)</th></tr>"
            )
            for name in sorted(phases.keys()):
                row = phases[name] or {}
                out.append(
                    f"<tr><td style='text-align:left'>{name}</td>"
                    f"<td>{int(row.get('count', 0))}</td>"
                    f"<td>{int(row.get('errors', 0))}</td>"
                    f"{cell(row.get('p50'), '.1f')}"
                    f"{cell(row.get('p95'), '.1f')}"
                    f"{cell(row.get('p99'), '.1f')}"
                    f"{cell(row.get('mean'), '.1f')}"
                    f"{cell(row.get('max'), '.1f')}"
                    "</tr>"
                )
            out.append("</table>")
        return "\n".join(out)

    # ----- disk -----
    def render_disk_section() -> str:
        """Was the EBS volume the limiting factor?

        None of the target metal instance types has instance store, so every
        sandbox's writable layer, work dir and serial log goes to EBS. The
        load is IOPS-shaped, and gp3's baseline is only 3,000 IOPS - which
        this workload exceeds around c=64. A provisioned gp3 (up to 80,000
        IOPS / 2,000 MiB/s on a single volume) has ample headroom.
        """
        rows: list[str] = []
        any_sat = False
        for bid, runs in summary["benchmarks"].items():
            for r in runs:
                dk = (r.get("resource") or {}).get("disk") or {}
                if not dk:
                    continue
                sat = bool(dk.get("disk_saturated"))
                any_sat = any_sat or sat
                flag = ("<b style='color:#b00'>SATURATED</b>" if sat
                        else "<span style='color:#080'>ok</span>")
                if not sat and dk.get("disk_pressure"):
                    flag = "<span style='color:#a60'>pressure</span>"
                devices = ",".join(dk.get("devices") or []) or "-"
                rows.append(
                    f"<tr><td style='text-align:left'>{bid}</td>"
                    f"<td>{r['concurrency']}</td>"
                    f"<td style='text-align:left'>{devices}</td>"
                    f"{cell(dk.get('iops_avg'), ',.0f')}"
                    f"{cell(dk.get('iops_p95'), ',.0f')}"
                    f"{cell(dk.get('read_mbps_avg'), '.1f')}"
                    f"{cell(dk.get('write_mbps_avg'), '.1f')}"
                    f"{cell(dk.get('await_ms_p95'), '.2f')}"
                    f"{cell(dk.get('queue_depth_p95'), '.1f')}"
                    f"<td>{100.0 * (dk.get('util_p95') or 0):.0f}%</td>"
                    f"<td>{flag}</td></tr>"
                )
        if not rows:
            return ""
        warning = ""
        if any_sat:
            warning = (
                "<p style='color:#b00'><b>At least one point saturated the "
                "disk.</b> Those rows measure EBS, not the sandbox workload. "
                "gp3's baseline is only 3,000 IOPS - provision the volume. A "
                "single gp3 reaches 80,000 IOPS / 2,000 MiB/s, which is well "
                "above what this workload needs.</p>"
            )
        return (
            "<h2>Disk (EBS)</h2>"
            "<p class='meta'>No target metal instance type has instance store, so all "
            "sandbox I/O lands on the EBS volume behind <code>/data/cubelet</code>. "
            "Reads should be near zero after warmup: sandboxes restore from a "
            "per-node memory snapshot through reflink copy-on-write, so an untouched "
            "page is shared rather than re-read. Writes are per-rollout lifecycle "
            "churn - a CoW upper layer, a shim log and a snapshot pack created and "
            "destroyed for every rollout - which makes the load IOPS-shaped rather "
            "than bandwidth-shaped.</p>"
            "<p class='meta'><b>Read <code>await</code>, not <code>util</code>.</b> "
            "For a network-attached volume, util only means \"at least one I/O in "
            "flight\" and can sit high while far from the provisioned limit "
            "(measured: 43-56% util at ~740 IOPS on a 10,000 IOPS gp3). EBS "
            "throttles by queueing, so rising service time with flat throughput is "
            "the real signal. gp3 normally serves in ~1 ms.</p>"
            + warning
            + "<table><tr><th style='text-align:left'>Benchmark</th><th>Concurrency</th>"
            "<th style='text-align:left'>Device</th>"
            "<th>IOPS avg</th><th>IOPS p95</th><th>read MB/s</th>"
            "<th>write MB/s</th><th>await p95 (ms)</th><th>queue p95</th>"
            "<th>util p95</th><th>verdict</th></tr>"
            + "".join(rows) + "</table>"
        )

    sub_sections: list[str] = []
    sec = render_sandbox_section()
    if sec:
        sub_sections.append(sec)
    sec = render_control_plane_section()
    if sec:
        sub_sections.append(sec)
    sec = render_disk_section()
    if sec:
        sub_sections.append(sec)
    for bid, runs in summary["benchmarks"].items():
        # Currently B5 carries per_query and B9 carries per_task; the
        # generic checks above handle either case if a future runner
        # adopts the same shape.
        sec = render_per_query_section(bid, runs)
        if sec:
            sub_sections.append(sec)
        sec = render_per_task_section(bid, runs)
        if sec:
            sub_sections.append(sec)
        sec = render_phases_section(bid, runs)
        if sec:
            sub_sections.append(sec)
    sub_html = "\n".join(sub_sections)

    inst = summary["instance"]
    cfg_block = summary.get("config") or {}
    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Agentic-RL Sandbox Bench - {inst['instance_type']}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0"></script>
<style>
body{{font-family:system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%;margin:1rem 0}}
th,td{{border:1px solid #ddd;padding:6px 10px;text-align:right}}
th{{background:#f5f5f5}}
h1{{font-size:1.4rem}} h2{{font-size:1.1rem;margin-top:2rem}}
h3{{font-size:1rem;margin-top:1rem;color:#444}}
.meta{{color:#666;font-size:.9rem}}
</style></head><body>
<h1>Agentic-RL Sandbox Benchmark <span style="font-weight:400;color:#888">on CubeSandbox</span></h1>
<p class="meta">
  Instance: <b>{inst['instance_type']}</b> ({inst['arch']})
  &middot; Region: {inst['region']}
  &middot; Generated: {summary['generated_at']}
</p>
<p class="meta">
  CPU: {inst.get('vcpus', '?')} vCPU / <b>{inst.get('physical_cores', '?')} physical cores</b>
  ({inst.get('threads_per_core', '?')} threads per core{', SMT' if inst.get('smt') else ', no SMT'})
  &middot; {inst.get('cpu_model', '?')}
  &middot; kernel {inst.get('kernel', '?')}
</p>
<p class="meta">
  Sandbox runtime: CubeSandbox (RustVMM/KVM microVM), one sandbox per rollout
  &middot; strategy: <b>{cfg_block.get('cube_strategy', '?')}</b>
  &middot; transport: {cfg_block.get('transport', '?')}
</p>
<p class="meta" style="background:#f8f8e8;padding:.6rem;border-left:3px solid #cc0">
  Read physical cores, not vCPU, when comparing architectures: at the same
  192 vCPU, m7i/m8i are 96 cores plus SMT while m8g/m9g are 192 real cores.
  A fleet of hundreds of single-vCPU microVMs is exactly the workload shape
  where that difference shows up.
</p>
<h2>All runs</h2>
<table>
<tr><th>Benchmark</th><th>Concurrency</th><th>Throughput</th>
    <th>P99 (ms)</th><th>CPU avg (%)</th><th>$/1k units</th></tr>
{body_rows}
</table>
{sub_html}
</body></html>
"""
    out_path.write_text(html)


async def main() -> int:
    cfg = CFG
    instance = await detect_instance()
    log.info("instance=%s arch=%s region=%s",
             instance["instance_type"], instance["arch"], instance["region"])

    store = ResultStore(
        results_dir=cfg.results_dir,
        bucket=cfg.s3_bucket,
        prefix=cfg.s3_prefix,
        region=cfg.aws_region,
    )

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "instance": instance,
        "config": {
            "sandbox_runtime": "cubesandbox",
            "rollout_model": "one-sandbox-per-rollout",
            "cube_api_url": cfg.cube_api_url,
            "cube_proxy_url": cfg.cube_proxy_url,
            "cube_strategy": cfg.cube_strategy,
            "transport": cfg.transport_label(),
            "sandbox_timeout_sec": cfg.cube_sandbox_timeout_sec,
            "duration_sec": cfg.duration_sec,
            "warmup_sec": cfg.warmup_sec,
            "cooldown_sec": cfg.cooldown_sec,
            "concurrencies": {
                bid: cfg.concurrencies_for(bid) for bid in sorted(ALL_RUNNERS)
            },
            "durations": {bid: cfg.duration_for(bid) for bid in sorted(ALL_RUNNERS)},
            "episode_steps": {
                "B1": cfg.b1_episode_steps,
                "B5": cfg.b5_episode_steps,
                "B7_episodes_per_rollout": cfg.b7_episodes_per_rollout,
            },
            "think_time_ms": cfg.think_time_ms,
            "think_time_pause": cfg.think_time_pause,
            "templates": {bid: cfg.template_for(bid) for bid in sorted(ALL_RUNNERS)},
            # Resolve through hourly_price, not cfg.price_for: the former
            # also applies the legacy .4xlarge fallback, so the summary
            # agrees with the per-run cost blocks instead of reporting $0
            # for an instance the runners happily priced.
            "instance_hourly_usd": hourly_price(instance["instance_type"], cfg),
            "instance_price_exact": cfg.price_for(instance["instance_type"]) > 0,
        },
        "benchmarks": {},
    }
    if not summary["config"]["instance_hourly_usd"]:
        log.warning(
            "no price known for %s - cost_per_1k/1m will be null. "
            "Add it to INSTANCE_PRICES in .env.", instance["instance_type"],
        )

    for bid in sorted(ALL_RUNNERS.keys()):
        if bid in cfg.skip:
            log.info("skipping %s (in SKIP)", bid)
            continue
        runs = await run_benchmark(bid, cfg, instance, store)
        summary["benchmarks"][bid] = runs

    # Final summary + report
    ts = time.strftime("%Y%m%d-%H%M%S")
    summary_rel = f"{instance['arch']}/summary_{instance['instance_type']}_{ts}.json"
    store.write_json(summary_rel, summary)

    report_rel = f"{instance['arch']}/report_{instance['instance_type']}_{ts}.html"
    report_path = Path(cfg.results_dir) / report_rel
    report_path.parent.mkdir(parents=True, exist_ok=True)
    render_html(summary, report_path)
    store.write_text(report_rel, report_path.read_text())

    log.info("ALL DONE. Local: %s | S3: s3://%s/%s",
             cfg.results_dir, cfg.s3_bucket or "-", cfg.s3_prefix)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
