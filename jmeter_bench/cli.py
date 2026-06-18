"""bench — a Python-friendly front end for the ClickHouse JMeter benchmark.

    uv run bench run threads=50 duration=30      # monitor + run + analyze + plot
    uv run bench analyze results/results-*.jtl    # summarize an existing JTL
    uv run bench plot   results/results-*.jtl results/monitor-*.csv
"""

import argparse
import sys
from pathlib import Path

from .config import load_config, parse_overrides
from . import analyze, runner


def _cmd_run(args):
    from .monitor import Monitor
    from .plot import make_report

    overrides = parse_overrides(args.overrides)
    cfg = load_config(args.config, overrides)
    paths = runner.make_paths()

    threads = int(cfg.get("threads", "?")) if str(cfg.get("threads", "")).isdigit() else cfg.get("threads", "?")
    print("=" * 64)
    print(" ClickHouse JMeter benchmark (python)")
    print("-" * 64)
    print(f"  Threads:  {cfg.get('threads', '?')}   Ramp-up: {cfg.get('ramp_up', '?')}s   Duration: {cfg.get('duration', '?')}s")
    print(f"  Target:   {cfg.get('ch_protocol')}://{cfg.get('ch_host')}:{cfg.get('ch_port')}  db={cfg.get('ch_database')}")
    print(f"  JVM_ARGS: {runner.build_jvm_args(int(cfg.get('threads', 1000)))}")
    print(f"  Monitor:  every {args.interval}s -> {paths['monitor']}")
    print("=" * 64)

    for warning in runner.preflight_warnings(cfg):
        print(f"WARNING: {warning}\n", file=sys.stderr)

    mon = Monitor(cfg, paths["monitor"], interval=args.interval)
    mon.start()
    try:
        proc = runner.launch(cfg, paths)
        rc = proc.wait()
    finally:
        mon.stop()
        mon.join(timeout=args.interval + 5)
    if mon.error:
        print(f"  [monitor] server sampling degraded: {mon.error}", file=sys.stderr)

    print(f"\nJMeter exited with code {rc}. Analyzing {paths['jtl'].name} ...\n")
    if not paths["jtl"].exists():
        print("No JTL produced — JMeter likely failed to start. See the log:", file=sys.stderr)
        print(f"  {paths['log']}", file=sys.stderr)
        return 1

    df = analyze.load_jtl(paths["jtl"])
    summary = analyze.summarize(df)
    analyze.print_summary(summary)

    verdict = make_report(paths["jtl"], paths["monitor"], paths["plot"], summary)
    print("\n" + "-" * 64)
    print(f"VERDICT: {verdict['label']}")
    print(f"  {verdict['reason']}")
    print("-" * 64)
    print(f"Plot:       {paths['plot']}")
    print(f"HTML report:{paths['report']}/index.html")
    return 0 if rc == 0 else rc


def _cmd_analyze(args):
    df = analyze.load_jtl(args.jtl)
    analyze.print_summary(analyze.summarize(df))
    return 0


def _cmd_plot(args):
    from .plot import make_report

    out = args.out or str(Path(args.jtl).with_suffix("").as_posix() + "-verdict.png")
    verdict = make_report(args.jtl, args.monitor, out)
    print(f"VERDICT: {verdict['label']}")
    print(f"  {verdict['reason']}")
    print(f"Plot: {out}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="bench", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="monitor + run JMeter + analyze + plot")
    pr.add_argument("overrides", nargs="*", help="config overrides, e.g. threads=50 duration=30")
    pr.add_argument("--config", default=None, help="path to benchmark.properties")
    pr.add_argument("--interval", type=float, default=2.0, help="monitor sampling interval (s)")
    pr.set_defaults(func=_cmd_run)

    pa = sub.add_parser("analyze", help="summarize an existing JTL")
    pa.add_argument("jtl")
    pa.set_defaults(func=_cmd_analyze)

    pp = sub.add_parser("plot", help="plot JTL + monitor CSV into a verdict figure")
    pp.add_argument("jtl")
    pp.add_argument("monitor", nargs="?", default=None, help="monitor CSV (optional)")
    pp.add_argument("-o", "--out", default=None, help="output PNG path")
    pp.set_defaults(func=_cmd_plot)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
