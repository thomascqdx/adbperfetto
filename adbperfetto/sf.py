"""Periodic SurfaceFlinger dumpsys capture during a recording session.

Ported from the standalone sf.py script: every interval_ms, run
`adb shell dumpsys SurfaceFlinger` and write a timestamped .txt file.
"""

import threading
import time
from datetime import datetime
from pathlib import Path


class SfCaptureError(Exception):
    pass


class SfCapture:
    """Background SurfaceFlinger dump loop for one device session."""

    def __init__(self, adb, serial: str, output_dir, interval_ms: int = 200,
                 on_message=None, sleep=None):
        self.adb = adb
        self.serial = serial
        self.output_dir = Path(output_dir)
        self.interval_ms = max(50, int(interval_ms))
        self.on_message = on_message or (lambda message: None)
        self._sleep = sleep or time.sleep
        self._stop = threading.Event()
        self._thread = None
        self.dump_count = 0
        self.running = False
        self.error = None
        self.started = False

    def start(self):
        if self.running:
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self.running = True
        self.started = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"sf-{self.serial}")
        self._thread.start()
        self.on_message(
            f"SF capture started (every {self.interval_ms} ms)")

    def stop(self):
        """Stop the dump loop and wait for the worker to exit."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(5.0, self.interval_ms / 1000.0 + 5.0))
        self.running = False
        self._thread = None

    def _loop(self):
        interval_s = self.interval_ms / 1000.0
        while not self._stop.is_set():
            try:
                self._dump_once()
            except Exception as exc:
                self.error = str(exc)
            # Interruptible wait so stop() returns promptly.
            if self._stop.wait(interval_s):
                break
        self.running = False

    def _dump_once(self):
        now = datetime.now()
        timestamp = (now.strftime("%Y%m%d_%H%M%S_")
                     + f"{now.microsecond // 1000:03d}")
        file_path = self.output_dir / f"sf_{timestamp}.txt"
        result = self.adb.shell(
            self.serial, "dumpsys SurfaceFlinger", timeout=30)
        text = result.stdout or ""
        if result.stderr:
            text += "\n\n[stderr]\n" + result.stderr
        file_path.write_text(text, encoding="utf-8", errors="replace")
        self.dump_count += 1

    def state_dict(self) -> dict:
        return {
            "enabled": True,
            "interval_ms": self.interval_ms,
            "output_dir": str(self.output_dir),
            "dump_count": self.dump_count,
            "error": self.error,
        }

    def api_dict(self) -> dict:
        return {
            "running": self.running,
            "interval_ms": self.interval_ms,
            "dump_count": self.dump_count,
            "error": self.error,
        }

    @classmethod
    def from_state(cls, adb, serial: str, record: dict, on_message=None,
                   sleep=None):
        capture = cls(
            adb, serial,
            record.get("output_dir") or ".",
            interval_ms=int(record.get("interval_ms") or 200),
            on_message=on_message,
            sleep=sleep,
        )
        capture.dump_count = int(record.get("dump_count") or 0)
        capture.error = record.get("error")
        capture.started = True
        return capture
