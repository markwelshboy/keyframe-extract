# keyframe-extract

A local, image-first web UI for finding and exporting exact still frames from HEVC/H.265 and other ffmpeg-readable video files.

The browser is deliberately **not** responsible for decoding HEVC. `ffprobe` indexes the source frames and true codec keyframes, and `ffmpeg` decodes individual stills on demand. This keeps the UI reliable across browsers and makes the final export independent of browser codec support.

## What it does

- Scans a local video directory for MP4, MOV, MKV, M4V, HEVC/H.265, TS/MTS/M2TS files.
- Uses `ffprobe` to index every presented video frame and its timestamp.
- Builds a contact sheet from frames where ffprobe reports `key_frame=1`.
- Click a keyframe, then step through decoded frames one at a time.
- `←` / `→`: step one frame.
- `Shift` + `←` / `→`: step ten frames.
- `[` / `]`: previous / next keyframe.
- Shows a small neighboring-frame strip around the current selection.
- Exports the selected source frame directly to full-resolution PNG.
- Uses 16-bit RGB PNG for sources reported as greater than 8-bit; ordinary 8-bit sources export as RGB24 PNG.
- Caches the ffprobe frame index and browser preview images, but **never exports from the preview cache**.

The contact-sheet thumbnails are small JPEGs purely to make browsing cheap. The large selected preview is a scaled PNG. Final exports are always freshly decoded from the original source video.

## Docker Compose

```bash
git clone https://github.com/markwelshboy/keyframe-extract.git
cd keyframe-extract
mkdir -p videos output .cache
```

With the default layout, put source files under `./videos`, then run:

```bash
docker compose up --build
```

Open:

```text
http://localhost:8088
```

Extracted PNG files are written to `./output` and are also downloaded by the browser when you click **Extract full-resolution PNG**.

The Compose defaults are:

```text
./videos  -> /videos   (read-only source)
./output  -> /output
./.cache  -> /cache
port 8088 -> container port 8000
```

You do **not** need to move or copy large videos into the repository. Point Compose at any existing source directory:

```bash
VIDEO_DIR=/mnt/media/training-videos docker compose up --build
```

You can also override the PNG output directory, cache directory, or local port:

```bash
VIDEO_DIR=/mnt/media/training-videos \
PNG_OUTPUT_DIR=/mnt/media/extracted-pngs \
KEYFRAME_CACHE_DIR=/tmp/keyframe-extract-cache \
KEYFRAME_PORT=8099 \
docker compose up --build
```

The source mount remains read-only inside the container.

## Native run

Requirements:

- Python 3.11+
- ffmpeg / ffprobe on `PATH`

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p videos output .cache
uvicorn app:app --host 127.0.0.1 --port 8088
```

You can point the app at different directories with environment variables:

```bash
VIDEO_ROOT=/path/to/videos \
OUTPUT_DIR=/path/to/png-output \
CACHE_DIR=/path/to/cache \
uvicorn app:app --host 127.0.0.1 --port 8088
```

## Frame accuracy

The app does not assume constant FPS. `ffprobe` records `best_effort_timestamp_time` for each decoded frame, so VFR phone footage can still be navigated by actual presented frames rather than by `timestamp × nominal_fps` arithmetic.

The contact sheet uses ffprobe's `key_frame` flag rather than treating every `pict_type=I` frame as a seek/key frame.

When exporting, ffmpeg seeks to the selected presentation timestamp, decodes from the original source, and writes a PNG. There is no intermediate JPEG export or browser re-encode.

## Notes on high-bit-depth HEVC

For a source that reports more than 8 bits per component, the exporter asks ffmpeg for `rgb48be`, producing a 16-bit-per-channel PNG. This avoids silently reducing a 10/12-bit source to an 8-bit PNG during extraction.

HEVC itself is normally a lossy source format, so PNG cannot restore information that was already lost during HEVC encoding. The important point is that the extractor introduces no additional lossy image compression step.
