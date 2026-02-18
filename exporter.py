#!/usr/bin/env python3
import json
import os
import subprocess
import threading
import time
import http.server
import socketserver
import sys

# Path to the JSON configuration file located next to this script
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

# Default configuration values
DEFAULT_CONFIG = {
    "mode": "metal",  # "metal" or "vm"
    "interval": 15,    # collection interval in seconds
    "port": 9100       # HTTP port for Prometheus metrics
}

# Global dictionary that holds the latest metric values
metrics = {}

def load_config() -> dict:
    """Load configuration from *config.json*.

    The function merges the user‑provided values with :data:`DEFAULT_CONFIG`.
    It normalises the ``mode`` field to lower‑case and validates that it is either
    ``"metal"`` or ``"vm"`` – otherwise the default ``"metal"`` is used.
    """
    cfg = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        # Missing file or malformed JSON – fall back to defaults
        cfg = {}
    # Merge defaults
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    cfg["mode"] = cfg["mode"].lower()
    if cfg["mode"] not in ("metal", "vm"):
        cfg["mode"] = "metal"
    return cfg

def run_cmd(command: str) -> str:
    """Execute *command* and return its stdout as a string.

    Errors are silenced – an empty string is returned on failure.
    """
    try:
        return subprocess.check_output(command, shell=True, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""

# ---------------------------------------------------------------------------
# Metric collection helpers (each returns a numeric value or a tuple of values)
# ---------------------------------------------------------------------------

def cpu_usage() -> float:
    """Parse ``top -bn1`` to obtain the overall CPU usage percentage.

    The line ``%Cpu(s):  2.0 us,  1.0 sy, … 96.5 id, …`` is used – the idle
    percentage is subtracted from 100 %.
    """
    out = run_cmd("top -bn1")
    for line in out.splitlines():
        if "%Cpu" in line:
            parts = line.split()
            try:
                idx = parts.index("id")
                idle = float(parts[idx - 1])
                return 100.0 - idle
            except Exception:
                continue
    return 0.0


def memory_usage() -> float:
    """Parse ``free -b`` and return memory usage as a percentage of total.
    """
    out = run_cmd("free -b")
    for line in out.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            try:
                total = int(parts[1])
                used = int(parts[2])
                return (used / total) * 100.0 if total else 0.0
            except Exception:
                continue
    return 0.0


def disk_io() -> tuple:
    """Run ``iotop -b -n1 -qqq`` and extract total read/write megabytes per second.

    The output contains lines like ``Total DISK READ: 0.00 B/s  Total DISK WRITE: 0.00 B/s``.
    """
    out = run_cmd("iotop -b -n1 -qqq")
    read = 0.0
    write = 0.0
    for line in out.splitlines():
        if "Total DISK READ:" in line:
            parts = line.split()
            try:
                idx = parts.index("B/s")
                read = float(parts[idx - 1])
            except Exception:
                pass
        if "Total DISK WRITE:" in line:
            parts = line.split()
            try:
                idx = parts.index("B/s")
                write = float(parts[idx - 1])
            except Exception:
                pass
    # Convert bytes per second to megabytes per second
    read_mb = read / (1024 * 1024)
    write_mb = write / (1024 * 1024)
    return read_mb, write_mb


def network_io() -> tuple:
    """Read ``/proc/net/dev`` and sum received and transmitted megabytes across all interfaces.
    """
    out = run_cmd("cat /proc/net/dev")
    recv = 0
    trans = 0
    for line in out.splitlines():
        if ':' in line:
            _, data = line.split(':', 1)
            fields = data.split()
            if len(fields) >= 16:
                try:
                    recv += int(fields[0])
                    trans += int(fields[8])
                except Exception:
                    pass
    # Convert bytes to megabytes
    recv_mb = recv / (1024 * 1024)
    trans_mb = trans / (1024 * 1024)
    return recv_mb, trans_mb


def process_count() -> int:
    """Count the number of processes using ``ps -e | wc -l``.
    """
    out = run_cmd("ps -e | wc -l")
    try:
        return int(out.strip())
    except Exception:
        return 0


def filesystem_usage() -> float:
    """Aggregate filesystem usage from ``df -B1`` (bytes) and return the overall % used.
    """
    out = run_cmd("df -B1")
    total = 0
    used = 0
    for line in out.splitlines():
        if line.startswith("Filesystem"):
            continue
        parts = line.split()
        if len(parts) >= 6:
            try:
                total += int(parts[1])
                used += int(parts[2])
            except Exception:
                pass
    return (used / total) * 100.0 if total else 0.0


def per_device_filesystem_usage() -> dict:
    """Parse ``df -B1`` and return a dict mapping device names to usage percent for block devices."""
    out = run_cmd("df -B1")
    usage = {}
    for line in out.splitlines():
        if line.startswith("Filesystem"):
            continue
        parts = line.split()
        if len(parts) >= 6:
            device = parts[0]
            # Filter for block devices (e.g., /dev/sd*, not tmpfs, etc.)
            if device.startswith('/dev/') and not any(x in device for x in ['tmpfs', 'devtmpfs', 'squashfs']):
                try:
                    total = int(parts[1])
                    used = int(parts[2])
                    percent = (used / total) * 100.0 if total else 0.0
                    usage[device] = percent
                except Exception:
                    pass
    return usage


def temperature() -> float:
    """Parse ``sensors`` output and return the average temperature in Celsius.
    """
    out = run_cmd("sensors")
    temps = []
    for line in out.splitlines():
        if "temp" in line.lower() and "+" in line and "°C" in line:
            try:
                start = line.find('+')
                end = line.find('°C')
                if start != -1 and end != -1:
                    temps.append(float(line[start + 1:end]))
            except Exception:
                pass
    return sum(temps) / len(temps) if temps else 0.0


def smart_health() -> int:
    """Run ``smartctl -a /dev/sda`` and return 1 for *PASSED* and 0 for *FAILED*.
    """
    out = run_cmd("smartctl -a /dev/sda")
    for line in out.splitlines():
        if "SMART overall-health self-assessment test result" in line:
            if "PASSED" in line:
                return 1
            else:
                return 0
    return -1  # unknown / not available


def swap_usage_percent() -> float:
    """Parse ``free -b`` and return swap usage as a percentage of total."""
    out = run_cmd("free -b")
    for line in out.splitlines():
        if line.startswith("Swap:"):
            parts = line.split()
            try:
                total = int(parts[1])
                used = int(parts[2])
                return (used / total) * 100.0 if total else 0.0
            except Exception:
                continue
    return 0.0


def network_dropped_packets() -> int:
    """Read ``/proc/net/dev`` and sum dropped packets across all interfaces."""
    out = run_cmd("cat /proc/net/dev")
    dropped = 0
    for line in out.splitlines():
        if ':' in line:
            _, data = line.split(':', 1)
            fields = data.split()
            if len(fields) >= 4:
                try:
                    dropped += int(fields[3])
                except Exception:
                    pass
    return dropped
# ---------------------------------------------------------------------------
# Collector loop – runs every *interval* seconds and populates the global *metrics*
# ---------------------------------------------------------------------------

def collect():
    cfg = load_config()
    mode = cfg["mode"]
    metrics["cpu_usage_percent"] = cpu_usage()
    metrics["memory_usage_percent"] = memory_usage()
    rd, wr = disk_io()
    metrics["disk_read_megabytes_total"] = rd
    metrics["disk_write_megabytes_total"] = wr
    rx, tx = network_io()
    metrics["network_receive_megabytes_total"] = rx
    metrics["network_transmit_megabytes_total"] = tx
    metrics["process_count"] = process_count()
    # Overall filesystem usage percent
    metrics["filesystem_usage_percent"] = filesystem_usage()
    # Per-device filesystem usage metrics
    device_usage = per_device_filesystem_usage()
    for device, percent in device_usage.items():
        # Sanitize device name for metric key
        metric_name = f"filesystem_usage_percent_{device.replace('/', '_').replace('.', '_')}"
        metrics[metric_name] = percent
    # Swap usage percent
    metrics["swap_usage_percent"] = swap_usage_percent()
    # Network dropped packets total
    metrics["network_dropped_packets_total"] = network_dropped_packets()
    if mode == "metal":
        metrics["temperature_celsius"] = temperature()
        metrics["smart_health_status"] = smart_health()
    else:
        metrics["temperature_celsius"] = 0.0
        metrics["smart_health_status"] = -1


def collector_thread():
    cfg = load_config()
    interval = cfg["interval"]
    while True:
        collect()
        time.sleep(interval)

# ---------------------------------------------------------------------------
# HTTP exporter – Prometheus text exposition format
# ---------------------------------------------------------------------------

class MetricsHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        lines = []
        for name, value in metrics.items():
            lines.append(f"# HELP {name} {name.replace('_', ' ')}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")
        self.wfile.write("\n".join(lines).encode())

    # Suppress default logging to stdout/stderr
    def log_message(self, *args, **kwargs):
        pass


def start_http_server():
    cfg = load_config()
    port = cfg["port"]
    with socketserver.TCPServer(("", port), MetricsHandler) as httpd:
        httpd.serve_forever()

# ---------------------------------------------------------------------------
# Main entry point – start collector thread and HTTP server
# ---------------------------------------------------------------------------

def main():
    threading.Thread(target=collector_thread, daemon=True).start()
    start_http_server()

if __name__ == "__main__":
    main()
