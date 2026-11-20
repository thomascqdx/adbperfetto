import tempfile
import unittest
import zipfile
from pathlib import Path

from adbperfetto import outputs
from adbperfetto.outputs import (
    check_integrity, recent, safe_filename, unique_path, write_zip)


class FilenameTests(unittest.TestCase):
    def test_clean_and_collapse(self):
        self.assertEqual(safe_filename("TECNO KN8"), "TECNO_KN8")
        self.assertEqual(safe_filename("a 中文/b--c.1"), "a_b--c.1")

    def test_empty_and_windows_reserved(self):
        self.assertEqual(safe_filename("..."), "unknown")
        self.assertEqual(safe_filename("CON.txt"), "_CON.txt")
        self.assertEqual(safe_filename("___"), "unknown")

    def test_build_display_id(self):
        build = "KN8-16.3.0.155SP07(OP003PF001AZ)"
        self.assertEqual(safe_filename(build),
                         "KN8-16.3.0.155SP07_OP003PF001AZ")

    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = unique_path(Path(tmp) / "a.pftrace")
            self.assertEqual(first.name, "a.pftrace")
            first.touch()
            second = unique_path(Path(tmp) / "a.pftrace")
            self.assertEqual(second.name, "a_2.pftrace")
            second.touch()
            self.assertEqual(unique_path(Path(tmp) / "a.pftrace").name,
                             "a_3.pftrace")


class IntegrityTests(unittest.TestCase):
    def test_missing_and_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "trace").mkdir()
            (root / "trace" / "a.pftrace").touch()
            checks = check_integrity(root)
            self.assertFalse(checks["Perfetto trace"][0])
            self.assertEqual(checks["MobileLog session"][2], [
                "main_log", "kernel_log", "sys_log", "events_log"])
            self.assertFalse(checks["device_info.txt"][0])

    def test_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "trace").mkdir()
            (root / "trace" / "a.pftrace").write_bytes(b"123")
            log = root / "mobilelog" / "APLog_1"
            log.mkdir(parents=True)
            for name in ("main_log", "kernel_log", "sys_log", "events_log"):
                (log / (name + "_1")).write_bytes(b"12")
            (root / "device_info.txt").write_text("info")
            checks = check_integrity(root)
            self.assertTrue(all(item[0] for item in checks.values()))
            self.assertEqual(checks["Perfetto trace"][1], 3)
            self.assertEqual(checks["MobileLog session"][1], 8)

    def test_sf_only_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "trace").mkdir()
            (root / "trace" / "a.pftrace").write_bytes(b"123")
            sf = root / "sf"
            sf.mkdir()
            (sf / "sf_20260101_120000_000.txt").write_text("sf")
            (root / "device_info.txt").write_text("info")
            checks = check_integrity(root)
            self.assertNotIn("MobileLog session", checks)
            self.assertTrue(checks["SF dumps"][0])
            self.assertEqual(checks["SF dumps"][1], 2)


class ArchiveTests(unittest.TestCase):
    def test_write_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "files"
            (root / "trace").mkdir(parents=True)
            (root / "trace" / "a.pftrace").write_bytes(b"trace")
            archive = Path(tmp) / "out.zip"
            write_zip(root, archive)
            with zipfile.ZipFile(archive) as zf:
                self.assertEqual(zf.read("trace/a.pftrace"), b"trace")
                self.assertEqual(zf.getinfo("trace/a.pftrace").compress_type,
                                 zipfile.ZIP_DEFLATED)

    def test_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            older = out / "a.pftrace"
            older.write_bytes(b"1")
            newer = out / "b.zip"
            newer.write_bytes(b"2")
            (out / "ignored.txt").write_text("x")
            import os, time
            past = time.time() - 100
            os.utime(older, (past, past))
            entries = recent(out)
            self.assertEqual([e["name"] for e in entries],
                             ["b.zip", "a.pftrace"])
            self.assertEqual(entries[0]["size"], 1)
            self.assertEqual(entries[1]["size"], 1)


if __name__ == "__main__":
    unittest.main()
