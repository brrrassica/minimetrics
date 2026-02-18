# Lightweight Prometheus Node Exporter

A minimal, self‑contained metrics exporter written in **Python 3** with a tiny **Bash** wrapper. It collects a curated set of system metrics suitable for both bare‑metal servers and VMs while keeping the memory footprint under **1 MiB**.

## Features
- Low‑cardinality Prometheus exposition format (text protocol)
- Configurable mode (`metal` or `vm`) via a JSON file (`config.json`)
- Background collector (default 15 s interval) and HTTP endpoint on `/metrics`
- No external Python dependencies – only the Python standard library
- Simple Bash wrapper (`run_exporter.sh`) that daemonises the process and handles logging

## Metrics Exported
| Metric | Description |
|--------|-------------|
| `cpu_usage_percent` | Overall CPU usage percentage |
| `memory_usage_percent` | Memory usage percentage |
| `disk_read_bytes_total` | Total disk read bytes per second |
| `disk_write_bytes_total` | Total disk write bytes per second |
| `network_receive_bytes_total` | Total network received bytes |
| `network_transmit_bytes_total` | Total network transmitted bytes |
| `process_count` | Number of processes |
| `filesystem_usage_percent` | Aggregate filesystem usage percentage |
| `temperature_celsius` | Average temperature (metal only) |
| `smart_health_status` | SMART health (1 = PASSED, 0 = FAILED, -1 = N/A) |

All metrics are exposed as **gauges**.

## Installation
```bash
# Clone the repository (or copy the files into a directory)
git clone https://github.com/yourorg/minimetrics.git
cd minimetrics

# Ensure the script is executable
chmod +x run_exporter.sh exporter.py
```

## Configuration (`config.json`)
```json
{
  "mode": "metal",   // "metal" or "vm"
  "interval": 15,     // collection interval in seconds
  "port": 9100        // HTTP port for Prometheus to scrape
}
```

- **Metal** mode enables temperature and SMART health collection.
- **VM** mode skips hardware‑specific metrics.

## Running the Exporter
```bash
# Start the exporter as a background daemon
./run_exporter.sh

# The process writes its PID to exporter.pid and logs to exporter.log
```

Prometheus can be configured to scrape the exporter:
```yaml
scrape_configs:
  - job_name: 'mini_metrics'
    static_configs:
      - targets: ['<host_ip>:9100']
```

## Testing
A quick sanity check can be performed with `curl`:
```bash
curl http://localhost:9100/metrics
```
You should see a plain‑text response containing the metric names and values.

## Packaging as a Single Executable (zipapp)
The exporter can be bundled into a zipapp for easy distribution:
```bash
# From the project root
python -m zipapp . -p "#!/usr/bin/env python3" -o mini_exporter.pyz
chmod +x mini_exporter.pyz
```
Run the zipapp directly:
```bash
./mini_exporter.pyz
```

## Memory Optimisation
- All imports are from the Python standard library.
- Metric values are stored in a tiny dictionary (`metrics`).
- Subprocess output is read in a streaming fashion where possible.
- The total resident set size stays well below **1 MiB** on typical Linux installations.

## License
MIT – feel free to modify and adapt for your own monitoring stack.
