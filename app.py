from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
VIDEO_ROOT = Path(os.environ.get("VIDEO_ROOT", BASE_DIR / "videos")).resolve()
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", BASE_DIR / "output")).resolve()
CACHE_DIR = Path(os.environ.get("CACHE_DIR", BASE_DIR / ".cache")).resolve()

SUPPORTED_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".m4v",
    ".hevc",
    ".h265",
    ".ts",
    ".mts",
    ".m2ts",
}

app = FastAPI(title="Keyframe Extract", version="0.1.0")

for directory in (VIDEO_ROOT, OUTPUT_DIR, CACHE_DIR):
    directory.mkdir(parents=True, exist_ok=True)

if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    raise RuntimeError("ffmpeg and ffprobe must be installed and available on PATH")


class OpenRequest(BaseModel):
    path: str


class ExtractRequest(BaseModel):
    session_id: str
    frame_index: int


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "ffmpeg command failed"
        raise RuntimeError(detail)
    return proc.stdout


def _safe_source(relative_path: str) -> Path:
    candidate = (VIDEO_ROOT / relative_path).resolve()
    try:
        candidate.relative_to(VIDEO_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Path escapes VIDEO_ROOT") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Video file not found")
    if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported video extension")
    return candidate


def _session_id(path: Path) -> str:
    stat = path.stat()
    identity = f"{path}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:20]


def _session_file(session_id: str) -> Path:
    return CACHE_DIR / "indexes" / f"{session_id}.json"


def _load_session(session_id: str) -> dict[str, Any]:
    path = _session_file(session_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Session not found; reopen the video")
    return json.loads(path.read_text(encoding="utf-8"))


def _ratio(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    try:
        numerator, denominator = value.split("/", 1)
        denominator_f = float(denominator)
        if denominator_f == 0:
            return None
        return float(numerator) / denominator_f
    except (ValueError, ZeroDivisionError):
        return None


def _source_bit_depth(stream: dict[str, Any]) -> int:
    raw = str(stream.get("bits_per_raw_sample") or "").strip()
    if raw.isdigit():
        return int(raw)
    pix_fmt = str(stream.get("pix_fmt") or "")
    match = re.search(r"p(9|10|12|14|16)", pix_fmt)
    if match:
        return int(match.group(1))
    if pix_fmt.startswith("p010"):
        return 10
    if pix_fmt.startswith("p012"):
        return 12
    return 8


def _probe_video(path: Path) -> dict[str, Any]:
    probe = json.loads(
        _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=index,codec_name,profile,width,height,pix_fmt,avg_frame_rate,r_frame_rate,nb_frames,bits_per_raw_sample,color_range,color_space,color_transfer,color_primaries:stream_side_data=rotation:format=duration,start_time,size,format_name",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    streams = probe.get("streams") or []
    if not streams:
        raise RuntimeError("No video stream found")
    stream = streams[0]
    fmt = probe.get("format") or {}

    frames_probe = json.loads(
        _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=key_frame,best_effort_timestamp_time,pict_type",
                "-of",
                "json",
                str(path),
            ]
        )
    )

    frames: list[dict[str, Any]] = []
    for raw_frame in frames_probe.get("frames") or []:
        timestamp = raw_frame.get("best_effort_timestamp_time")
        if timestamp is None:
            continue
        try:
            timestamp_f = float(timestamp)
        except (TypeError, ValueError):
            continue
        frames.append(
            {
                "t": timestamp_f,
                "key": bool(int(raw_frame.get("key_frame") or 0)),
                "type": raw_frame.get("pict_type") or "?",
            }
        )

    if not frames:
        raise RuntimeError("ffprobe returned no decodable video frames")

    start_time = float(fmt.get("start_time") or 0.0)
    duration = float(fmt.get("duration") or max(0.0, frames[-1]["t"] - frames[0]["t"]))
    keyframe_indices = [index for index, frame in enumerate(frames) if frame["key"]]
    if not keyframe_indices:
        keyframe_indices = [0]

    return {
        "source": str(path),
        "relative_path": str(path.relative_to(VIDEO_ROOT)),
        "filename": path.name,
        "start_time": start_time,
        "duration": duration,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "codec": stream.get("codec_name"),
        "profile": stream.get("profile"),
        "pix_fmt": stream.get("pix_fmt"),
        "bit_depth": _source_bit_depth(stream),
        "avg_fps": _ratio(stream.get("avg_frame_rate")),
        "nominal_fps": _ratio(stream.get("r_frame_rate")),
        "color_range": stream.get("color_range"),
        "color_space": stream.get("color_space"),
        "color_transfer": stream.get("color_transfer"),
        "color_primaries": stream.get("color_primaries"),
        "frame_count": len(frames),
        "frames": frames,
        "keyframe_indices": keyframe_indices,
    }


def _open_video(path: Path) -> dict[str, Any]:
    session_id = _session_id(path)
    cache_file = _session_file(session_id)
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    if cache_file.is_file():
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        data = _probe_video(path)
        data["session_id"] = session_id
        cache_file.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")

    data["session_id"] = session_id
    return data


def _seek_seconds(session: dict[str, Any], frame_index: int) -> float:
    frames = session["frames"]
    if frame_index < 0 or frame_index >= len(frames):
        raise HTTPException(status_code=400, detail="Frame index out of range")
    timestamp = float(frames[frame_index]["t"])
    start_time = float(session.get("start_time") or 0.0)
    return max(0.0, timestamp - start_time)


def _render_frame(session: dict[str, Any], frame_index: int, kind: str) -> Path:
    source = Path(session["source"])
    if not source.is_file():
        raise HTTPException(status_code=404, detail="Source video is no longer available")

    session_id = session["session_id"]
    frame_dir = CACHE_DIR / "frames" / session_id / kind
    frame_dir.mkdir(parents=True, exist_ok=True)

    if kind == "thumb":
        output = frame_dir / f"{frame_index:09d}.jpg"
    else:
        output = frame_dir / f"{frame_index:09d}.png"

    if output.is_file():
        return output

    seek = _seek_seconds(session, frame_index)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{seek:.9f}",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-an",
        "-sn",
        "-dn",
    ]

    if kind == "thumb":
        cmd += [
            "-vf",
            "scale='min(360,iw)':-2:flags=lanczos",
            "-q:v",
            "4",
            "-y",
            str(output),
        ]
    else:
        cmd += [
            "-vf",
            "scale='min(1600,iw)':-2:flags=lanczos",
            "-compression_level",
            "2",
            "-y",
            str(output),
        ]

    try:
        _run(cmd)
    except RuntimeError as exc:
        output.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return output


def _timestamp_label(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000.0))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}-{minutes:02d}-{secs:02d}.{millis:03d}"


def _extract_png(session: dict[str, Any], frame_index: int) -> Path:
    source = Path(session["source"])
    if not source.is_file():
        raise HTTPException(status_code=404, detail="Source video is no longer available")

    seek = _seek_seconds(session, frame_index)
    frame_time = float(session["frames"][frame_index]["t"])
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem).strip("._") or "frame"
    filename = f"{stem}_f{frame_index:09d}_{_timestamp_label(frame_time)}.png"
    output = OUTPUT_DIR / filename

    suffix = 1
    while output.exists():
        output = OUTPUT_DIR / f"{Path(filename).stem}_{suffix}.png"
        suffix += 1

    pix_fmt = "rgb48be" if int(session.get("bit_depth") or 8) > 8 else "rgb24"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{seek:.9f}",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-an",
        "-sn",
        "-dn",
        "-pix_fmt",
        pix_fmt,
        "-compression_level",
        "6",
        "-y",
        str(output),
    ]

    try:
        _run(cmd)
    except RuntimeError as exc:
        output.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return output


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/files")
def list_files() -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(VIDEO_ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        stat = path.stat()
        files.append(
            {
                "path": str(path.relative_to(VIDEO_ROOT)),
                "name": path.name,
                "bytes": stat.st_size,
            }
        )
        if len(files) >= 2000:
            break
    return {"root": str(VIDEO_ROOT), "files": files}


@app.post("/api/open")
def open_video(request: OpenRequest) -> dict[str, Any]:
    source = _safe_source(request.path)
    try:
        session = _open_video(source)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # The frontend only needs keyframe metadata initially. Individual frame metadata
    # is returned compactly so stepping remains exact for VFR sources.
    return {
        "session_id": session["session_id"],
        "relative_path": session["relative_path"],
        "filename": session["filename"],
        "duration": session["duration"],
        "width": session["width"],
        "height": session["height"],
        "codec": session["codec"],
        "profile": session["profile"],
        "pix_fmt": session["pix_fmt"],
        "bit_depth": session["bit_depth"],
        "avg_fps": session["avg_fps"],
        "nominal_fps": session["nominal_fps"],
        "color_range": session["color_range"],
        "color_space": session["color_space"],
        "color_transfer": session["color_transfer"],
        "color_primaries": session["color_primaries"],
        "frame_count": session["frame_count"],
        "keyframe_indices": session["keyframe_indices"],
        "frames": session["frames"],
    }


@app.get("/api/frame/{session_id}/{frame_index}")
def get_frame(
    session_id: str,
    frame_index: int,
    kind: str = Query(default="preview", pattern="^(preview|thumb)$"),
) -> FileResponse:
    session = _load_session(session_id)
    output = _render_frame(session, frame_index, kind)
    media_type = "image/jpeg" if kind == "thumb" else "image/png"
    return FileResponse(output, media_type=media_type)


@app.post("/api/extract")
def extract_frame(request: ExtractRequest) -> dict[str, Any]:
    session = _load_session(request.session_id)
    output = _extract_png(session, request.frame_index)
    return {
        "filename": output.name,
        "path": str(output),
        "download_url": f"/api/output/{output.name}",
        "bit_depth": 16 if int(session.get("bit_depth") or 8) > 8 else 8,
    }


@app.get("/api/output/{filename}")
def get_output(filename: str) -> FileResponse:
    candidate = (OUTPUT_DIR / filename).resolve()
    try:
        candidate.relative_to(OUTPUT_DIR)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid output filename") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Output file not found")
    return FileResponse(candidate, media_type="image/png", filename=candidate.name)


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
