"""Session lifecycle: record / flight recorder sessions per device.

Verified device facts (KN8, Perfetto v49):
- `perfetto --detach=K` requires `write_into_file: true` in the config.
- `perfetto --is_detached=K` rc 0 = alive, rc 2 = not found/ended; stderr
  shows a harmless "Warning: No PTY..." line.
- Flight: `perfetto --txt -c <cfg> -o <base> --background-wait` prints the
  pid on stdout and survives `adb reconnect`.
- `perfetto --clone-by-name <name> -o <file>` writes a snapshot
  synchronously; the session keeps recording.
- `trigger_perfetto <name>` produces `<base>.0`, `.1` ... asynchronously
  in CLONE_SNAPSHOT mode.
- `kill -TERM <pid>` ends the flight session; the main `-o` file stays
  0 bytes. There is no `--cancel` flag in perfetto v49.
- Perfetto cannot read configs from /data/local/tmp (SELinux); configs go
  to /data/misc/perfetto-configs/, traces to /data/misc/perfetto-traces/.
"""

import dataclasses
import logging
import os
import queue
import re
import shlex
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import BUILD, VERSION, pbtx
from .adb import AdbError, is_wifi_serial
from .device import DeviceInfo, probe
from .mobilelog import MobileLog, other_log_names
from .sf import SfCapture
from . import outputs

log = logging.getLogger(__name__)

DETACH_KEY = "adbperfetto"
FLIGHT_NAME = "adbperfetto_flight"
FLIGHT_TRIGGER = "adbperfetto_dump"
REMOTE_TRACE_DIR = "/data/misc/perfetto-traces"
REMOTE_CONFIG_DIR = "/data/misc/perfetto-configs"
CONFIG_RECORD = f"{REMOTE_CONFIG_DIR}/adbperfetto_record.pbtx"
CONFIG_FLIGHT = f"{REMOTE_CONFIG_DIR}/adbperfetto_flight.pbtx"

TERMINAL = ("done", "failed", "ended", "abandoned")


class SessionError(Exception):
    pass


# ------------------------------------------------------------------ parsers

def parse_record_status(text: str) -> dict | None:
    """Parse `KEY=value` lines from the record monitor command."""
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in ("DET", "SIZE", "MB", "LOG"):
            value = value.strip()
            if re.fullmatch(r"[0-9]+", value):
                try:
                    values[key.strip()] = int(value)
                except ValueError:
                    pass
    if "DET" not in values:
        return None
    return {
        "det": values["DET"],
        "size": values.get("SIZE", 0),
        "mobile_running": values.get("MB") == 1 if "MB" in values else None,
        "log_kb": values.get("LOG"),
    }


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def parse_query_sessions(text: str) -> list[dict]:
    """Parse the TRACING SESSIONS table of `perfetto --query`."""
    text = _ANSI_RE.sub("", text)
    lines = text.splitlines()
    sessions = []
    start = None
    for index, line in enumerate(lines):
        if "TRACING SESSIONS" in line:
            start = index
            break
    if start is None:
        return sessions
    name_col = None
    for line in lines[start + 1:]:
        if not line.strip():
            if name_col is not None and sessions:
                break
            continue
        if name_col is None:
            if "NAME" in line:
                name_col = line.index("NAME")
            continue
        if line.lstrip().startswith("="):
            continue
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit():
            break
        name = line[name_col:].strip() if len(line) > name_col else ""
        sessions.append({"id": parts[0], "state": parts[2], "name": name})
    return sessions


def parse_flight_ps(text: str) -> tuple[int, str | None] | None:
    """Find (pid, base_path) of a flight perfetto process in `ps` output."""
    for line in text.splitlines():
        if FLIGHT_NAME not in line:
            continue
        parts = line.split()
        pid = None
        base = None
        for index, token in enumerate(parts):
            if pid is None and token.isdigit():
                pid = int(token)
            if token == "-o" and index + 1 < len(parts):
                base = parts[index + 1]
        if pid is not None:
            return pid, base
    return None


def filter_perfetto_stderr(stderr: str) -> str:
    lines = [l for l in stderr.splitlines()
             if l.strip() and "No PTY" not in l]
    return "\n".join(lines).strip()


def last_pid_line(stdout: str) -> int | None:
    pid = None
    for line in stdout.splitlines():
        if line.strip().isdigit():
            pid = int(line.strip())
    return pid


def newest_remote_trace(adb, serial: str, pattern: str) -> str:
    try:
        result = adb.shell(
            serial, f"ls -t {pattern} 2>/dev/null | head -1", timeout=10)
    except AdbError:
        return ""
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


# ------------------------------------------------------------------- base

class _Session:
    mode = ""

    def __init__(self, adb, serial: str, *, info, state_store,
                 output_dir, on_change=None, poll_interval=2.0,
                 retry_interval=5.0, progress_interval=1.0,
                 attach_timeout=120.0):
        self.adb = adb
        self.serial = serial
        self.info = info
        self.state_store = state_store
        self._output_dir = output_dir  # Path or callable -> Path
        self.on_change = on_change or (lambda: None)
        self.poll_interval = poll_interval
        self.retry_interval = retry_interval
        self.progress_interval = progress_interval
        self.attach_timeout = attach_timeout
        self.status = "starting"
        self.connected = True
        self.disconnects = []
        self._open_disconnect = None
        self.stop_requested = False
        self.abandoned = False
        self.started_at = time.time()
        self.stopped_at = None
        self.size_bytes = 0
        self.pull_progress = None
        self.preset_name = ""
        self.model = {}
        self._config_text = ""
        self.message = "Starting..."
        self.error = None
        self.outputs = []
        self.checklist = []
        self.snapshots = []
        self.snapshot_busy = False
        self.one_shot = False
        self.lifetime_ms = None
        self.duration_ms = None
        self.remote_path = ""
        self.pid = None
        self.mobilelog = None
        self._mobilelog_running = False
        self.sf = None
        self._want_sf = False
        self._sf_interval_ms = 200
        self.ended_by_duration = False
        self._thread = None
        self._resumed = False  # monitor thread started in this process

    # -- helpers ---------------------------------------------------------

    @property
    def output_dir(self) -> Path:
        out = (self._output_dir() if callable(self._output_dir)
               else Path(self._output_dir))
        out.mkdir(parents=True, exist_ok=True)
        return out

    @property
    def model_name(self) -> str:
        return getattr(self.info, "model", "") or "device"

    def _shell(self, command: str, timeout: float = 15, input_text=None):
        return self.adb.shell(self.serial, command, timeout=timeout,
                              input_text=input_text)

    def _mark_stopped(self):
        if self.stopped_at is not None:
            return
        now = time.time()
        if self.duration_ms:
            now = min(now, self.started_at + self.duration_ms / 1000)
        self.stopped_at = now

    def _set_status(self, status: str, message: str | None = None):
        self.status = status
        if status in ("starting", "recording"):
            self.stopped_at = None
        elif status in ("pulling", "done", "failed", "ended",
                        "abandoned"):
            self._mark_stopped()
        if message is not None:
            self.message = message
        log.info("[%s] %s -> %s", self.serial, self.mode, status)
        self._persist()
        self.on_change()

    def _set_message(self, message: str):
        self.message = message
        self.on_change()

    def _persist(self):
        if self.state_store is not None:
            try:
                self.state_store.put(self.serial, self.record())
            except OSError as exc:
                log.warning("Could not persist session state: %s", exc)

    def _mark_disconnected(self):
        if self._open_disconnect is None:
            self._open_disconnect = time.time()
            self.connected = False
            self._persist()
            self.on_change()

    def _mark_connected(self):
        if self._open_disconnect is None and self.connected:
            return
        if self._open_disconnect is not None:
            self.disconnects.append([self._open_disconnect, time.time()])
            self._open_disconnect = None
        self.connected = True
        self._persist()
        self.on_change()

    def _fail(self, exc):
        if self.abandoned:
            return
        self.error = str(exc)
        self._set_status("failed", f"Failed: {exc}")

    # -- state record / API ----------------------------------------------

    def record(self) -> dict:
        return {
            "mode": self.mode,
            "status": self.status,
            "started_at": datetime.fromtimestamp(
                self.started_at, tz=timezone.utc).isoformat(),
            "stopped_at": self.stopped_at,
            "size_bytes": self.size_bytes,
            "model": self.model,
            "info": (self.info.to_dict() if self.info is not None
                     else None),
            "preset_name": self.preset_name,
            "remote_path": self.remote_path,
            "remote_config": (CONFIG_RECORD if self.mode == "record"
                              else CONFIG_FLIGHT),
            "pid": self.pid,
            "snapshots": list(self.snapshots),
            "mobilelog": (self.mobilelog.state_dict()
                          if self.mobilelog else None),
            "sf": (self.sf.state_dict() if self.sf else (
                {"enabled": True, "interval_ms": self._sf_interval_ms,
                 "output_dir": "", "dump_count": 0}
                if self._want_sf else None)),
            "outputs": list(self.outputs),
            "duration_ms": self.duration_ms,
            "lifetime_ms": self.lifetime_ms,
            "one_shot": self.one_shot,
            "disconnects": list(self.disconnects),
            "error": self.error,
        }

    def api_dict(self) -> dict:
        disc = [list(d) for d in self.disconnects]
        if self._open_disconnect is not None:
            disc.append([self._open_disconnect, None])
        mobilelog = None
        if self.mobilelog:
            paused = (other_log_names(self.mobilelog.paused_mask)
                      if self.mobilelog.paused_mask else "")
            mobilelog = {
                "running": self._mobilelog_running,
                "log_bytes": getattr(self, "_log_bytes", 0),
                "paused": paused,
            }
        sf = self.sf.api_dict() if self.sf else None
        return {
            "mode": self.mode,
            "status": self.status,
            "connected": self.connected,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "elapsed_s": max(0, int((self.stopped_at or time.time())
                                   - self.started_at)),
            "size_bytes": self.size_bytes,
            "preset_name": self.preset_name,
            "duration_ms": self.duration_ms,
            "disconnects": disc,
            "pull_progress": self.pull_progress,
            "snapshots": [dict(s) for s in self.snapshots],
            "snapshot_busy": self.snapshot_busy,
            "one_shot": self.one_shot,
            "lifetime_ms": self.lifetime_ms,
            "remote_path": self.remote_path,
            "buffer_kb": (pbtx.total_buffer_kb(self._config_text)
                          if self._config_text else 0),
            "mobilelog": mobilelog,
            "sf": sf,
            "stop_requested": self.stop_requested,
            "ended_by_duration": self.ended_by_duration,
            "outputs": list(self.outputs),
            "checklist": list(self.checklist),
            "error": self.error,
            "message": self.message,
        }

    def _load_record(self, record: dict):
        self.status = record.get("status", "recording")
        try:
            self.started_at = datetime.fromisoformat(
                record["started_at"]).timestamp()
        except (KeyError, ValueError):
            self.started_at = time.time()
        self.stopped_at = record.get("stopped_at")
        self.model = record.get("model", {})
        info = record.get("info")
        if info and self.info is None:
            fields = {f.name for f in dataclasses.fields(DeviceInfo)}
            self.info = DeviceInfo(
                **{k: v for k, v in info.items() if k in fields})
        self.preset_name = record.get("preset_name", "")
        self.remote_path = record.get("remote_path", "")
        self.pid = record.get("pid")
        self.snapshots = list(record.get("snapshots", []))
        self.outputs = list(record.get("outputs", []))
        self.duration_ms = record.get("duration_ms")
        self.lifetime_ms = record.get("lifetime_ms")
        self.one_shot = record.get("one_shot", False)
        self.disconnects = list(record.get("disconnects", []))
        self.error = record.get("error")
        self.size_bytes = record.get("size_bytes", 0)
        if (self.stopped_at is None and
                self.status in ("done", "failed", "ended", "abandoned")):
            self._mark_stopped()
            for out in self.outputs:
                try:
                    st = Path(out).stat()
                except OSError:
                    continue
                self.stopped_at = max(self.started_at,
                                      min(self.stopped_at, st.st_mtime))
                self.size_bytes = self.size_bytes or st.st_size
                break
        ml = record.get("mobilelog")
        if ml and ml.get("enabled"):
            self.mobilelog = MobileLog.from_state(
                self.adb, self.serial, ml,
                on_message=self._set_message)
        sf = record.get("sf")
        if sf and sf.get("enabled"):
            self._want_sf = True
            self._sf_interval_ms = int(sf.get("interval_ms") or 200)
            if sf.get("output_dir"):
                self.sf = SfCapture.from_state(
                    self.adb, self.serial, sf,
                    on_message=self._set_message)

    # -- pull helper ------------------------------------------------------

    @staticmethod
    def _part_path(dest: Path) -> Path:
        """A unique '.part' temp name next to dest (renamed on success)."""
        fd, name = tempfile.mkstemp(dir=dest.parent,
                                    prefix=dest.name + ".",
                                    suffix=".part")
        os.close(fd)
        return Path(name)

    def _pull_with_retry(self, remote: str, local: Path,
                         remote_size: int = 0) -> bool:
        """Pull remote -> local, retrying every retry_interval on failure.

        Progress is sampled from the local file size while adb pull runs.
        Returns False when abandoned mid-pull.
        """
        while not self.abandoned:
            done = threading.Event()
            result_box = {}

            def work():
                try:
                    result_box["result"] = self.adb.pull(
                        self.serial, remote, local)
                except AdbError as exc:
                    result_box["error"] = exc
                finally:
                    done.set()

            threading.Thread(target=work, daemon=True).start()
            while not done.is_set():
                done.wait(self.progress_interval)
                if remote_size and local.exists() and local.is_file():
                    self.pull_progress = min(
                        1.0, local.stat().st_size / remote_size)
                    self.on_change()
            result = result_box.get("result")
            if (result is not None and result.returncode == 0
                    and local.exists()):
                self.pull_progress = None
                self._mark_connected()
                self.on_change()
                return True
            self._mark_disconnected()
            # Remove the partial download so a directory pull does not
            # nest <name>/<name> on the next attempt.
            if local.is_dir():
                shutil.rmtree(local, ignore_errors=True)
            else:
                local.unlink(missing_ok=True)
            self._set_message(
                f"Pull of {remote.rsplit('/', 1)[-1]} failed; retrying...")
            self._wait(self.retry_interval)
        return False

    def _wait(self, seconds: float):
        """Interruptible sleep: returns early when abandoned."""
        deadline = time.monotonic() + seconds
        while not self.abandoned and time.monotonic() < deadline:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


# ------------------------------------------------------------- record mode

class RecordSession(_Session):
    mode = "record"

    def start(self, config_text: str, preset_name: str,
              duration_ms: int | None, mobilelog: bool = False,
              sf: bool = False, sf_interval_ms: int = 200):
        self.preset_name = preset_name
        self.duration_ms = duration_ms
        self._config_text = config_text
        self._want_mobilelog = mobilelog
        self._want_sf = bool(sf)
        self._sf_interval_ms = max(50, int(sf_interval_ms or 200))
        # Preflight: refuse when a detached session is already running.
        result = self._shell(f"perfetto --is_detached={DETACH_KEY}",
                             timeout=10)
        if result.returncode == 0:
            raise SessionError(
                "A recording is already running on this device")
        self.started_at = time.time()
        # Persist before spawning the worker: a trailing persist could
        # otherwise overwrite a newer record with a stale snapshot.
        self._persist()
        self._thread = threading.Thread(target=self._run_new, daemon=True)
        self._thread.start()
        self._resumed = True

    def resume(self):
        """Restart the worker thread at the phase matching our status."""
        if self.status in ("done", "failed", "ended", "abandoned"):
            return
        if self.status == "pulling":
            target = self._run_pulling
        else:
            if self.status == "stopping":
                self.stop_requested = True
            if not self.remote_path:
                self.remote_path = newest_remote_trace(
                    self.adb, self.serial,
                    f"{REMOTE_TRACE_DIR}/adbperfetto_*.pftrace")
                if not self.remote_path:
                    self._fail(SessionError(
                        "Could not find the remote trace file"))
                    return
            if self.status == "starting":
                self.status = "recording"
            self._ensure_sf_running()
            target = self._run_resumed
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()
        self._resumed = True

    def adopt(self, remote_path: str, started_at: float):
        """Attach to an orphan detached session found on the device."""
        self.remote_path = remote_path
        self.started_at = started_at
        self._set_status("recording", "Adopted the running recording")
        self._thread = threading.Thread(target=self._run_resumed,
                                        daemon=True)
        self._thread.start()
        self._resumed = True

    def stop(self):
        self.stop_requested = True
        if self.status == "recording":
            self._set_status("stopping", "Stopping the trace...")

    def abandon(self):
        """Stop monitoring; leave the device session and file alone."""
        self.abandoned = True
        self.status = "abandoned"
        self._mark_stopped()
        self.message = "Left running on the device"
        self._stop_sf()

    # -- worker -----------------------------------------------------------

    def _sf_staging_dir(self) -> Path:
        ts = datetime.fromtimestamp(self.started_at)
        return (self.output_dir /
                f".sf_staging_{outputs.safe_filename(self.serial)}_"
                f"{ts:%Y%m%d_%H%M%S}")

    def _ensure_sf_running(self):
        if not self._want_sf:
            return
        if self.sf is None:
            self.sf = SfCapture(
                self.adb, self.serial, self._sf_staging_dir(),
                interval_ms=self._sf_interval_ms,
                on_message=self._set_message)
        if not self.sf.running:
            self.sf.start()
            self._persist()
            self.on_change()

    def _stop_sf(self):
        if self.sf is None:
            return
        try:
            self.sf.stop()
        except Exception as exc:
            self._set_message(f"SF capture stop failed: {exc}")

    def _run_new(self):
        try:
            text = pbtx.to_record(self._config_text, self.duration_ms)
            if self._want_mobilelog:
                self._set_status("starting", "Preparing MobileLog...")
                self.mobilelog = MobileLog(
                    self.adb, self.serial, on_message=self._set_message)
                self.mobilelog.prepare()
                self._mobilelog_running = True
            self._set_status("starting", "Starting the trace...")
            pushed = self._shell(f"cat > {CONFIG_RECORD}", input_text=text,
                                 timeout=15)
            if pushed.returncode != 0:
                raise SessionError(
                    "Could not write the config on the device: "
                    + (pushed.stderr or pushed.stdout).strip())
            self.started_at = time.time()
            ts = datetime.fromtimestamp(self.started_at)
            self.remote_path = (f"{REMOTE_TRACE_DIR}/adbperfetto_"
                                f"{ts:%Y%m%d_%H%M%S}.pftrace")
            launched = self._shell(
                f"perfetto --txt -c {CONFIG_RECORD} --detach={DETACH_KEY} "
                f"-o {shlex.quote(self.remote_path)}", timeout=30)
            alive = (launched.returncode == 0
                     and self._detached() == 0)
            self._shell(f"rm -f {CONFIG_RECORD}", timeout=10)
            if not alive:
                raise SessionError(
                    "Could not start the Perfetto trace: "
                    + (filter_perfetto_stderr(launched.stderr)
                       or launched.stdout.strip() or "unknown error"))
            self._ensure_sf_running()
            self._set_status("recording", "Recording")
            self._monitor_and_finish()
        except Exception as exc:
            message = str(exc)
            self._stop_sf()
            if self.mobilelog is not None and (
                    self.mobilelog.started
                    or self.mobilelog.paused_mask):
                try:
                    self.mobilelog.stop()
                except Exception as ml_exc:
                    message += ("; MobileLog cleanup also failed: "
                                f"{ml_exc}")
            self._fail(SessionError(message))

    def _run_resumed(self):
        try:
            self._monitor_and_finish()
        except Exception as exc:
            self._fail(exc)

    def _run_pulling(self):
        try:
            self._finish()
        except Exception as exc:
            self._fail(exc)

    def _detached(self) -> int:
        result = self._shell(f"perfetto --is_detached={DETACH_KEY}",
                             timeout=10)
        return result.returncode

    def _monitor_command(self) -> str:
        command = (
            f"perfetto --is_detached={DETACH_KEY} >/dev/null 2>&1; "
            f"echo DET=$?; "
            f"echo SIZE=$(stat -c %s {shlex.quote(self.remote_path)} "
            "2>/dev/null)")
        if self.mobilelog:
            command += "; " + self.mobilelog.monitor_fragment()
        return command

    def _monitor_and_finish(self):
        last_attach = 0.0
        while not self.abandoned:
            status = self._poll_status()
            if status is not None:
                self.size_bytes = status["size"]
                if status["log_kb"] is not None:
                    self._log_bytes = status["log_kb"] * 1024
                if status["mobile_running"] is not None:
                    self._mobilelog_running = status["mobile_running"]
                if status["det"] == 2:
                    self.ended_by_duration = not self.stop_requested
                    if self.ended_by_duration:
                        self._set_message(
                            "The trace reached its duration limit")
                    break
                if self.stop_requested:
                    if self.status != "stopping":
                        self._set_status("stopping",
                                         "Stopping the trace...")
                    now = time.monotonic()
                    if now - last_attach >= self.retry_interval:
                        last_attach = now
                        try:
                            self._shell(
                                f"perfetto --attach={DETACH_KEY} --stop",
                                timeout=self.attach_timeout)
                        except AdbError:
                            self._mark_disconnected()
                elif self.status != "recording":
                    self._set_status("recording", "Recording")
            elif not self.connected:
                self._set_message("Waiting for the device to reconnect...")
            self._wait(self.poll_interval)
        if not self.abandoned:
            self._finish()

    def _poll_status(self) -> dict | None:
        try:
            result = self._shell(self._monitor_command(),
                                 timeout=max(10, self.poll_interval * 3))
        except AdbError:
            self._mark_disconnected()
            return None
        if result.returncode != 0:
            self._mark_disconnected()
            return None
        status = parse_record_status(result.stdout)
        if status is None or status["det"] not in (0, 2):
            self._mark_disconnected()
            return None
        self._mark_connected()
        return status

    def _finish(self):
        self._set_status("pulling", "Pulling the trace...")
        self._stop_sf()
        if self.mobilelog:
            try:
                self.mobilelog.stop()
            except Exception as exc:
                self._set_message(f"MobileLog stop failed: {exc}")
        ts = datetime.fromtimestamp(self.started_at)
        timestamp = f"{ts:%Y%m%d_%H%M%S}"
        out = self.output_dir
        if not self.mobilelog and not self.sf:
            dest = outputs.unique_path(
                out / outputs.record_name(self.model_name, timestamp))
            part = self._part_path(dest)
            if not self._pull_with_retry(self.remote_path, part,
                                         self.size_bytes):
                part.unlink(missing_ok=True)
                return
            part.replace(dest)
            self._rm_remote()
            size = dest.stat().st_size
            self.checklist = [{"label": "Perfetto trace",
                               "ok": size > 0, "size": size}]
            self.outputs = [str(dest)]
        else:
            if not self._finish_bundle(out, ts, timestamp):
                return
        self._set_status("done", "Done")

    def _rm_remote(self):
        try:
            self._shell(f"rm -f {shlex.quote(self.remote_path)}", timeout=10)
        except AdbError:
            pass

    def _finish_bundle(self, out: Path, ts: datetime,
                       timestamp: str) -> bool:
        model = outputs.safe_filename(self.model_name)
        build = outputs.safe_filename(getattr(self.info, "build_id", ""))
        root = outputs.unique_path(out / f"{model}_{timestamp}")
        (root / "trace").mkdir(parents=True)
        if self.mobilelog:
            (root / "mobilelog").mkdir()
        trace_dest = root / "trace" / f"{model}_{timestamp}.pftrace"
        if not self._pull_with_retry(self.remote_path, trace_dest,
                                     self.size_bytes):
            return False
        self._rm_remote()
        if self.mobilelog and self.mobilelog.log_path:
            log_dir = root / "mobilelog" / \
                self.mobilelog.log_path.rsplit("/", 1)[-1]
            if not self._pull_with_retry(self.mobilelog.log_path, log_dir):
                return False
        if self.sf is not None:
            sf_dest = root / "sf"
            staging = Path(self.sf.output_dir)
            if staging.exists():
                shutil.copytree(staging, sf_dest, dirs_exist_ok=True)
                shutil.rmtree(staging, ignore_errors=True)
            else:
                sf_dest.mkdir(parents=True, exist_ok=True)
        stopped = datetime.now()
        device_time = ""
        props_text = ""
        try:
            device_time = self._shell("date", timeout=5).stdout.strip()
            props = self._shell("getprop", timeout=30)
            if props.returncode == 0:
                props_text = props.stdout
        except AdbError:
            pass
        log_path = self.mobilelog.log_path if self.mobilelog else ""
        original_mask = (self.mobilelog.original_mask
                         if self.mobilelog else None)
        paused_names = (other_log_names(self.mobilelog.original_mask or 0)
                        if self.mobilelog else "")
        logs_restored = (self.mobilelog.logs_restored
                         if self.mobilelog else False)
        extra = ""
        if self.sf is not None:
            extra = (
                f"SF capture: yes\n"
                f"SF interval ms: {self.sf.interval_ms}\n"
                f"SF dump count: {self.sf.dump_count}\n"
            )
        info_text = outputs.device_info_text(
            self.info, self.serial, mode="record",
            preset_name=self.preset_name,
            started_iso=ts.isoformat(timespec="seconds"),
            stopped_iso=stopped.isoformat(timespec="seconds"),
            duration_s=time.time() - self.started_at,
            device_time=device_time,
            log_path=log_path,
            original_mask=original_mask,
            paused_names=paused_names,
            logs_restored=logs_restored,
            ended_early=self.ended_by_duration,
            extra=extra)
        (root / "device_info.txt").write_text(info_text, encoding="utf-8")
        if props_text:
            (root / "getprop.txt").write_text(props_text, encoding="utf-8")
        checks = outputs.check_integrity(root)
        archive = outputs.unique_path(
            out / outputs.zip_name(model, build, timestamp))
        outputs.write_zip(root, archive)
        shutil.rmtree(root, ignore_errors=True)
        self.checklist = outputs.checklist_items(checks)
        self.outputs = [str(archive)]
        return True


# ------------------------------------------------------------- flight mode

class FlightSession(_Session):
    mode = "flight"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._snap_queue = queue.Queue()
        self._snap_index = 0
        self.clone = True
        self._one_shot_fired = False
        self._stop_after_snapshot = False

    # -- lifecycle ---------------------------------------------------------

    def start(self, config_text: str, preset_name: str, lifetime_ms: int):
        self.preset_name = preset_name
        self.lifetime_ms = lifetime_ms
        self._config_text = config_text
        self.clone = getattr(self.info, "supports_clone", True)
        self.one_shot = not self.clone
        # Preflight: refuse when our named session already runs.
        result = self._shell("perfetto --query", timeout=15)
        if result.returncode == 0:
            for sess in parse_query_sessions(result.stdout):
                if sess["name"] == FLIGHT_NAME and sess["state"] == "STARTED":
                    raise SessionError(
                        "A flight recorder session is already running "
                        "on this device")
        self.started_at = time.time()
        self._snap_index = len(self.snapshots)
        self._persist()
        self._spawn_threads(self._run_new)

    def resume(self):
        if self.status in TERMINAL:
            return
        self.clone = not self.one_shot
        self._snap_index = len(self.snapshots)
        if self.status == "stopping":
            self.stop_requested = True
        if not self.pid:
            found = self._find_pid()
            if found:
                self.pid, base = found
                if base:
                    self.remote_path = base
            else:
                self.status = "ended"
                self._mark_stopped()
                return
        if self.status == "starting":
            self.status = "recording"
        self._spawn_threads(self._run_resumed)

    def adopt(self, pid: int, base_path: str | None):
        self.pid = pid
        if base_path:
            self.remote_path = base_path
        else:
            ts = datetime.fromtimestamp(self.started_at)
            self.remote_path = (f"{REMOTE_TRACE_DIR}/adbperfetto_flight_"
                                f"{ts:%Y%m%d_%H%M%S}.pftrace")
        self.clone = getattr(self.info, "supports_clone", True)
        self.one_shot = not self.clone
        self._set_status("recording", "Adopted the running flight session")
        self._spawn_threads(self._run_resumed)

    def _spawn_threads(self, target):
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()
        self._snap_thread = threading.Thread(target=self._snap_worker,
                                             daemon=True)
        self._snap_thread.start()
        self._resumed = True

    def _find_pid(self):
        try:
            result = self._shell("ps -A -o PID,ARGS", timeout=15)
            if result.returncode == 0:
                return parse_flight_ps(result.stdout)
        except AdbError:
            pass
        return None

    # -- user actions --------------------------------------------------------

    def request_snapshot(self):
        if self.status not in ("recording", "stopping"):
            raise SessionError(
                f"Cannot take a snapshot while the session is "
                f"'{self.status}'")
        if not self.connected:
            raise SessionError("The device is disconnected")
        self._snap_queue.put("snap")

    def stop(self, save_snapshot: bool = False):
        """Non-blocking: a requested final snapshot is queued; the
        snapshot worker sets stop_requested when it finishes."""
        if save_snapshot and self.status == "recording":
            self._stop_after_snapshot = True
            self._snap_queue.put("snap")
            self._set_status("stopping", "Saving a final snapshot...")
            return
        self.stop_requested = True
        if self.status in ("recording", "stopping"):
            self._set_status("stopping", "Stopping the flight session...")

    def abandon(self):
        self.abandoned = True
        self.status = "abandoned"
        self._mark_stopped()
        self.message = "Left running on the device"
        self._snap_queue.put(None)

    # -- worker ---------------------------------------------------------------

    def _run_new(self):
        try:
            text = pbtx.to_flight(
                self._config_text, session_name=FLIGHT_NAME,
                trigger_name=FLIGHT_TRIGGER, lifetime_ms=self.lifetime_ms,
                clone=self.clone)
            pushed = self._shell(f"cat > {CONFIG_FLIGHT}",
                                 input_text=text, timeout=15)
            if pushed.returncode != 0:
                raise SessionError(
                    "Could not write the config on the device: "
                    + (pushed.stderr or pushed.stdout).strip())
            self.started_at = time.time()
            ts = datetime.fromtimestamp(self.started_at)
            self.remote_path = (f"{REMOTE_TRACE_DIR}/adbperfetto_flight_"
                                f"{ts:%Y%m%d_%H%M%S}.pftrace")
            launched = self._shell(
                f"perfetto --txt -c {CONFIG_FLIGHT} "
                f"-o {shlex.quote(self.remote_path)} --background-wait",
                timeout=30)
            pid = last_pid_line(launched.stdout)
            if launched.returncode != 0 or pid is None:
                self._cleanup_remote()
                raise SessionError(
                    "Could not start the flight session: "
                    + (filter_perfetto_stderr(launched.stderr)
                       or launched.stdout.strip() or "unknown error"))
            self.pid = pid
            self._set_status("recording", "Flight recorder running")
            self._monitor_and_finish()
        except Exception as exc:
            self._fail(exc)

    def _run_resumed(self):
        try:
            self._monitor_and_finish()
        except Exception as exc:
            self._fail(exc)

    def _alive(self) -> bool | None:
        """True = alive, False = gone, None = could not check.

        Greps /proc/<pid>/cmdline rather than `kill -0` or a plain
        `test -d`: on the KN8 `kill -0` fails with EPERM for the
        perfetto process (SELinux domain), and matching the cmdline
        protects against pid reuse after a phone reboot.
        """
        try:
            result = self._shell(
                f"grep -q {FLIGHT_NAME} /proc/{self.pid}/cmdline "
                f"2>/dev/null; echo ALIVE=$?",
                timeout=10)
        except AdbError:
            return None
        if result.returncode != 0 or not self.pid:
            return None
        match = re.search(r"ALIVE=(\d+)", result.stdout)
        if not match:
            return None
        return match.group(1) == "0"

    def _monitor_and_finish(self):
        last_kill = 0.0
        try:
            while not self.abandoned:
                alive = self._alive()
                if alive is None:
                    self._mark_disconnected()
                    self._set_message(
                        "Waiting for the device to reconnect...")
                else:
                    self._mark_connected()
                    if not alive:
                        if self.status in TERMINAL:
                            return
                        if self.stop_requested:
                            break
                        if self._one_shot_fired and self.snapshot_busy:
                            pass  # snapshot worker owns the ending
                        else:
                            self._set_status(
                                "ended",
                                "The flight session ended (lifetime "
                                "reached or stopped externally)")
                            self._pull_stray_snapshots()
                            return
                    elif self.stop_requested:
                        if self.status != "stopping":
                            self._set_status(
                                "stopping", "Stopping the flight session...")
                        now = time.monotonic()
                        if now - last_kill >= self.retry_interval:
                            last_kill = now
                            try:
                                self._shell(
                                    f"kill -TERM {self.pid} 2>/dev/null",
                                    timeout=10)
                            except AdbError:
                                self._mark_disconnected()
                self._wait(self.poll_interval)
            if self.abandoned:
                return
            self._kill_and_cleanup()
        finally:
            self._snap_queue.put(None)

    def _kill_and_cleanup(self):
        if self.pid:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    self._shell(f"kill -TERM {self.pid} 2>/dev/null",
                                timeout=10)
                except AdbError:
                    pass
                alive = self._alive()
                if alive is False or alive is None:
                    break
                self._wait(1)
        self._pull_stray_snapshots()
        self._cleanup_remote()
        self._set_status("done", "Done")

    def _cleanup_remote(self):
        try:
            self._shell(
                f"rm -f {shlex.quote(self.remote_path)}* {CONFIG_FLIGHT}",
                timeout=10)
        except AdbError:
            pass

    def _list_remote_snapshots(self) -> list[str]:
        try:
            result = self._shell(
                f"ls -1 {shlex.quote(self.remote_path)}.[0-9]* 2>/dev/null",
                timeout=10)
        except AdbError:
            return []
        if result.returncode != 0:
            return []
        return sorted(l.strip() for l in result.stdout.splitlines()
                      if l.strip())

    def _pull_stray_snapshots(self):
        """Pull numbered <base>.N snapshot files left on the device."""
        known = {s.get("remote") for s in self.snapshots}
        for remote in self._list_remote_snapshots():
            if remote not in known:
                self._do_snapshot_pull(remote)

    def _snap_worker(self):
        while True:
            item = self._snap_queue.get()
            if item is None or self.abandoned:
                return
            self.snapshot_busy = True
            self.on_change()
            try:
                if self.clone:
                    self._do_clone_snapshot()
                else:
                    self._do_one_shot()
            except Exception as exc:
                self._set_message(f"Snapshot failed: {exc}")
            finally:
                self.snapshot_busy = False
                if self._stop_after_snapshot:
                    self._stop_after_snapshot = False
                    self.stop_requested = True
                self._persist()
                self.on_change()

    def _snapshot_local_name(self, index: int, when: datetime) -> str:
        model = outputs.safe_filename(self.model_name)
        session_ts = datetime.fromtimestamp(self.started_at)
        return (f"{model}_{session_ts:%Y%m%d_%H%M%S}_snap"
                f"{index:02d}_{when:%H%M%S}.pftrace")

    def _do_snapshot_pull(self, remote: str):
        index = self._snap_index
        self._snap_index += 1
        when = datetime.now()
        local = outputs.unique_path(
            self.output_dir / self._snapshot_local_name(index, when))
        part = self._part_path(local)
        size = 0
        try:
            result = self._shell(f"stat -c %s {shlex.quote(remote)}",
                                 timeout=10)
            if result.returncode == 0 and result.stdout.strip().isdigit():
                size = int(result.stdout.strip())
        except AdbError:
            pass
        if not self._pull_with_retry(remote, part, size):
            part.unlink(missing_ok=True)
            return
        part.replace(local)
        try:
            self._shell(f"rm -f {shlex.quote(remote)}", timeout=10)
        except AdbError:
            pass
        entry = {"index": index, "remote": remote, "local": str(local),
                 "size": local.stat().st_size, "at": time.time()}
        self.snapshots.append(entry)
        self._set_message(f"Snapshot {index} saved to {local.name}")
        self._persist()
        self.on_change()

    def _do_clone_snapshot(self):
        index = self._snap_index
        remote = (f"{REMOTE_TRACE_DIR}/adbperfetto_snap_{int(time.time())}_"
                  f"{index:02d}.pftrace")
        result = self._shell(
            f"perfetto --clone-by-name {FLIGHT_NAME} "
            f"-o {shlex.quote(remote)}", timeout=180)
        if result.returncode != 0:
            raise SessionError(
                "Could not clone the flight session: "
                + (filter_perfetto_stderr(result.stderr)
                   or result.stdout.strip() or "unknown error"))
        self._do_snapshot_pull(remote)

    def _do_one_shot(self):
        """STOP_TRACING mode: the trigger dumps the ring buffer once."""
        self._one_shot_fired = True
        result = self._shell(f"trigger_perfetto {FLIGHT_TRIGGER}",
                             timeout=30)
        if result.returncode != 0:
            self._one_shot_fired = False
            raise SessionError(
                "Could not trigger the flight session: "
                + (result.stderr or result.stdout).strip())
        self._set_message("Trigger sent; waiting for the trace to flush...")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            alive = self._alive()
            if alive is False:
                break
            if alive is None:
                self._mark_disconnected()
            self._wait(1)
        size = 0
        try:
            stat = self._shell(
                f"stat -c %s {shlex.quote(self.remote_path)}", timeout=10)
            if stat.returncode == 0 and stat.stdout.strip().isdigit():
                size = int(stat.stdout.strip())
        except AdbError:
            pass
        if size <= 0:
            raise SessionError("The triggered snapshot is empty")
        index = self._snap_index
        self._snap_index += 1
        when = datetime.now()
        local = outputs.unique_path(
            self.output_dir / self._snapshot_local_name(index, when))
        part = self._part_path(local)
        if not self._pull_with_retry(self.remote_path, part, size):
            part.unlink(missing_ok=True)
            return
        part.replace(local)
        self.snapshots.append({
            "index": index, "remote": self.remote_path,
            "local": str(local), "size": local.stat().st_size,
            "at": time.time()})
        self.outputs = [str(local)]
        self._cleanup_remote()
        self._set_status("done", "Done")


# ---------------------------------------------------- manager and monitor

class DeviceMonitor:
    """Watches `adb devices`, probes info, and runs recovery per serial."""

    def __init__(self, manager):
        self.manager = manager

    def tick(self):
        m = self.manager
        if m.adb is None:
            m.adb_ok = False
            m.adb_error = ("ADB was not found in PATH. Install Android "
                           "platform-tools and add it to PATH.")
            m._bump()
            return
        try:
            if hasattr(m.adb, "devices_with_hints"):
                raw = m.adb.devices_with_hints()
            else:
                raw = m.adb.devices()
            devices = [(item[0], item[1], item[2] if len(item) > 2 else "")
                       for item in raw]
            m.adb_ok = True
            m.adb_error = None
        except AdbError as exc:
            m.adb_ok = False
            m.adb_error = str(exc)
            devices = []
        seen = {serial for serial, _state, _hint in devices}
        for serial, state, hint in devices:
            entry = m.devices.setdefault(serial, {
                "serial": serial, "state": state, "info": None,
                "last_seen": time.time()})
            entry["state"] = state
            entry["last_seen"] = time.time()
            if entry["info"] is None and hint:
                entry["info"] = DeviceInfo(serial=serial, model=hint)
        for serial in list(m.devices):
            if serial not in seen:
                # Keep the entry (marked disconnected) while a session
                # or orphan still belongs to it, so it stays in the UI.
                if serial in m.sessions or serial in m.orphans:
                    m.devices[serial]["state"] = "disconnected"
                else:
                    del m.devices[serial]
                m._probed.discard(serial)
                m._recovered.discard(serial)
        for serial, state, _hint in devices:
            if state != "device":
                continue
            if serial not in m._probed:
                try:
                    info = probe(m.adb, serial)
                    prev = m.devices[serial]["info"]
                    if not info.model and prev and prev.model:
                        info.model = prev.model
                    m.devices[serial]["info"] = info
                    session = m.sessions.get(serial)
                    if session is not None:
                        session.info = info
                    m._probed.add(serial)
                except Exception as exc:
                    log.warning("Could not probe %s: %s", serial, exc)
            if serial not in m._recovered:
                m._recovered.add(serial)
                try:
                    m._recover(serial)
                except Exception as exc:
                    log.warning("Recovery check failed for %s: %s",
                                serial, exc)
        m._bump()


class SessionManager:
    def __init__(self, adb, state_store, settings, output_dir,
                 poll_interval=2.0, flight_poll_interval=3.0,
                 monitor_interval=2.0, retry_interval=5.0,
                 progress_interval=1.0, autostart=True):
        self.adb = adb
        self.state_store = state_store
        self.settings = settings
        self._output_dir = output_dir  # Path or callable
        self.poll_interval = poll_interval
        self.flight_poll_interval = flight_poll_interval
        self.monitor_interval = monitor_interval
        self.retry_interval = retry_interval
        self.progress_interval = progress_interval
        self.devices = {}
        self.sessions = {}
        self.orphans = {}
        self.adb_ok = adb is not None
        self.adb_error = None
        self._probed = set()
        self._recovered = set()
        self._pending = (state_store.all() if state_store else {})
        self._lock = threading.Lock()
        self._changed = threading.Event()
        self._version = 0
        self._stop_event = threading.Event()
        self._monitor_thread = None
        self._load_persisted_sessions()
        self.monitor = DeviceMonitor(self)
        if autostart:
            self.start_monitor()

    # -- infrastructure -----------------------------------------------------

    @property
    def version(self):
        return self._version

    def _bump(self):
        self._version += 1
        self._changed.set()

    def wait_change(self, timeout: float) -> bool:
        self._changed.wait(timeout)
        self._changed.clear()
        return True

    def start_monitor(self):
        def loop():
            while not self._stop_event.is_set():
                try:
                    self.monitor.tick()
                except Exception as exc:
                    log.warning("Device monitor tick failed: %s", exc)
                self._stop_event.wait(self.monitor_interval)
        self._monitor_thread = threading.Thread(target=loop, daemon=True)
        self._monitor_thread.start()

    def shutdown(self):
        self._stop_event.set()
        # Stop worker threads without touching session state; running
        # sessions persist as-is and resume on the next launch.
        for session in list(self.sessions.values()):
            session.abandoned = True

    def _output_path(self) -> Path:
        out = (self._output_dir() if callable(self._output_dir)
               else Path(self._output_dir))
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _session_kwargs(self, flight: bool = False):
        return dict(
            state_store=self.state_store,
            output_dir=self._output_dir,
            on_change=self._bump,
            poll_interval=(self.flight_poll_interval if flight
                           else self.poll_interval),
            retry_interval=self.retry_interval,
            progress_interval=self.progress_interval,
        )

    def _info(self, serial: str):
        entry = self.devices.get(serial)
        return entry.get("info") if entry else None

    def _load_persisted_sessions(self):
        """Rebuild session objects from state records (no threads).

        Loaded sessions show as disconnected and resume when the
        device returns to `device` state.
        """
        for serial, record in list(self._pending.items()):
            try:
                flight = record.get("mode") == "flight"
                cls = FlightSession if flight else RecordSession
                session = cls(self.adb, serial, info=None,
                              **self._session_kwargs(flight=flight))
                session._load_record(record)
            except Exception as exc:
                log.warning("Could not load session for %s: %s",
                            serial, exc)
                continue
            session.connected = False
            self.sessions[serial] = session
            self.devices.setdefault(serial, {
                "serial": serial, "state": "disconnected",
                "info": session.info, "last_seen": 0})
            self._pending.pop(serial, None)

    # -- recovery ------------------------------------------------------------

    def _recover(self, serial: str):
        entry = self.devices[serial]
        with self._lock:
            session = self.sessions.get(serial)
            if session is not None:
                if not session._resumed:
                    session._resumed = True
                    if entry.get("info") is not None:
                        session.info = entry["info"]
                    session.resume()
                    log.info(
                        "[%s] Resumed %s session in state %s",
                        serial, session.mode, session.status)
                return
            record = self._pending.pop(serial, None)
            if record is None and self.state_store:
                record = self.state_store.get(serial)
            if record is not None:
                self._resume_from_record(serial, record, entry)
                return
            self._detect_orphan(serial, entry)

    def _resume_from_record(self, serial: str, record: dict, entry: dict):
        if record.get("mode") == "flight":
            session = FlightSession(
                self.adb, serial, info=entry.get("info"),
                **self._session_kwargs(flight=True))
        else:
            session = RecordSession(
                self.adb, serial, info=entry.get("info"),
                **self._session_kwargs())
        session._load_record(record)
        self.sessions[serial] = session
        session._resumed = True
        session.resume()
        log.info("[%s] Resumed %s session in state %s",
                 serial, session.mode, session.status)

    def _detect_orphan(self, serial: str, entry: dict):
        try:
            result = self.adb.shell(
                serial, f"perfetto --is_detached={DETACH_KEY}", timeout=10)
        except AdbError:
            return
        if result.returncode == 0:
            remote = newest_remote_trace(
                self.adb, serial,
                f"{REMOTE_TRACE_DIR}/adbperfetto_*.pftrace")
            self.orphans[serial] = {"mode": "record", "remote": remote}
            return
        try:
            query = self.adb.shell(serial, "perfetto --query", timeout=15)
        except AdbError:
            return
        if query.returncode == 0:
            for sess in parse_query_sessions(query.stdout):
                if (sess["name"] == FLIGHT_NAME
                        and sess["state"] == "STARTED"):
                    self.orphans[serial] = {"mode": "flight",
                                            "name": FLIGHT_NAME}
                    return

    # -- API operations -------------------------------------------------------

    def _require_device(self, serial: str):
        if serial not in self.devices:
            raise KeyError(f"Unknown device: {serial}")

    def _active_session(self, serial: str):
        session = self.sessions.get(serial)
        if session and session.status not in TERMINAL:
            return session
        return None

    def start_record(self, serial: str, config_text: str, preset_name: str,
                     duration_ms: int | None, mobilelog: bool = False,
                     sf: bool = False, sf_interval_ms: int = 200):
        self._require_device(serial)
        with self._lock:
            if self._active_session(serial):
                raise SessionError(
                    "A session is already tracked for this device")
            session = RecordSession(
                self.adb, serial, info=self._info(serial),
                **self._session_kwargs())
            session.start(config_text, preset_name, duration_ms, mobilelog,
                          sf=sf, sf_interval_ms=sf_interval_ms)
            self.sessions[serial] = session
            self.orphans.pop(serial, None)
        self._bump()
        return session

    def start_flight(self, serial: str, config_text: str, preset_name: str,
                     lifetime_ms: int):
        self._require_device(serial)
        with self._lock:
            if self._active_session(serial):
                raise SessionError(
                    "A session is already tracked for this device")
            session = FlightSession(
                self.adb, serial, info=self._info(serial),
                **self._session_kwargs(flight=True))
            session.start(config_text, preset_name, lifetime_ms)
            self.sessions[serial] = session
            self.orphans.pop(serial, None)
        self._bump()
        return session

    def stop(self, serial: str, save_snapshot: bool = False):
        session = self._require_session(serial)
        if isinstance(session, FlightSession):
            session.stop(save_snapshot)
        else:
            session.stop()
        self._bump()

    def request_snapshot(self, serial: str):
        session = self._require_session(serial)
        if not isinstance(session, FlightSession):
            raise SessionError(
                "Snapshots are only available in flight mode")
        session.request_snapshot()
        self._bump()

    def adopt(self, serial: str):
        self._require_device(serial)
        orphan = self.orphans.get(serial)
        if not orphan:
            raise SessionError("Nothing to adopt on this device")
        with self._lock:
            if orphan["mode"] == "record":
                session = RecordSession(
                    self.adb, serial, info=self._info(serial),
                    **self._session_kwargs())
                session.preset_name = "adopted"
                remote = orphan.get("remote") or ""
                started_at = time.time()
                if remote:
                    try:
                        stat = self.adb.shell(
                            serial, f"stat -c %Y {shlex.quote(remote)}",
                            timeout=10)
                        if (stat.returncode == 0
                                and stat.stdout.strip().isdigit()):
                            started_at = float(stat.stdout.strip())
                    except AdbError:
                        pass
                if not remote:
                    ts = datetime.fromtimestamp(started_at)
                    remote = (f"{REMOTE_TRACE_DIR}/adbperfetto_"
                              f"{ts:%Y%m%d_%H%M%S}.pftrace")
                session.adopt(remote, started_at)
            else:
                found = None
                try:
                    ps = self.adb.shell(serial, "ps -A -o PID,ARGS",
                                        timeout=15)
                    if ps.returncode == 0:
                        found = parse_flight_ps(ps.stdout)
                except AdbError:
                    pass
                if not found:
                    raise SessionError(
                        "Could not find the flight session process")
                pid, base = found
                session = FlightSession(
                    self.adb, serial, info=self._info(serial),
                    **self._session_kwargs(flight=True))
                session.preset_name = "adopted"
                session.adopt(pid, base)
            self.sessions[serial] = session
            self.orphans.pop(serial, None)
        self._bump()
        return session

    def dismiss(self, serial: str):
        with self._lock:
            session = self.sessions.get(serial)
            if session:
                if session.status not in TERMINAL:
                    raise SessionError(
                        "The session is still running; stop it first")
                del self.sessions[serial]
                if self.state_store:
                    self.state_store.delete(serial)
            elif serial in self.orphans:
                del self.orphans[serial]
            else:
                raise SessionError("Nothing to dismiss")
        self._bump()

    def abandon(self, serial: str):
        with self._lock:
            session = self.sessions.get(serial)
            if not session:
                if serial in self.orphans:
                    del self.orphans[serial]
                    self._bump()
                    return
                raise SessionError("No session to abandon")
            session.abandon()
            del self.sessions[serial]
            if self.state_store:
                self.state_store.delete(serial)
        self._bump()

    def _require_session(self, serial: str):
        session = self.sessions.get(serial)
        if not session:
            self._require_device(serial)
            raise SessionError("No session on this device")
        return session

    # -- snapshot for the API --------------------------------------------------

    def snapshot(self) -> dict:
        devices = []
        for serial, entry in list(self.devices.items()):
            session = self.sessions.get(serial)
            info = entry.get("info")
            devices.append({
                "serial": serial,
                "state": entry["state"],
                "wifi": is_wifi_serial(serial),
                "info": info.to_dict() if info else None,
                "orphan": self.orphans.get(serial),
                "session": session.api_dict() if session else None,
            })
        for serial, session in self.sessions.items():
            if serial in self.devices:
                continue
            info = session.info
            devices.append({
                "serial": serial,
                "state": "disconnected",
                "wifi": is_wifi_serial(serial),
                "info": info.to_dict() if info else None,
                "orphan": self.orphans.get(serial),
                "session": session.api_dict(),
            })
        return {
            "version": VERSION,
            "build": BUILD,
            "output_dir": str(self._output_path()),
            "adb_ok": self.adb_ok,
            "adb_error": self.adb_error,
            "devices": devices,
        }
