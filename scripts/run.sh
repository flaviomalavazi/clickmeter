#!/usr/bin/env bash
# Launch the ClickHouse JMeter benchmark.
#
# Usage:
#   scripts/run.sh                     # uses config/benchmark.properties as-is
#   scripts/run.sh threads=10000       # override individual knobs ad-hoc
#   scripts/run.sh threads=40000 duration=900
#
# Requires JMeter on PATH (or set $JMETER_HOME).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CFG="$ROOT/config/benchmark.properties"
JMX="$ROOT/jmeter/clickhouse-benchmark.jmx"
REPORT_DIR="$ROOT/results/report-$(date +%Y%m%d-%H%M%S)"
JTL="$ROOT/results/results-$(date +%Y%m%d-%H%M%S).jtl"
LOG="$ROOT/results/jmeter-$(date +%Y%m%d-%H%M%S).log"

# ---------------------------------------------------------------------------
# Resolve jmeter binary
# ---------------------------------------------------------------------------
if command -v jmeter >/dev/null 2>&1; then
    JMETER_BIN="$(command -v jmeter)"
elif [[ -n "${JMETER_HOME:-}" && -x "$JMETER_HOME/bin/jmeter" ]]; then
    JMETER_BIN="$JMETER_HOME/bin/jmeter"
else
    echo "ERROR: jmeter not found on PATH and JMETER_HOME is not set." >&2
    echo "  brew install jmeter   # macOS" >&2
    echo "  or download from https://jmeter.apache.org/download_jmeter.cgi" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Load defaults from properties file, allow CLI overrides (key=val args)
#
# macOS ships Bash 3.2, which has no associative arrays (`declare -A`). We
# emulate a key->value map with one shell variable per key (prefix OPT_) and
# an ordered list of keys, so this runs on stock macOS without Homebrew Bash.
# ---------------------------------------------------------------------------
OPT_KEYS=()

# opt_set <key> <value>  -- store value; track key once for ordered iteration.
opt_set() {
    local k="$1" v="$2" existing found=0
    for existing in ${OPT_KEYS[@]+"${OPT_KEYS[@]}"}; do
        [[ "$existing" == "$k" ]] && { found=1; break; }
    done
    (( found )) || OPT_KEYS+=("$k")
    printf -v "OPT_$(printf '%s' "$k" | tr -c 'A-Za-z0-9_' '_')" '%s' "$v"
}

# opt_get <key> [default] -- echo stored value, or default if unset.
opt_get() {
    local var="OPT_$(printf '%s' "$1" | tr -c 'A-Za-z0-9_' '_')"
    printf '%s' "${!var-${2-}}"
}

while IFS='=' read -r key val; do
    [[ -z "$key" || "$key" =~ ^# ]] && continue
    opt_set "$key" "$val"
done < "$CFG"

# Overlay credentials (and any overrides) from .env at the repo root. Same
# key=value format as the properties file; surrounding quotes are stripped so
# passwords with shell-special chars survive. Precedence: CLI > .env > .properties.
ENV_FILE="$ROOT/.env"
if [[ -f "$ENV_FILE" ]]; then
    while IFS='=' read -r key val; do
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        key="$(printf '%s' "$key" | tr -d '[:space:]')"
        [[ -z "$key" ]] && continue
        val="${val%\'}"; val="${val#\'}"; val="${val%\"}"; val="${val#\"}"
        opt_set "$key" "$val"
    done < "$ENV_FILE"
fi

for arg in "$@"; do
    opt_set "${arg%%=*}" "${arg#*=}"
done

# ---------------------------------------------------------------------------
# JVM tuning for high thread counts.
# Rule of thumb: ~512KB stack * threads + heap for buffers.
# 40k threads => ~20GB stack + 8GB heap. Override with JVM_ARGS if needed.
# ---------------------------------------------------------------------------
THREADS="$(opt_get threads 1000)"
DEFAULT_HEAP="4g"
if   (( THREADS >= 20000 )); then DEFAULT_HEAP="12g"
elif (( THREADS >= 10000 )); then DEFAULT_HEAP="8g"
elif (( THREADS >=  5000 )); then DEFAULT_HEAP="6g"
fi

# 256KB thread stack lets you fit ~4k threads per GB of stack memory.
export JVM_ARGS="${JVM_ARGS:--Xms${DEFAULT_HEAP} -Xmx${DEFAULT_HEAP} -Xss256k \
  -XX:+UseG1GC -XX:MaxGCPauseMillis=200 \
  -Djava.net.preferIPv4Stack=true \
  -Dsun.net.inetaddr.ttl=0}"

# ---------------------------------------------------------------------------
# Build -J args from OPTS.
# Secrets (ch_password) are NOT passed via -J: command-line args are visible to
# any user via `ps`. Instead we write them to a mode-600 temp properties file
# and hand it to JMeter with -q, so __P(ch_password) still resolves.
# ---------------------------------------------------------------------------
SECRET_KEYS=" ch_password "
JARGS=()
for k in ${OPT_KEYS[@]+"${OPT_KEYS[@]}"}; do
    [[ "$SECRET_KEYS" == *" $k "* ]] && continue
    JARGS+=("-J${k}=$(opt_get "$k")")
done
JARGS+=("-Jjtl_path=${JTL}")

SECRET_PROPS="$(mktemp -t jmeter-secret.XXXXXX)"
chmod 600 "$SECRET_PROPS"
trap 'rm -f "$SECRET_PROPS"' EXIT
for k in $SECRET_KEYS; do
    printf '%s=%s\n' "$k" "$(opt_get "$k")" >> "$SECRET_PROPS"
done

mkdir -p "$ROOT/results"

echo "================================================================"
echo " ClickHouse JMeter benchmark"
echo "----------------------------------------------------------------"
echo "  JMeter:    $JMETER_BIN"
echo "  Plan:      $JMX"
echo "  Threads:   $(opt_get threads '?')    Ramp-up: $(opt_get ramp_up '?')s    Duration: $(opt_get duration '?')s"
echo "  Target:    $(opt_get ch_protocol)://$(opt_get ch_host):$(opt_get ch_port)  db=$(opt_get ch_database)"
echo "  Queries:   $(opt_get queries_csv ../queries/queries.csv)"
echo "  JTL out:   $JTL"
echo "  Log:       $LOG"
echo "  HTML:      $REPORT_DIR"
echo "  JVM_ARGS:  $JVM_ARGS"
echo "================================================================"
echo

# ---------------------------------------------------------------------------
# Soft pre-flight: file descriptors
# ---------------------------------------------------------------------------
SOFT_FDS=$(ulimit -n)
if (( SOFT_FDS < THREADS * 2 )); then
    echo "WARNING: ulimit -n is $SOFT_FDS but threads=$THREADS (need ~$((THREADS * 2)) fds)."
    echo "         Run scripts/tune-os.sh or 'ulimit -n 200000' in this shell first."
    echo
fi

# ---------------------------------------------------------------------------
# Soft pre-flight: ramp-up vs duration
# If ramp_up >= duration the test ends before all threads start, so it never
# reaches full concurrency and throughput is understated.
# ---------------------------------------------------------------------------
RAMP="$(opt_get ramp_up 0)"
DURATION_S="$(opt_get duration 0)"
if (( DURATION_S > 0 && RAMP >= DURATION_S )); then
    STARTED=$(( THREADS * DURATION_S / RAMP ))
    echo "WARNING: ramp_up (${RAMP}s) >= duration (${DURATION_S}s)."
    echo "         Only ~${STARTED} of $THREADS threads will have started before the test stops,"
    echo "         so it never reaches full concurrency and throughput will be understated."
    echo "         Set ramp_up well below duration (e.g. ramp_up=$(( DURATION_S / 6 > 0 ? DURATION_S / 6 : 1 )))."
    echo
fi

# Not `exec`: we keep the shell alive so the EXIT trap can shred the secret file.
"$JMETER_BIN" -n -t "$JMX" -l "$JTL" -j "$LOG" -e -o "$REPORT_DIR" -q "$SECRET_PROPS" "${JARGS[@]}"
