"""B3 trajectory generator (τ-bench-style multi-pattern replay).

Replaces the original single hard-coded 12-step trajectory with a
diverse set of templates inspired by tau-bench retail tasks:

  - browse_only         (8  steps)   user just looks around, no purchase
  - buy_simple          (10 steps)   search + add to cart + checkout
  - buy_with_compare    (14 steps)   compare several products before buying
  - refund_flow         (12 steps)   purchase + refund the latest order
  - inventory_heavy     (18 steps)   GET-dominated inventory polling
  - checkout_loop       (16 steps)   multiple sequential purchases
  - profile_admin       (9  steps)   profile read/update churn

Templates are weighted to roughly approximate the distribution of
real LLM tool-call traces (more browsing/buying than refunds).

At process start we expand the templates into ~200 concrete
trajectories with all SKU / search-term / reason placeholders
already substituted, using a deterministic seed for reproducibility.

A single "op" in B3 is one full trajectory: the runner picks a
random index 0..N-1 and replays the steps end to end, like a
client agent walking through a tool-call episode.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Mock data backing the b3-mock-api seed (200 SKUs, "alice" user).
NUM_SKUS = 200
SEARCH_TERMS = [
    "headphones", "laptop", "phone", "camera", "tablet",
    "watch", "speaker", "monitor", "keyboard", "mouse",
    "router", "ssd", "ram", "gpu", "psu",
    "cable", "charger", "case", "stand", "hub",
]
REFUND_REASONS = [
    "defective", "wrong item", "damaged in shipping",
    "no longer needed", "found cheaper elsewhere", "duplicate order",
]
NAMES = ["alice", "bob", "carol", "dave", "eve", "frank"]


@dataclass
class StepTemplate:
    method: str
    path: str           # may contain {sku_id} placeholder
    body: dict | None   # values can be sentinel strings: $Q, $SKU, $REASON, $NAME

    def materialize(self, rng: random.Random) -> tuple[str, str, dict | None]:
        sku = rng.randrange(NUM_SKUS)
        q = rng.choice(SEARCH_TERMS)
        reason = rng.choice(REFUND_REASONS)
        name = rng.choice(NAMES)

        path = self.path.replace("{sku_id}", str(sku))
        body = None
        if self.body is not None:
            body = {}
            for k, v in self.body.items():
                if isinstance(v, str):
                    body[k] = (
                        v.replace("$SKU", str(sku))
                         .replace("$Q", q)
                         .replace("$REASON", reason)
                         .replace("$NAME", name)
                    )
                else:
                    body[k] = v
        return self.method, path, body


def _S(method: str, path: str, body: dict | None = None) -> StepTemplate:
    return StepTemplate(method, path, body)


TEMPLATES: list[dict] = [
    {
        "name": "browse_only",
        "weight": 20,
        "steps": [
            _S("GET",  "/search",       {"q": "$Q"}),
            _S("GET",  "/product/{sku_id}"),
            _S("GET",  "/search",       {"q": "$Q"}),
            _S("GET",  "/product/{sku_id}"),
            _S("GET",  "/recommend",    {"k": 5}),
            _S("GET",  "/product/{sku_id}"),
            _S("GET",  "/inventory",    {"sku": "SKU-$SKU"}),
            _S("GET",  "/profile"),
        ],
    },
    {
        "name": "buy_simple",
        "weight": 25,
        "steps": [
            _S("GET",  "/search",       {"q": "$Q"}),
            _S("GET",  "/product/{sku_id}"),
            _S("POST", "/cart/add",     {"sku": "SKU-$SKU", "qty": 1}),
            _S("GET",  "/cart"),
            _S("GET",  "/product/{sku_id}"),
            _S("POST", "/cart/add",     {"sku": "SKU-$SKU", "qty": 2}),
            _S("GET",  "/cart"),
            _S("POST", "/checkout",     {"payment": "card"}),
            _S("GET",  "/order/last"),
            _S("GET",  "/profile"),
        ],
    },
    {
        "name": "buy_with_compare",
        "weight": 15,
        "steps": [
            _S("GET",  "/search",       {"q": "$Q"}),
            _S("GET",  "/product/{sku_id}"),
            _S("GET",  "/product/{sku_id}"),
            _S("GET",  "/product/{sku_id}"),
            _S("GET",  "/recommend",    {"k": 5}),
            _S("GET",  "/product/{sku_id}"),
            _S("POST", "/cart/add",     {"sku": "SKU-$SKU", "qty": 1}),
            _S("GET",  "/cart"),
            _S("POST", "/cart/add",     {"sku": "SKU-$SKU", "qty": 1}),
            _S("GET",  "/cart"),
            _S("POST", "/checkout",     {"payment": "card"}),
            _S("GET",  "/order/last"),
            _S("GET",  "/profile"),
            _S("GET",  "/inventory",    {"sku": "SKU-$SKU"}),
        ],
    },
    {
        "name": "refund_flow",
        "weight": 10,
        "steps": [
            _S("GET",  "/profile"),
            _S("GET",  "/order/last"),
            _S("GET",  "/search",       {"q": "$Q"}),
            _S("GET",  "/product/{sku_id}"),
            _S("POST", "/cart/add",     {"sku": "SKU-$SKU", "qty": 1}),
            _S("POST", "/checkout",     {"payment": "card"}),
            _S("GET",  "/order/last"),
            _S("POST", "/order/refund", {"reason": "$REASON"}),
            _S("GET",  "/order/last"),
            _S("GET",  "/profile"),
            _S("GET",  "/recommend",    {"k": 3}),
            _S("GET",  "/search",       {"q": "$Q"}),
        ],
    },
    {
        "name": "inventory_heavy",
        "weight": 10,
        "steps": [
            _S("GET",  "/search",       {"q": "$Q"}),
            _S("GET",  "/inventory",    {"sku": "SKU-$SKU"}),
            _S("GET",  "/inventory",    {"sku": "SKU-$SKU"}),
            _S("GET",  "/product/{sku_id}"),
            _S("GET",  "/inventory",    {"sku": "SKU-$SKU"}),
            _S("GET",  "/search",       {"q": "$Q"}),
            _S("GET",  "/inventory",    {"sku": "SKU-$SKU"}),
            _S("GET",  "/product/{sku_id}"),
            _S("GET",  "/inventory",    {"sku": "SKU-$SKU"}),
            _S("GET",  "/recommend",    {"k": 10}),
            _S("GET",  "/inventory",    {"sku": "SKU-$SKU"}),
            _S("GET",  "/product/{sku_id}"),
            _S("GET",  "/inventory",    {"sku": "SKU-$SKU"}),
            _S("GET",  "/healthz"),
            _S("GET",  "/profile"),
            _S("GET",  "/inventory",    {"sku": "SKU-$SKU"}),
            _S("GET",  "/inventory",    {"sku": "SKU-$SKU"}),
            _S("GET",  "/inventory",    {"sku": "SKU-$SKU"}),
        ],
    },
    {
        "name": "checkout_loop",
        "weight": 10,
        "steps": [
            _S("GET",  "/search",       {"q": "$Q"}),
            _S("POST", "/cart/add",     {"sku": "SKU-$SKU", "qty": 1}),
            _S("POST", "/checkout",     {"payment": "card"}),
            _S("GET",  "/order/last"),
            _S("POST", "/cart/add",     {"sku": "SKU-$SKU", "qty": 2}),
            _S("POST", "/checkout",     {"payment": "card"}),
            _S("GET",  "/order/last"),
            _S("POST", "/cart/add",     {"sku": "SKU-$SKU", "qty": 1}),
            _S("POST", "/checkout",     {"payment": "card"}),
            _S("GET",  "/order/last"),
            _S("POST", "/cart/add",     {"sku": "SKU-$SKU", "qty": 3}),
            _S("POST", "/checkout",     {"payment": "card"}),
            _S("GET",  "/order/last"),
            _S("GET",  "/profile"),
            _S("GET",  "/recommend",    {"k": 5}),
            _S("GET",  "/search",       {"q": "$Q"}),
        ],
    },
    {
        "name": "profile_admin",
        "weight": 10,
        "steps": [
            _S("GET",  "/profile"),
            _S("POST", "/profile/update", {"name": "$NAME"}),
            _S("GET",  "/profile"),
            _S("POST", "/profile/update", {"name": "$NAME"}),
            _S("GET",  "/profile"),
            _S("POST", "/profile/update", {"name": "$NAME"}),
            _S("GET",  "/profile"),
            _S("GET",  "/recommend",    {"k": 5}),
            _S("GET",  "/profile"),
        ],
    },
]


@dataclass
class Trajectory:
    template: str
    steps: list[tuple[str, str, dict | None]]


def build_trajectories(
    target_total: int = 200, seed: int = 42
) -> tuple[list[Trajectory], dict]:
    """Materialize the templates into `target_total` concrete trajectories.

    Returns (trajectories, breakdown). `breakdown` summarises how many
    of each template were generated, plus avg steps - included in the
    result JSON for reproducibility.
    """
    rng = random.Random(seed)
    total_weight = sum(t["weight"] for t in TEMPLATES)
    out: list[Trajectory] = []
    breakdown: dict[str, int] = {}

    for tmpl in TEMPLATES:
        share = tmpl["weight"] / total_weight
        # Always produce at least 1 of each so coverage is guaranteed.
        n = max(1, round(target_total * share))
        breakdown[tmpl["name"]] = n
        for _ in range(n):
            steps = [step.materialize(rng) for step in tmpl["steps"]]
            out.append(Trajectory(template=tmpl["name"], steps=steps))

    rng.shuffle(out)
    avg_steps = sum(len(t.steps) for t in out) / len(out)
    log.info(
        "B3 trajectories built: total=%d, templates=%s, avg_steps=%.1f",
        len(out), breakdown, avg_steps,
    )
    return out, {
        "templates": breakdown,
        "total": len(out),
        "avg_steps": round(avg_steps, 2),
        "seed": seed,
    }
