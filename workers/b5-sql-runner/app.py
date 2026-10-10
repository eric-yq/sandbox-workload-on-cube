"""B5 worker - DuckDB in-process with the TPC-H workload.

At startup:
  - Install + load the official `tpch` DuckDB extension
  - Run `CALL dbgen(sf=...)` to materialize the 8 standard tables
    (lineitem, orders, customer, part, partsupp, supplier, nation, region)
  - Cache the 22 official TPC-H query texts via `tpch_queries()` table function

Per request:
  - POST /query {q: 1..22}  -> run that TPC-H query, return wall_ms + row count

Multi-arch image (linux/amd64 + linux/arm64). On aarch64, DuckDB's
vectorized executor uses NEON / SVE - that is the central signal the
original B5 design called for: c7i (SPR) AVX2/AVX-512 vs c8g
(Graviton4) NEON/SVE on the same columnar OLAP workload.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import duckdb
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Scale factor for TPC-H dbgen. Larger = more work per query (better
# signal) but longer dbgen + more RAM.
#   SF=0.1  ~  600K  lineitem rows  (~80MB)   - smoke test only
#   SF=0.5  ~  3M    lineitem rows  (~250MB)  - decent signal
#   SF=1.0  ~  6M    lineitem rows  (~500MB)  - default
TPCH_SF = float(os.getenv("TPCH_SF", "1.0"))

# DuckDB threads. 0 = auto = all vCPUs of the container.
DUCKDB_THREADS = int(os.getenv("DUCKDB_THREADS", "0"))

# DuckDB memory limit (string accepted by SET memory_limit, e.g. "12GB").
DUCKDB_MEM_LIMIT = os.getenv("DUCKDB_MEM_LIMIT", "")

_con: duckdb.DuckDBPyConnection | None = None
_query_text: dict[int, str] = {}
_dbgen_sec: float = 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _con, _query_text, _dbgen_sec

    print(f"[b5] DuckDB {duckdb.__version__}, sf={TPCH_SF}, "
          f"threads={DUCKDB_THREADS or 'auto'}", flush=True)

    _con = duckdb.connect(database=":memory:")
    if DUCKDB_THREADS > 0:
        _con.execute(f"PRAGMA threads={DUCKDB_THREADS}")
    if DUCKDB_MEM_LIMIT:
        _con.execute(f"SET memory_limit='{DUCKDB_MEM_LIMIT}'")

    # Try LOAD first (extension is baked into the image at build time);
    # only fall back to INSTALL if it isn't there. This avoids any
    # network access at runtime in normal operation.
    try:
        print("[b5] loading pre-baked tpch extension ...", flush=True)
        _con.execute("LOAD tpch")
    except Exception as e:
        print(f"[b5] LOAD failed ({e}); falling back to INSTALL "
              "(needs outbound network to extensions.duckdb.org)",
              flush=True)
        _con.execute("INSTALL tpch")
        _con.execute("LOAD tpch")

    print(f"[b5] running dbgen(sf={TPCH_SF}) ...", flush=True)
    t0 = time.perf_counter()
    _con.execute(f"CALL dbgen(sf={TPCH_SF})")
    _dbgen_sec = time.perf_counter() - t0
    print(f"[b5] dbgen done in {_dbgen_sec:.1f}s", flush=True)

    # The tpch extension exposes the 22 standard queries through a
    # table function: `tpch_queries()`. Schema: (query_nr int, query varchar).
    rows = _con.execute(
        "SELECT query_nr, query FROM tpch_queries() ORDER BY query_nr"
    ).fetchall()
    _query_text = {int(r[0]): r[1] for r in rows}
    print(f"[b5] cached {len(_query_text)} TPC-H query texts, ready",
          flush=True)

    yield

    _con.close()


app = FastAPI(lifespan=lifespan)


class QueryRequest(BaseModel):
    q: int = Field(..., ge=1, le=22)


class QueryResponse(BaseModel):
    q: int
    rows: int
    wall_ms: float


def _exec_blocking(q: int) -> tuple[int, float]:
    """Run TPC-H query `q` on a fresh cursor.

    DuckDB connections are thread-safe but a single connection serializes
    queries internally. Using a per-call cursor isolates query state so
    multiple FastAPI workers can issue requests in parallel without
    interleaving result sets.
    """
    if _con is None:
        raise HTTPException(503, "duckdb not ready")
    sql = _query_text.get(q)
    if not sql:
        raise HTTPException(404, f"unknown TPC-H query {q}")
    cur = _con.cursor()
    try:
        t0 = time.perf_counter()
        rows = cur.execute(sql).fetchall()
        return len(rows), (time.perf_counter() - t0) * 1000.0
    finally:
        cur.close()


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": _con is not None and len(_query_text) > 0,
        "tpch_sf": TPCH_SF,
        "queries_loaded": len(_query_text),
    }


@app.get("/info")
async def info() -> dict:
    if _con is None:
        raise HTTPException(503, "not ready")
    counts: dict[str, int] = {}
    for tbl in ("lineitem", "orders", "customer", "part",
                "partsupp", "supplier", "nation", "region"):
        try:
            n = _con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            counts[tbl] = int(n)
        except Exception:
            pass
    return {
        "engine": "duckdb",
        "duckdb_version": duckdb.__version__,
        "tpch_sf": TPCH_SF,
        "duckdb_threads": DUCKDB_THREADS or "auto",
        "dbgen_sec": round(_dbgen_sec, 2),
        "row_counts": counts,
    }


@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest) -> QueryResponse:
    # Run the (potentially expensive) DuckDB call in a worker thread
    # so the FastAPI event loop stays responsive at high concurrency.
    n_rows, wall = await asyncio.to_thread(_exec_blocking, req.q)
    return QueryResponse(q=req.q, rows=n_rows, wall_ms=wall)
