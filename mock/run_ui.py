"""Local mock of the video-converter web UI.

Run this to preview the UI on Windows/macOS/Linux without a NAS, Docker,
ffmpeg, or an Intel iGPU. All state lives in a temp directory that is
recreated on each launch.

    python mock/run_ui.py

Then open http://127.0.0.1:8080

The mock spins up:
  * The real FastAPI app from app/webui.py
  * A fake encoder thread that cycles jobs through the pipeline stages
    with live-updating speed/time/bitrate/size numbers
  * A fake scanner thread that responds to the "Scan now" button
  * A seeded SQLite state DB so the KPI cards and recent-activity table
    show realistic data immediately
"""
from __future__ import annotations

import os
import random
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Make the real app importable and point it at a throwaway workspace.
# Everything must happen BEFORE we import state/webui/store.
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
APP = HERE.parent / "app"
sys.path.insert(0, str(APP))

WORKSPACE = Path(tempfile.gettempdir()) / "video_converter_mock"
WORKSPACE.mkdir(exist_ok=True)
CONFIG_PATH = WORKSPACE / "config.yaml"
LOG_FILE = WORKSPACE / "converter.log"
DB_PATH = WORKSPACE / "state.db"
WORK_DIR = WORKSPACE / "work"
WORK_DIR.mkdir(exist_ok=True)

# Two real empty folders so the folder add/remove UI has something valid
# to point at on your local machine.
MEDIA = WORKSPACE / "fake_media"
(MEDIA / "volume2" / "Movies").mkdir(parents=True, exist_ok=True)
(MEDIA / "volume2" / "TVShows").mkdir(parents=True, exist_ok=True)
(MEDIA / "volume2" / "Home Videos").mkdir(parents=True, exist_ok=True)

CONFIG_PATH.write_text(f"""\
scan_paths:
  - {(MEDIA / "volume2" / "Movies").as_posix()}
  - {(MEDIA / "volume2" / "TVShows").as_posix()}

video_extensions: [.mp4, .mkv, .mov, .avi, .wmv, .flv, .m4v, .ts, .m2ts, .webm]
skip_codecs: [hevc, h265, av1, vp9]
raw_codecs: [prores, dnxhd, dnxhr, cfhd, ffv1, huffyuv, rawvideo, mjpeg]
raw_extensions: [.braw, .r3d, .ari, .arriraw, .dng, .crm, .mxf]
raw_filename_markers: ["_log", ".log.", "slog", "vlog", "flog", "prores", "master"]
min_size_bytes: 20971520

encoder:
  codec: hevc_qsv
  global_quality: 21
  preset: veryslow
  look_ahead: true
  look_ahead_depth: 40
  allow_10bit: true
  max_bitrate_kbps: 0

output:
  fallback_container: .mkv
  copy_audio: true
  copy_subs: true

validation:
  duration_tolerance_seconds: 1.5
  full_decode: true
  check_stream_counts: true

runtime:
  delete_original: true
  work_dir: {WORK_DIR.as_posix()}
  log_file: {LOG_FILE.as_posix()}
  state_db: {DB_PATH.as_posix()}
  dry_run: false
  stall_timeout_seconds: 300
  stability_check_seconds: 2.0
  sweep_at_time: "03:00"
""", encoding="utf-8")

os.environ["CONFIG_PATH"] = str(CONFIG_PATH)
os.environ["VHC_METADATA_PROVIDER"] = "mock"

import state          # noqa: E402
import webui          # noqa: E402
from store import Store  # noqa: E402

# Point webui at a fake compose file so the Container mounts UI renders as it
# would on the real NAS (where /compose/docker-compose.yml is a bind mount).
MOCK_COMPOSE = WORKSPACE / "docker-compose.yml"
MOCK_COMPOSE.write_text(
    "services:\n"
    "  video-converter:\n"
    "    build: .\n"
    "    volumes:\n"
    "      - ./config:/config\n"
    "      - ./logs:/logs\n"
    "      - ./state:/state\n"
    "      - /var/run/docker.sock:/var/run/docker.sock\n"
    "      - ./docker-compose.yml:/compose/docker-compose.yml:rw\n"
    "      - /volume2:/media/volume2\n"
    "      - ./tmp:/tmp/convert\n",
    encoding="utf-8",
)
webui._COMPOSE_PATH = str(MOCK_COMPOSE)
# Root the mock folder picker at the fake media tree instead of /media.
webui._BROWSE_ROOT = str(MEDIA)


# ---------------------------------------------------------------------------
# Seed the state DB with sample rows.
# ---------------------------------------------------------------------------
Store(str(DB_PATH))  # creates schema

SAMPLE = [
    # (path, status, orig_codec, orig_size, new_size, reason)
    ("/media/Movies/Blade Runner 2049 (2017).mkv",       "ok",      "h264",   12_500_000_000, 5_100_000_000, None),
    ("/media/Movies/Dune Part Two (2024).mkv",           "ok",      "h264",   22_000_000_000, 9_800_000_000, None),
    ("/media/Movies/Interstellar (2014).mp4",            "ok",      "h264",    8_400_000_000, 3_700_000_000, None),
    ("/media/Movies/Arrival (2016).mkv",                 "ok",      "h264",    7_200_000_000, 3_150_000_000, None),
    ("/media/Movies/Oppenheimer (2023).mkv",             "skipped", "hevc",   15_000_000_000, None,          "already efficient codec: hevc"),
    ("/media/Movies/The Batman (2022).mkv",              "skipped", "hevc",   18_000_000_000, None,          "already efficient codec: hevc"),
    ("/media/Movies/Untitled_slog3.mov",                 "skipped", "prores", 40_000_000_000, None,          "filename marker 'slog' suggests raw/log source"),
    ("/media/Movies/Family_camera_master.mov",           "skipped", "prores", 32_000_000_000, None,          "raw/log codec: prores"),
    ("/media/TVShows/Series-A/S01E01.mkv",               "ok",      "h264",    3_200_000_000, 1_400_000_000, None),
    ("/media/TVShows/Series-A/S01E02.mkv",               "ok",      "h264",    3_100_000_000, 1_350_000_000, None),
    ("/media/TVShows/Series-A/S01E03.mkv",               "ok",      "h264",    3_400_000_000, 1_500_000_000, None),
    ("/media/TVShows/Series-A/S01E04.mkv",               "failed",  "h264",    2_900_000_000, None,          "encode: all encoders failed"),
    ("/media/TVShows/Series-B/S02E11.mkv",               "ok",      "h264",    2_700_000_000, 1_200_000_000, None),
    ("/media/TVShows/Series-B/S02E12.mkv",               "ok",      "mpeg4",   4_500_000_000, 1_800_000_000, None),
    ("/media/Movies/Corrupt_test.mp4",                   "failed",  "h264",    1_500_000_000, None,          "validate: duration mismatch: orig=5432.10s new=5401.30s"),
]

now = time.time()
with sqlite3.connect(DB_PATH) as conn:
    for i, (path, status_val, codec, orig, new, reason) in enumerate(SAMPLE):
        ts = now - i * 1800 - random.randint(0, 600)
        conn.execute(
            "INSERT OR REPLACE INTO processed "
            "(path, size, mtime, status, reason, orig_codec, new_codec, "
            " orig_size, new_size, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                path, orig, ts, status_val, reason, codec,
                "hevc" if status_val == "ok" else None,
                orig, new, ts,
            ),
        )


# ---------------------------------------------------------------------------
# Seed a realistic log file.
# ---------------------------------------------------------------------------
_log_templates = [
    "scan starting — paths: ['/media/Movies', '/media/TVShows']",
    "PLAN  /media/Movies/Blade Runner 2049 (2017).mkv  codec=h264 3840x2160 8940.1s 10-bit",
    "QSV full-HW encode: Dune Part Two (2024).mkv",
    "progress: speed=1.47x time=00:12:34.10 bitrate=4210.5kbits/s",
    "encode done: speed=1.51x time=02:29:04.10 size=9800000000",
    "DONE  Interstellar (2014).mp4 -> Interstellar (2014).mp4  8014.4 MiB -> 3527.1 MiB (44%)",
    "SKIP  /media/Movies/Oppenheimer (2023).mkv  (already efficient codec: hevc)",
    "SKIP  /media/Movies/Untitled_slog3.mov  (filename marker 'slog' suggests raw/log source)",
    "scan complete — 42 file(s) examined",
    "Intel /dev/dri/renderD128 present — QSV path available",
    "web UI on http://0.0.0.0:8080 (auth: OPEN — no password)",
]
with LOG_FILE.open("w", encoding="utf-8") as f:
    for i in range(40):
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - (40 - i) * 90))
        level = random.choice(["INFO", "INFO", "INFO", "INFO", "WARNING"])
        f.write(f"{t} {level} converter: {random.choice(_log_templates)}\n")


# ---------------------------------------------------------------------------
# Fake encoder loop — walks jobs through the pipeline with live progress.
# ---------------------------------------------------------------------------
JOB_QUEUE = [
    # (display path, encoder-tier label, source duration seconds)
    ("/media/Movies/Nosferatu (2024).mkv",       "QSV full-HW",     7200),
    ("/media/Movies/Poor Things (2023).mkv",     "QSV full-HW",     8340),
    ("/media/TVShows/Series-A/S01E05.mkv",       "QSV full-HW",     2680),
    ("/media/TVShows/Series-B/S02E13.mkv",       "QSV encode-only", 2690),
    ("/media/Movies/Wicked (2024).mkv",          "QSV full-HW",     9560),
]


# Seed pending list so the Convert button lights up immediately.
def _sample_pending() -> list[dict]:
    return [
        {"path": "/media/Movies/Nosferatu (2024).mkv",     "codec": "h264",  "width": 3840, "height": 2160, "duration": 7200,  "size": 18_000_000_000, "bit_depth": 10},
        {"path": "/media/Movies/Poor Things (2023).mkv",   "codec": "h264",  "width": 3840, "height": 2160, "duration": 8340,  "size": 21_500_000_000, "bit_depth": 10},
        {"path": "/media/Movies/Furiosa (2024).mkv",       "codec": "h264",  "width": 1920, "height": 1080, "duration": 8880,  "size": 9_800_000_000,  "bit_depth": 8},
        {"path": "/media/TVShows/Series-A/S01E05.mkv",     "codec": "h264",  "width": 1920, "height": 1080, "duration": 2680,  "size": 3_100_000_000,  "bit_depth": 8},
        {"path": "/media/TVShows/Series-A/S01E06.mkv",     "codec": "h264",  "width": 1920, "height": 1080, "duration": 2712,  "size": 3_200_000_000,  "bit_depth": 8},
        {"path": "/media/TVShows/Series-B/S02E13.mkv",     "codec": "mpeg4", "width": 1280, "height": 720,  "duration": 2690,  "size": 1_800_000_000,  "bit_depth": 8},
        {"path": "/media/Movies/Wicked (2024).mkv",        "codec": "h264",  "width": 3840, "height": 2160, "duration": 9560,  "size": 23_400_000_000, "bit_depth": 10},
        # Extra rows so the Pending/Up-next scroll boxes actually overflow
        # in the mock demo. Real libraries usually have hundreds.
        {"path": "/media/Movies/The Fall Guy (2024).mkv",           "codec": "h264",  "width": 3840, "height": 2160, "duration": 7860,  "size": 17_200_000_000, "bit_depth": 10},
        {"path": "/media/Movies/Kingdom of the Planet of the Apes (2024).mkv", "codec": "h264",  "width": 3840, "height": 2160, "duration": 8520,  "size": 19_800_000_000, "bit_depth": 10},
        {"path": "/media/Movies/Twisters (2024).mkv",              "codec": "h264",  "width": 1920, "height": 1080, "duration": 7080,  "size": 8_400_000_000,  "bit_depth": 8},
        {"path": "/media/Movies/Deadpool and Wolverine (2024).mkv","codec": "h264",  "width": 3840, "height": 2160, "duration": 7620,  "size": 18_600_000_000, "bit_depth": 10},
        {"path": "/media/Movies/A Quiet Place Day One (2024).mkv", "codec": "h264",  "width": 3840, "height": 2160, "duration": 5820,  "size": 12_400_000_000, "bit_depth": 10},
        {"path": "/media/TVShows/Series-A/S01E07.mkv",             "codec": "h264",  "width": 1920, "height": 1080, "duration": 2645,  "size": 3_150_000_000,  "bit_depth": 8},
        {"path": "/media/TVShows/Series-A/S01E08.mkv",             "codec": "h264",  "width": 1920, "height": 1080, "duration": 2698,  "size": 3_180_000_000,  "bit_depth": 8},
        {"path": "/media/TVShows/Series-B/S02E14.mkv",             "codec": "mpeg4", "width": 1280, "height": 720,  "duration": 2710,  "size": 1_820_000_000,  "bit_depth": 8},
        {"path": "/media/TVShows/Series-B/S02E15.mkv",             "codec": "mpeg4", "width": 1280, "height": 720,  "duration": 2680,  "size": 1_790_000_000,  "bit_depth": 8},
        {"path": "/media/Movies/Godzilla x Kong The New Empire (2024).mkv", "codec": "h264", "width": 3840, "height": 2160, "duration": 6720, "size": 15_800_000_000, "bit_depth": 10},
    ]


# Seed all-media list: pending items (marked needs_convert) plus a bunch of
# already-HEVC files so the "X of Y in library" indicator has real numbers.
def _sample_all_media() -> list[dict]:
    # Include a few deliberately messy filenames so the Rename tab has
    # non-trivial suggestions to show.
    already_hevc = [
        {"path": "/media/Movies/Dune Part Two (2024).mkv",  "codec": "hevc", "width": 3840, "height": 2160, "duration": 9840,  "size": 14_200_000_000, "bit_depth": 10},
        {"path": "/media/Movies/Oppenheimer (2023).mkv",    "codec": "hevc", "width": 3840, "height": 2160, "duration": 10800, "size": 15_600_000_000, "bit_depth": 10},
        {"path": "/media/Movies/Inside Out 2 (2024).mkv",   "codec": "hevc", "width": 1920, "height": 1080, "duration": 5760,  "size": 4_100_000_000,  "bit_depth": 8},
        {"path": "/media/Movies/The.Dark.Knight.2008.EXTENDED.2160p.UHD.BluRay.x265.10bit.HDR.mkv", "codec": "hevc", "width": 3840, "height": 2160, "duration": 10380, "size": 14_800_000_000, "bit_depth": 10},
        {"path": "/media/Movies/messy_filename_no_year.mkv", "codec": "hevc", "width": 1920, "height": 1080, "duration": 5400, "size": 3_800_000_000, "bit_depth": 8},
        {"path": "/media/TVShows/Series-A/Show.Name.S01E01.Pilot.1080p.WEB-DL.mkv", "codec": "hevc", "width": 1920, "height": 1080, "duration": 2650,  "size": 1_400_000_000,  "bit_depth": 8},
        {"path": "/media/TVShows/Series-A/Show.Name.S01E02.The.Second.One.1080p.WEB-DL.mkv", "codec": "hevc", "width": 1920, "height": 1080, "duration": 2670,  "size": 1_420_000_000,  "bit_depth": 8},
        {"path": "/media/TVShows/Series-A/Show.Name.S01E03.mkv", "codec": "hevc", "width": 1920, "height": 1080, "duration": 2680,  "size": 1_450_000_000,  "bit_depth": 8},
        # Drag-and-drop demo: a show with dirty season folders + Vietnamese
        # bare-episode filenames. Drag one of these into a different season
        # folder to see the Rename tab re-parse the season number live.
        {"path": "/media/TVShows/Money.Heist/ss1/Tập 3.mkv", "codec": "hevc", "width": 1920, "height": 1080, "duration": 2700, "size": 1_500_000_000, "bit_depth": 8},
        {"path": "/media/TVShows/Money.Heist/ss1/Tập 5.mkv", "codec": "hevc", "width": 1920, "height": 1080, "duration": 2700, "size": 1_510_000_000, "bit_depth": 8},
        {"path": "/media/TVShows/Money.Heist/ss2/Tập 1.mkv", "codec": "hevc", "width": 1920, "height": 1080, "duration": 2700, "size": 1_520_000_000, "bit_depth": 8},
    ]
    items = []
    for it in _sample_pending():
        items.append({**it, "needs_convert": True, "skip_reason": None})
    for it in already_hevc:
        items.append({**it, "needs_convert": False, "skip_reason": "already hevc"})
    return items


state.set_pending(_sample_pending())
state.set_all_media(_sample_all_media())


def _fake_encoder() -> None:
    """Runs when the user clicks Convert in the UI.

    Drains the pending list one file at a time, walking each through
    probing → encoding → validating → replacing with live progress.
    """
    while True:
        # Wait for a convert action from the UI.
        while True:
            action = state.wait_for_action(3600)
            if action == "convert":
                break
            if action == "scan":
                state.scan_started()
                time.sleep(1.2)  # enumeration phase (indeterminate)
                total = random.randint(120, 180)
                state.scan_probing(total)
                for _ in range(total):
                    time.sleep(0.05)  # ~50 ms per "probe" so the bar visibly fills
                    state.scan_probe_tick()
                state.set_pending(_sample_pending())
                state.set_all_media(_sample_all_media())
                state.scan_ended(total)

        items = state.get_pending()
        if not items:
            continue
        state.clear_stop()
        stopped = False
        for item in items:
            if state.stop_requested():
                stopped = True
                break
            path = item["path"]
            encoder = random.choice(["QSV full-HW", "QSV full-HW", "QSV encode-only"])
            duration_s = item.get("duration") or 3600

            state.set_current(path=path, stage="probing",
                              started_at=time.time(), encoder=None,
                              duration=duration_s, progress={})
            time.sleep(2)
            if state.stop_requested():
                state.clear_current(); stopped = True; break

            state.set_current(stage="encoding", encoder=encoder)
            wall = 20
            aborted = False
            for i in range(wall):
                if state.stop_requested():
                    aborted = True
                    break
                t = duration_s * (i + 1) / wall
                h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
                state.set_progress({
                    "speed": f"{random.uniform(1.20, 1.55):.2f}x",
                    "out_time": f"{h:02d}:{m:02d}:{s:06.3f}",
                    "bitrate": f"{random.uniform(3800, 4600):.1f}kbits/s",
                    "total_size": str(int(500_000 * t * random.uniform(0.9, 1.1))),
                    "progress": "continue",
                })
                time.sleep(1)
            if aborted:
                state.clear_current(); stopped = True; break

            # Validation: quick simulated decode with live out_time so the
            # progress bar animates too.
            state.set_current(stage="validating", progress={})
            val_ticks = 6
            for i in range(val_ticks):
                if state.stop_requested():
                    aborted = True; break
                t = duration_s * (i + 1) / val_ticks
                h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
                state.set_progress({
                    "speed": f"{random.uniform(8.0, 14.0):.2f}x",
                    "out_time": f"{h:02d}:{m:02d}:{s:06.3f}",
                    "progress": "continue",
                })
                time.sleep(0.5)
            if aborted:
                state.clear_current(); stopped = True; break
            state.set_current(stage="replacing");  time.sleep(1)

            orig = int(item.get("size") or 4_000_000_000)
            new = int(orig * random.uniform(0.35, 0.5))
            with sqlite3.connect(DB_PATH) as c:
                c.execute(
                    "INSERT OR REPLACE INTO processed (path, size, mtime, "
                    "status, reason, orig_codec, new_codec, orig_size, "
                    "new_size, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (path, orig, time.time(), "ok", None,
                     item.get("codec", "h264"), "hevc", orig, new, time.time()),
                )
            state.remove_pending(path)
            state.clear_current()
            time.sleep(1)
        if stopped:
            state.clear_stop()


def _fake_scanner() -> None:
    # Unused now — the encoder thread handles scan actions too. Kept as a
    # daemon so a legacy start doesn't crash. Sits idle forever.
    while True:
        time.sleep(3600)


threading.Thread(target=_fake_encoder, daemon=True).start()
threading.Thread(target=_fake_scanner, daemon=True).start()
state.scan_ended(42)


# ---------------------------------------------------------------------------
# Serve.
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("UI_PORT", "8080"))
print()
print("=" * 62)
print(" Video HEVC Converter — Local UI Preview")
print("=" * 62)
print(f"  Workspace : {WORKSPACE}")
print(f"  Config    : {CONFIG_PATH}")
print(f"  State DB  : {DB_PATH}")
print(f"  Log file  : {LOG_FILE}")
print()
print(f"  Open in a browser: http://127.0.0.1:{PORT}")
print()
print("  Notes:")
print("   * The Pending panel is pre-seeded — hit Convert to watch each file")
print("     walk through probing → encoding → validating → replacing (live).")
print("   * Click 'Scan now' to refill the Pending list (fake).")
print("   * Save Settings — the temp config is rewritten; comments preserved.")
print("   * Folder add validates against a real directory on your machine")
print(f"     (try {(MEDIA / 'Movies').as_posix()} or any local folder)")
print()
print("  Press Ctrl+C to stop.")
print("=" * 62)
print()

webui.serve(str(CONFIG_PATH), host="127.0.0.1", port=PORT)
