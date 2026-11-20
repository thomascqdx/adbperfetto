"""FastAPI app: REST endpoints + SSE state stream + static frontend."""

import asyncio
import json
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import BUILD, VERSION, builder, outputs, paths, pbtx
from .adb import AdbError
from .device import atrace_categories, ftrace_events
from .presets import DevicePrefs
from .sessions import SessionError

TERMINAL = ("done", "failed", "ended", "abandoned")


class RecordBody(BaseModel):
    preset_id: str | None = None
    text: str | None = None
    duration_min: float | None = None
    mobilelog: bool = False
    sf: bool = False
    sf_interval_ms: int = 200


class FlightBody(BaseModel):
    preset_id: str | None = None
    text: str | None = None
    lifetime_h: float | None = None


class StopBody(BaseModel):
    save_snapshot: bool = False


class PresetBody(BaseModel):
    name: str
    text: str


class ParseBody(BaseModel):
    text: str


class RenderBody(BaseModel):
    text: str
    model: dict


class PreviewBody(BaseModel):
    text: str
    mode: str = "record"
    duration_min: float | None = None
    lifetime_h: float | None = None
    serial: str | None = None


class RevealBody(BaseModel):
    path: str


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, str(exc).strip("'"))
    if isinstance(exc, SessionError):
        return HTTPException(409, str(exc))
    if isinstance(exc, pbtx.PbtxError):
        return HTTPException(400, str(exc))
    if isinstance(exc, AdbError):
        return HTTPException(400, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(400, str(exc))
    return HTTPException(400, str(exc))


def create_app(manager, presets, settings, quit_hook=None,
               shutdown_event=None, demo_adb=None,
               device_prefs=None) -> FastAPI:
    if device_prefs is None:
        device_prefs = DevicePrefs(settings.path.parent)
    app = FastAPI(title="adbperfetto", version=VERSION)
    app.state.quit_hook = quit_hook
    app.state.shutdown_event = shutdown_event or threading.Event()
    app.state.demo_adb = demo_adb

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            raise _http_error(exc) from exc

    def config_text(preset_id, text):
        if text is not None:
            pbtx.parse(text)  # validate early
            return text, "custom"
        pid = preset_id or settings.default_preset
        preset = call(presets.get, pid)
        return preset["text"], preset["name"]

    @app.get("/api/ping")
    def ping():
        return {"app": "adbperfetto", "version": VERSION,
                "build": BUILD}

    @app.get("/api/state")
    def state():
        return manager.snapshot()

    @app.get("/api/events")
    async def events():
        async def stream():
            last_version = -1
            last_send = 0.0
            last_heartbeat = time.monotonic()
            while not app.state.shutdown_event.is_set():
                snap = await asyncio.to_thread(manager.snapshot)
                now = time.monotonic()
                if manager.version != last_version or now - last_send >= 1.0:
                    yield (f"event: state\n"
                           f"data: {json.dumps(snap)}\n\n")
                    last_version = manager.version
                    last_send = now
                if now - last_heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now
                await asyncio.sleep(0.2)

        return StreamingResponse(stream(), media_type="text/event-stream")

    # -- sessions ----------------------------------------------------------

    @app.post("/api/devices/{serial}/record")
    def start_record(serial: str, body: RecordBody | None = None):
        body = body or RecordBody()
        text, name = config_text(body.preset_id, body.text)
        duration_ms = (int(body.duration_min * 60000)
                       if body.duration_min else None)
        session = call(manager.start_record, serial, text, name,
                       duration_ms, body.mobilelog,
                       sf=body.sf, sf_interval_ms=body.sf_interval_ms)
        return {"ok": True, "status": session.status}

    @app.post("/api/devices/{serial}/flight")
    def start_flight(serial: str, body: FlightBody | None = None):
        body = body or FlightBody()
        text, name = config_text(body.preset_id, body.text)
        lifetime_h = (body.lifetime_h if body.lifetime_h
                      else settings.flight_lifetime_h)
        session = call(manager.start_flight, serial, text, name,
                       int(lifetime_h * 3600000))
        return {"ok": True, "status": session.status}

    @app.post("/api/devices/{serial}/stop")
    def stop(serial: str, body: StopBody | None = None):
        body = body or StopBody()
        call(manager.stop, serial, body.save_snapshot)
        return {"ok": True}

    @app.post("/api/devices/{serial}/snapshot")
    def snapshot(serial: str):
        call(manager.request_snapshot, serial)
        return {"ok": True}

    @app.post("/api/devices/{serial}/adopt")
    def adopt(serial: str):
        call(manager.adopt, serial)
        return {"ok": True}

    @app.post("/api/devices/{serial}/dismiss")
    def dismiss(serial: str):
        call(manager.dismiss, serial)
        return {"ok": True}

    @app.post("/api/devices/{serial}/abandon")
    def abandon(serial: str):
        call(manager.abandon, serial)
        return {"ok": True}

    @app.get("/api/devices/{serial}/atrace")
    def atrace(serial: str):
        if manager.adb is None:
            raise HTTPException(400, "ADB is not available")
        return {"categories": call(atrace_categories, manager.adb, serial)}

    @app.get("/api/devices/{serial}/ftrace")
    def ftrace(serial: str):
        if manager.adb is None:
            raise HTTPException(400, "ADB is not available")
        return {"events": call(ftrace_events, manager.adb, serial)}

    @app.get("/api/builtin-lists")
    def builtin_lists():
        return {"ftrace_events": builder.BUILTIN_FTRACE_EVENTS,
                "atrace_categories": builder.BUILTIN_ATRACE_CATEGORIES}

    # -- presets / settings ----------------------------------------------------

    @app.get("/api/presets")
    def list_presets():
        return {"presets": presets.list()}

    @app.get("/api/presets/{preset_id}")
    def get_preset(preset_id: str):
        return call(presets.get, preset_id)

    @app.post("/api/presets")
    def save_preset(body: PresetBody):
        preset_id = call(presets.save, body.name, body.text)
        return {"ok": True, "id": preset_id}

    @app.delete("/api/presets/{preset_id}")
    def delete_preset(preset_id: str):
        call(presets.delete, preset_id)
        return {"ok": True}

    @app.get("/api/settings")
    def get_settings():
        return settings.to_dict()

    @app.put("/api/settings")
    def put_settings(body: dict = Body(...)):
        call(settings.update, body)
        return settings.to_dict()

    @app.get("/api/prefs")
    def get_prefs():
        return device_prefs.to_dict()

    @app.put("/api/prefs/{serial}")
    def put_prefs(serial: str, body: dict = Body(...)):
        return call(device_prefs.update, serial, body)

    # -- config helpers -----------------------------------------------------------

    @app.post("/api/config/parse")
    def config_parse(body: ParseBody):
        try:
            model = builder.model_from_text(body.text)
        except pbtx.PbtxError as exc:
            return {"ok": False, "error": {
                "message": exc.message, "line": exc.line, "col": exc.col}}
        return {"ok": True, "model": model,
                "duration_ms": pbtx.duration_ms(body.text),
                "buffer_kb": pbtx.total_buffer_kb(body.text)}

    @app.post("/api/config/render")
    def config_render(body: RenderBody):
        return {"text": call(builder.apply_model, body.text, body.model)}

    @app.post("/api/config/preview")
    def config_preview(body: PreviewBody):
        try:
            if body.mode == "flight":
                lifetime_h = (body.lifetime_h if body.lifetime_h
                              else settings.flight_lifetime_h)
                clone = True
                if body.serial:
                    info = (manager.devices.get(body.serial) or {}
                            ).get("info")
                    if info is not None:
                        clone = getattr(info, "supports_clone", True)
                from .sessions import FLIGHT_NAME, FLIGHT_TRIGGER
                text = pbtx.to_flight(
                    body.text, session_name=FLIGHT_NAME,
                    trigger_name=FLIGHT_TRIGGER,
                    lifetime_ms=int(lifetime_h * 3600000), clone=clone)
            else:
                duration_ms = (int(body.duration_min * 60000)
                               if body.duration_min else None)
                text = pbtx.to_record(body.text, duration_ms)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"text": text}

    # -- outputs -----------------------------------------------------------------

    @app.get("/api/outputs")
    def list_outputs():
        return {"outputs": outputs.recent(manager._output_path())}

    @app.post("/api/outputs/reveal")
    def reveal(body: RevealBody):
        output_dir = manager._output_path().resolve()
        target = Path(body.path)
        try:
            resolved = target.resolve()
        except OSError:
            raise HTTPException(400, "Invalid path")
        if output_dir not in resolved.parents and resolved != output_dir:
            raise HTTPException(400, "Path is outside the output directory")
        call(outputs.reveal, resolved)
        return {"ok": True}

    @app.post("/api/outputs/open-dir")
    def open_dir():
        call(outputs.open_dir, manager._output_path())
        return {"ok": True}

    # -- demo controls (only when running on the in-memory DemoAdb) ---------

    if demo_adb is not None:
        class DemoBody(BaseModel):
            serial: str | None = None
            state: str | None = None
            mode: str | None = None

        @app.post("/api/demo/unplug")
        def demo_unplug(body: DemoBody):
            call(demo_adb.unplug, body.serial)
            manager._bump()
            return {"ok": True}

        @app.post("/api/demo/replug")
        def demo_replug(body: DemoBody):
            call(demo_adb.replug, body.serial)
            manager._bump()
            return {"ok": True}

        @app.post("/api/demo/state")
        def demo_state(body: DemoBody):
            call(demo_adb.set_state, body.serial, body.state)
            manager._bump()
            return {"ok": True}

        @app.post("/api/demo/remove_all")
        def demo_remove_all():
            call(demo_adb.remove_all)
            manager._bump()
            return {"ok": True}

        @app.post("/api/demo/fail_next")
        def demo_fail_next(body: DemoBody):
            call(demo_adb.fail_next_start, body.serial)
            manager._bump()
            return {"ok": True}

        @app.post("/api/demo/orphan")
        def demo_orphan(body: DemoBody):
            call(demo_adb.plant_orphan, body.serial,
                 body.mode or "record")
            manager._bump()
            return {"ok": True}

        @app.post("/api/demo/reset")
        def demo_reset():
            call(demo_adb.reset)
            manager._bump()
            return {"ok": True}

    # -- quit -------------------------------------------------------------------

    @app.post("/api/quit")
    def quit_app():
        app.state.shutdown_event.set()
        hook = app.state.quit_hook
        if hook:
            threading.Timer(0.3, hook).start()
        return {"ok": True}

    # -- static ------------------------------------------------------------------

    @app.middleware("http")
    async def revalidate_ui(request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    web_dir = paths.resource_dir() / "web"
    if web_dir.is_dir():
        app.mount("/static", StaticFiles(directory=web_dir), name="static")

        @app.get("/")
        def index():
            return FileResponse(web_dir / "index.html")

    return app
