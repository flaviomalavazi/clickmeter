"""Join benchmark results (JTL) with the client + server time-series and render a
single figure plus a heuristic bottleneck verdict: your machine vs ClickHouse.

Two monitor sources feed this:
  - monitor-<stamp>.csv  client-side (psutil/netstat + a server RTT probe)
  - server-<stamp>.csv   ClickHouse system tables (metrics/events/async_metrics)

When the server CSV is present the verdict can attribute a ClickHouse-side
bottleneck (CPU / IO / S3 / memory) from what the server reports about itself,
instead of inferring it from rising client RTT. When it's absent the behavior is
exactly the pre-existing client-only verdict.
"""

import matplotlib

matplotlib.use("Agg")  # headless: write a PNG, no display needed
import matplotlib.pyplot as plt
import pandas as pd

from .analyze import load_jtl


def _load_monitor(path) -> pd.DataFrame:
    df = pd.read_csv(path)
    for col in df.columns:
        if col != "ch_ok":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _load_server(path) -> pd.DataFrame:
    """Load server-<stamp>.csv; tolerate missing file / header-only (degraded)."""
    if not path:
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except (FileNotFoundError, OSError, pd.errors.EmptyDataError):
        return pd.DataFrame()
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _steady(df: pd.DataFrame) -> pd.DataFrame:
    """Last 80% of the run (skip ramp-up)."""
    if len(df) == 0 or "t_rel_s" not in df.columns or not df["t_rel_s"].notna().any():
        return df
    cutoff = df["t_rel_s"].max() * 0.2
    s = df[df["t_rel_s"] >= cutoff]
    return s if len(s) else df


def _server_signals(srv: pd.DataFrame) -> dict:
    """Derive steady-state ClickHouse signals from the server CSV. Empty dict if
    no usable server data."""
    if len(srv) == 0 or "t_rel_s" not in srv.columns:
        return {}
    s = _steady(srv)

    def med(name):
        return s[name].median() if name in s.columns else float("nan")

    def total(name):
        return s[name].sum() if name in s.columns else 0.0

    cpu_busy = med("a_OSUserTimeNormalized")
    sys_busy = med("a_OSSystemTimeNormalized")
    cpu_busy_pct = None
    if pd.notna(cpu_busy):
        cpu_busy_pct = (cpu_busy + (sys_busy if pd.notna(sys_busy) else 0)) * 100

    iowait_pct = med("a_OSIOWaitTimeNormalized")
    iowait_pct = None if pd.isna(iowait_pct) else iowait_pct * 100

    cpu_us = total("e_OSCPUVirtualTimeMicroseconds")
    cpu_wait_us = total("e_OSCPUWaitMicroseconds")
    io_us = total("e_OSIOWaitMicroseconds")
    s3_us = total("e_S3ReadMicroseconds")

    def frac(part, whole):
        return (part / whole) if whole else None

    sig = {
        "cpu_busy_pct": None if cpu_busy_pct is None else round(cpu_busy_pct, 1),
        "iowait_pct": None if iowait_pct is None else round(iowait_pct, 1),
        "cpu_wait_ratio": frac(cpu_wait_us, cpu_us),
        "io_frac": frac(io_us, io_us + cpu_us),
        "s3_frac": frac(s3_us, s3_us + cpu_us),
        "mem_limit_hits": int(total("e_QueryMemoryLimitExceeded")),
        "merges_med": None if pd.isna(med("m_BackgroundMergesAndMutationsPoolTask"))
        else round(med("m_BackgroundMergesAndMutationsPoolTask"), 1),
        "running_q_med": None if pd.isna(med("m_Query")) else round(med("m_Query"), 1),
    }
    for k in ("cpu_wait_ratio", "io_frac", "s3_frac"):
        if sig[k] is not None:
            sig[k] = round(sig[k], 2)
    return sig


def compute_verdict(jtl, mon, summary, srv=None) -> dict:
    """Transparent, rule-based call. Returns label/color/reason/evidence.

    Order: errors -> client saturation (your machine) -> ClickHouse self-reported
    saturation (CPU/IO/S3/memory) -> server latency rising -> headroom. The first
    matching rule wins, so the most decisive signal names the bottleneck.
    """
    srv = srv if srv is not None else pd.DataFrame()
    ev = {}
    have_mon = len(mon) > 0 and "t_rel_s" in mon.columns

    def col(df, name):
        return df[name] if name in df.columns else pd.Series(dtype="float64")

    steady = _steady(mon) if have_mon else mon

    ncpu_series = col(steady, "ncpu").dropna()
    ncpu = int(ncpu_series.iloc[0]) if len(ncpu_series) else 1

    cpu_med = col(steady, "jmeter_cpu_pct").median()
    cpu_norm = (cpu_med / ncpu) if pd.notna(cpu_med) else None  # % of whole machine
    tw_max = col(steady, "time_wait").max()
    q_med = col(steady, "ch_running_queries").median()

    rtt_all = col(mon, "ch_rtt_ms").dropna()
    rtt_base = rtt_all.min() if len(rtt_all) else None
    rtt_steady = col(steady, "ch_rtt_ms").median()
    rtt_ratio = (rtt_steady / rtt_base) if (rtt_base and rtt_base > 0) else None

    srv_sig = _server_signals(srv)

    ev["cpu_pct_of_machine"] = None if cpu_norm is None else round(cpu_norm, 1)
    ev["time_wait_max"] = None if pd.isna(tw_max) else int(tw_max)
    ev["server_running_queries_median"] = None if pd.isna(q_med) else round(q_med, 1)
    ev["server_rtt_baseline_ms"] = None if rtt_base is None else round(rtt_base, 1)
    ev["server_rtt_steady_ms"] = None if pd.isna(rtt_steady) else round(rtt_steady, 1)
    ev["server_rtt_ratio"] = None if rtt_ratio is None else round(rtt_ratio, 2)
    ev["error_pct"] = round(summary.get("error_pct", 0), 2)
    ev["client_p50_ms"] = round(summary.get("lat_p50", 0), 0)
    ev.update({f"srv_{k}": v for k, v in srv_sig.items()})

    codes = summary.get("response_codes", {})
    has = lambda c: any(str(k).startswith(c) for k in codes)

    def g(k):  # server signal getter
        return srv_sig.get(k)

    # --- ordered rules ---
    if summary.get("error_pct", 0) > 20:
        if has("503") or has("429"):
            label = "ClickHouse — rejecting load (concurrency / quota limit)"
            color, reason = "#b3541e", "High 503/429 rate: server-side limit hit, not a throughput ceiling."
        elif has("403") or has("401"):
            label = "Errors — auth/permission, NOT a perf result"
            color, reason = "#7d2b2b", "Most requests rejected at auth (403/401). Fix credentials/grants first."
        else:
            label = "Errors dominate — results not trustworthy"
            color, reason = "#7d2b2b", f"{ev['error_pct']}% errored; investigate before reading the numbers."
    elif cpu_norm is not None and cpu_norm > 85:
        label = "Your machine — CPU bound (load generator saturated)"
        color, reason = "#1f6f6f", f"JMeter JVM used ~{ev['cpu_pct_of_machine']}% of all {ncpu} cores. Add load generators."
    elif ev["time_wait_max"] is not None and ev["time_wait_max"] > 16000:
        label = "Your machine — socket / ephemeral-port pressure"
        color, reason = "#1f6f6f", f"TIME_WAIT peaked at {ev['time_wait_max']}: connection churn on this host limits throughput."
    # --- ClickHouse self-reported saturation (only when server CSV is present) ---
    elif g("mem_limit_hits"):
        label = "ClickHouse — memory pressure"
        color, reason = "#b3541e", f"{g('mem_limit_hits')} query memory-limit hit(s) server-side. Queries are spilling/being killed on RAM."
    elif g("cpu_busy_pct") is not None and g("cpu_busy_pct") > 85:
        if g("cpu_wait_ratio") is not None and g("cpu_wait_ratio") > 1.0:
            label = "ClickHouse — CPU starved (queries waiting for cores)"
            color, reason = "#b3541e", f"Server CPU {g('cpu_busy_pct')}% busy and CPU-wait:run ratio {g('cpu_wait_ratio')}x: more cores would help."
        else:
            label = "ClickHouse — CPU bound"
            color, reason = "#b3541e", f"Server CPU {g('cpu_busy_pct')}% busy in steady state. The service is compute-limited."
    elif g("s3_frac") is not None and g("s3_frac") > 0.4:
        label = "ClickHouse — object-storage bound (S3 reads dominate)"
        color, reason = "#b3541e", f"S3 read time is {int(g('s3_frac') * 100)}% of CPU+S3 time. Cold cache / large scans against object storage."
    elif (g("iowait_pct") is not None and g("iowait_pct") > 30) or (g("io_frac") is not None and g("io_frac") > 0.5):
        label = "ClickHouse — disk / IO bound"
        color, reason = "#b3541e", f"Server iowait ~{g('iowait_pct')}% of cores (IO {int((g('io_frac') or 0) * 100)}% of IO+CPU time). Storage is the limiter."
    elif rtt_ratio is not None and rtt_ratio > 2.0:
        label = "ClickHouse — server latency rising under load"
        color, reason = "#b3541e", f"Server RTT grew {ev['server_rtt_ratio']}x ({ev['server_rtt_baseline_ms']}→{ev['server_rtt_steady_ms']} ms) as load climbed."
    else:
        label = "Headroom / network-bound — no single saturation signal"
        color, reason = "#555555", "Neither client nor server is clearly saturated. Run a thread sweep (e.g. 10→50→100→500) to find the knee."

    return {"label": label, "color": color, "reason": reason, "evidence": ev}


def make_report(jtl_path, monitor_path, out_png, summary=None, server_path=None) -> dict:
    from .analyze import summarize

    jtl = load_jtl(jtl_path)
    summary = summary or summarize(jtl)
    mon = _load_monitor(monitor_path) if monitor_path else pd.DataFrame()
    srv = _load_server(server_path)
    have_srv = len(srv) > 1 and "t_rel_s" in srv.columns

    t0 = jtl["timeStamp"].min()
    if len(mon) and mon["epoch_ms"].notna().any():
        t0 = min(t0, mon["epoch_ms"].min())
    if have_srv and srv["epoch_ms"].notna().any():
        t0 = min(t0, srv["epoch_ms"].min())

    jtl = jtl.copy()
    jtl["sec"] = ((jtl["timeStamp"] - t0) // 1000).astype(int)
    grp = jtl.groupby("sec")
    tput = grp.size()
    succ = grp["ok"].sum()
    err_rate = (1 - succ / tput) * 100
    p50 = grp["elapsed"].quantile(0.50)
    p95 = grp["elapsed"].quantile(0.95)
    p99 = grp["elapsed"].quantile(0.99)

    if len(mon):
        mon = mon.copy()
        mon["x"] = (mon["epoch_ms"] - t0) / 1000.0
    if have_srv:
        srv = srv.copy()
        srv["x"] = (srv["epoch_ms"] - t0) / 1000.0
        # per-interval seconds, for converting event deltas into rates
        srv["dt"] = srv["x"].diff().fillna(srv["x"].clip(lower=1)).clip(lower=0.1)

    verdict = compute_verdict(jtl, mon, summary, srv if have_srv else None)

    npanels = 6 if have_srv else 4
    fig, axes = plt.subplots(npanels, 1, figsize=(11, 3.1 * npanels + 0.5), sharex=True)
    fig.suptitle(
        f"Bottleneck verdict: {verdict['label']}",
        fontsize=14, fontweight="bold", color=verdict["color"], y=0.997,
    )

    # 1) throughput + error rate
    ax = axes[0]
    ax.plot(tput.index, tput.values, label="requests/s", color="#1f77b4")
    ax.plot(succ.index, succ.values, label="successful/s", color="#2ca02c", lw=1)
    ax.set_ylabel("req/s")
    ax.legend(loc="upper left", fontsize=8)
    axb = ax.twinx()
    axb.plot(err_rate.index, err_rate.values, label="error %", color="#d62728", lw=0.8, alpha=0.6)
    axb.set_ylabel("error %", color="#d62728")
    axb.set_ylim(0, max(5, err_rate.max() * 1.1 if len(err_rate) else 5))
    ax.set_title("Benchmark throughput", fontsize=10)

    # 2) latency percentiles + server RTT
    ax = axes[1]
    ax.plot(p50.index, p50.values, label="client p50", color="#1f77b4")
    ax.plot(p95.index, p95.values, label="client p95", color="#ff7f0e")
    ax.plot(p99.index, p99.values, label="client p99", color="#d62728")
    if len(mon) and mon["ch_rtt_ms"].notna().any():
        ax.plot(mon["x"], mon["ch_rtt_ms"], label="server RTT (SELECT)", color="#2ca02c", ls="--", lw=1.2)
    ax.set_ylabel("ms")
    ax.set_title("Latency: client-measured vs server round-trip", fontsize=10)
    ax.legend(loc="upper left", fontsize=8)

    # 3) client: CPU% of machine + TIME_WAIT
    ax = axes[2]
    if len(mon) and mon["jmeter_cpu_pct"].notna().any():
        ncpu = int(mon["ncpu"].dropna().iloc[0]) if mon["ncpu"].notna().any() else 1
        ax.plot(mon["x"], mon["jmeter_cpu_pct"] / ncpu, label=f"JMeter CPU (% of {ncpu} cores)", color="#9467bd")
    ax.axhline(85, color="#9467bd", ls=":", lw=0.8, alpha=0.6)
    ax.set_ylabel("CPU % of machine", color="#9467bd")
    ax.set_ylim(0, 110)
    ax.set_title("Load generator (this machine)", fontsize=10)
    if ax.get_legend_handles_labels()[1]:
        ax.legend(loc="upper left", fontsize=8)
    if len(mon) and mon["time_wait"].notna().any():
        axb = ax.twinx()
        axb.plot(mon["x"], mon["time_wait"], label="TIME_WAIT", color="#8c564b", lw=1)
        axb.set_ylabel("TIME_WAIT sockets", color="#8c564b")
        axb.legend(loc="upper right", fontsize=8)

    # 4) server: concurrent running queries (prefer server CSV, fall back to monitor)
    ax = axes[3]
    if have_srv and srv["m_Query"].notna().any():
        ax.plot(srv["x"], srv["m_Query"], label="running queries (system.metrics)", color="#17becf")
        if srv["m_BackgroundMergesAndMutationsPoolTask"].notna().any():
            ax.plot(srv["x"], srv["m_BackgroundMergesAndMutationsPoolTask"],
                    label="background merges/mutations", color="#bcbd22", lw=1)
    elif len(mon) and mon["ch_running_queries"].notna().any():
        ax.plot(mon["x"], mon["ch_running_queries"], label="ClickHouse running queries", color="#17becf")
    ax.set_ylabel("count")
    ax.set_title("ClickHouse server concurrency", fontsize=10)
    if ax.get_legend_handles_labels()[1]:
        ax.legend(loc="upper left", fontsize=8)

    if have_srv:
        # 5) server CPU busy vs IO-wait (normalized, % of all cores)
        ax = axes[4]
        if srv["a_OSUserTimeNormalized"].notna().any():
            cpu_busy = (srv["a_OSUserTimeNormalized"].fillna(0)
                        + srv["a_OSSystemTimeNormalized"].fillna(0)) * 100
            ax.plot(srv["x"], cpu_busy, label="CPU busy (user+sys)", color="#d62728")
        if srv["a_OSIOWaitTimeNormalized"].notna().any():
            ax.plot(srv["x"], srv["a_OSIOWaitTimeNormalized"] * 100, label="IO wait", color="#1f77b4")
        ax.axhline(85, color="#d62728", ls=":", lw=0.8, alpha=0.5)
        ax.set_ylabel("% of cores")
        ax.set_ylim(0, 110)
        ax.set_title("ClickHouse host CPU vs IO-wait (system.asynchronous_metrics)", fontsize=10)
        if ax.get_legend_handles_labels()[1]:
            ax.legend(loc="upper left", fontsize=8)

        # 6) data scanned/s + mark-cache hit ratio
        ax = axes[5]
        if srv["e_SelectedBytes"].notna().any():
            mb_s = (srv["e_SelectedBytes"] / srv["dt"]) / 1e6
            ax.plot(srv["x"], mb_s, label="data scanned (MB/s)", color="#2ca02c")
        ax.set_ylabel("MB/s read", color="#2ca02c")
        ax.set_xlabel("seconds since start")
        ax.set_title("ClickHouse data scanned & cache effectiveness", fontsize=10)
        if ax.get_legend_handles_labels()[1]:
            ax.legend(loc="upper left", fontsize=8)
        hits = srv.get("e_MarkCacheHits")
        misses = srv.get("e_MarkCacheMisses")
        if hits is not None and misses is not None and (hits.fillna(0) + misses.fillna(0)).gt(0).any():
            denom = hits.fillna(0) + misses.fillna(0)
            # NaN (not pd.NA) where there were no lookups: matplotlib gaps the
            # line on NaN but can't coerce pandas' NAType to float.
            ratio = (hits.fillna(0) / denom.where(denom > 0)) * 100
            axb = ax.twinx()
            axb.plot(srv["x"], ratio, label="mark-cache hit %", color="#9467bd", lw=1, ls="--")
            axb.set_ylabel("mark-cache hit %", color="#9467bd")
            axb.set_ylim(0, 105)
            axb.legend(loc="upper right", fontsize=8)
    else:
        axes[3].set_xlabel("seconds since start")

    # evidence footnote
    ev = verdict["evidence"]
    foot = (
        f"{verdict['reason']}\n"
        f"client: CPU={ev['cpu_pct_of_machine']}% of machine · TIME_WAIT max={ev['time_wait_max']} · "
        f"p50={ev['client_p50_ms']}ms · errors={ev['error_pct']}%\n"
        f"server RTT {ev['server_rtt_baseline_ms']}→{ev['server_rtt_steady_ms']} ms ({ev['server_rtt_ratio']}x)"
    )
    if have_srv:
        foot += (
            f" · CPU={ev.get('srv_cpu_busy_pct')}% · iowait={ev.get('srv_iowait_pct')}% · "
            f"IO frac={ev.get('srv_io_frac')} · S3 frac={ev.get('srv_s3_frac')} · "
            f"running q(med)={ev.get('srv_running_q_med')} · mem-limit hits={ev.get('srv_mem_limit_hits')}"
        )
    fig.text(0.5, 0.004, foot, ha="center", va="bottom", fontsize=8, wrap=True, color="#333333")

    fig.tight_layout(rect=(0, 0.04, 1, 0.975))
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    return verdict
