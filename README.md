# Video HEVC Converter

Docker service that watches your movie/video libraries, transcodes anything that
isn't already HEVC/AV1/VP9 to **HEVC (x265)**, validates the result, then
atomically replaces the original with the same filename. Intel Quick Sync Video
(QSV) acceleration is used when available; otherwise it falls back to software
encoding with `libx265`.

## Hardware acceleration

An Intel GPU with QSV support is optional. When `/dev/dri/renderD128` is
available in the container, the converter first attempts hardware encoding. If
the device is unavailable or hardware encoding fails, it uses CPU-based
`libx265` encoding instead.

## What gets converted vs skipped

| Case                                             | Action  |
| ------------------------------------------------ | ------- |
| Already HEVC / H.265 / AV1 / VP9                 | skip    |
| Raw / log / intermediate codecs (ProRes, DNxHR, CineForm, FFV1, HuffYUV, MJPEG, rawvideo, …) | skip |
| Raw containers (`.braw`, `.r3d`, `.ari`, `.mxf`, `.dng`, `.crm`) | skip |
| Filenames containing `slog`, `vlog`, `flog`, `_log`, `prores`, `master`, … | skip |
| Files smaller than `min_size_bytes` (default 20 MiB) | skip |
| Everything else (H.264, MPEG-2, MPEG-4 Part 2, VC-1, WMV, …) | **transcode → HEVC** |

Full list is in [config/config.yaml](config/config.yaml) — edit to taste.

## Corruption safety

Before the original is replaced, the new file must pass:

1. `ffprobe` parses cleanly and reports ≥ 1 video stream.
2. Audio stream count matches the source.
3. Duration is within `duration_tolerance_seconds` (default 1.5 s).
4. Full `ffmpeg -f null` decode pass with `-xerror` — any decode error, drop it.
5. New file must be at most `max_size_ratio` × original size (default 95%);
   otherwise the transcode is discarded and the original is kept.

Only after all five pass does the atomic swap happen. On failure, the temp file
is deleted, the original is untouched, and the failure is recorded in
`state/converter.db`.

## Filename policy

- Output keeps the **same base name** as the source.
- If the source container natively supports HEVC (`.mp4`, `.mkv`, `.mov`,
  `.m4v`, `.ts`, `.mts`, `.m2ts`, `.webm`) → **same extension**.
- Legacy containers (`.avi`, `.wmv`, `.flv`, `.vob`, `.mpg`, …) are remuxed into
  `.mkv` (configurable via `output.fallback_container`). This is unavoidable
  because AVI/WMV don't cleanly carry HEVC.

## Setup

1. Edit [docker-compose.yml](docker-compose.yml):
  - Replace the `/volume1/...` bind mounts with your actual media paths.
  - For Intel QSV acceleration, find the correct GIDs and update `group_add`:
     ```bash
     getent group render video
     ```
  - For CPU-only encoding, remove the `devices` and `group_add` sections.
2. Edit [config/config.yaml](config/config.yaml) — at minimum, update
   `scan_paths` if you added or renamed mounts.
3. Build and run:
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```

Set `SCAN_INTERVAL=0` in `docker-compose.yml` to run one pass and exit
(useful for cron-style scheduling). Any other value = loop with that sleep in
seconds between scans.

## Tuning quality vs size

In `config/config.yaml`:

- `encoder.global_quality`: **lower = better quality, bigger file**.
  - `20` → visually transparent, ~30% smaller than H.264
  - `23` (default) → excellent, ~50% smaller
  - `26` → mobile-friendly, ~65% smaller
- `encoder.preset`: `veryfast | fast | medium | slow | slower | veryslow`.
  Slower presets generally improve compression but take longer.

## Dry run

Set `runtime.dry_run: true` in the config. The service will scan, classify, and
log the plan for every file — but never encode or replace anything. Great for
verifying your skip rules against the actual library before committing.

## State & logs

- `state/converter.db` — SQLite. One row per file (ok/skipped/failed). Files are
  reprocessed only if their size or mtime changes.
- `logs/converter.log` — rotating log, 10 MiB × 5.

Inspect the DB:
```bash
docker compose exec video-converter sqlite3 /state/converter.db \
  "SELECT status, COUNT(*), SUM(orig_size)/1024/1024 AS orig_mib, SUM(new_size)/1024/1024 AS new_mib FROM processed GROUP BY status;"
```

## Verifying optional hardware acceleration

Once the container is running:
```bash
docker compose exec video-converter intel_gpu_top
```
During a transcode, the **Video** and **Render/3D** engines should show
activity. If they don't, the container isn't seeing the iGPU — recheck
`/dev/dri` passthrough and `group_add` GIDs.
