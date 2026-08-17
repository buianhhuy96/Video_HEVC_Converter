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
| 4:2:2 or 4:4:4 chroma sources (would downsample to 4:2:0) | skip |
| Sources deeper than 10-bit (no lossless HEVC profile in this pipeline)   | skip |
| Files smaller than `min_size_bytes` (default 20 MiB) | skip |
| Everything else (H.264, MPEG-2, MPEG-4 Part 2, VC-1, WMV, …) | **transcode → HEVC** |

Full list is in [config/config.yaml](config/config.yaml) — edit to taste.

## Corruption safety

Before the original is replaced, the new file must pass every check below:

1. `ffprobe` parses cleanly and reports ≥ 1 video stream.
2. Output codec is `hevc` (silent codec passthrough is rejected).
3. Bit depth is not downgraded; width and height match the source.
4. Video, audio, and subtitle stream counts match the source.
5. Duration is within `duration_tolerance_seconds` (default 1.5 s).
6. Full `ffmpeg -f null` decode pass with `-xerror` — any decode error, drop it.
7. Source file's size and mtime are unchanged since the encode started — a file
   that was modified during encoding is never overwritten.

Only after all pass does the atomic swap happen. On failure, the temp file
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
   - Replace `/volume2` in the bind mount with the volume that holds your
     media (or add more mount lines for extra volumes). After first start,
     you can also edit media mounts from the web UI's **Container** card.
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

### Editing container settings from the UI

The web UI's **Container** card (Setup tab) shows the current timezone, port,
auth, PUID/PGID, and iGPU status. Media bind mounts are editable in-place;
clicking **Save & Restart app** rewrites [docker-compose.yml](docker-compose.yml)
and recreates the container with the new mounts.

This flow relies on two mounts already declared in the shipping
`docker-compose.yml`:

- `/var/run/docker.sock:/var/run/docker.sock` — gives the container access
  to the Docker daemon on the host. This is the standard mechanism used by
  Portainer / Yacht / etc. Anyone who can reach the web UI can, in principle,
  control Docker on your NAS through it — keep the UI on a LAN interface
  (or protected with `UI_PASSWORD`) if that matters to you.
- `./docker-compose.yml:/compose/docker-compose.yml:rw` — gives the
  container write access to its own compose file.

Remove either mount to disable the feature; the Container card will fall back
to read-only display and print instructions to edit the file on the host.

Set `runtime.sweep_at_time: "03:00"` in [config/config.yaml](config/config.yaml)
to run one full sweep (scan + convert) every day at that local time (container
timezone follows the `TZ` variable in `docker-compose.yml`). Set it to `""` to
disable the automatic sweep and rely only on the UI's **Scan now** and
**Convert queued files** buttons. A sweep always runs at container startup
either way.

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
