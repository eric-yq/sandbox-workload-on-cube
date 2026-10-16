"""Configuration loaded from environment variables.

The benchmark runs on CubeSandbox (RustVMM/KVM microVMs) rather than
Docker containers. Two things follow from that, and both are visible in
the knobs below:

* A *rollout* - not a single HTTP request - is the unit of isolation:
  every rollout gets its own sandbox, which is how an Agentic-RL trainer
  actually provisions environments. See `orchestrator/rollout.py`.
* The control plane and the data plane are separate services on separate
  ports (CubeAPI :3000, CubeProxy :80), so `CUBE_API_URL` and
  `CUBE_PROXY_URL` are configured independently, and node-level
  observability comes from a third one (CubeOps :3010).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv_int(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x.strip()]


def _csv_str(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _kv_float(s: str) -> dict[str, float]:
    """Parse `a=1.5,b=2.0` into {"a": 1.5, "b": 2.0}.

    Malformed entries are skipped rather than fatal - a typo in a price
    override should not abort a 30-minute benchmark run.
    """
    out: dict[str, float] = {}
    for item in s.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        k, _, v = item.partition("=")
        try:
            out[k.strip().lower()] = float(v)
        except ValueError:
            continue
    return out


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Exact us-east-1 Linux on-demand prices (verified against the AWS
# Pricing API). Used when INSTANCE_PRICES does not list the instance.
# m8g is Graviton4, m9g is Graviton5.
DEFAULT_INSTANCE_PRICES: dict[str, float] = {
    "m7i.metal-48xl": 9.67680,
    "m8i.metal-48xl": 10.16064,
    "m8g.metal-48xl": 8.61696,
    "m9g.metal-48xl": 9.39264,
    # The .4xlarge SKUs the previous generation of this benchmark used.
    "m7i.4xlarge": 0.80640,
    "m8i.4xlarge": 0.84672,
    "m8g.4xlarge": 0.71808,
    "m9g.4xlarge": 0.78272,
    # Smaller metal SKUs used for functional verification rather than for
    # headline numbers. Listed because the alternative is worse than a
    # rough price: without an entry, `hourly_price` returns 0.0 and every
    # cost field in the result file comes out null, which reads as "cost
    # was not measured" instead of "this instance is not in the table".
    "c7g.metal": 2.32000,       # 64 vCPU / 128 GiB, Graviton3
    "c8g.metal-24xl": 4.35744,  # 96 vCPU / 192 GiB, Graviton4
}


@dataclass
class Config:
    # ---------------- storage ----------------
    s3_bucket: str = field(default_factory=lambda: os.getenv("S3_BUCKET", ""))
    s3_prefix: str = field(default_factory=lambda: os.getenv("S3_PREFIX", "agentic-rl-bench"))
    aws_region: str = field(default_factory=lambda: os.getenv("AWS_REGION", "us-east-1"))
    results_dir: str = field(default_factory=lambda: os.getenv("RESULTS_DIR", "./results"))

    # ---------------- measurement window ----------------
    duration_sec: int = field(default_factory=lambda: _env_int("DURATION_SEC", 300))
    warmup_sec: int = field(default_factory=lambda: _env_int("WARMUP_SEC", 30))
    cooldown_sec: int = field(default_factory=lambda: _env_int("COOLDOWN_SEC", 60))

    # Concurrency = number of simultaneously live rollout sandboxes.
    concurrencies: list[int] = field(
        default_factory=lambda: _csv_int(os.getenv("CONCURRENCIES", "32,128,512,1024"))
    )

    # ---------------- CubeSandbox control plane ----------------
    # CubeAPI, the E2B-compatible REST surface. One-click installs bind
    # it on :3000 (CUBE_API_BIND) and mount every route at the root
    # prefix, which is what the E2B SDK expects.
    cube_api_url: str = field(
        default_factory=lambda: os.getenv("CUBE_API_URL", "http://127.0.0.1:3000").rstrip("/")
    )
    # Empty is correct for a default one-click install: with neither
    # CUBE_API_KEY nor AUTH_CALLBACK_URL set, CubeAPI runs unauthenticated.
    cube_api_key: str = field(default_factory=lambda: os.getenv("CUBE_API_KEY", ""))

    # ---------------- data plane ----------------
    # CubeProxy is a *separate* process from CubeAPI, so control-plane
    # calls and in-sandbox traffic no longer contend for one process. Its
    # plaintext HTTP listener is port 80 on a one-click install
    # (CUBE_PROXY_HTTP_PORT; the shipped nginx.conf's 8081 is rewritten at
    # bundle build time).
    cube_proxy_url: str = field(
        default_factory=lambda: os.getenv("CUBE_PROXY_URL", "http://127.0.0.1:80").rstrip("/")
    )
    # How to address a port inside a sandbox:
    #   path   - {proxy}/sandbox/<id>/<port><path>   (default; no DNS needed)
    #   host   - Host: <port>-<id>.<domain>          (E2B-compatible form)
    #   direct - http://<sandbox-ip>:<port><path>    (proxy off the path)
    cube_transport: str = field(
        default_factory=lambda: os.getenv("CUBE_TRANSPORT", "path").strip().lower()
    )
    # Only cosmetic for `host` transport - CubeProxy matches the leading
    # "<port>-<id>" of the Host header and ignores the suffix - but keep it
    # aligned with CUBE_API_SANDBOX_DOMAIN so captured URLs are real.
    cube_sandbox_domain: str = field(
        default_factory=lambda: os.getenv("CUBE_SANDBOX_DOMAIN", "cube.app")
    )
    # Direct transport reads the sandbox IP from the routing map that
    # CubeMaster writes and CubeProxy reads; no API returns it.
    cube_redis_host: str = field(
        default_factory=lambda: os.getenv("CUBE_REDIS_HOST", "127.0.0.1")
    )
    cube_redis_port: int = field(default_factory=lambda: _env_int("CUBE_REDIS_PORT", 6379))
    cube_redis_password: str = field(
        default_factory=lambda: os.getenv("CUBE_REDIS_PASSWORD", "ceuhvu123")
    )

    # ---------------- node observability ----------------
    # CubeOps owns the node registry; CubeAPI has no /nodes route and its
    # /health reports a hardcoded sandboxes=0.
    cube_ops_url: str = field(
        default_factory=lambda: os.getenv("CUBE_OPS_URL", "http://127.0.0.1:3010").rstrip("/")
    )
    # Cubelet's Prometheus surface, for the scheduler gauges.
    cube_let_metrics_url: str = field(
        default_factory=lambda: os.getenv(
            "CUBE_LET_METRICS_URL", "http://127.0.0.1:9998"
        ).rstrip("/")
    )

    # Idle TTL handed to every sandbox we create. The shipped cluster
    # default is `default_timeout_insec: -1`, i.e. no reclamation at all,
    # so a leaked sandbox would hold its scheduler quota for the whole
    # suite. Must comfortably exceed WARMUP_SEC + DURATION_SEC.
    cube_sandbox_timeout_sec: int = field(
        default_factory=lambda: _env_int("CUBE_SANDBOX_TIMEOUT_SEC", 3600)
    )

    # ---------------- rollout -> sandbox strategy ----------------
    # fresh | pool | clone | rollback - see rollout.SandboxProvider.
    cube_strategy: str = field(
        default_factory=lambda: os.getenv("CUBE_STRATEGY", "fresh").strip().lower()
    )
    cube_pool_size: int = field(default_factory=lambda: _env_int("CUBE_POOL_SIZE", 0))
    # `clone` strategy: how many children to create per snapshot. Cube has
    # no server-side fan-out (no `fork`), so each child is an independent
    # create; this only controls how many we pipeline concurrently while
    # refilling, and it mirrors GRPO sampling G rollouts from one state.
    cube_clone_batch: int = field(default_factory=lambda: _env_int("CUBE_CLONE_BATCH", 16))

    # ---------------- templates ----------------
    template_b1: str = field(default_factory=lambda: os.getenv("TEMPLATE_B1", "arl-b1"))
    template_b3: str = field(default_factory=lambda: os.getenv("TEMPLATE_B3", "arl-b3"))
    template_b4: str = field(default_factory=lambda: os.getenv("TEMPLATE_B4", "arl-b4"))
    template_b5: str = field(default_factory=lambda: os.getenv("TEMPLATE_B5", "arl-b5"))
    template_b7: str = field(default_factory=lambda: os.getenv("TEMPLATE_B7", "arl-b7"))
    template_b8: str = field(default_factory=lambda: os.getenv("TEMPLATE_B8", "arl-b8"))
    template_b9: str = field(default_factory=lambda: os.getenv("TEMPLATE_B9", "arl-b9"))

    # ---------------- episode length ----------------
    b1_episode_steps: int = field(default_factory=lambda: _env_int("B1_EPISODE_STEPS", 8))
    b5_episode_steps: int = field(default_factory=lambda: _env_int("B5_EPISODE_STEPS", 5))
    b7_episodes_per_rollout: int = field(
        default_factory=lambda: _env_int("B7_EPISODES_PER_ROLLOUT", 1)
    )
    # B9 session length, in steps. The RL default (10-30) is a training
    # mini-episode. An *agent hosting* session is two orders of magnitude
    # longer - a coding agent runs for hours - and the difference is not
    # cosmetic: at 20 steps a `fresh` rollout spends ~15% of its wall time
    # provisioning, which is what makes B9's headline number a measure of
    # the create path rather than of the work. These were module constants
    # in b9_concurrent.py until it turned out that the one axis separating
    # the two archetypes was the one axis that could not be set.
    b9_episode_min_steps: int = field(
        default_factory=lambda: _env_int("B9_EPISODE_MIN_STEPS", 10)
    )
    b9_episode_max_steps: int = field(
        default_factory=lambda: _env_int("B9_EPISODE_MAX_STEPS", 30)
    )

    # ---------------- archetype label ----------------
    # `rl`: a trainer dispatching short rollouts, each in its own fresh
    # sandbox. Churn is part of the workload and sandbox state must not
    # survive a rollout.
    # `hosting`: a long-lived pool of sandboxes, each holding one agent
    # session. Provisioning is a one-off fixture and state carry-over
    # between steps is the product, not a leak.
    # Only affects labelling and which SLO view the report builds; the
    # mechanics come from CUBE_STRATEGY / THINK_TIME_MS / B9_EPISODE_*.
    bench_mode: str = field(
        default_factory=lambda: os.getenv("BENCH_MODE", "rl").strip().lower()
    )

    # ---------------- SLO density ----------------
    # Hosting capacity is "how many sessions fit before steps get slow",
    # so the headline is a session count at a latency budget, not a
    # throughput number. Budgets are per-stack because B9's step mix spans
    # two orders of magnitude (B7 ~2ms, B4 ~600ms) - a single ms budget
    # across all of them would be meaningless.
    #
    # Default budget is relative: a stack passes while its step p95 stays
    # within SLO_DEGRADE_FACTOR of the same stack's p95 at the *lowest*
    # concurrency in the sweep. That self-calibrates per instance and
    # needs no magic constants, which matters when the whole point is
    # comparing two different CPUs.
    slo_degrade_factor: float = field(
        default_factory=lambda: _env_float("SLO_DEGRADE_FACTOR", 2.0)
    )
    # Absolute override, e.g. `SLO_STEP_P95_MS=b1=40,b3=25`. Any stack
    # listed here is judged against the fixed budget instead of the
    # relative one. Use it when you have a real product SLO to hold to.
    slo_step_p95_ms: dict[str, float] = field(
        default_factory=lambda: _kv_float(os.getenv("SLO_STEP_P95_MS", ""))
    )
    slo_metric: str = field(
        default_factory=lambda: os.getenv("SLO_METRIC", "p95").strip().lower()
    )

    # ---------------- simulated policy think time ----------------
    think_time_ms: float = field(default_factory=lambda: _env_float("THINK_TIME_MS", 0.0))
    think_time_jitter_ms: float = field(
        default_factory=lambda: _env_float("THINK_TIME_JITTER_MS", 0.0)
    )
    think_time_pause: bool = field(default_factory=lambda: _env_bool("THINK_TIME_PAUSE", False))

    # ---------------- per-benchmark concurrency sweeps ----------------
    # Empty list -> fall back to `concurrencies`.
    b1_concurrencies: list[int] = field(
        default_factory=lambda: _csv_int(os.getenv("B1_CONCURRENCIES", ""))
    )
    b3_concurrencies: list[int] = field(
        default_factory=lambda: _csv_int(os.getenv("B3_CONCURRENCIES", ""))
    )
    b4_concurrencies: list[int] = field(
        default_factory=lambda: _csv_int(os.getenv("B4_CONCURRENCIES", "16,64,128,256"))
    )
    b5_concurrencies: list[int] = field(
        default_factory=lambda: _csv_int(os.getenv("B5_CONCURRENCIES", ""))
    )
    b7_concurrencies: list[int] = field(
        default_factory=lambda: _csv_int(os.getenv("B7_CONCURRENCIES", ""))
    )
    b8_concurrencies: list[int] = field(
        default_factory=lambda: _csv_int(os.getenv("B8_CONCURRENCIES", "8,32,64"))
    )
    b9_concurrencies: list[int] = field(
        default_factory=lambda: _csv_int(os.getenv("B9_CONCURRENCIES", "64,128,256"))
    )
    b9_duration_sec: int = field(default_factory=lambda: _env_int("B9_DURATION_SEC", 1800))

    # ---------------- load generator ----------------
    client_nprocs: int = field(default_factory=lambda: _env_int("CLIENT_NPROCS", 0))
    # A 192-vCPU host would otherwise spawn 192 client processes, each
    # with its own httpx connection pool.
    client_nprocs_max: int = field(default_factory=lambda: _env_int("CLIENT_NPROCS_MAX", 64))

    # ---------------- B8 lifecycle benchmark ----------------
    b8_trials: int = field(default_factory=lambda: _env_int("B8_TRIALS", 500))
    # Children per clone fan-out. Cube has no batched fork, so this is N
    # independent creates from one snapshot - the GRPO shape, and the
    # number that shows whether cloning beats creating from a template.
    b8_clone_count: int = field(default_factory=lambda: _env_int("B8_CLONE_COUNT", 8))
    # There is no per-sandbox cold-start API in Cube (no `/sandboxes-cold`):
    # the OCI-to-rootfs conversion happens once, at template build time.
    # So the Docker-era cold-start phase has no counterpart here and the
    # B8_INCLUDE_COLD / B8_COLD_IMAGE knobs are gone.

    # ---------------- workload knobs ----------------
    tpch_sf: float = field(default_factory=lambda: _env_float("TPCH_SF", 1.0))

    # ---------------- run control ----------------
    skip: list[str] = field(default_factory=lambda: _csv_str(os.getenv("SKIP", "")))

    # ---------------- pricing ----------------
    instance_prices: dict[str, float] = field(
        default_factory=lambda: _kv_float(os.getenv("INSTANCE_PRICES", ""))
    )
    price_c7i_4xl: float = field(default_factory=lambda: _env_float("PRICE_C7I_4XL", 0.7140))
    price_c8g_4xl: float = field(default_factory=lambda: _env_float("PRICE_C8G_4XL", 0.6381))

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    def template_for(self, bid: str) -> str:
        return getattr(self, f"template_{bid.lower()}", "")

    def concurrencies_for(self, bid: str) -> list[int]:
        """Per-benchmark sweep, falling back to the global list."""
        specific = getattr(self, f"{bid.lower()}_concurrencies", None) or []
        return list(specific) if specific else list(self.concurrencies)

    def duration_for(self, bid: str) -> int:
        if bid == "B9":
            return self.b9_duration_sec
        return self.duration_sec

    def b9_episode_range(self) -> tuple[int, int]:
        """(min, max) steps per B9 session, clamped to a sane ordering.

        A swapped pair (max < min) would make `random.randint` raise
        inside every load-generator child at once, which surfaces as
        "every rollout errored" rather than as a bad setting.
        """
        lo = max(1, self.b9_episode_min_steps)
        hi = max(lo, self.b9_episode_max_steps)
        return lo, hi

    def is_hosting(self) -> bool:
        return self.bench_mode == "hosting"

    def slo_budget_for(self, stack: str) -> float | None:
        """Absolute step-latency budget in ms for `stack`, if one is set."""
        return self.slo_step_p95_ms.get((stack or "").strip().lower())

    def price_for(self, instance_type: str) -> float:
        """Exact-match price lookup: overrides, then built-in table."""
        key = (instance_type or "").strip().lower()
        if key in self.instance_prices:
            return self.instance_prices[key]
        return DEFAULT_INSTANCE_PRICES.get(key, 0.0)

    def transport_label(self) -> str:
        """How results and reports name the data-plane transport."""
        if self.cube_transport == "direct":
            return "direct-ip"
        return f"cubeproxy/{self.cube_transport}"

    def cube_setup(self) -> dict:
        """Picklable bundle handed to load-generator child processes.

        The child processes are spawned (not forked), so they cannot see
        this Config object; everything they need to build a CubeSandbox
        client has to travel as plain data.
        """
        return {
            "api_url": self.cube_api_url,
            "api_key": self.cube_api_key,
            "proxy_url": self.cube_proxy_url,
            "transport": self.cube_transport,
            "sandbox_domain": self.cube_sandbox_domain,
            "ops_url": self.cube_ops_url,
            "redis_host": self.cube_redis_host,
            "redis_port": self.cube_redis_port,
            "redis_password": self.cube_redis_password,
            "sandbox_timeout_sec": self.cube_sandbox_timeout_sec,
            "strategy": self.cube_strategy,
            "pool_size": self.cube_pool_size,
            "clone_batch": self.cube_clone_batch,
            "think_time_ms": self.think_time_ms,
            "think_time_jitter_ms": self.think_time_jitter_ms,
            "think_time_pause": self.think_time_pause,
        }


CFG = Config()
