# ClickHouse JMeter Benchmark

Single-machine [Apache JMeter](https://jmeter.apache.org/) harness for driving high-concurrency ClickHouse query workloads over the HTTPS interface (`:8443`). Queries are parameterized from a CSV you supply.

## Layout

```
clickhouse-jmeter-benchmark/
├── jmeter/clickhouse-benchmark.jmx   # the test plan
├── queries/
│   ├── queries.csv                   # YOUR query templates + params (sample provided)
│   └── README.md                     # CSV format spec
├── schema/schema.sql.example         # placeholder DDL — replace with your real schema
├── config/benchmark.properties       # run params only: threads, ramp-up, duration, ...
├── .env                              # ClickHouse connection + credentials (gitignored)
├── .env.example                      # template for .env
├── pyproject.toml                    # uv project: the `bench` CLI + deps
├── jmeter_bench/                     # Python harness (run, monitor, analyze, plot)
│   ├── runner.py                     # launches JMeter (JVM autosizing, -J forwarding)
│   ├── monitor.py                    # samples this machine vs ClickHouse during a run
│   ├── analyze.py                    # JTL summary (throughput, percentiles, errors)
│   └── plot.py                       # bottleneck verdict figure
├── scripts/
│   ├── run.sh                        # shell launcher with JVM autosizing
│   ├── load-schema.sh                # apply DDL via HTTP
│   ├── tune-os.sh                    # Linux ulimit + sysctl tuning
│   └── analyze-results.sh            # terminal summary of a JTL file
└── results/                          # JTL, HTML reports, monitor CSVs, verdict PNGs
```

## Prerequisites

- JMeter 5.6+ on `PATH` (`brew install jmeter` / [download](https://jmeter.apache.org/download_jmeter.cgi))
- Java 17+ (JMeter requirement)
- A reachable ClickHouse instance with the HTTP interface enabled
- [uv](https://docs.astral.sh/uv/) for the Python harness (`brew install uv`); `uv sync` installs everything else

## Quickstart

> Benchmarking tables that already exist? Skip step 1 — see
> [Benchmarking an existing database](#benchmarking-an-existing-database-the-common-case) below.

```bash
# 1. (Only if you need a fresh dataset) drop your DDL into schema/schema.sql, or
#    keep the example for smoke testing, then seed it. Skip this against an
#    existing database.
cp schema/schema.sql.example schema/schema.sql
scripts/load-schema.sh schema/schema.sql

# 2. Replace queries/queries.csv with your own query templates. See queries/README.md.

# 3. Put the ClickHouse connection + credentials in .env:
#       cp .env.example .env   &&   edit ch_host / ch_port / ch_protocol / ch_database / ch_user / ch_password
#    Tune run parameters (threads, ramp_up, duration) in config/benchmark.properties.

# 4. Smoke test at low concurrency first.
scripts/run.sh threads=50 duration=30

# 5. Once green, ramp to the real target.
scripts/run.sh threads=40000 ramp_up=180 duration=600
```

`run.sh` writes:

- `results/results-<timestamp>.jtl` — raw sample log (CSV)
- `results/report-<timestamp>/index.html` — JMeter's HTML dashboard
- `results/jmeter-<timestamp>.log` — JMeter's own log

`scripts/analyze-results.sh results/results-*.jtl` prints a one-screen summary.

## Benchmarking an existing database (the common case)

Most of the time you're pointing this harness at tables that **already exist** —
a production-like dataset, a staging copy, or a database someone else loaded.
In that case you **skip schema loading entirely**: there's no DDL to apply and
nothing in `schema/` to touch. You only need to (1) tell the harness where the
database lives and (2) write queries that match its real tables.

```bash
# 1. Point .env at the existing instance/database — no schema step.
cp .env.example .env
# edit ch_host / ch_port / ch_protocol / ch_user / ch_password, and set
#   ch_database  to the database you already have (e.g. analytics, prod, ...)

# 2. Write queries/queries.csv against your existing tables & columns.
#    Reference real table and column names — see "How parameterized queries work".
#    cp queries/queries.csv.example queries/queries.csv   # then edit

# 3. (Optional but recommended) sanity-check connectivity + a real query first.
scripts/run.sh threads=1 loops=1 duration=10

# 4. Smoke test at low concurrency, then ramp.
scripts/run.sh threads=50 duration=30
scripts/run.sh threads=40000 ramp_up=180 duration=600
```

Notes when running against an existing schema:

- **Don't run `load-schema.sh`.** It's only for seeding a fresh benchmark
  database (see [below](#creating-a-fresh-benchmark-database-smoke-testing)).
  The `schema/` directory and its example DDL are irrelevant here.
- **`ch_database` must name the existing database.** Queries that omit a
  database prefix resolve against `ch_database`; queries can also fully qualify
  tables as `db.table` to hit a different one.
- **Every column you reference must exist in the real schema.** A query hitting
  an unknown identifier returns HTTP 404 (`UNKNOWN_IDENTIFIER`) and counts as a
  failed sample, quietly inflating your error rate. Validate the queries
  interactively before a high-concurrency run.
- **You're hitting live data.** All queries are `SELECT`-style reads, but make
  sure the user in `.env` has read-only access if you're pointing at anything
  you can't afford to perturb.

### Creating a fresh benchmark database (smoke testing)

If you *don't* have a schema yet — or just want to validate the harness
end-to-end before pointing it at real data — `schema/schema.sql.example` creates
a self-contained `bench.events` table that matches `queries/queries.csv.example`:

```bash
# Apply the example DDL (seeds ~10M rows) to a throwaway benchmark database.
cp schema/schema.sql.example schema/schema.sql      # or drop in your own DDL
scripts/load-schema.sh schema/schema.sql

# Then point queries/queries.csv at it and run as above.
```

`load-schema.sh` reads the same `.env`, so set `ch_database` to the database the
DDL creates (`bench` in the example) before running it.

## Python harness (uv) — recommended

The `bench` CLI wraps the same JMeter plan but adds live monitoring and a
bottleneck verdict, so you can tell whether **your machine** or **ClickHouse**
is the limiter. One-time setup:

```bash
uv sync          # creates .venv and installs pandas, matplotlib, psutil, clickhouse-connect
```

Then:

```bash
# Run: monitors the load generator + ClickHouse, runs JMeter, analyzes, and plots a verdict.
uv run bench run threads=50 duration=30
uv run bench run threads=40000 ramp_up=180 duration=600 --interval 5

# Summarize an existing JTL (pandas-based replacement for analyze-results.sh):
uv run bench analyze results/results-<timestamp>.jtl

# Re-plot from saved data (JTL + monitor CSV) without re-running:
uv run bench plot results/results-<timestamp>.jtl results/monitor-<timestamp>.csv
```

Config overrides use the same `key=value` syntax as `run.sh`. In addition to
the JTL/HTML/log files, a `bench run` writes:

- `results/monitor-<timestamp>.csv` — per-interval time series of JMeter CPU/RSS,
  TIME_WAIT/ESTABLISHED socket counts (this machine), and ClickHouse running-query
  count + round-trip latency (the server).
- `results/server-<timestamp>.csv` — per-interval ClickHouse **system-table** time
  series: running queries, background merges, tracked memory (`system.metrics`);
  per-interval deltas of CPU/IO-wait/S3 microseconds, bytes scanned, cache hits,
  memory-limit hits (`system.events`); and normalized host CPU / IO-wait / load
  (`system.asynchronous_metrics`). See [Server-side visibility](#server-side-visibility-clickhouse-system-tables).
- `results/query_log-<timestamp>.csv` — post-run per-query attribution from
  `system.query_log`, one row per normalized query pattern.
- `results/verdict-<timestamp>.png` — a four-panel figure (throughput, client-vs-server
  latency, load-generator saturation, server concurrency) — **plus two more panels
  (host CPU vs IO-wait, data-scanned/s + cache hit ratio) when server sampling is on** —
  topped with a heuristic verdict: *your machine*, *ClickHouse (CPU / IO / S3 / memory /
  concurrency)*, or *headroom/network-bound*.

**How the verdict works.** The key signal is the latency panel: if client p50/p99 sit
far above the server's `SELECT` round-trip, the time is being lost on your host or the
network — not in ClickHouse. The verdict also flags JMeter CPU saturation (% of all
cores), TIME_WAIT socket pressure, rising server RTT under load, and high error rates
(503/429 → server limits; 403/401 → auth, not performance). **When server sampling is
on, it adds ClickHouse-side rules** — CPU-bound, CPU-starved, disk/IO-bound,
object-storage-bound (S3), and memory-pressure — derived from `system.events` and
`system.asynchronous_metrics`, so a *ClickHouse* verdict comes with server-reported
evidence rather than an inference from latency. It's a heuristic from a single run —
confirm with a thread sweep (`10→50→100→500`) to find the knee.

## Server-side visibility (ClickHouse system tables)

The client-side monitor can only measure ClickHouse from the outside (round-trip
latency, a running-query count). To see *why* the server is slow, the `bench`
harness also reads ClickHouse's own [system
tables](https://clickhouse.com/docs/operations/system-tables/) during and after
each run. This is on by default (`server_monitor=true`); it degrades to the
client-only behavior if the benchmark user can't read system tables.

**During the run** (`server-<timestamp>.csv`, sampled every `--interval`):

| table | what we pull | bottleneck it exposes |
| ----- | ------------ | --------------------- |
| `system.metrics` | `Query`, `Merge`, `BackgroundMergesAndMutationsPoolTask`, `TCPConnection`/`HTTPConnection`, `MemoryTracking` | concurrency ceiling, background-merge contention, connection/memory pressure |
| `system.events` (per-interval deltas) | `OSCPUWaitMicroseconds`÷`OSCPUVirtualTimeMicroseconds`, `OSIOWaitMicroseconds`, `S3ReadMicroseconds`, `ContextLockWaitMicroseconds`, `SelectedBytes`, `MarkCacheHits`/`Misses`, `QueryMemoryLimitExceeded` | **CPU-bound / CPU-starved**, **disk-IO-bound**, **object-storage-bound (S3)**, lock contention, scan volume, cold cache, memory kills |
| `system.asynchronous_metrics` | `OSUserTimeNormalized`, `OSIOWaitTimeNormalized`, `LoadAverage1`, `MemoryResident`, `MaxPartCountForPartition` | host CPU vs IO-wait (normalized per core), too-many-parts |

**After the run** (`query_log-<timestamp>.csv`, per query pattern): every request
is tagged with `log_comment=clickmeter-<timestamp>`, so the harness filters
`system.query_log` to exactly this run and groups by `normalized_query_hash`. For
each pattern you get server-side p50/p99 `query_duration_ms`, rows/bytes read,
peak memory, error count, and a ProfileEvents breakdown that labels the limiter
(`cpu` / `cpu-starved` / `io-wait` / `s3` / `lock`). Comparing server-side
`query_duration_ms` against the client latency in the JTL is what cleanly
separates "ClickHouse is slow" from "the time is lost in the network/client".

### ClickHouse Cloud and clusters

A Cloud service is multi-node behind one endpoint, so a single connection only
samples whichever replica the load balancer picked. Set `ch_cluster=default`
(the Cloud cluster name; it's the default in `benchmark.properties`) and the
harness wraps every read in `clusterAllReplicas('default', system.*)` to
aggregate across replicas — summing counters/gauges and averaging the normalized
async metrics. For a **single self-managed node**, leave `ch_cluster` blank to
read the plain `system.*` tables.

For the post-run `query_log` pull specifically, the harness reads
`clusterAllReplicas('default', merge(system, '^query_log'))`. The
[`merge()`](https://clickhouse.com/docs/sql-reference/table-functions/merge)
table function unions the current `query_log` with the rotated `query_log_<N>`
tables ClickHouse leaves behind when the log schema changes across an upgrade —
without it a run that straddled a rotation would lose rows. It walks a fallback
chain so it always returns *something*:
`clusterAllReplicas(cluster, merge(...))` → `merge(system, '^query_log')`
(local node) → `system.query_log` (current table only), noting in the output
whenever it had to drop down (missing cluster, missing grant, or a schema
mismatch among very old rotated tables). The live `system.metrics`/`events`/
`asynchronous_metrics` tables aren't `*_log` tables and never rotate, so they're
read directly (cluster-wrapped only).

### Permissions & caveats

- The benchmark user needs `SELECT` on `system.metrics`, `system.events`,
  `system.asynchronous_metrics`, and `system.query_log` (the Cloud `default`
  user has these). A read-only/restricted user simply yields the client-only
  verdict — nothing breaks.
- `system.metrics`/`system.events` are **server-wide**: on a shared service the
  deltas include other activity. The `log_comment`-scoped `query_log` is the
  precise per-benchmark view; treat the live series as whole-server context.
- The sampler's own queries are tagged `log_comment=clickmeter-monitor` and
  excluded from the per-query report, and its one in-flight query is netted out
  of the running-query count.
- `system.query_log` flushes on an interval (~7.5s); the harness issues
  `SYSTEM FLUSH LOGS` after the run if granted, so the final seconds are
  captured.
- Disable all of this with `server_monitor=false`.

## Honest caveats about 40k concurrent threads on one machine

This framework was built using a single-machine path, so be clear-eyed about what this means:

1. **JMeter is thread-per-VU.** 40k JMeter threads = 40k OS threads. With the default `-Xss512k`, that's ~20 GB just for thread stacks. `run.sh` autosizes to `-Xss256k` plus 8–12 GB heap, but you still need a machine with **at least 32 GB RAM** and ideally 16+ cores to drive this load without becoming the bottleneck yourself.
2. **You'll hit OS limits long before JMeter does.** Run `scripts/tune-os.sh` as root on Linux, or on macOS:
   ```bash
   sudo launchctl limit maxfiles 200000 200000
   ulimit -n 200000
   ```
3. **Ephemeral port exhaustion** is the next ceiling. Keep-alive is enabled in the JMX (so most threads reuse connections), but watch `ss -s` on the driver host and `system.metrics` on ClickHouse during the run.
4. **A single JVM realistically caps around 10k–20k useful concurrent threads** even with tuning. If you can't hit 40k from one box, the answers are: (a) switch to distributed JMeter (controller + workers), (b) use the [Async HTTP / Java sampler](https://jmeter-plugins.org/wiki/HttpRawRequest/) so one thread can drive multiple in-flight requests, or (c) accept "40k req/s sustained" instead of "40k strictly concurrent" as the real KPI.
5. **The driver host should not also run ClickHouse.** If it does, you're benchmarking the contention between them, not ClickHouse.

## Connection & credentials (`.env`)

The **entire ClickHouse connection — host, port, protocol, database — plus the
credentials** lives in a gitignored `.env` at the repo root. It's both secret
and environment-specific, so it stays out of version control:

```bash
cp .env.example .env
# edit ch_host / ch_port / ch_protocol / ch_database / ch_user / ch_password
# (single/double quotes optional, stripped on load)
```

`config/benchmark.properties` holds **only the run parameters** (threads,
ramp-up, duration, loops, query CSV) — nothing environment-specific.

Both the shell launcher (`run.sh`) and the Python harness (`bench`) load `.env`
automatically, as does `scripts/load-schema.sh`. Precedence is
**CLI args > `.env` > `benchmark.properties`**.

The password is **never** passed as a `-Jch_password=…` argument (which would be
visible in `ps`); `run.sh`/`runner.py` write it to a `chmod 600` temp file handed
to JMeter via `-q`, then delete it when the run ends.

## Configuration knobs

All keys are overridable on the CLI (`key=val`). The `source` column shows where
each one is defined:

| key            | meaning                                                       | default     | source |
| -------------- | ------------------------------------------------------------- | ----------- | ------ |
| `ch_host`      | ClickHouse host                                               | `localhost` | **.env** |
| `ch_port`      | HTTP port                                                     | `8443`      | **.env** |
| `ch_protocol`  | `http` or `https`                                             | `http`      | **.env** |
| `ch_database`  | database name                                                 | `default`   | **.env** |
| `ch_user`      | ClickHouse user                                               | `default`   | **.env** |
| `ch_password`  | password (blank for no auth)                                  | ``          | **.env** |
| `threads`      | simultaneous virtual users                                    | `800`     | properties |
| `ramp_up`      | seconds to reach `threads`                                    | `10`       | properties |
| `duration`     | wall-clock seconds for the run                                | `300`       | properties |
| `loops`        | iterations per thread (`-1` = run until duration)             | `-1`        | properties |
| `queries_csv`  | path to CSV (relative to `jmeter/`)                           | `../queries/queries.csv` | properties |
| `jtl_path`     | output JTL path (run.sh overrides with timestamped name)      | `../results/results.jtl` | properties |
| `server_monitor` | sample ClickHouse system tables + pull query_log (`bench` only) | `true`    | properties |
| `ch_cluster`   | cluster for `clusterAllReplicas()` aggregation; blank = single node | `default` | properties |

CLI override examples:

```bash
scripts/run.sh threads=1000                          # quick load test
scripts/run.sh ch_host=ch-prod.internal threads=40000 duration=900
JVM_ARGS="-Xms16g -Xmx16g -Xss256k" scripts/run.sh   # custom JVM tuning
```

## How parameterized queries work

`queries/queries.csv` rows look like:

```csv
query_template,p1,p2,p3,p4,p5
"SELECT count() FROM events WHERE toDate(ts_column) = '${p1}'",2026-06-01,,,,
```

JMeter's `${__eval(${query_template})}` resolves the `${pN}` placeholders against
the per-row values. `shareMode=all` + `recycle=true` means VUs cooperatively
consume rows and the file loops, so you can drive 40k threads off a small CSV.

> Make sure every column you reference actually exists in your schema — a query
> that hits an unknown identifier comes back as HTTP 404 (`UNKNOWN_IDENTIFIER`)
> and counts as a failed sample, quietly inflating your error rate.

To make each thread see a unique row instead, edit the `<CSVDataSet>` element
in the JMX and change `shareMode` to `shareMode.thread`.

## Editing the test plan visually

```bash
jmeter -t jmeter/clickhouse-benchmark.jmx
```

Then add timers, throughput controllers, the
[Concurrency Thread Group](https://jmeter-plugins.org/wiki/ConcurrencyThreadGroup/)
(more efficient at high concurrency than the default Thread Group), or
listeners as needed. Save back to the same path and re-run.

## Troubleshooting

| symptom                                              | likely cause + fix                                                                              |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `java.lang.OutOfMemoryError: unable to create native thread` | OS thread limit. Run `tune-os.sh` (Linux) or raise `ulimit -u`.                                 |
| `Non HTTP response code: java.net.SocketException`   | Ephemeral port exhaustion. Tune `net.ipv4.ip_local_port_range` + `tcp_tw_reuse`.                 |
| `Code: 202, DB::Exception: Too many simultaneous queries` | Raise `max_concurrent_queries` on ClickHouse.                                                   |
| Throughput plateaus well below target                | JMeter driver is the bottleneck. Lower `threads`, increase `loops`, or go distributed.          |
| HTML report not generated                            | The run died before completing. Check `results/jmeter-*.log`.                                   |
| `WARNING: package sun.awt.X11 not in java.desktop` / `package scanning to locate plugins is deprecated` | Harmless JMeter/JDK startup noise; `bench` filters these from the console. Set `BENCH_RAW_JMETER=1` to see JMeter's unfiltered output. |
