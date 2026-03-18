#!/usr/bin/env python3
import json
import os
import subprocess
import threading
import time
import signal
import sys
import logging
import atexit
import http.server
import socketserver

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('exporter.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger.info("Logging initialized")

# Global reference to the HTTP server for graceful shutdown
_http_server = None
_shutdown_event = threading.Event()

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
    logger.info("Loading config from %s", CONFIG_PATH)
    try:
        cfg = {}
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        logger.info(f"Loaded config: {cfg}")
    except Exception as e:
        logger.warning(f"Config load failed ({e}), using defaults")
        # Missing file or malformed JSON – fall back to defaults
        cfg = {}
    # Merge defaults
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    cfg["mode"] = cfg["mode"].lower()
    if cfg["mode"] not in ("metal", "vm"):
        cfg["mode"] = "metal"
    logger.info(f"Final config mode: {cfg['mode']}, interval: {cfg['interval']}, port: {cfg['port']}")
    return cfg

def run_cmd(command: str) -> str:
    """Execute *command* and return its stdout as a string.

    Errors are silenced – an empty string is returned on failure.
    """
    logger.debug("Running command: %s", command)
    try:
        output = subprocess.check_output(command, shell=True, text=True, stderr=subprocess.DEVNULL, timeout=30)
        logger.debug(f"Command '{command}' succeeded, output length: {len(output)}")
        return output
    except subprocess.TimeoutExpired:
        logger.error(f"Command '{command}' timed out after 30s")
        return ""
    except Exception as e:
        logger.warning(f"Command '{command}' failed: {e}")
        return ""

# ---------------------------------------------------------------------------
# Metric collection helpers (each returns a numeric value or a tuple of values)
# ---------------------------------------------------------------------------

def cpu_usage() -> float:
    """Parse ``top -bn1`` to obtain the overall CPU usage percentage.

    The line ``%Cpu(s):  2.0 us,  1.0 sy, … 96.5 id, …`` is used – the idle
    percentage is subtracted from 100 %.
    """
    try:
        out = run_cmd("top -bn1")
        for line in out.splitlines():
            if "%Cpu" in line:
                parts = line.split()
                try:
                    idx = parts.index("id")
                    idle = float(parts[idx - 1])
                    logger.debug(f"CPU idle: {idle}%, usage: {100.0 - idle}%")
                    return 100.0 - idle
                except Exception as e:
                    logger.debug(f"CPU parse error: {e}")
                    continue
        logger.warning("No CPU line found in top output")
        return 0.0
    except Exception as e:
        logger.error(f"CPU usage collection failed: {e}")
        return 0.0


def memory_usage() -> float:
    """Parse ``free -b`` and return memory usage as a percentage of total.
    """
    try:
        out = run_cmd("free -b")
        for line in out.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                try:
                    total = int(parts[1])
                    used = int(parts[2])
                    percent = (used / total) * 100.0 if total else 0.0
                    logger.debug(f"Memory: used {used}/{total} ({percent}%)")
                    return percent
                except Exception as e:
                    logger.debug(f"Memory parse error: {e}")
                    continue
        logger.warning("No Mem line found in free output")
        return 0.0
    except Exception as e:
        logger.error(f"Memory usage collection failed: {e}")
        return 0.0


def disk_io() -> tuple:
    """Run ``iotop -b -n1 -qqq`` and extract total read/write megabytes per second.

    The output contains lines like ``Total DISK READ: 0.00 B/s  Total DISK WRITE: 0.00 B/s``.
    """
    try:
        out = run_cmd("iotop -b -n1 -qqq")
        read = 0.0
        write = 0.0
        for line in out.splitlines():
            if "Total DISK READ:" in line:
                parts = line.split()
                try:
                    idx = parts.index("B/s")
                    read = float(parts[idx - 1])
                except Exception as e:
                    logger.debug(f"Disk read parse error: {e}")
                    pass
            if "Total DISK WRITE:" in line:
                parts = line.split()
                try:
                    idx = parts.index("B/s")
                    write = float(parts[idx - 1])
                except Exception as e:
                    logger.debug(f"Disk write parse error: {e}")
                    pass
        # Convert bytes per second to megabytes per second
        read_mb = read / (1024 * 1024)
        write_mb = write / (1024 * 1024)
        logger.debug(f"Disk I/O: read {read_mb} MB/s, write {write_mb} MB/s")
        return read_mb, write_mb
    except Exception as e:
        logger.error(f"Disk I/O collection failed: {e}")
        return 0.0, 0.0


def network_io() -> tuple:
    """Read ``/proc/net/dev`` and sum received and transmitted megabytes across all interfaces.
    """
    try:
        out = run_cmd("cat /proc/net/dev")
        recv = 0
        trans = 0
        interfaces = 0
        for line in out.splitlines():
            if ':' in line:
                _, data = line.split(':', 1)
                fields = data.split()
                if len(fields) >= 16:
                    try:
                        recv += int(fields[0])
                        trans += int(fields[8])
                        interfaces += 1
                    except Exception as e:
                        logger.debug(f"Network parse error for line: {e}")
                        pass
        # Convert bytes to megabytes
        recv_mb = recv / (1024 * 1024)
        trans_mb = trans / (1024 * 1024)
        logger.debug(f"Network I/O: {interfaces} interfaces, recv {recv_mb} MB, trans {trans_mb} MB")
        return recv_mb, trans_mb
    except Exception as e:
        logger.error(f"Network I/O collection failed: {e}")
        return 0.0, 0.0


def process_count() -> int:
    """Count the number of processes using ``ps -e | wc -l``.
    """
    try:
        out = run_cmd("ps -e | wc -l")
        count = int(out.strip())
        logger.debug(f"Process count: {count}")
        return count
    except Exception as e:
        logger.error(f"Process count failed: {e}")
        return 0


def filesystem_usage() -> float:
    """Aggregate filesystem usage from ``df -B1`` (bytes) and return the overall % used.
    """
    try:
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
                except Exception as e:
                    logger.debug(f"Filesystem aggregate parse error: {e}")
                    pass
        percent = (used / total) * 100.0 if total else 0.0
        logger.debug(f"Aggregate filesystem: {percent}% used ({used}/{total} bytes)")
        return percent
    except Exception as e:
        logger.error(f"Filesystem usage collection failed: {e}")
        return 0.0


def per_device_filesystem_usage() -> dict:
    """Parse ``df -B1`` and return a dict mapping device names to usage percent for block devices."""
    try:
        out = run_cmd("df -B1")
        usage = {}
        block_devices = 0
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
                        block_devices += 1
                    except Exception as e:
                        logger.debug(f"Per-device parse error for {device}: {e}")
                        pass
        logger.debug(f"Per-device filesystem: {block_devices} block devices tracked")
        return usage
    except Exception as e:
        logger.error(f"Per-device filesystem collection failed: {e}")
        return {}


def temperature() -> float:
    """Parse ``sensors`` output and return the average temperature in Celsius.
    """
    try:
        out = run_cmd("sensors")
        temps = []
        for line in out.splitlines():
            if "temp" in line.lower() and "+" in line and "°C" in line:
                try:
                    start = line.find('+')
                    end = line.find('°C')
                    if start != -1 and end != -1:
                        temp_val = float(line[start + 1:end])
                        temps.append(temp_val)
                except Exception as e:
                    logger.debug(f"Temperature parse error: {e}")
                    pass
        avg = sum(temps) / len(temps) if temps else 0.0
        logger.debug(f"Temperature: {len(temps)} sensors, avg {avg}°C")
        return avg
    except Exception as e:
        logger.error(f"Temperature collection failed: {e}")
        return 0.0


def smart_health() -> int:
    """Run ``smartctl -a /dev/sda`` and return 1 for *PASSED* and 0 for *FAILED*.
    """
    try:
        out = run_cmd("smartctl -a /dev/sda")
        for line in out.splitlines():
            if "SMART overall-health self-assessment test result" in line:
                if "PASSED" in line:
                    logger.debug("SMART health: PASSED")
                    return 1
                else:
                    logger.warning("SMART health: FAILED")
                    return 0
        logger.warning("SMART health: unknown/not available")
        return -1  # unknown / not available
    except Exception as e:
        logger.error(f"SMART health collection failed: {e}")
        return -1


def swap_usage_percent() -> float:
    """Parse ``free -b`` and return swap usage as a percentage of total."""
    try:
        out = run_cmd("free -b")
        for line in out.splitlines():
            if line.startswith("Swap:"):
                parts = line.split()
                try:
                    total = int(parts[1])
                    used = int(parts[2])
                    percent = (used / total) * 100.0 if total else 0.0
                    logger.debug(f"Swap: used {used}/{total} ({percent}%)")
                    return percent
                except Exception as e:
                    logger.debug(f"Swap parse error: {e}")
                    continue
        logger.warning("No Swap line found in free output")
        return 0.0
    except Exception as e:
        logger.error(f"Swap usage collection failed: {e}")
        return 0.0


def network_dropped_packets() -> int:
    """Read ``/proc/net/dev`` and sum dropped packets across all interfaces."""
    try:
        out = run_cmd("cat /proc/net/dev")
        dropped = 0
        interfaces = 0
        for line in out.splitlines():
            if ':' in line:
                _, data = line.split(':', 1)
                fields = data.split()
                if len(fields) >= 4:
                    try:
                        dropped += int(fields[3])
                        interfaces += 1
                    except Exception as e:
                        logger.debug(f"Dropped packets parse error: {e}")
                        pass
        logger.debug(f"Network dropped packets: {dropped} across {interfaces} interfaces")
        return dropped
    except Exception as e:
        logger.error(f"Network dropped packets collection failed: {e}")
        return 0
# ---------------------------------------------------------------------------
# Collector loop – runs every *interval* seconds and populates the global *metrics*
# ---------------------------------------------------------------------------

def collect():
    logger.debug("Starting collect()")
    global metrics
    logger.debug("Declared global metrics")
    try:
        cfg = load_config()
        mode = cfg["mode"]
        logger.debug(f"Collecting metrics in mode: {mode}")
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
            # Sanitize device name for metric key - Prometheus compatible
            # Remove leading /dev/ prefix and replace remaining / with single underscore
            sanitized = device.lstrip('/').replace('/', '_')
            # Replace any remaining non-alphanumeric characters (except underscore) with underscore
            sanitized = ''.join(c if c.isalnum() or c == '_' else '_' for c in sanitized)
            # Collapse multiple underscores into single underscore
            while '__' in sanitized:
                sanitized = sanitized.replace('__', '_')
            metric_name = f"filesystem_usage_percent_{sanitized}"
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
        logger.info(f"Metrics collected successfully: {len(metrics)} keys")
    except Exception as e:
        logger.error(f"Error in collect(): {e}", exc_info=True)
        # Ensure metrics dict is not corrupted
        if not isinstance(metrics, dict):
            metrics = {}
            logger.error("Metrics dict corrupted, reset to empty")


def collector_thread():
    logger.info("Collector thread starting")
    cfg = load_config()
    interval = cfg["interval"]
    logger.info(f"Collector thread running with interval {interval}s")
    while not _shutdown_event.is_set():
        try:
            collect()
            time.sleep(interval)
        except Exception as e:
            logger.error(f"Collector thread error: {e}", exc_info=True)
            time.sleep(5)  # Backoff on error
    logger.info("Collector thread shutting down")

# ---------------------------------------------------------------------------
# HTTP exporter – Prometheus text exposition format
# ---------------------------------------------------------------------------

class MetricsHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        logger.debug(f"Handling GET request for path: {self.path}")
        try:
            if self.path != "/metrics":
                self.send_response(404)
                self.end_headers()
                logger.warning(f"404 for path: {self.path}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            lines = []
            for name, value in metrics.items():
                lines.append(f"# HELP {name} {name.replace('_', ' ')}")
                lines.append(f"# TYPE {name} gauge")
                if isinstance(value, float):
                    value_str = "%.3f" % value
                else:
                    value_str = str(value)
                lines.append(f"{name} {value_str}")
            output = ("\n".join(lines) + "\n").encode()
            self.wfile.write(output)
            logger.info(f"Served /metrics: {len(metrics)} metrics, {len(output)} bytes")
        except Exception as e:
            logger.error(f"Error serving /metrics: {e}", exc_info=True)
            self.send_response(500)
            self.end_headers()

    # Suppress default logging to stdout/stderr
    def log_message(self, *args, **kwargs):
        pass


def shutdown_handler(signum, frame):
    """Handle shutdown signals (SIGINT, SIGTERM) gracefully."""
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    _shutdown_event.set()
    sys.exit()

def start_http_server():
    global _http_server
    cfg = load_config()
    port = cfg["port"]
    logger.info(f"Starting HTTP server on port {port}")
    
    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    
    # Register atexit handler for cleanup
    atexit.register(cleanup)
    
    try:
        # Create server without binding first, then set allow_reuse_address before binding
        httpd = socketserver.TCPServer(("", port), MetricsHandler, bind_and_activate=False)
        httpd.allow_reuse_address = True
        httpd.server_bind()
        httpd.server_activate()
        _http_server = httpd
        logger.info("HTTP server started successfully")
        httpd.serve_forever()
    except OSError as e:
        logger.error(f"Port binding failed (perhaps in use?): {e}")
        raise
    except Exception as e:
        logger.error(f"HTTP server failed: {e}", exc_info=True)
        raise

def cleanup():
    """Clean up resources on shutdown."""
    global _http_server
    if _http_server:
        logger.info("Shutting down HTTP server...")
        _http_server.shutdown()
        _http_server.server_close()
        _http_server = None
        logger.info("HTTP server shut down successfully")

# ---------------------------------------------------------------------------
# Main entry point – start collector thread and HTTP server
# ---------------------------------------------------------------------------

def main():
    logger.info("Starting exporter...")
    try:
        threading.Thread(target=collector_thread, daemon=True).start()
        logger.info("Collector thread started")
        start_http_server()
    except KeyboardInterrupt:
        logger.info("Main loop interrupted by user")
    except Exception as e:
        logger.error(f"Main startup failed: {e}")
        raise
    finally:
        cleanup()

if __name__ == "__main__":
    main()
