#!/usr/bin/env bash
# OS tuning for driving high thread counts from a single JMeter JVM.
# Linux only. Re-run after every reboot, or persist via /etc/security/limits.conf
# and /etc/sysctl.d/.
#
#   sudo scripts/tune-os.sh
#
set -euo pipefail

if [[ "$(uname)" != "Linux" ]]; then
    echo "This script targets Linux. On macOS, raise fd limits with:"
    echo "  sudo launchctl limit maxfiles 200000 200000"
    echo "  ulimit -n 200000"
    exit 0
fi

echo "[+] Raising file descriptor limit for this shell"
ulimit -n 1048576 || echo "    (run as root or persist via /etc/security/limits.conf)"

echo "[+] Tuning ephemeral port range and TIME_WAIT reuse"
sysctl -w net.ipv4.ip_local_port_range="1024 65535"
sysctl -w net.ipv4.tcp_tw_reuse=1
sysctl -w net.ipv4.tcp_fin_timeout=15

echo "[+] Backlog + somaxconn for many concurrent connects"
sysctl -w net.core.somaxconn=65535
sysctl -w net.ipv4.tcp_max_syn_backlog=65535

echo "[+] Raising max processes/threads"
sysctl -w kernel.threads-max=4194304
sysctl -w vm.max_map_count=1048576

echo "[+] Done.  Persist these in /etc/sysctl.d/99-jmeter.conf to survive reboots."
