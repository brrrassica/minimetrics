import io
import unittest
from unittest.mock import patch

import exporter


class CommandOutputParsingTests(unittest.TestCase):
    @patch("exporter.run_cmd")
    def test_read_and_write_on_shared_iotop_line(self, run_cmd):
        run_cmd.return_value = (
            "Total DISK READ: 5.00 B/s | Total DISK WRITE: 1.00 B/s\n"
        )

        read, write = exporter.disk_io()

        self.assertAlmostEqual(read, 5 / (1024 * 1024))
        self.assertAlmostEqual(write, 1 / (1024 * 1024))

    @patch("exporter.run_cmd")
    def test_iotop_units_are_normalized(self, run_cmd):
        run_cmd.return_value = (
            "Total DISK READ: 2.00 M/s | Total DISK WRITE: 1.00 K/s\n"
        )

        read, write = exporter.disk_io()

        self.assertAlmostEqual(read, 2.0)
        self.assertAlmostEqual(write, 1 / 1024)
        self.assertAlmostEqual(
            exporter.parse_iotop_rate("Total DISK WRITE: 3.00 G/s", "Total DISK WRITE:"),
            3 * 1024 ** 3,
        )

    @patch("exporter.run_cmd")
    def test_cpu_idle_with_trailing_comma(self, run_cmd):
        run_cmd.return_value = (
            "%Cpu(s):  1.0 us,  0.5 sy,  0.0 ni, 98.5 id,  0.0 wa\n"
        )

        self.assertAlmostEqual(exporter.cpu_usage(), 1.5)

    @patch("exporter.run_cmd")
    def test_cpu_idle_without_punctuation(self, run_cmd):
        run_cmd.return_value = "%Cpu(s):  2.0 us, 98.0 id\n"

        self.assertAlmostEqual(exporter.cpu_usage(), 2.0)

    @patch("exporter.run_cmd")
    def test_process_count_ignores_ps_header(self, run_cmd):
        run_cmd.return_value = "386\n"

        self.assertEqual(exporter.process_count(), 386)
        run_cmd.assert_called_once_with("ps -e --no-headers | wc -l")


class MetricsStateTests(unittest.TestCase):
    @staticmethod
    def collector_patches(**overrides):
        patches = {
            "load_config": lambda: {"mode": "vm"},
            "cpu_usage": lambda: 10.0,
            "memory_usage": lambda: 20.0,
            "disk_io": lambda: (1.0, 2.0),
            "network_io": lambda: (3.0, 4.0),
            "process_count": lambda: 5,
            "filesystem_usage": lambda: 6.0,
            "per_device_filesystem_usage": lambda: {},
            "swap_usage_percent": lambda: 7.0,
            "network_dropped_packets": lambda: 8,
        }
        patches.update(overrides)
        return patches

    def setUp(self):
        self.original_metrics = dict(exporter.metrics)
        exporter.metrics.clear()

    def tearDown(self):
        exporter.metrics.clear()
        exporter.metrics.update(self.original_metrics)

    def test_snapshot_is_detached_from_live_metrics(self):
        exporter.metrics["cpu_usage_percent"] = 1.0
        snapshot = exporter.get_metrics_snapshot()

        exporter.metrics["memory_usage_percent"] = 2.0

        self.assertEqual(snapshot, {"cpu_usage_percent": 1.0})

    def test_collect_publishes_complete_metrics_and_prunes_stale_devices(self):
        patches = self.collector_patches(
            per_device_filesystem_usage=lambda: {"/dev/sda1": 50.0}
        )
        exporter.metrics["filesystem_usage_percent_dev_sda9"] = 99.0

        with patch.multiple("exporter", **patches):
            exporter.collect()

        self.assertNotIn("filesystem_usage_percent_dev_sda9", exporter.metrics)
        self.assertEqual(exporter.metrics["filesystem_usage_percent_dev_sda1"], 50.0)
        self.assertEqual(exporter.metrics["cpu_usage_percent"], 10.0)
        self.assertEqual(exporter.metrics["process_count"], 5)

    def test_collect_does_not_publish_partial_metrics(self):
        observed = {}

        def late_collector():
            observed["during_collection"] = dict(exporter.metrics)
            return 7.0

        with patch.multiple("exporter", **self.collector_patches(swap_usage_percent=late_collector)):
            exporter.collect()

        self.assertEqual(observed["during_collection"], {})
        self.assertEqual(exporter.metrics["swap_usage_percent"], 7.0)

    def test_do_get_renders_metric_snapshot(self):
        exporter.metrics["cpu_usage_percent"] = 1.0
        handler = type(
            "Handler",
            (),
            {
                "path": "/metrics",
                "wfile": io.BytesIO(),
                "send_response": lambda self, code: setattr(self, "status", code),
                "send_header": lambda self, key, value: None,
                "end_headers": lambda self: None,
            },
        )()

        exporter.MetricsHandler.do_GET(handler)

        self.assertEqual(handler.status, 200)
        self.assertIn(b"cpu_usage_percent 1.000", handler.wfile.getvalue())


if __name__ == "__main__":
    unittest.main()
