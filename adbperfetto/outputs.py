import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from . import BUILD, VERSION

LOG_TYPES = ("main_log", "kernel_log", "sys_log", "events_log")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def safe_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip(" ._")
    if not value:
        return "unknown"
    if value.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        value = "_" + value
    return value


def unique_path(path: Path) -> Path:
    """Never overwrite: append _2, _3 ... if the name exists."""
    path = Path(path)
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(
            f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find a free name for {path}")


def record_name(model: str, timestamp: str) -> str:
    return f"{safe_filename(model)}_{timestamp}.pftrace"


def zip_name(model: str, build_id: str, timestamp: str) -> str:
    return (f"{safe_filename(model)}_{safe_filename(build_id)}_"
            f"{timestamp}.zip")


def check_integrity(root) -> dict:
    root = Path(root)
    trace_files = []
    trace_dir = root / "trace"
    if trace_dir.exists():
        trace_files = [p for pattern in ("*.pftrace", "*.perfetto-trace")
                       for p in trace_dir.rglob(pattern) if p.is_file()]
    trace_size = sum(p.stat().st_size for p in trace_files)
    log_root = root / "mobilelog"
    files = ([p for p in log_root.rglob("*") if p.is_file()]
             if log_root.exists() else [])
    missing_logs = [name for name in LOG_TYPES
                    if not any(p.name.startswith(name) for p in files)]
    info = root / "device_info.txt"
    checks = {
        "Perfetto trace": (
            trace_size > 0, trace_size,
            [] if trace_size else ["Perfetto trace"]),
        "device_info.txt": (
            info.is_file(), info.stat().st_size if info.is_file() else 0,
            [] if info.is_file() else ["device_info.txt"]),
    }
    # Keep MobileLog in the checklist whenever the folder exists, or when
    # neither optional sidecars exist (legacy mobilelog-only bundles).
    sf_root = root / "sf"
    sf_files = ([p for p in sf_root.rglob("sf_*.txt") if p.is_file()]
                if sf_root.exists() else [])
    if log_root.exists() or not sf_root.exists():
        checks["MobileLog session"] = (
            not missing_logs, sum(p.stat().st_size for p in files),
            missing_logs)
    if sf_root.exists():
        sf_size = sum(p.stat().st_size for p in sf_files)
        checks["SF dumps"] = (
            len(sf_files) > 0, sf_size,
            [] if sf_files else ["SF dumps"])
    return checks


def checklist_items(checks: dict) -> list[dict]:
    return [{"label": label, "ok": ok, "size": size}
            for label, (ok, size, _missing) in checks.items()]


def write_zip(root, archive):
    root = Path(root)
    archive = Path(archive)
    files = sorted(p for p in root.rglob("*") if p.is_file())
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED,
                         allowZip64=True) as output:
        for path in files:
            info = zipfile.ZipInfo.from_file(
                path, path.relative_to(root).as_posix())
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open("rb") as source, output.open(
                    info, "w", force_zip64=True) as target:
                shutil.copyfileobj(source, target, 1024 * 1024)


def device_info_text(info, serial: str, *, mode: str, preset_name: str,
                     started_iso: str, stopped_iso: str, duration_s: float,
                     device_time: str, log_path: str, original_mask,
                     paused_names: str, logs_restored: bool,
                     ended_early: bool, extra: str = "") -> str:
    model = getattr(info, "model", "") if info else ""
    device = getattr(info, "device", "") if info else ""
    build_id = getattr(info, "build_id", "") if info else ""
    fingerprint = getattr(info, "fingerprint", "") if info else ""
    release = getattr(info, "release", "") if info else ""
    text = (
        f"Tool: adbperfetto {VERSION} ({BUILD})\n"
        f"Mode: {mode}\nPreset: {preset_name}\n"
        f"Serial: {serial}\nModel: {model}\nDevice: {device}\n"
        f"Build display ID: {build_id}\nFingerprint: {fingerprint}\n"
        f"Android version: {release}\n"
        f"PC start time: {started_iso}\nPC stop time: {stopped_iso}\n"
        f"Duration: {duration_s:.1f} seconds\n"
        f"Device time at start: {device_time}\n"
        f"MobileLog session: {log_path}\n"
        f"Logs paused during capture: "
        f"{'unknown' if original_mask is None else paused_names or 'none'}\n"
        f"Paused logs restored: {'yes' if logs_restored else 'no'}\n"
        f"Trace ended before capture stop: "
        f"{'yes' if ended_early else 'no'}\n"
    )
    return text + extra


def recent(output_dir, limit: int = 50) -> list[dict]:
    output_dir = Path(output_dir)
    entries = []
    if output_dir.is_dir():
        for pattern in ("*.pftrace", "*.perfetto-trace", "*.zip"):
            for path in output_dir.glob(pattern):
                if path.is_file():
                    stat = path.stat()
                    entries.append({
                        "path": str(path), "name": path.name,
                        "size": stat.st_size, "mtime": stat.st_mtime,
                    })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries[:limit]


def reveal(path):
    """Reveal the file in Windows Explorer."""
    try:
        subprocess.Popen(["explorer", f"/select,{path}"],
                         creationflags=CREATE_NO_WINDOW)
        return True
    except OSError:
        return False


def open_dir(path):
    try:
        subprocess.Popen(["explorer", str(path)],
                         creationflags=CREATE_NO_WINDOW)
        return True
    except OSError:
        return False
