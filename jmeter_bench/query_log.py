"""Post-run per-query attribution from system.query_log.

The live sampler (jmeter_bench.server) shows *whole-server* state over time.
This module answers the next question — **which query, and why** — by reading
the rows ClickHouse logged for this exact run and aggregating them by
normalized query pattern.

Each request was tagged with `log_comment='clickmeter-<stamp>'` (see
jmeter_bench.runner.run_log_comment), so we can pull precisely this run's
queries even on a shared/busy service, then group by `normalized_query_hash`
to get, per template:

  - how often it ran and its server-side p50/p99 `query_duration_ms`
  - rows/bytes read and peak memory (the cost drivers)
  - a ProfileEvents breakdown that names the bottleneck: CPU vs IO-wait vs S3
  - how many executions errored (`type != 'QueryFinish'`)

Server-side `query_duration_ms` here, compared against the client-measured
latency in the JTL, is what separates "ClickHouse is slow" from "the time is
lost in the network / HTTP / client" — the core question the verdict answers.

Cloud: query_log is per-node, so we read `clusterAllReplicas('<cluster>',
system.query_log)` to cover every replica that served traffic. Falls back to
the plain table, and degrades to an empty result (with a reason) if the user
can't read query_log at all.
"""

import csv

from .server import MONITOR_LOG_COMMENT, _make_client

# ProfileEvents we surface per query pattern. Microsecond counters become the
# CPU/IO/S3 attribution; byte/row counters quantify the work.
PROFILE_EVENTS = [
    "OSCPUVirtualTimeMicroseconds",
    "OSCPUWaitMicroseconds",
    "OSIOWaitMicroseconds",
    "S3ReadMicroseconds",
    "NetworkSendElapsedMicroseconds",
    "ContextLockWaitMicroseconds",
]


def _table_exprs(cluster, name="query_log"):
    """FROM-expressions to try, most-complete first.

    `merge(system, '^query_log')` unions the current `query_log` with the
    rotated `query_log_<N>` tables ClickHouse leaves behind when the log schema
    changes across an upgrade — without it we'd only see the current table and
    could miss rows that straddled a rotation. `clusterAllReplicas` then fans
    that across every node of a (Cloud) service. We fall back step by step so a
    missing cluster, or a schema mismatch among very old rotated tables, still
    yields a result instead of an error:

        clusterAllReplicas(cluster, merge(...))   # all nodes, all rotations
        merge(system, '^query_log')               # local node, all rotations
        system.query_log                          # local node, current only
    """
    merged = f"merge(system, '^{name}')"
    exprs = []
    if cluster:
        exprs.append(f"clusterAllReplicas('{cluster}', {merged})")
    exprs.append(merged)
    exprs.append(f"system.{name}")
    return exprs


def _pe(col):
    """SQL to pull a ProfileEvents counter (a Map(String, UInt64))."""
    return f"sum(ProfileEvents['{col}'])"


def _build_query(table_expr, log_comment):
    pe_cols = ",\n        ".join(
        f"{_pe(k)} AS pe_{k}" for k in PROFILE_EVENTS
    )
    return f"""
    SELECT
        normalized_query_hash AS qhash,
        normalizeQuery(any(query)) AS sample_query,
        count()                AS runs,
        countIf(type != 'QueryFinish') AS errors,
        round(quantile(0.50)(query_duration_ms), 1) AS dur_p50_ms,
        round(quantile(0.99)(query_duration_ms), 1) AS dur_p99_ms,
        round(avg(read_rows))  AS avg_read_rows,
        round(avg(read_bytes)) AS avg_read_bytes,
        round(avg(memory_usage)) AS avg_mem_bytes,
        max(memory_usage)      AS peak_mem_bytes,
        {pe_cols}
    FROM {table_expr}
    WHERE log_comment = {{lc:String}}
      AND type != 'QueryStart'
      AND event_time >= now() - INTERVAL 1 DAY
    GROUP BY qhash
    ORDER BY dur_p99_ms DESC
    """


def fetch(cfg: dict, log_comment: str, out_csv=None, flush=True) -> dict:
    """Pull and aggregate this run's query_log. Returns
    {"rows": [...], "error": <str|None>, "total_runs": int}. Never raises."""
    cluster = (cfg.get("ch_cluster") or "").strip() or None
    try:
        client = _make_client(cfg)
    except Exception as exc:
        return {"rows": [], "error": f"could not connect: {exc}", "total_runs": 0}

    # query_log flushes on an interval (default ~7.5s). Nudge it so rows from
    # the final seconds of the run are visible; ignore if not granted.
    if flush:
        try:
            client.command("SYSTEM FLUSH LOGS")
        except Exception:
            pass

    # Try each FROM-expression in order; the first that succeeds wins. Note in
    # `err` whenever we had to drop to a less-complete source.
    exprs = _table_exprs(cluster)
    cols = rows = None
    err = None
    last_exc = None
    for i, expr in enumerate(exprs):
        try:
            res = client.query(_build_query(expr, log_comment),
                               parameters={"lc": log_comment})
            cols, rows = res.column_names, res.result_rows
            if i > 0:
                err = f"fell back to `{expr}` ({last_exc})"
            break
        except Exception as exc:
            last_exc = exc
    if rows is None:
        return {"rows": [], "error": str(last_exc), "total_runs": 0}

    records = [dict(zip(cols, r)) for r in rows]
    if out_csv and records:
        with open(out_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(records)

    return {
        "rows": records,
        "error": err,
        "total_runs": sum(int(r.get("runs", 0)) for r in records),
    }


def classify(rec: dict) -> str:
    """One-word bottleneck label for a query pattern from its ProfileEvents."""
    cpu = rec.get("pe_OSCPUVirtualTimeMicroseconds", 0) or 0
    cpu_wait = rec.get("pe_OSCPUWaitMicroseconds", 0) or 0
    io_wait = rec.get("pe_OSIOWaitMicroseconds", 0) or 0
    s3 = rec.get("pe_S3ReadMicroseconds", 0) or 0
    lock = rec.get("pe_ContextLockWaitMicroseconds", 0) or 0
    busiest = max(("cpu", cpu), ("io-wait", io_wait), ("s3", s3),
                  ("lock", lock), key=lambda kv: kv[1])
    if busiest[1] == 0:
        return "n/a"
    label = busiest[0]
    # CPU starvation (runnable but waiting for a core) dominates real CPU work.
    if cpu and cpu_wait > cpu:
        return "cpu-starved"
    return label


def _human_bytes(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def print_report(result: dict, top: int = 10):
    if result.get("error") and not result.get("rows"):
        print(f"  [query_log] unavailable: {result['error']}")
        print("  (server-side per-query attribution skipped — see README grants.)")
        return
    if result.get("error"):
        print(f"  [query_log] note: {result['error']}")
    rows = result.get("rows", [])
    if not rows:
        print("  [query_log] no rows matched this run's log_comment "
              "(flush lag, or the tag didn't reach the server).")
        return

    print(f"Per-query attribution (system.query_log, {result['total_runs']:,} executions):")
    print(f"  {'p99 ms':>8} {'p50 ms':>8} {'runs':>8} {'err':>5} "
          f"{'read':>9} {'peak mem':>9}  {'limiter':<11} query")
    for rec in rows[:top]:
        q = (rec.get("sample_query") or "").replace("\n", " ")
        q = (q[:60] + "…") if len(q) > 61 else q
        print(f"  {rec.get('dur_p99_ms', 0):>8} {rec.get('dur_p50_ms', 0):>8} "
              f"{int(rec.get('runs', 0)):>8} {int(rec.get('errors', 0)):>5} "
              f"{_human_bytes(rec.get('avg_read_bytes')):>9} "
              f"{_human_bytes(rec.get('peak_mem_bytes')):>9}  "
              f"{classify(rec):<11} {q}")
    if len(rows) > top:
        print(f"  … {len(rows) - top} more pattern(s) in the CSV.")
