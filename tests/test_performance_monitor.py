import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import sys


APP_DIR = Path(__file__).resolve().parents[1] / "apps" / "expression-ui"
sys.path.insert(0, str(APP_DIR))

from performance_monitor import PerformanceMonitor, format_bytes


class PerformanceMonitorTests(unittest.TestCase):
    def test_format_bytes(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1536), "1.5 KB")
        self.assertEqual(format_bytes(2 * 1024 * 1024, per_second=True), "2.0 MB/s")

    def test_memory_uses_available_capacity(self) -> None:
        content = "MemTotal:       1000 kB\nMemAvailable:    250 kB\n"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "meminfo"
            source.write_text(content, encoding="utf-8")
            original_read_text = Path.read_text

            def read_text(path: Path, *args, **kwargs):
                if str(path) == "/proc/meminfo":
                    return original_read_text(source, *args, **kwargs)
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", read_text):
                used, total, percent = PerformanceMonitor._read_memory()
        self.assertEqual(total, 1000 * 1024)
        self.assertEqual(used, 750 * 1024)
        self.assertEqual(percent, 75.0)

    def test_cpu_delta(self) -> None:
        monitor = PerformanceMonitor()
        monitor._previous_cpu = (1000, 600)
        monitor._previous_network = (100, 100, 10.0)
        with ExitStack() as stack:
            stack.enter_context(patch.object(monitor, "_read_cpu_counters", return_value=(1100, 640)))
            stack.enter_context(patch.object(monitor, "_read_network_counters", return_value=(100, 100)))
            stack.enter_context(patch.object(monitor, "_read_memory", return_value=(1, 2, 50.0)))
            stack.enter_context(patch.object(monitor, "_read_disk", return_value=(1, 4, 25.0)))
            stack.enter_context(patch.object(monitor, "_read_hailo_and_health", return_value=(True, False, 25, 25)))
            stack.enter_context(patch.object(monitor, "_read_number", return_value=50.0))
            stack.enter_context(patch.object(monitor, "_read_uptime", return_value=100))
            stack.enter_context(patch.object(monitor, "_read_process_rss", return_value=1024))
            stack.enter_context(patch("performance_monitor.time.monotonic", return_value=11.0))
            snapshot = monitor.collect()
        self.assertEqual(snapshot.cpu_percent, 60.0)
        self.assertTrue(snapshot.hailo_ready)
        self.assertFalse(snapshot.hailo_active)


if __name__ == "__main__":
    unittest.main()
