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


if __name__ == "__main__":
    unittest.main()
