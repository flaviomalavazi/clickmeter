"""Server-side sampler: what ClickHouse reports about *itself* during a run.

The client-side Monitor (jmeter_bench.monitor) sees only what the load
generator can measure from outside — RTT, sockets, a running-query count. This
sampler reads ClickHouse's native system tables so the verdict can attribute a
bottleneck to the server with evidence instead of inferring it from latency:

  - system.metrics              instantaneous gauges (running queries, merges,
                                connections, tracked memory)
  - system.events               cumulative counters; we store per-interval
                                deltas so plot/verdict can derive rates and the
                                CPU-wait / IO-wait / S3 ratios that name the
                                bottleneck
  - system.asynchronous_metrics OS-level gauges already normalized per core
                                (CPU user/io-wait fraction, load average, RSS)

Cloud note: a ClickHouse Cloud service is multi-node behind one endpoint, so a
plain `system.metrics` read only reflects whichever replica the load balancer
picked. We wrap reads in `clusterAllReplicas('<cluster>', system.X)` (cluster
`default` on Cloud) and aggregate across replicas — SUM for counters/gauges,
AVG for the already-normalized async metrics. If the cluster name doesn't
resolve or the user can't read the table, we fall back to the plain table, and
if even that fails we degrade to client-only (today's behavior) with a warning.

Output is a wide `server-<stamp>.csv` keyed by `epoch_ms` so it aligns with the
client `monitor-<stamp>.csv` on the same time axis. The sampler tags its own
queries with `log_comment='clickmeter-monitor'` so the post-run query_log
analysis can exclude them from the benchmark's own traffic.
"""

import csv
import threading
import time

MONITOR_LOG_COMMENT = "clickmeter-monitor"

# Instantaneous gauges from system.metrics. SUM across replicas = whole-service.
METRICS = [
    "Query",                                  # concurrent running queries
    "Merge",                                  # background merges in flight
    "BackgroundMergesAndMutationsPoolTask",   # merge/mutation pool occupancy
    "TCPConnection",
    "HTTPConnection",
    "MemoryTracking",                         # bytes tracked by the server
    "QueryThread",                            # threads busy on queries
]

# Cumulative counters from system.events. We store the per-interval delta of
# each (SUM across replicas), so a rate is delta/dt and a ratio is delta/delta.
EVENTS = [
    "SelectQuery",                    # queries actually executed (vs JMeter req)
    "FailedQuery",                    # server-side failures (vs JMeter errors)
    "SelectedBytes",                  # data scanned -> efficiency
    "SelectedRows",
    "OSCPUVirtualTimeMicroseconds",   # CPU consumed
    "OSCPUWaitMicroseconds",          # runnable-but-waiting -> CPU saturation
    "OSIOWaitMicroseconds",           # blocked on IO -> disk bound
    "DiskReadElapsedMicroseconds",
    "S3ReadMicroseconds",             # object-storage read latency (Cloud)
    "ReadBufferFromS3Bytes",
    "ContextLockWaitMicroseconds",    # lock contention
    "MarkCacheHits",
    "MarkCacheMisses",
    "QueryMemoryLimitExceeded",
]

# OS gauges from system.asynchronous_metrics. Already normalized per core where
# noted; AVG across replicas.
ASYNC = [
    "OSUserTimeNormalized",      # 0..1 per core: userspace CPU
    "OSSystemTimeNormalized",    # 0..1 per core: kernel CPU
    "OSIOWaitTimeNormalized",    # 0..1 per core: iowait
    "LoadAverage1",
    "MemoryResident",            # RSS bytes
    "MaxPartCountForPartition",  # too-many-parts pressure
]

COLUMNS = (
    ["epoch_ms", "t_rel_s", "ch_ok"]
    + [f"m_{m}" for m in METRICS]
    + [f"e_{e}" for e in EVENTS]      # per-interval deltas
    + [f"a_{a}" for a in ASYNC]
)


def _make_client(cfg):
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=cfg["ch_host"],
        port=int(cfg["ch_port"]),
        username=cfg.get("ch_user", "default"),
        password=cfg.get("ch_password", ""),
        secure=(cfg.get("ch_protocol", "https") == "https"),
        connect_timeout=5,
        send_receive_timeout=15,
        # Tag the sampler's own queries so query_log analysis can drop them.
        settings={"log_comment": MONITOR_LOG_COMMENT},
    )


class ServerSampler(threading.Thread):
    """Samples ClickHouse system tables every `interval` seconds into a CSV.

    Never raises into the benchmark: any failure flips the sampler to degraded
    mode (blank server columns) and is surfaced via `self.error`.
    """

    def __init__(self, cfg: dict, out_path, interval: float = 2.0):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.out_path = str(out_path)
        self.interval = float(interval)
        self.cluster = (cfg.get("ch_cluster") or "").strip() or None
        self._stop = threading.Event()
        self.error = None
        self.degraded = False
        # filled in by _probe(): the table-expression builder actually usable
        self._use_cluster = self.cluster is not None

    def stop(self):
        self._stop.set()

    # --- table-expression helpers -------------------------------------------

    def _expr(self, table: str) -> str:
        """`system.<table>`, wrapped in clusterAllReplicas when a cluster is set
        and confirmed reachable by the probe."""
        if self._use_cluster:
            return f"clusterAllReplicas('{self.cluster}', system.{table})"
        return f"system.{table}"

    def _agg_query(self, table, key_col, val_agg, names) -> str:
        names_sql = ", ".join("'" + n.replace("'", "''") + "'" for n in names)
        return (
            f"SELECT {key_col}, {val_agg} FROM {self._expr(table)} "
            f"WHERE {key_col} IN ({names_sql}) GROUP BY {key_col}"
        )

    def _fetch_map(self, client, table, key_col, val_agg, names) -> dict:
        res = client.query(self._agg_query(table, key_col, val_agg, names))
        return {row[0]: float(row[1]) for row in res.result_rows}

    # --- lifecycle ----------------------------------------------------------

    def run(self):
        try:
            self._loop()
        except Exception as exc:  # never let the sampler crash the run
            self.error = exc
            self.degraded = True

    def _probe(self, client):
        """Confirm we can read the metrics table; if clusterAllReplicas fails
        (bad cluster name / no grant), retry against the plain table."""
        try:
            client.query(self._agg_query("metrics", "metric", "sum(value)", ["Query"]))
            return
        except Exception as exc:
            if self._use_cluster:
                self._use_cluster = False  # fall back to single-node table
                try:
                    client.query(
                        self._agg_query("metrics", "metric", "sum(value)", ["Query"])
                    )
                    self.error = (
                        f"clusterAllReplicas('{self.cluster}', ...) unavailable "
                        f"({exc}); sampling the local node only."
                    )
                    return
                except Exception as exc2:
                    raise exc2
            raise exc

    def _loop(self):
        try:
            client = _make_client(self.cfg)
            self._probe(client)
        except Exception as exc:
            # No system-table access: write a header-only file and bail so the
            # rest of the harness behaves exactly as it did before this feature.
            self.error = exc
            self.degraded = True
            with open(self.out_path, "w", newline="") as fh:
                csv.writer(fh).writerow(COLUMNS)
            return

        prev_events = None  # previous cumulative event sums, for deltas
        with open(self.out_path, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(COLUMNS)
            fh.flush()
            t0 = time.time()

            while not self._stop.is_set():
                now = time.time()
                ch_ok = 1
                metrics = events = async_m = {}
                try:
                    metrics = self._fetch_map(
                        client, "metrics", "metric", "sum(value)", METRICS
                    )
                    events_now = self._fetch_map(
                        client, "events", "event", "sum(value)", EVENTS
                    )
                    async_m = self._fetch_map(
                        client, "asynchronous_metrics", "metric", "avg(value)", ASYNC
                    )
                except Exception:
                    ch_ok = 0
                    events_now = None

                # system.metrics 'Query' counts this sampler's own query; net it.
                if "Query" in metrics:
                    metrics["Query"] = max(metrics["Query"] - 1, 0)

                # Events are cumulative: report the delta since the last sample.
                # The first sample has no baseline -> blanks.
                if events_now is not None and prev_events is not None:
                    event_deltas = {
                        e: max(events_now.get(e, 0) - prev_events.get(e, 0), 0)
                        for e in EVENTS
                    }
                else:
                    event_deltas = {}
                if events_now is not None:
                    prev_events = events_now

                row = [int(now * 1000), round(now - t0, 1), ch_ok]
                row += [_fmt(metrics.get(m)) for m in METRICS]
                row += [_fmt(event_deltas.get(e)) for e in EVENTS]
                row += [_fmt(async_m.get(a)) for a in ASYNC]
                writer.writerow(row)
                fh.flush()
                self._stop.wait(self.interval)


def _fmt(v):
    if v is None:
        return ""
    # ints stay ints; everything else rounded for a readable CSV
    return int(v) if float(v).is_integer() else round(v, 4)
