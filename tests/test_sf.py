import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from adbperfetto.adb import Result
from adbperfetto.sf import SfCapture


class SfCaptureTests(unittest.TestCase):
    def test_dump_writes_file_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = mock.Mock()
            adb.shell.return_value = Result(0, "SF BODY", "")
            cap = SfCapture(adb, "SER", tmp, interval_ms=50,
                            sleep=lambda s: None)
            cap._dump_once()
            files = list(Path(tmp).glob("sf_*.txt"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(encoding="utf-8"), "SF BODY")
            self.assertEqual(cap.dump_count, 1)
            adb.shell.assert_called_once_with(
                "SER", "dumpsys SurfaceFlinger", timeout=30)

    def test_start_stop_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = mock.Mock()
            adb.shell.return_value = Result(0, "x", "e")
            started = threading.Event()

            def shell(*args, **kwargs):
                started.set()
                return Result(0, "x", "")

            adb.shell.side_effect = shell
            cap = SfCapture(adb, "SER", tmp, interval_ms=50)
            cap.start()
            self.assertTrue(started.wait(2))
            cap.stop()
            self.assertFalse(cap.running)
            self.assertGreaterEqual(cap.dump_count, 1)
            text = next(Path(tmp).glob("sf_*.txt")).read_text(encoding="utf-8")
            self.assertIn("x", text)

    def test_from_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = mock.Mock()
            cap = SfCapture(adb, "SER", tmp, interval_ms=300)
            cap.dump_count = 7
            restored = SfCapture.from_state(
                adb, "SER", cap.state_dict())
            self.assertEqual(restored.interval_ms, 300)
            self.assertEqual(restored.dump_count, 7)
            self.assertEqual(str(restored.output_dir), str(Path(tmp)))

    def test_interval_clamped(self):
        cap = SfCapture(mock.Mock(), "SER", ".", interval_ms=10)
        self.assertEqual(cap.interval_ms, 50)


if __name__ == "__main__":
    unittest.main()
