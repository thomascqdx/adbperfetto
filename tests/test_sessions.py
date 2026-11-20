import os
import re
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from adbperfetto.adb import Result
from adbperfetto.device import DeviceInfo
from adbperfetto.sessions import (
    CONFIG_FLIGHT, FLIGHT_NAME, FlightSession, RecordSession, SessionError,
    SessionManager, parse_flight_ps, parse_query_sessions,
    parse_record_status)
from adbperfetto.state import StateStore

from tests.fake_adb import FakeAdb, wait_for

SERIAL = "SER1"
INFO = DeviceInfo(serial=SERIAL, model="Test Phone", build_id="B1",
                  sdk=36, supports_clone=True)
INFO_OLD = DeviceInfo(serial=SERIAL, model="Old Phone", build_id="B0",
                      sdk=34, supports_clone=False)
CONFIG = """\
buffers { size_kb: 1024 fill_policy: RING_BUFFER }
data_sources { config { name: "linux.sys_stats" } }
duration_ms: 60000
"""

QUERY_EMPTY = """\
Not meant for machine consumption. Use --query-raw for scripts.

Service: Perfetto v49.0 (N/A)
Tracing sessions: 1 (started: 1)


TRACING SESSIONS:

ID      UID     STATE      BUF (#) KB   DUR (s)   #DS  STARTED  NAME
===     ===     =====      ==========   =======   ===  =======  ====
34      2000    STARTED    (1) 1024     300       1    05:39:44
"""

QUERY_FLIGHT = QUERY_EMPTY.replace(
    "05:39:44\n", "05:39:44 adbperfetto_flight\n")


def make_record_adb():
    adb = FakeAdb()
    adb.det = 2
    adb.trace_size = 1234
    adb.pushed = []
    adb.push_gate = None     # threading.Event delays the config push
    adb.launch_time = 0.0

    @adb.on(r"echo DET=")
    def _monitor(ctx):
        return Result(0, f"DET={adb.det}\nSIZE={adb.trace_size}\n", "")

    @adb.on(r"perfetto --is_detached")
    def _detached(ctx):
        return Result(adb.det, "", "")

    @adb.on(r"cat > ")
    def _push(ctx):
        if adb.push_gate is not None:
            adb.push_gate.wait(10)
        adb.pushed.append(ctx["input_text"])
        return Result(0, "", "")

    @adb.on(r"perfetto --txt -c .* --detach=")
    def _launch(ctx):
        adb.launch_time = time.time()
        adb.det = 0
        return Result(0, "", "")

    @adb.on(r"perfetto --attach=")
    def _attach(ctx):
        adb.det = 2
        return Result(0, "", "")

    @adb.on(r"echo MODEL=")
    def _probe(ctx):
        return Result(
            0, "MODEL=Test Phone\nSDK=36\nBUILD_ID=B1\n"
               "PERFETTO=Perfetto v49.0 (N/A)\n"
               "LOGGERUI=package:/x\n", "")

    @adb.on(r"ls -t")
    def _ls(ctx):
        return Result(
            0, "/data/misc/perfetto-traces/adbperfetto_orphan.pftrace\n", "")

    @adb.on(r"stat -c %Y")
    def _mtime(ctx):
        return Result(0, "1700000000\n", "")

    @adb.on(r"stat -c %s")
    def _size(ctx):
        return Result(0, "1234\n", "")

    return adb


def make_flight_adb(query=QUERY_EMPTY, clone_delay=0.0):
    adb = FakeAdb()
    adb.alive = 0
    adb.pushed = []
    adb.push_gate = None
    adb.launch_time = 0.0

    @adb.on(r"perfetto --is_detached")
    def _detached(ctx):
        return Result(2, "", "")

    @adb.on(r"perfetto --query")
    def _query(ctx):
        return Result(0, query, "")

    @adb.on(r"cat > ")
    def _push(ctx):
        if adb.push_gate is not None:
            adb.push_gate.wait(10)
        adb.pushed.append(ctx["input_text"])
        return Result(0, "", "")

    @adb.on(r"--background-wait")
    def _launch(ctx):
        adb.launch_time = time.time()
        adb.alive = 1
        return Result(0, "Connected to traced\n4321\n", "")

    @adb.on(r"echo ALIVE=")
    def _alive(ctx):
        return Result(0, f"ALIVE={0 if adb.alive else 1}\n", "")

    @adb.on(r"--clone-by-name")
    def _clone(ctx):
        if clone_delay:
            time.sleep(clone_delay)
        return Result(0, "", "Wrote 1234 bytes into /data/...")

    @adb.on(r"kill -TERM")
    def _kill(ctx):
        adb.alive = 0
        return Result(0, "", "")

    @adb.on(r"trigger_perfetto")
    def _trigger(ctx):
        adb.alive = 0
        return Result(0, "", "")

    @adb.on(r"stat -c %s")
    def _size(ctx):
        return Result(0, "1234\n", "")

    @adb.on(r"echo MODEL=")
    def _probe(ctx):
        return Result(0, "MODEL=Test Phone\nSDK=36\n", "")

    return adb


def record_kwargs(tmp, adb, store=None, info=INFO):
    return dict(adb=adb, serial=SERIAL, info=info,
                state_store=store or StateStore(Path(tmp) / "state.json"),
                output_dir=Path(tmp) / "out", poll_interval=0.01,
                retry_interval=0.02, progress_interval=0.005,
                attach_timeout=0.5)


def manager_kwargs(tmp, adb):
    return dict(adb=adb, state_store=StateStore(Path(tmp) / "state.json"),
                settings=None, output_dir=Path(tmp) / "out",
                poll_interval=0.01, flight_poll_interval=0.01,
                monitor_interval=0.02, retry_interval=0.02,
                progress_interval=0.005, autostart=True)


class ParserTests(unittest.TestCase):
    def test_parse_record_status(self):
        status = parse_record_status("DET=0\nSIZE=123\nMB=1\nLOG=7\n")
        self.assertEqual(status["det"], 0)
        self.assertEqual(status["size"], 123)
        self.assertTrue(status["mobile_running"])
        self.assertEqual(status["log_kb"], 7)
        self.assertIsNone(parse_record_status("nonsense\nDET=oops\n"))
        self.assertIsNone(parse_record_status("SIZE=3\n"))

    def test_parse_query_sessions(self):
        sessions = parse_query_sessions(QUERY_FLIGHT)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["name"], "adbperfetto_flight")
        self.assertEqual(sessions[0]["state"], "STARTED")
        self.assertEqual(parse_query_sessions(QUERY_EMPTY)[0]["name"], "")
        self.assertEqual(parse_query_sessions("no table here"), [])

    def test_parse_flight_ps(self):
        text = (
            "  PID ARGS\n"
            "  1000 /system/bin/init\n"
            "  4321 perfetto --txt -c "
            "/data/misc/perfetto-configs/adbperfetto_flight.pbtx "
            "-o /data/misc/perfetto-traces/adbperfetto_flight_1.pftrace "
            "--background-wait\n")
        self.assertEqual(parse_flight_ps(text), (
            4321,
            "/data/misc/perfetto-traces/adbperfetto_flight_1.pftrace"))
        self.assertIsNone(parse_flight_ps("  1 init\n"))


class RecordSessionTests(unittest.TestCase):
    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            session = RecordSession(**record_kwargs(tmp, adb))
            session.start(CONFIG, "test", 60000)
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            self.assertIn("write_into_file: true", adb.pushed[0])
            self.assertTrue(session.remote_path.startswith(
                "/data/misc/perfetto-traces/adbperfetto_"))
            session.stop()
            self.assertTrue(wait_for(lambda: session.status == "done", 10))
            files = list((Path(tmp) / "out").glob("*.pftrace"))
            self.assertEqual(len(files), 1)
            self.assertIn(str(files[0]), session.outputs)
            self.assertTrue(any("rm -f" in c
                                for c in adb.shell_calls("perfetto-traces")))
            self.assertEqual(session.api_dict()["mode"], "record")
            self.assertFalse(session.ended_by_duration)

    def test_sf_capture_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()

            @adb.on(r"dumpsys SurfaceFlinger")
            def _sf(ctx):
                return Result(0, "SurfaceFlinger dumpsys\n", "")

            session = RecordSession(**record_kwargs(tmp, adb))
            session.start(CONFIG, "test", 60000, sf=True, sf_interval_ms=50)
            self.assertTrue(wait_for(
                lambda: session.status == "recording"
                and session.sf is not None
                and session.sf.dump_count >= 1, 5))
            self.assertTrue(session.api_dict()["sf"]["running"])
            session.stop()
            self.assertTrue(wait_for(lambda: session.status == "done", 10))
            zips = list((Path(tmp) / "out").glob("*.zip"))
            self.assertEqual(len(zips), 1)
            self.assertIn(str(zips[0]), session.outputs)
            labels = {item["label"] for item in session.checklist}
            self.assertIn("SF dumps", labels)
            self.assertIn("Perfetto trace", labels)
            self.assertFalse(session.sf.running)

    def test_started_at_tracks_launch(self):
        """started_at is stamped at the perfetto launch, not at click."""
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            adb.push_gate = threading.Event()  # hold the config push
            session = RecordSession(**record_kwargs(tmp, adb))
            session.start(CONFIG, "test", 60000)
            click_started = session.started_at
            time.sleep(0.05)
            adb.push_gate.set()
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            self.assertGreater(session.started_at, click_started)
            self.assertLessEqual(session.started_at, adb.launch_time)
            self.assertLess(adb.launch_time - session.started_at, 1)
            ts = datetime.fromtimestamp(session.started_at)
            self.assertTrue(session.remote_path.endswith(
                f"adbperfetto_{ts:%Y%m%d_%H%M%S}.pftrace"))
            session.stop()
            self.assertTrue(wait_for(lambda: session.status == "done", 10))

    def test_refuse_when_already_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            adb.det = 0
            session = RecordSession(**record_kwargs(tmp, adb))
            with self.assertRaises(SessionError):
                session.start(CONFIG, "test", 60000)

    def test_disconnect_and_reconnect_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            session = RecordSession(**record_kwargs(tmp, adb))
            session.start(CONFIG, "test", 60000)
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            adb.offline = True
            self.assertTrue(wait_for(lambda: not session.connected))
            self.assertFalse(session.connected)
            adb.offline = False
            self.assertTrue(wait_for(lambda: session.connected))
            self.assertEqual(len(session.disconnects), 1)
            session.stop()
            self.assertTrue(wait_for(lambda: session.status == "done", 10))

    def test_stop_delivered_after_reconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            session = RecordSession(**record_kwargs(tmp, adb))
            session.start(CONFIG, "test", 60000)
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            adb.offline = True
            self.assertTrue(wait_for(lambda: not session.connected))
            session.stop()
            time.sleep(0.1)
            self.assertEqual(len(adb.shell_calls("--attach")), 0)
            adb.offline = False
            self.assertTrue(wait_for(lambda: session.status == "done", 10))
            self.assertGreaterEqual(len(adb.shell_calls("--attach")), 1)

    def test_duration_end_auto_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            session = RecordSession(**record_kwargs(tmp, adb))
            session.start(CONFIG, "test", 60000)
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            adb.det = 2  # trace ended on its own
            self.assertTrue(wait_for(lambda: session.status == "done", 10))
            self.assertTrue(session.ended_by_duration)
            self.assertTrue(session.outputs)

    def test_pull_failure_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            adb.pull_failures = 1
            session = RecordSession(**record_kwargs(tmp, adb))
            session.start(CONFIG, "test", 60000)
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            session.stop()
            self.assertTrue(wait_for(lambda: session.status == "done", 10))
            self.assertGreaterEqual(len(adb.pull_calls()), 2)

    def test_resume_from_state_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            store = StateStore(Path(tmp) / "state.json")
            session = RecordSession(**record_kwargs(tmp, adb, store=store))
            session.start(CONFIG, "test", 60000)
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            record = store.get(SERIAL)
            self.assertEqual(record["mode"], "record")
            resumed = RecordSession(**record_kwargs(tmp, adb, store=store))
            resumed._load_record(record)
            resumed.resume()
            self.assertTrue(wait_for(
                lambda: resumed.status == "recording"))
            resumed.stop()
            self.assertTrue(wait_for(
                lambda: resumed.status == "done", 10))

    def test_resume_from_state_pulling(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            store = StateStore(Path(tmp) / "state.json")
            session = RecordSession(**record_kwargs(tmp, adb, store=store))
            session.start(CONFIG, "test", 60000)
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            record = dict(store.get(SERIAL))
            record["status"] = "pulling"
            resumed = RecordSession(**record_kwargs(tmp, adb, store=store))
            resumed._load_record(record)
            resumed.resume()
            self.assertTrue(wait_for(
                lambda: resumed.status == "done", 10))

    def test_mobilelog_cleanup_when_start_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = FakeAdb()
            adb.mb_running = "0"
            adb.other_mask = 22
            adb.broadcasts = []

            @adb.on(r"perfetto --is_detached")
            def _detached(ctx):
                return Result(2, "", "")

            @adb.on(r"pm path com\.debug\.loggerui")
            def _pm(ctx):
                return Result(0, "package:/x\n", "")

            @adb.on(r"getprop vendor\.MB\.running")
            def _mb_running(ctx):
                return Result(0, adb.mb_running + "\n", "")

            @adb.on(r"getprop vendor\.MB\.realpath")
            def _mb_path(ctx):
                return Result(
                    0, "/data/debuglogger/mobilelog/APLog_test\n", "")

            @adb.on(r"dumpsys notification")
            def _dumpsys(ctx):
                names = [name for name, bit in
                         (("Modem", 2), ("Network", 4), ("GPSHost", 16))
                         if adb.other_mask & bit]
                text = (",".join(names) + " Log are on.") if names \
                    else "Warning : All logs are stopped!!!"
                return Result(
                    0, "NotificationRecord(x pkg=com.debug.loggerui)\n"
                       f"  android.bigText=String ({text})\n", "")

            @adb.on(r"am broadcast")
            def _broadcast(ctx):
                command = ctx["command"]
                name = re.search(r"cmd_name (\S+)", command).group(1)
                match = re.search(r"cmd_target (\d+)", command)
                target = int(match.group(1)) if match else 1
                adb.broadcasts.append((name, target))
                if name == "start" and target == 1:
                    adb.mb_running = "1"
                elif name == "stop" and target == 1:
                    adb.mb_running = "0"
                elif name == "stop":
                    adb.other_mask &= ~target
                elif name == "start":
                    adb.other_mask |= target
                return Result(0, "", "")

            @adb.on(r"ls -1 /data/debuglogger")
            def _ls(ctx):
                return Result(0, "APLog_test\n", "")

            @adb.on(r"cat > ")
            def _push(ctx):
                return Result(0, "", "")

            @adb.on(r"perfetto --txt -c .* --detach=")
            def _launch(ctx):
                return Result(1, "", "boom")

            session = RecordSession(**record_kwargs(tmp, adb))
            session.start(CONFIG, "test", 60000, mobilelog=True)
            self.assertTrue(wait_for(
                lambda: session.status == "failed", 10))
            session._thread.join(timeout=5)  # let the worker finish
            self.assertIn("boom", session.error)
            # Cleanup: MobileLog stopped and paused logs restored.
            self.assertIn(("stop", 1), adb.broadcasts)
            self.assertIn(("start", 22), adb.broadcasts)
            self.assertTrue(session.mobilelog.logs_restored)

    def test_mobilelog_cleanup_when_prepare_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = FakeAdb()
            adb.other_mask = 4
            adb.broadcasts = []

            @adb.on(r"perfetto --is_detached")
            def _detached(ctx):
                return Result(2, "", "")

            @adb.on(r"dumpsys notification")
            def _dumpsys(ctx):
                text = ("Network Log is on." if adb.other_mask & 4
                        else "Warning : All logs are stopped!!!")
                return Result(
                    0, "NotificationRecord(x pkg=com.debug.loggerui)\n"
                       f"  android.bigText=String ({text})\n", "")

            @adb.on(r"am broadcast")
            def _broadcast(ctx):
                command = ctx["command"]
                name = re.search(r"cmd_name (\S+)", command).group(1)
                match = re.search(r"cmd_target (\d+)", command)
                target = int(match.group(1)) if match else 1
                adb.broadcasts.append((name, target))
                if name == "stop" and target != 1:
                    adb.other_mask &= ~target
                elif name == "start" and target != 1:
                    adb.other_mask |= target
                return Result(0, "", "")

            @adb.on(r"pm path com\.debug\.loggerui")
            def _pm(ctx):
                return Result(1, "", "")  # prepare fails here

            session = RecordSession(**record_kwargs(tmp, adb))
            session.start(CONFIG, "test", 60000, mobilelog=True)
            self.assertTrue(wait_for(
                lambda: session.status == "failed", 10))
            session._thread.join(timeout=5)  # let the worker finish
            # The Network log paused during prepare() is restored.
            self.assertIn(("start", 4), adb.broadcasts)
            self.assertTrue(session.mobilelog.logs_restored)

    def test_directory_pull_retry_removes_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = FakeAdb()
            session = RecordSession(**record_kwargs(tmp, adb))
            local = Path(tmp) / "out" / "APLog_x"
            attempts = []

            @adb.on(r"APLog_x")
            def _pull(ctx):
                attempts.append(1)
                dest = ctx["local"]
                if len(attempts) == 1:
                    (dest / "APLog_x").mkdir(parents=True)
                    (dest / "APLog_x" / "junk.txt").write_bytes(b"x")
                    return Result(1, "", "pull failed")
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "main.txt").write_bytes(b"ok")
                return Result(0, "", "")

            self.assertTrue(session._pull_with_retry(
                "/data/debuglogger/mobilelog/APLog_x", local))
            self.assertFalse((local / "APLog_x").exists())
            self.assertTrue((local / "main.txt").exists())


class StoppedAtTests(unittest.TestCase):
    def test_done_freezes_elapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            session = RecordSession(**record_kwargs(tmp, adb))
            session.start(CONFIG, "test", 60000)
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            session.stop()
            self.assertTrue(wait_for(lambda: session.status == "done", 10))
            first = session.api_dict()
            self.assertIsNotNone(first["stopped_at"])
            self.assertEqual(
                first["elapsed_s"],
                int(first["stopped_at"] - first["started_at"]))
            with mock.patch("adbperfetto.sessions.time.time",
                            return_value=first["stopped_at"] + 3600):
                later = session.api_dict()
            self.assertEqual(later["stopped_at"], first["stopped_at"])
            self.assertEqual(later["elapsed_s"], first["elapsed_s"])

    def test_stopped_at_clamped_to_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            session = RecordSession(**record_kwargs(tmp, adb))
            session.duration_ms = 10000
            session.started_at = time.time() - 60
            session._set_status("pulling")
            self.assertAlmostEqual(
                session.stopped_at, session.started_at + 10, places=3)

    def test_recording_clears_and_stopping_keeps_stopped_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            session = RecordSession(**record_kwargs(tmp, adb))
            session._set_status("stopping")
            self.assertIsNone(session.stopped_at)
            session._set_status("pulling")
            self.assertIsNotNone(session.stopped_at)
            session._set_status("recording")
            self.assertIsNone(session.stopped_at)
            session._set_status("done")
            self.assertIsNotNone(session.stopped_at)
            session._set_status("starting")
            self.assertIsNone(session.stopped_at)

    def test_record_round_trip_stopped_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            session = RecordSession(**record_kwargs(tmp, adb))
            session._set_status("done")
            record = session.record()
            self.assertEqual(record["stopped_at"], session.stopped_at)
            loaded = RecordSession(**record_kwargs(tmp, adb))
            loaded._load_record(record)
            self.assertEqual(loaded.stopped_at, session.stopped_at)
            record.pop("stopped_at")
            out = Path(tmp) / "old.pftrace"
            out.write_bytes(b"x")
            start = time.time() - 600
            mtime = start + 27
            os.utime(out, (mtime, mtime))
            record["started_at"] = datetime.fromtimestamp(
                start, tz=timezone.utc).isoformat()
            record["outputs"] = [str(out)]
            record["status"] = "done"
            loaded._load_record(record)
            self.assertAlmostEqual(loaded.stopped_at, mtime, places=3)
            self.assertEqual(loaded.size_bytes, 1)
            record["status"] = "recording"
            loaded._load_record(record)
            self.assertIsNone(loaded.stopped_at)


class FlightSessionTests(unittest.TestCase):
    def test_clone_snapshot_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_flight_adb()
            session = FlightSession(**record_kwargs(tmp, adb))
            session.start(CONFIG, "test", 600000)
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            self.assertEqual(session.pid, 4321)
            self.assertIn("CLONE_SNAPSHOT", adb.pushed[0])
            self.assertIn(f'"{FLIGHT_NAME}"', adb.pushed[0])
            session.request_snapshot()
            self.assertTrue(wait_for(
                lambda: len(session.snapshots) == 1, 10))
            self.assertTrue(Path(session.snapshots[0]["local"]).exists())
            session.stop()
            self.assertTrue(wait_for(lambda: session.status == "done", 10))
            self.assertTrue(any("kill -TERM 4321" in c
                                for c in adb.shell_calls()))
            self.assertTrue(any(CONFIG_FLIGHT in c and "rm -f" in c
                                for c in adb.shell_calls()))

    def test_started_at_tracks_launch(self):
        """started_at is stamped at the perfetto launch, not at click."""
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_flight_adb()
            adb.push_gate = threading.Event()
            session = FlightSession(**record_kwargs(tmp, adb))
            session.start(CONFIG, "test", 600000)
            click_started = session.started_at
            time.sleep(0.05)
            adb.push_gate.set()
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            self.assertGreater(session.started_at, click_started)
            self.assertLessEqual(session.started_at, adb.launch_time)
            self.assertLess(adb.launch_time - session.started_at, 1)
            ts = datetime.fromtimestamp(session.started_at)
            self.assertTrue(session.remote_path.endswith(
                f"adbperfetto_flight_{ts:%Y%m%d_%H%M%S}.pftrace"))
            session.stop()
            self.assertTrue(wait_for(lambda: session.status == "done", 10))

    def test_one_shot_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_flight_adb()
            session = FlightSession(
                **record_kwargs(tmp, adb, info=INFO_OLD))
            session.start(CONFIG, "test", 600000)
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            self.assertTrue(session.one_shot)
            self.assertIn("STOP_TRACING", adb.pushed[0])
            session.request_snapshot()
            self.assertTrue(wait_for(lambda: session.status == "done", 10))
            self.assertEqual(len(session.snapshots), 1)
            self.assertTrue(Path(session.snapshots[0]["local"]).exists())

    def test_refuse_when_already_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_flight_adb(query=QUERY_FLIGHT)
            session = FlightSession(**record_kwargs(tmp, adb))
            with self.assertRaises(SessionError):
                session.start(CONFIG, "test", 600000)

    def test_ended_when_process_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_flight_adb()
            session = FlightSession(**record_kwargs(tmp, adb))
            session.start(CONFIG, "test", 600000)
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            adb.alive = 0  # process died on its own
            self.assertTrue(wait_for(lambda: session.status == "ended", 10))

    def test_stop_with_final_snapshot_nonblocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_flight_adb(clone_delay=0.5)
            session = FlightSession(**record_kwargs(tmp, adb))
            session.start(CONFIG, "test", 600000)
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            start = time.monotonic()
            session.stop(save_snapshot=True)
            self.assertLess(time.monotonic() - start, 0.3)
            self.assertEqual(session.status, "stopping")
            self.assertTrue(wait_for(
                lambda: len(session.snapshots) == 1, 10))
            self.assertTrue(wait_for(
                lambda: session.status == "done", 10))


class ManagerTests(unittest.TestCase):
    def test_device_appears_and_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            adb.device_list = [(SERIAL, "device")]
            manager = SessionManager(**manager_kwargs(tmp, adb))
            try:
                self.assertTrue(wait_for(
                    lambda: SERIAL in manager.devices
                    and manager.devices[SERIAL]["info"] is not None))
                info = manager.devices[SERIAL]["info"]
                self.assertEqual(info.model, "Test Phone")
                self.assertTrue(info.supports_clone)
                self.assertTrue(manager.snapshot()["adb_ok"])
            finally:
                manager.shutdown()

    def test_unauthorized_uses_model_hint_and_retries_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = FakeAdb()
            adb.device_list = [("S1", "unauthorized", "Infinix X1202")]
            calls = []

            @adb.on(r"echo MODEL=")
            def _probe(_ctx):
                calls.append(1)
                if len(calls) == 1:
                    raise Exception("adbd not ready")
                return Result(
                    0, "MODEL=Infinix X1202\nMARKET_TRAN=XPAD Edge 2\n"
                       "SDK=37\nRELEASE=17\n", "")

            manager = SessionManager(
                adb, StateStore(Path(tmp) / "state.json"), None,
                Path(tmp) / "out", autostart=False)
            manager.monitor.tick()
            snap1 = manager.snapshot()["devices"][0]
            self.assertEqual(snap1["info"]["model"], "Infinix X1202")
            self.assertEqual(calls, [])

            # User taps Allow -> first probe fails -> next tick retries & succeeds
            adb.device_list = [("S1", "device", "Infinix X1202")]
            manager.monitor.tick()
            self.assertEqual(len(calls), 1)
            manager.monitor.tick()
            self.assertEqual(len(calls), 2)
            snap2 = manager.snapshot()["devices"][0]
            self.assertEqual(snap2["info"]["model"], "Infinix X1202")
            self.assertEqual(snap2["info"]["device_name"], "XPAD Edge 2")

    def test_snapshot_marks_wifi_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            wifi = "192.168.1.23:5555"
            adb.device_list = [(wifi, "device"), (SERIAL, "device")]
            manager = SessionManager(**manager_kwargs(tmp, adb))
            try:
                self.assertTrue(wait_for(
                    lambda: wifi in manager.devices
                    and SERIAL in manager.devices, 10))
                snap = manager.snapshot()
                by_serial = {d["serial"]: d for d in snap["devices"]}
                self.assertTrue(by_serial[wifi]["wifi"])
                self.assertFalse(by_serial[SERIAL]["wifi"])
            finally:
                manager.shutdown()

    def test_orphan_record_and_adopt(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            adb.det = 0  # a detached session is already running
            adb.device_list = [(SERIAL, "device")]
            manager = SessionManager(**manager_kwargs(tmp, adb))
            try:
                self.assertTrue(wait_for(
                    lambda: manager.orphans.get(SERIAL) is not None))
                self.assertEqual(manager.orphans[SERIAL]["mode"], "record")
                session = manager.adopt(SERIAL)
                self.assertEqual(session.status, "recording")
                self.assertEqual(session.started_at, 1700000000)
                manager.stop(SERIAL)
                self.assertTrue(wait_for(
                    lambda: session.status == "done", 10))
                manager.dismiss(SERIAL)
                self.assertNotIn(SERIAL, manager.sessions)
            finally:
                manager.shutdown()

    def test_recovery_from_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            store = StateStore(Path(tmp) / "state.json")
            session = RecordSession(
                **record_kwargs(tmp, adb, store=store))
            session.start(CONFIG, "test", 60000)
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            # Simulate a restart: new manager loads the persisted record.
            adb.device_list = [(SERIAL, "device")]
            session.abandoned = True  # quiet the original worker
            manager = SessionManager(**manager_kwargs(tmp, adb))
            try:
                self.assertTrue(wait_for(
                    lambda: SERIAL in manager.sessions, 10))
                resumed = manager.sessions[SERIAL]
                self.assertTrue(wait_for(
                    lambda: resumed.status == "recording"))
                manager.stop(SERIAL)
                self.assertTrue(wait_for(
                    lambda: resumed.status == "done", 10))
            finally:
                manager.shutdown()

    def test_orphan_flight(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_flight_adb(query=QUERY_FLIGHT)
            adb.alive = 1  # the orphan flight process is still running
            adb.device_list = [(SERIAL, "device")]

            @adb.on(r"ps -A -o PID,ARGS")
            def _ps(ctx):
                return Result(
                    0, "  PID ARGS\n  4321 perfetto --txt -c "
                       f"{CONFIG_FLIGHT} -o "
                       "/data/misc/perfetto-traces/"
                       "adbperfetto_flight_1.pftrace --background-wait\n",
                    "")

            manager = SessionManager(**manager_kwargs(tmp, adb))
            try:
                self.assertTrue(wait_for(
                    lambda: manager.orphans.get(SERIAL) is not None, 10))
                self.assertEqual(manager.orphans[SERIAL]["mode"], "flight")
                session = manager.adopt(SERIAL)
                self.assertEqual(session.pid, 4321)
                manager.stop(SERIAL)
                self.assertTrue(wait_for(
                    lambda: session.status == "done", 10))
            finally:
                manager.shutdown()

    def test_start_record_via_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            adb.device_list = [(SERIAL, "device")]
            manager = SessionManager(**manager_kwargs(tmp, adb))
            try:
                self.assertTrue(wait_for(
                    lambda: SERIAL in manager.devices
                    and manager.devices[SERIAL]["info"] is not None))
                session = manager.start_record(SERIAL, CONFIG, "test",
                                               60000)
                self.assertTrue(wait_for(
                    lambda: session.status == "recording"))
                with self.assertRaises(SessionError):
                    manager.start_record(SERIAL, CONFIG, "test", 60000)
                snap = manager.snapshot()
                device = snap["devices"][0]
                self.assertEqual(device["session"]["status"], "recording")
                manager.stop(SERIAL)
                self.assertTrue(wait_for(
                    lambda: session.status == "done", 10))
            finally:
                manager.shutdown()

    def test_unplugged_device_kept_when_session_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            adb.device_list = [(SERIAL, "device")]
            manager = SessionManager(**manager_kwargs(tmp, adb))
            try:
                self.assertTrue(wait_for(
                    lambda: SERIAL in manager.devices
                    and manager.devices[SERIAL]["info"] is not None))
                session = manager.start_record(SERIAL, CONFIG, "test",
                                               60000)
                self.assertTrue(wait_for(
                    lambda: session.status == "recording"))
                adb.offline = True
                adb.device_list = []
                self.assertTrue(wait_for(
                    lambda: manager.devices.get(SERIAL, {})
                    .get("state") == "disconnected", 10))
                snap = manager.snapshot()
                device = next(d for d in snap["devices"]
                              if d["serial"] == SERIAL)
                self.assertEqual(device["session"]["status"],
                                 "recording")
                self.assertFalse(device["session"]["connected"])
                # Plug back in.
                adb.offline = False
                adb.device_list = [(SERIAL, "device")]
                self.assertTrue(wait_for(
                    lambda: manager.devices[SERIAL]["state"] == "device",
                    10))
                manager.stop(SERIAL)
                self.assertTrue(wait_for(
                    lambda: session.status == "done", 10))
            finally:
                manager.shutdown()

    def test_persisted_session_visible_offline_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            adb = make_record_adb()
            store = StateStore(Path(tmp) / "state.json")
            session = RecordSession(
                **{**record_kwargs(tmp, adb), "state_store": store})
            session.start(CONFIG, "test", 60000)
            self.assertTrue(wait_for(
                lambda: session.status == "recording"))
            record = store.get(SERIAL)
            self.assertEqual(record["info"]["model"], "Test Phone")
            session.abandoned = True  # stop the old worker
            # Restart the app while the phone is unplugged.
            adb.offline = True
            kwargs = manager_kwargs(tmp, adb)
            kwargs["state_store"] = store
            manager = SessionManager(**kwargs)
            try:
                loaded = manager.sessions[SERIAL]
                self.assertFalse(loaded.connected)
                self.assertIsNotNone(loaded.info)
                self.assertEqual(loaded.info.model, "Test Phone")
                self.assertEqual(manager.devices[SERIAL]["state"],
                                 "disconnected")
                snap = manager.snapshot()
                device = next(d for d in snap["devices"]
                              if d["serial"] == SERIAL)
                self.assertEqual(device["session"]["status"],
                                 "recording")
                self.assertFalse(device["session"]["connected"])
                # Plug the phone back in: the session resumes.
                adb.offline = False
                adb.device_list = [(SERIAL, "device")]
                self.assertTrue(wait_for(
                    lambda: loaded.status == "recording"
                    and loaded.connected, 10))
                manager.stop(SERIAL)
                self.assertTrue(wait_for(
                    lambda: loaded.status == "done", 10))
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main()
