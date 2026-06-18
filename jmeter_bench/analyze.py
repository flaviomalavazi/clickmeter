"""Load a JMeter JTL and summarize throughput, latency percentiles, errors."""

import pandas as pd


def load_jtl(path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timeStamp"] = pd.to_numeric(df["timeStamp"], errors="coerce")
    df["elapsed"] = pd.to_numeric(df["elapsed"], errors="coerce")
    df = df.dropna(subset=["timeStamp", "elapsed"])
    df["ok"] = df["success"].astype(str).str.lower().eq("true")
    return df


def summarize(df: pd.DataFrame) -> dict:
    n = len(df)
    if n == 0:
        return {"samples": 0}
    successes = int(df["ok"].sum())
    errors = n - successes
    duration_s = max((df["timeStamp"].max() - df["timeStamp"].min()) / 1000.0, 0.001)
    el = df["elapsed"]
    codes = df.get("responseCode")
    code_counts = (
        codes.astype(str).value_counts().head(6).to_dict() if codes is not None else {}
    )
    return {
        "samples": n,
        "successes": successes,
        "errors": errors,
        "error_pct": errors / n * 100,
        "duration_s": duration_s,
        "throughput_rps": n / duration_s,
        "lat_avg": float(el.mean()),
        "lat_p50": float(el.quantile(0.50)),
        "lat_p90": float(el.quantile(0.90)),
        "lat_p95": float(el.quantile(0.95)),
        "lat_p99": float(el.quantile(0.99)),
        "lat_max": float(el.max()),
        "response_codes": code_counts,
    }


def print_summary(s: dict):
    if s.get("samples", 0) == 0:
        print("No samples found.")
        return
    print(f"Samples:    {s['samples']:>12,}")
    print(f"Successes:  {s['successes']:>12,}")
    print(f"Errors:     {s['errors']:>12,}    ({s['error_pct']:.2f}%)")
    print(f"Duration:   {s['duration_s']:>12.1f} s")
    print(f"Throughput: {s['throughput_rps']:>12.1f} req/s")
    print("Latency ms:")
    print(f"  avg  {s['lat_avg']:>10.1f}")
    print(f"  p50  {s['lat_p50']:>10.0f}")
    print(f"  p90  {s['lat_p90']:>10.0f}")
    print(f"  p95  {s['lat_p95']:>10.0f}")
    print(f"  p99  {s['lat_p99']:>10.0f}")
    print(f"  max  {s['lat_max']:>10.0f}")
    if s.get("response_codes"):
        print("Response codes:")
        for code, cnt in s["response_codes"].items():
            print(f"  {code:<8} {cnt:>10,}")
