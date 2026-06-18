"""Background sampler: load-generator (this machine) vs ClickHouse server.

Runs in a thread alongside the benchmark and appends a row per interval to a
CSV consumed by jmeter_bench.plot. Client metrics come from psutil + netstat
(no root needed on macOS); server metrics from clickhouse-connect.
"""

import csv
import subprocess
import threading
import time

import psutil

COLUMNS = [
    "epoch_ms", "t_rel_s", "ncpu",
    "jmeter_cpu_pct", "jmeter_rss_mb",
    "time_wait", "established",
    "ch_running_queries", "ch_rtt_ms", "ch_ok",
]


def find_jmeter() -> "psutil.Process | None":
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "org.apache.jmeter.NewDriver" in cmd or "ApacheJMeter" in cmd:
            return proc
    return None


def socket_counts(port) -> tuple:
    """(TIME_WAIT total, ESTABLISHED to ClickHouse port). netstat avoids the
    root requirement psutil.net_connections has on macOS."""
    try:
        out = subprocess.run(
            ["netstat", "-an", "-p", "tcp"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return (None, None)
    time_wait = established = 0
    needle = f".{port} "
    for line in out.splitlines():
        if "TIME_WAIT" in line:
            time_wait += 1
        elif "ESTABLISHED" in line and needle in line + " ":
            established += 1
    return time_wait, established


def _make_client(cfg):
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=cfg["ch_host"],
        port=int(cfg["ch_port"]),
        username=cfg.get("ch_user", "default"),
        password=cfg.get("ch_password", ""),
        secure=(cfg.get("ch_protocol", "https") == "https"),
        connect_timeout=5,
        send_receive_timeout=10,
    )


class Monitor(threading.Thread):
    def __init__(self, cfg: dict, out_path, interval: float = 2.0):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.out_path = str(out_path)
        self.interval = float(interval)
        self.ncpu = psutil.cpu_count() or 1
        self._stop = threading.Event()
        self.error = None

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            self._loop()
        except Exception as exc:  # never let the monitor crash the run
            self.error = exc

    def _loop(self):
        try:
            client = _make_client(self.cfg)
            client.query("SELECT 1")  # fail fast on auth/connectivity
        except Exception as exc:
            self.error = exc
            client = None

        proc = None
        primed = False
        with open(self.out_path, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(COLUMNS)
            fh.flush()
            t0 = time.time()

            while not self._stop.is_set():
                now = time.time()

                # ---- client process ----
                if proc is None or not proc.is_running():
                    proc = find_jmeter()
                    primed = False
                cpu = rss = ""
                if proc is not None:
                    try:
                        if not primed:
                            proc.cpu_percent(None)  # prime the delta
                            primed = True
                        cpu = round(proc.cpu_percent(None), 1)
                        rss = round(proc.memory_info().rss / 1e6, 1)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        proc = None

                time_wait, established = socket_counts(self.cfg.get("ch_port"))

                # ---- server ----
                ch_q = ch_rtt = ""
                ch_ok = 0
                if client is not None:
                    try:
                        start = time.time()
                        res = client.query(
                            "SELECT value FROM system.metrics WHERE metric='Query'"
                        )
                        ch_rtt = round((time.time() - start) * 1000, 1)
                        val = int(res.result_rows[0][0])
                        ch_q = max(val - 1, 0)  # exclude this monitoring query
                        ch_ok = 1
                    except Exception:
                        ch_ok = 0

                writer.writerow([
                    int(now * 1000), round(now - t0, 1), self.ncpu,
                    cpu, rss, time_wait, established, ch_q, ch_rtt, ch_ok,
                ])
                fh.flush()
                self._stop.wait(self.interval)
