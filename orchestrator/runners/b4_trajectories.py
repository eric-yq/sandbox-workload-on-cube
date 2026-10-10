"""B4 trajectory generator (WebArena-style mock e-commerce).

Replaces the original single hard-coded 7-step trajectory with 8
templates inspired by WebArena shopping tasks. Selectors and paths
are kept consistent with workers/b4-webarena-static/html/.

Template mix (weighted to roughly approximate real LLM-driven web
agent behaviour, more browsing than checkout/refund):

  browse_only         (10 steps)   nav around index + product pages
  search_filter       (12 steps)   keyword search + sort + open hits
  add_to_cart         (10 steps)   add several items to cart
  checkout_flow       (14 steps)   add + checkout + form fill
  product_compare     (12 steps)   open multiple products in sequence
  paginate_scroll     (15 steps)   browse with scrolling + pagination
  screenshot_heavy    (10 steps)   render-heavy: scroll + screenshot loop
  profile_update      (10 steps)   account/profile form interactions

At process start we expand the templates into ~80 concrete
trajectories with placeholders substituted, deterministic seed.
A worker picks a random trajectory each iteration.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Mock catalogue size in the static site (workers/b4-webarena-static
# generates 60 products via JS at page load).
NUM_PRODUCTS = 60

SEARCH_TERMS = [
    "phone", "laptop", "headphones", "camera", "watch",
    "tablet", "speaker", "monitor", "mouse", "keyboard",
]
NAMES = ["alice", "bob", "carol", "dave", "eve"]
EMAILS = ["a@b.io", "c@d.io", "e@f.io"]
ADDRS = ["1 Main St", "42 Oak Ave", "7 Pine Rd"]


@dataclass
class StepTemplate:
    action: str
    args: dict


def _S(action: str, **args) -> StepTemplate:
    return StepTemplate(action, dict(args))


# Each template is a list of StepTemplate. Args may contain $-tokens
# (e.g. "$Q", "$PID", "$NAME") that get substituted at materialize time.
TEMPLATES: list[dict] = [
    {
        "name": "browse_only",
        "weight": 25,
        "steps": [
            _S("goto",       path="/"),
            _S("click",      selector="a.product-link[data-id='$PID1']"),
            _S("scroll",     y=400),
            _S("screenshot"),
            _S("goto",       path="/"),
            _S("click",      selector="a.product-link[data-id='$PID2']"),
            _S("scroll",     y=600),
            _S("screenshot"),
            _S("goto",       path="/"),
            _S("scroll",     y=300),
        ],
    },
    {
        "name": "search_filter",
        "weight": 20,
        "steps": [
            _S("goto",       path="/"),
            _S("type",       selector="input#search-box", text="$Q"),
            _S("click",      selector="button#search-btn"),
            _S("goto",       path="/search.html?q=$Q"),
            _S("click",      selector="button.sort-price"),
            _S("scroll",     y=400),
            _S("click",      selector="a.product-link"),
            _S("scroll",     y=200),
            _S("screenshot"),
            _S("goto",       path="/search.html?q=$Q"),
            _S("click",      selector="button.sort-rating"),
            _S("screenshot"),
        ],
    },
    {
        "name": "add_to_cart",
        "weight": 15,
        "steps": [
            _S("goto",       path="/"),
            _S("click",      selector="button.add-to-cart[data-id='$PID1']"),
            _S("click",      selector="button.add-to-cart[data-id='$PID2']"),
            _S("click",      selector="button.add-to-cart[data-id='$PID3']"),
            _S("goto",       path="/cart.html"),
            _S("click",      selector="button.qty-inc[data-id='$PID1']"),
            _S("click",      selector="button.qty-inc[data-id='$PID1']"),
            _S("click",      selector="button.qty-dec[data-id='$PID2']"),
            _S("scroll",     y=200),
            _S("screenshot"),
        ],
    },
    {
        "name": "checkout_flow",
        "weight": 10,
        "steps": [
            _S("goto",       path="/"),
            _S("click",      selector="button.add-to-cart[data-id='$PID1']"),
            _S("click",      selector="button.add-to-cart[data-id='$PID2']"),
            _S("goto",       path="/cart.html"),
            _S("click",      selector="a#checkout-link"),
            _S("type",       selector="input#name",     text="$NAME"),
            _S("type",       selector="input#email",    text="$EMAIL"),
            _S("type",       selector="input#address",  text="$ADDR"),
            _S("click",      selector="input#payment-card"),
            _S("click",      selector="button#place-order"),
            _S("goto",       path="/orders.html"),
            _S("scroll",     y=300),
            _S("screenshot"),
            _S("goto",       path="/profile.html"),
        ],
    },
    {
        "name": "product_compare",
        "weight": 10,
        "steps": [
            _S("goto",       path="/product.html?id=$PID1"),
            _S("scroll",     y=300),
            _S("screenshot"),
            _S("goto",       path="/product.html?id=$PID2"),
            _S("scroll",     y=300),
            _S("screenshot"),
            _S("goto",       path="/product.html?id=$PID3"),
            _S("scroll",     y=400),
            _S("click",      selector="button.add-to-cart"),
            _S("goto",       path="/product.html?id=$PID4"),
            _S("scroll",     y=200),
            _S("screenshot"),
        ],
    },
    {
        "name": "paginate_scroll",
        "weight": 8,
        "steps": [
            _S("goto",       path="/"),
            _S("scroll",     y=600),
            _S("scroll",     y=600),
            _S("click",      selector="button#load-more"),
            _S("scroll",     y=600),
            _S("scroll",     y=600),
            _S("click",      selector="button#load-more"),
            _S("scroll",     y=400),
            _S("click",      selector="a.product-link"),
            _S("scroll",     y=300),
            _S("scroll",     y=300),
            _S("screenshot"),
            _S("goto",       path="/"),
            _S("scroll",     y=800),
            _S("screenshot"),
        ],
    },
    {
        "name": "screenshot_heavy",
        "weight": 6,
        "steps": [
            _S("goto",       path="/"),
            _S("screenshot"),
            _S("scroll",     y=400),
            _S("screenshot"),
            _S("scroll",     y=400),
            _S("screenshot"),
            _S("scroll",     y=400),
            _S("screenshot"),
            _S("goto",       path="/product.html?id=$PID1"),
            _S("screenshot"),
        ],
    },
    {
        "name": "profile_update",
        "weight": 6,
        "steps": [
            _S("goto",       path="/profile.html"),
            _S("type",       selector="input#username", text="$NAME"),
            _S("type",       selector="input#email",    text="$EMAIL"),
            _S("click",      selector="button#save-profile"),
            _S("screenshot"),
            _S("goto",       path="/orders.html"),
            _S("scroll",     y=200),
            _S("click",      selector="button.tab-orders"),
            _S("screenshot"),
            _S("goto",       path="/profile.html"),
        ],
    },
]


def _sub(value, rng: random.Random) -> object:
    """Substitute $-tokens in str values, leave others untouched."""
    if not isinstance(value, str):
        return value
    if "$" not in value:
        return value
    pid_pool = [rng.randrange(1, NUM_PRODUCTS + 1) for _ in range(4)]
    out = (
        value.replace("$PID1", str(pid_pool[0]))
             .replace("$PID2", str(pid_pool[1]))
             .replace("$PID3", str(pid_pool[2]))
             .replace("$PID4", str(pid_pool[3]))
             .replace("$Q",    rng.choice(SEARCH_TERMS))
             .replace("$NAME", rng.choice(NAMES))
             .replace("$EMAIL",rng.choice(EMAILS))
             .replace("$ADDR", rng.choice(ADDRS))
    )
    return out


@dataclass
class Trajectory:
    template: str
    steps: list[dict]   # [{action, args}, ...] - JSON-ready


def build_trajectories(
    target_total: int = 80, seed: int = 42
) -> tuple[list[Trajectory], dict]:
    rng = random.Random(seed)
    total_weight = sum(t["weight"] for t in TEMPLATES)
    out: list[Trajectory] = []
    breakdown: dict[str, int] = {}

    for tmpl in TEMPLATES:
        share = tmpl["weight"] / total_weight
        n = max(1, round(target_total * share))
        breakdown[tmpl["name"]] = n
        for _ in range(n):
            steps_json: list[dict] = []
            for step in tmpl["steps"]:
                args = {k: _sub(v, rng) for k, v in step.args.items()}
                steps_json.append({"action": step.action, "args": args})
            out.append(Trajectory(template=tmpl["name"], steps=steps_json))

    rng.shuffle(out)
    avg_steps = sum(len(t.steps) for t in out) / len(out)
    log.info(
        "B4 trajectories built: total=%d, templates=%s, avg_steps=%.1f",
        len(out), breakdown, avg_steps,
    )
    return out, {
        "templates": breakdown,
        "total": len(out),
        "avg_steps": round(avg_steps, 2),
        "seed": seed,
    }
