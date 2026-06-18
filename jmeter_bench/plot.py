"""Join benchmark results (JTL) with the monitor time-series and render a
single figure plus a heuristic bottleneck verdict: your machine vs ClickHouse.
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


def compute_verdict(jtl: pd.DataFrame, mon: pd.DataFrame, summary: dict) -> dict:
    """Transparent, rule-based call. Returns label/color/reason/evidence.

    Steady window = last 80% of the run (skip ramp-up). Each signal is shown so
    you can judge the heuristic yourself; a thread sweep confirms it.
    """
    ev = {}
    have_mon = len(mon) > 0 and "t_rel_s" in mon.columns

    def col(df, name):
        """Series for a column, or an empty float Series if the monitor is absent."""
        return df[name] if name in df.columns else pd.Series(dtype="float64")

    # --- steady-state slice of the monitor series (skip the ramp-up) ---
    steady = mon
    if have_mon and col(mon, "t_rel_s").notna().any():
        cutoff = mon["t_rel_s"].max() * 0.2
        steady = mon[mon["t_rel_s"] >= cutoff]
    if len(steady) == 0:
        steady = mon

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

    ev["cpu_pct_of_machine"] = None if cpu_norm is None else round(cpu_norm, 1)
    ev["time_wait_max"] = None if pd.isna(tw_max) else int(tw_max)
    ev["server_running_queries_median"] = None if pd.isna(q_med) else round(q_med, 1)
    ev["server_rtt_baseline_ms"] = None if rtt_base is None else round(rtt_base, 1)
    ev["server_rtt_steady_ms"] = None if pd.isna(rtt_steady) else round(rtt_steady, 1)
    ev["server_rtt_ratio"] = None if rtt_ratio is None else round(rtt_ratio, 2)
    ev["error_pct"] = round(summary.get("error_pct", 0), 2)
    ev["client_p50_ms"] = round(summary.get("lat_p50", 0), 0)

    codes = summary.get("response_codes", {})
    has = lambda c: any(str(k).startswith(c) for k in codes)

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
    elif rtt_ratio is not None and rtt_ratio > 2.0:
        label = "ClickHouse — server latency rising under load"
        color, reason = "#b3541e", f"Server RTT grew {ev['server_rtt_ratio']}x ({ev['server_rtt_baseline_ms']}→{ev['server_rtt_steady_ms']} ms) as load climbed."
    else:
        label = "Headroom / network-bound — no single saturation signal"
        color, reason = "#555555", "Neither client nor server is clearly saturated. Run a thread sweep (e.g. 10→50→100→500) to find the knee."

    return {"label": label, "color": color, "reason": reason, "evidence": ev}


def make_report(jtl_path, monitor_path, out_png, summary=None) -> dict:
    from .analyze import summarize

    jtl = load_jtl(jtl_path)
    summary = summary or summarize(jtl)
    mon = _load_monitor(monitor_path) if monitor_path else pd.DataFrame()

    t0 = jtl["timeStamp"].min()
    if len(mon) and mon["epoch_ms"].notna().any():
        t0 = min(t0, mon["epoch_ms"].min())

    jtl = jtl.copy()
    jtl["sec"] = ((jtl["timeStamp"] - t0) // 1000).astype(int)
    grp = jtl.groupby("sec")
    tput = grp.size()
    succ = grp["ok"].sum()
    err_rate = (1 - succ / tput) * 100
    p50 = grp["elapsed"].quantile(0.50)
    p95 = grp["elapsed"].quantile(0.95)
    p99 = grp["elapsed"].quantile(0.99)

    mx = None
    if len(mon):
        mon = mon.copy()
        mon["x"] = (mon["epoch_ms"] - t0) / 1000.0

    verdict = compute_verdict(jtl, mon, summary)

    fig, axes = plt.subplots(4, 1, figsize=(11, 13), sharex=True)
    fig.suptitle(
        f"Bottleneck verdict: {verdict['label']}",
        fontsize=14, fontweight="bold", color=verdict["color"], y=0.995,
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

    # 4) server: concurrent running queries
    ax = axes[3]
    if len(mon) and mon["ch_running_queries"].notna().any():
        ax.plot(mon["x"], mon["ch_running_queries"], label="ClickHouse running queries", color="#17becf")
    ax.set_ylabel("queries")
    ax.set_xlabel("seconds since start")
    ax.set_title("ClickHouse server", fontsize=10)
    if ax.get_legend_handles_labels()[1]:
        ax.legend(loc="upper left", fontsize=8)

    # evidence footnote
    ev = verdict["evidence"]
    foot = (
        f"{verdict['reason']}\n"
        f"CPU={ev['cpu_pct_of_machine']}% of machine · TIME_WAIT max={ev['time_wait_max']} · "
        f"server queries(med)={ev['server_running_queries_median']} · "
        f"server RTT {ev['server_rtt_baseline_ms']}→{ev['server_rtt_steady_ms']} ms ({ev['server_rtt_ratio']}x) · "
        f"errors={ev['error_pct']}%"
    )
    fig.text(0.5, 0.005, foot, ha="center", va="bottom", fontsize=8, wrap=True, color="#333333")

    fig.tight_layout(rect=(0, 0.03, 1, 0.97))
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    return verdict
