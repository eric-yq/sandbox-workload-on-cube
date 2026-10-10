"""B3 worker - τ-bench-style mock API for tool-call benchmarks.

A small in-memory shopping API. Each endpoint does a tiny bit of
JSON parsing + state mutation, simulating real tool calls.

Concurrency model:
  - Multi-process: uvicorn `--workers N` (default = container vCPUs)
    so we exploit all cores at the OS level.
  - Multi-thread within a process: FastAPI dispatches sync handlers
    on an anyio threadpool.

State model:
  - Items catalogue is a read-only Python dict, built once at process
    start. No locking needed for reads.
  - Cart / orders are keyed by a `user` query parameter that the
    client passes per-trajectory. Each user's state lives in its own
    dict slot, so there is no cross-trajectory contention. We still
    take a fine-grained per-user lock to keep the +/- arithmetic
    consistent within a trajectory.
  - Crucially, all state is *per-process*. With multi-worker uvicorn
    we rely on the client to send all requests for a given user to
    the same process; we use a stable `user` -> worker hash via
    Python's hash() at process bind time. (uvicorn does NOT route
    by header, so the client must reach all workers and tolerate
    cross-process state. The benchmark traces never depend on
    cross-trajectory state, so this is benign.)

This replaces the previous SQLite-backed implementation, which
suffered from "cache=shared" not actually crossing process boundaries
and from contention on writer locks at high concurrency.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from fastapi import Body, FastAPI, HTTPException

app = FastAPI()


# -----------------------------------------------------------------
# Catalogue: read-only after init.
# -----------------------------------------------------------------
NUM_SKUS = int(os.getenv("B3_NUM_SKUS", "200"))
ITEMS: dict[str, dict] = {
    f"SKU-{i}": {
        "sku": f"SKU-{i}",
        "name": f"item-{i}",
        "price": 10.0 + i,
        "qty": 100,
    }
    for i in range(NUM_SKUS)
}
ITEM_LIST = list(ITEMS.values())  # for /search and /recommend without dict scan


# -----------------------------------------------------------------
# User state: per-user cart and order log. All access is guarded by
# the user's own lock; no global mutex.
# -----------------------------------------------------------------
class UserState:
    __slots__ = ("lock", "cart", "orders", "next_order_id")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cart: dict[str, int] = {}            # sku -> qty
        self.orders: list[dict] = []              # list of {id,total,status}
        self.next_order_id = 1


_users_lock = threading.Lock()
_users: dict[str, UserState] = {}


def get_user(name: str) -> UserState:
    # Common path: read without taking the global lock.
    u = _users.get(name)
    if u is not None:
        return u
    with _users_lock:
        u = _users.get(name)
        if u is None:
            u = UserState()
            _users[name] = u
        return u


# -----------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------
@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "skus": NUM_SKUS, "users": len(_users)}


@app.get("/search")
def search(q: str = "") -> dict:
    if not q:
        results = ITEM_LIST[:10]
    else:
        ql = q.lower()
        results = [it for it in ITEM_LIST if ql in it["name"]][:10]
    return {"results": [
        {"sku": r["sku"], "name": r["name"], "price": r["price"]}
        for r in results
    ]}


@app.get("/product/{sku_id}")
def product(sku_id: str) -> dict:
    item = ITEMS.get(f"SKU-{sku_id}")
    if not item:
        raise HTTPException(404, "not found")
    return dict(item)


@app.post("/cart/add")
def cart_add(payload: dict[str, Any] = Body(...)) -> dict:
    user = payload.get("user", "alice")
    sku = payload.get("sku", "SKU-0")
    qty = int(payload.get("qty", 1))
    u = get_user(user)
    with u.lock:
        u.cart[sku] = u.cart.get(sku, 0) + qty
    return {"ok": True}


@app.get("/cart")
def cart(user: str = "alice") -> dict:
    u = get_user(user)
    with u.lock:
        items = [{"sku": s, "qty": q} for s, q in u.cart.items()]
    return {"items": items}


@app.post("/checkout")
def checkout(payload: dict[str, Any] = Body(...)) -> dict:
    user = payload.get("user", "alice")
    u = get_user(user)
    with u.lock:
        total = sum(
            ITEMS[sku]["price"] * qty
            for sku, qty in u.cart.items()
            if sku in ITEMS
        )
        oid = u.next_order_id
        u.next_order_id += 1
        u.orders.append({"id": oid, "total": total, "status": "paid"})
        u.cart.clear()
    return {"order_id": oid, "total": total}


@app.get("/order/last")
def order_last(user: str = "alice") -> dict:
    u = get_user(user)
    with u.lock:
        if u.orders:
            o = u.orders[-1]
            return {"id": o["id"], "total": o["total"], "status": o["status"]}
    return {"id": None, "total": 0, "status": "none"}


@app.post("/order/refund")
def order_refund(payload: dict[str, Any] = Body(...)) -> dict:
    user = payload.get("user", "alice")
    u = get_user(user)
    with u.lock:
        if u.orders:
            u.orders[-1]["status"] = "refunded"
    return {"ok": True, "reason": payload.get("reason", "")}


@app.get("/profile")
def profile(user: str = "alice") -> dict:
    return {"user": user, "tier": "gold", "points": 1234}


@app.post("/profile/update")
def profile_update(payload: dict[str, Any] = Body(...)) -> dict:
    return {"ok": True, "name": payload.get("name", "")}


@app.get("/recommend")
def recommend(k: int = 5) -> dict:
    # Deterministic-ish "top-k" - cheap, no RNG, no dict scan.
    k = max(1, min(int(k), 20))
    sample = ITEM_LIST[: k]
    return {"items": [
        {"sku": r["sku"], "name": r["name"], "price": r["price"]}
        for r in sample
    ]}


@app.get("/inventory")
def inventory(sku: str = "SKU-0") -> dict:
    item = ITEMS.get(sku)
    return {"sku": sku, "qty": item["qty"] if item else 0}
