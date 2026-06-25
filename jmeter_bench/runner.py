"""Resolve and launch JMeter, mirroring scripts/run.sh (JVM heap heuristic,
-J arg forwarding, timestamped output paths) so the Python path is a drop-in."""

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from .config import ROOT

JMX = ROOT / "jmeter" / "clickhouse-benchmark.jmx"

# Passed via a mode-600 -q file instead of -J, so they don't show up in `ps`.
SECRET_KEYS = {"ch_password"}

# Cosmetic JMeter/JDK startup noise we drop from the console (set
# BENCH_RAW_JMETER=1 to see the unfiltered stream). Each entry is a substring;
# a line is suppressed if it contains any of them. Kept tight and specific so
# real warnings/errors and the throughput summariser always pass through:
#   - the JDK warns about JMeter's unconditional --add-opens for sun.awt.X11,
#     a package that only exists on Linux/X11 (harmless on macOS);
#   - log4j2 warns that JMeter's bundled config uses deprecated plugin scanning.
_NOISE = (
    "package sun.awt.X11 not in java.desktop",
    "The use of package scanning to locate plugins is deprecated",
)


def _filter_output(stream):
    """Echo JMeter's combined stdout/stderr to ours, dropping _NOISE lines."""
    try:
        for line in iter(stream.readline, ""):
            if not any(n in line for n in _NOISE):
                sys.stdout.write(line)
                sys.stdout.flush()
    finally:
        try:
            stream.close()
        except Exception:
            pass


def run_log_comment(stamp: str) -> str:
    """The log_comment stamped on every request of a run, so query_log rows can
    be matched back to it exactly. URL/SQL-safe by construction (stamp is
    timestamp digits + dashes)."""
    return f"clickmeter-{stamp}"


def resolve_jmeter() -> str:
    binp = shutil.which("jmeter")
    if binp:
        return binp
    home = os.environ.get("JMETER_HOME")
    if home and (Path(home) / "bin" / "jmeter").exists():
        return str(Path(home) / "bin" / "jmeter")
    raise FileNotFoundError(
        "jmeter not found on PATH and JMETER_HOME is not set. "
        "Install with `brew install jmeter` or set JMETER_HOME."
    )


def heap_for_threads(threads: int) -> str:
    if threads >= 20000:
        return "12g"
    if threads >= 10000:
        return "8g"
    if threads >= 5000:
        return "6g"
    return "4g"


def build_jvm_args(threads: int) -> str:
    if os.environ.get("JVM_ARGS"):
        return os.environ["JVM_ARGS"]
    heap = heap_for_threads(threads)
    # 256KB stacks fit ~4k threads per GB of stack memory.
    return (
        f"-Xms{heap} -Xmx{heap} -Xss256k "
        "-XX:+UseG1GC -XX:MaxGCPauseMillis=200 "
        "-Djava.net.preferIPv4Stack=true -Dsun.net.inetaddr.ttl=0"
    )


def preflight_warnings(cfg: dict) -> list:
    """Soft checks returned as human-readable strings (empty = all good)."""
    warnings = []

    def _int(key, default=0):
        try:
            return int(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    threads = _int("threads", 1000)
    ramp = _int("ramp_up")
    duration = _int("duration")

    # ramp_up >= duration: the test stops before all threads start, so it never
    # reaches full concurrency and throughput is understated.
    if duration > 0 and ramp >= duration:
        started = threads * duration // ramp if ramp else threads
        suggested = max(duration // 6, 1)
        warnings.append(
            f"ramp_up ({ramp}s) >= duration ({duration}s): only ~{started} of {threads} "
            f"threads will have started before the test stops, so it never reaches full "
            f"concurrency and throughput will be understated. "
            f"Set ramp_up well below duration (e.g. ramp_up={suggested})."
        )
    return warnings


def make_paths(results_dir=None, stamp=None) -> dict:
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    results = Path(results_dir or ROOT / "results")
    results.mkdir(parents=True, exist_ok=True)
    return {
        "stamp": stamp,
        "jtl": results / f"results-{stamp}.jtl",
        "log": results / f"jmeter-{stamp}.log",
        "report": results / f"report-{stamp}",
        "monitor": results / f"monitor-{stamp}.csv",
        "server": results / f"server-{stamp}.csv",
        "query_log": results / f"query_log-{stamp}.csv",
        "plot": results / f"verdict-{stamp}.png",
    }


def launch(cfg: dict, paths: dict) -> subprocess.Popen:
    """Start JMeter in non-GUI mode and return the Popen (does not wait)."""
    threads = int(cfg.get("threads", 1000))
    env = dict(os.environ)
    env["JVM_ARGS"] = build_jvm_args(threads)

    jargs = [f"-J{k}={v}" for k, v in cfg.items() if k not in SECRET_KEYS]
    jargs.append(f"-Jjtl_path={paths['jtl']}")
    # Tag every benchmark request so system.query_log can be filtered to exactly
    # this run (see jmeter_bench.query_log). The sampler's HTTP path appends
    # &log_comment=${__P(ch_log_comment,...)}; we set it to this run's stamp.
    # The SQL still travels in the POST body. Honor an explicit override.
    if "ch_log_comment" not in cfg:
        jargs.append(f"-Jch_log_comment={run_log_comment(paths['stamp'])}")

    secret_args = _write_secret_props(cfg)

    cmd = [
        resolve_jmeter(), "-n",
        "-t", str(JMX),
        "-l", str(paths["jtl"]),
        "-j", str(paths["log"]),
        "-e", "-o", str(paths["report"]),
        *secret_args,
        *jargs,
    ]
    if os.environ.get("BENCH_RAW_JMETER"):
        return subprocess.Popen(cmd, env=env)
    # Pipe and filter JMeter's console output. A daemon reader thread drains the
    # pipe (so JMeter never blocks on a full buffer); the caller joins it via
    # the `bench_output_filter` attribute after wait() so all lines flush before
    # the harness prints its own summary.
    proc = subprocess.Popen(
        cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    reader = threading.Thread(target=_filter_output, args=(proc.stdout,), daemon=True)
    reader.start()
    proc.bench_output_filter = reader
    return proc


def _write_secret_props(cfg: dict) -> list:
    """Write secret keys to a mode-600 temp properties file and return the
    JMeter `-q <file>` args. The file is removed at interpreter exit."""
    secrets = {k: cfg[k] for k in SECRET_KEYS if k in cfg}
    if not secrets:
        return []
    fd, path = tempfile.mkstemp(prefix="jmeter-secret-", suffix=".properties")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as fh:
        for key, val in secrets.items():
            fh.write(f"{key}={val}\n")
    atexit.register(lambda: os.path.exists(path) and os.unlink(path))
    return ["-q", path]
