"""FastAPI + HTMX control panel for the video converter.

Runs in a daemon thread alongside the scanner. Uses HTTP Basic auth when
UI_PASSWORD is set; otherwise open (intended for LAN-only exposure).
"""
from __future__ import annotations

import html
import logging
import math
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import state
from config import Config, load_config, save_config

log = logging.getLogger("webui")

app = FastAPI(title="Video HEVC Converter", docs_url=None, redoc_url=None)
_security = HTTPBasic(auto_error=False)
_config_path: str = "/config/config.yaml"

PRESETS = ["veryfast", "fast", "medium", "slow", "slower", "veryslow"]
SHARPEN_NAMES = ["Off", "Very light", "Light", "Moderate", "Strong", "Very strong"]

_COMPOSE_PATH = "/compose/docker-compose.yml"
_COMPOSE_SERVICE = "video-converter"

# Root the folder-picker walks. Everything selectable must be under this path.
# Overridden by the mock UI to point at a local fake tree.
_BROWSE_ROOT = "/media"

try:
    from ruamel.yaml import YAML  # roundtrip preserves comments
    _COMPOSE_YAML = YAML()
    _COMPOSE_YAML.preserve_quotes = True
    _COMPOSE_YAML.indent(mapping=2, sequence=4, offset=2)
    _COMPOSE_YAML.width = 4096  # don't wrap long lines
except ImportError:
    _COMPOSE_YAML = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def _check_auth(
    creds: Annotated[HTTPBasicCredentials | None, Depends(_security)],
) -> None:
    pw = os.environ.get("UI_PASSWORD")
    if not pw:
        return
    if creds is None or not secrets.compare_digest(
        creds.password.encode(), pw.encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Auth required",
            headers={"WWW-Authenticate": 'Basic realm="video-converter"'},
        )


Auth = Annotated[None, Depends(_check_auth)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load() -> Config:
    return load_config(_config_path)


def _fmt_bytes(n: int | float | None) -> str:
    if not n:
        return "—"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return "never"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_age(ts: float | None) -> str:
    if not ts:
        return ""
    s = int(time.time() - ts)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _db_stats(db_path: str) -> dict:
    out = {
        "ok": 0, "skipped": 0, "failed": 0,
        "orig_bytes": 0, "new_bytes": 0, "bytes_saved": 0,
    }
    try:
        with sqlite3.connect(db_path) as c:
            for st, cnt, orig, new in c.execute(
                "SELECT status, COUNT(*), COALESCE(SUM(orig_size),0), "
                "COALESCE(SUM(new_size),0) FROM processed GROUP BY status"
            ):
                if st in out:
                    out[st] = cnt
                if st == "ok":
                    out["orig_bytes"] = orig
                    out["new_bytes"] = new
    except sqlite3.OperationalError:
        pass
    out["bytes_saved"] = max(out["orig_bytes"] - out["new_bytes"], 0)
    return out


def _recent(db_path: str, limit: int = 15) -> list[dict]:
    try:
        with sqlite3.connect(db_path) as c:
            rows = c.execute(
                "SELECT path, status, reason, orig_codec, orig_size, new_size, ts "
                "FROM processed ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "path": r[0], "status": r[1], "reason": r[2] or "",
            "orig_codec": r[3] or "", "orig_size": r[4] or 0,
            "new_size": r[5] or 0, "ts": r[6],
        }
        for r in rows
    ]


def _tail(path: str, n: int) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            read = min(size, 64 * 1024)
            f.seek(-read, 2)
            data = f.read()
        return data.decode("utf-8", errors="replace").splitlines()[-n:]
    except (OSError, ValueError):
        return []


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _parse_ffmpeg_time(s: str | None) -> float | None:
    """Turn '00:12:34.500' into seconds, or None if unparseable."""
    if not s or s == "N/A":
        return None
    try:
        h, m, sec = s.split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec)
    except (ValueError, AttributeError):
        return None


def _fmt_elapsed(secs: float) -> str:
    if secs is None or secs < 0:
        return "—"
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs // 3600}h {(secs % 3600) // 60}m"


def _container_info() -> dict:
    """Runtime introspection of settings that come from docker-compose.yml."""
    info = {
        "timezone": os.environ.get("TZ") or "system default",
        "ui_port": os.environ.get("UI_PORT", "8080"),
        "ui_auth": "password required" if os.environ.get("UI_PASSWORD") else "open (no password)",
        "puid": os.environ.get("PUID", "unset"),
        "pgid": os.environ.get("PGID", "unset"),
        "qsv": ("present" if Path("/dev/dri/renderD128").exists() else "not detected"),
        "media_mounts": [],
    }
    media_root = Path(_BROWSE_ROOT)
    if media_root.is_dir():
        try:
            info["media_mounts"] = sorted(
                p.as_posix() for p in media_root.iterdir() if p.is_dir()
            )
        except OSError:
            pass
    return info


def _can_edit_compose() -> bool:
    return (
        _COMPOSE_YAML is not None
        and Path(_COMPOSE_PATH).is_file()
        and os.access(_COMPOSE_PATH, os.W_OK)
    )


def _can_docker_compose() -> bool:
    return (
        _can_edit_compose()
        and Path("/var/run/docker.sock").exists()
        and shutil.which("docker") is not None
    )


def _load_compose():
    if _COMPOSE_YAML is None or not Path(_COMPOSE_PATH).is_file():
        return None
    with open(_COMPOSE_PATH, encoding="utf-8") as f:
        return _COMPOSE_YAML.load(f) or {}


def _save_compose(doc) -> None:
    tmp = _COMPOSE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _COMPOSE_YAML.dump(doc, f)
    os.replace(tmp, _COMPOSE_PATH)


def _parse_volume(entry: str) -> tuple[str, str, str]:
    """Split a compose volume entry into (host, container, mode)."""
    parts = str(entry).split(":")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return entry, "", ""


def _media_mounts_from_compose() -> list[dict]:
    """Return list of {host, container} for volumes whose container path is /media."""
    doc = _load_compose()
    if not doc:
        return []
    svc = doc.get("services", {}).get(_COMPOSE_SERVICE, {}) or {}
    volumes = svc.get("volumes", []) or []
    result = []
    for v in volumes:
        host, container, _ = _parse_volume(v)
        if container == "/media" or container.startswith("/media/"):
            result.append({"host": host, "container": container})
    return result


def _fmt_hms(secs: float | None) -> str:
    if not secs:
        return "—"
    secs = int(secs)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# HTML rendering — tabs
# ---------------------------------------------------------------------------
def _render_page(cfg: Config) -> str:
    e, o, r = cfg.encoder, cfg.output, cfg.runtime
    preset_idx = PRESETS.index(e.preset) if e.preset in PRESETS else PRESETS.index("veryslow")
    # Quality slider works in percent (higher = better) but submits CRF.
    # CRF range is 18 (pristine) to 30 (small); 100% = 18, 0% = 30.
    quality_pct = round((30 - e.global_quality) * 100 / 12)

    return f"""<!DOCTYPE html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Video HEVC Converter</title>
  <link rel='icon' type='image/svg+xml' href="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 128 128'><rect width='128' height='128' rx='20' fill='%230f172a'/><rect x='16' y='28' width='96' height='72' rx='8' fill='%231e293b' stroke='%23334155' stroke-width='2'/><circle cx='26' cy='42' r='3' fill='%2322d3ee'/><circle cx='26' cy='64' r='3' fill='%2322d3ee'/><circle cx='26' cy='86' r='3' fill='%2322d3ee'/><circle cx='102' cy='42' r='3' fill='%2322d3ee'/><circle cx='102' cy='64' r='3' fill='%2322d3ee'/><circle cx='102' cy='86' r='3' fill='%2322d3ee'/><path d='M52 42 L84 64 L52 86 Z' fill='%2322d3ee'/></svg>">
  <script src='https://unpkg.com/htmx.org@1.9.12'></script>
  <script src='https://cdn.tailwindcss.com'></script>
  <style>
    body {{ background:#0f172a; color:#e2e8f0; }}
    .card {{ background:#1e293b; border:1px solid #334155; border-radius:10px; padding:20px; }}
    .btn {{ padding:8px 14px; border-radius:6px; font-weight:500; }}
    .btn-primary {{ background:#2563eb; }} .btn-primary:hover {{ background:#3b82f6; }}
    .btn-danger  {{ background:#b91c1c; }} .btn-danger:hover  {{ background:#dc2626; }}
    .btn-ghost   {{ background:#334155; }} .btn-ghost:hover   {{ background:#475569; }}
    input[type=text], input[type=number], select {{
      background:#0f172a; border:1px solid #475569; color:#e2e8f0;
      border-radius:6px; padding:6px 10px;
    }}
    input:focus, select:focus {{ outline:2px solid #2563eb; }}
    .badge {{ font-size:11px; padding:2px 8px; border-radius:9999px; font-weight:600; }}
    .b-ok {{ background:#065f46; color:#a7f3d0; }}
    .b-skip {{ background:#374151; color:#d1d5db; }}
    .b-fail {{ background:#7f1d1d; color:#fecaca; }}
    /* Tabs */
    .tab {{
      padding:10px 22px; border:none; background:transparent; cursor:pointer;
      color:#94a3b8; font-weight:500; font-size:15px;
      border-bottom:2px solid transparent; margin-bottom:-1px;
      transition: color .15s, border-color .15s, background .15s;
    }}
    .tab:hover {{ color:#e2e8f0; background:#1e293b; }}
    .tab-active {{ color:#e2e8f0; border-bottom-color:#2563eb; }}
    .tab-content.hidden {{ display:none; }}
    /* Progress bar */
    .progress-track {{
      width:100%; height:14px; background:#0f172a; border-radius:9999px;
      overflow:hidden; border:1px solid #334155;
    }}
    .progress-fill {{
      height:100%; background:linear-gradient(90deg, #10b981, #22d3ee);
      transition: width .6s ease-out;
    }}
    /* Sliders */
    .vhc-slider {{
      -webkit-appearance: none; appearance: none;
      height: 4px; background: #475569; border-radius: 2px;
      outline: none; width: 100%; cursor: pointer;
    }}
    .vhc-slider::-webkit-slider-thumb {{
      -webkit-appearance: none;
      width: 12px; height: 12px; border-radius: 3px;
      background: #e2e8f0; border: 1px solid #94a3b8; cursor: pointer;
    }}
    .vhc-slider::-moz-range-thumb {{
      width: 12px; height: 12px; border-radius: 3px;
      background: #e2e8f0; border: 1px solid #94a3b8; cursor: pointer;
    }}
    .vhc-slider:focus::-webkit-slider-thumb {{ background: #ffffff; }}
    .vhc-slider:focus::-moz-range-thumb {{ background: #ffffff; }}
  </style>
</head>
<body class='p-6'>
  <div class='max-w-6xl mx-auto'>
    <header class='mb-4'>
      <h1 class='text-2xl font-bold'>Video HEVC Converter</h1>
    </header>

    <div id='activity'
         hx-get='/api/activity' hx-trigger='load, every 2s'
         hx-swap='innerHTML'></div>

    <nav class='tabs-nav flex gap-1 border-b border-slate-700 mb-6'>
      <button data-tab='setup'   class='tab tab-active'>Setup</button>
      <button data-tab='convert' class='tab'>Convert</button>
      <button data-tab='status'  class='tab'>Status</button>
    </nav>

    <!-- ==================== TAB 1: SETUP ==================== -->
    <section id='tab-setup' class='tab-content space-y-6'>
      <div class='flex items-center gap-3 flex-wrap'>
        <button class='btn btn-primary' hx-post='/api/scan' hx-swap='none'>Scan now</button>
        <button type='button' class='btn btn-ghost'
                onclick='vhcOpenFileBrowser()'>Convert one file…</button>
        <span class='text-slate-400 text-sm'>
          Scan populates the Pending list. "Convert one file…" queues a single
          file and starts encoding immediately — useful for testing settings.
        </span>
      </div>

      <section class='card'>
        <div id='scan-folders' hx-get='/api/scan_folders' hx-trigger='load'>…</div>
      </section>

      <section class='card'>
        <h2 class='font-semibold mb-3 text-lg'>Pending conversion</h2>
        <div id='pending' hx-get='/api/pending' hx-trigger='load, every 5s'>…</div>
      </section>

      <div class='grid md:grid-cols-1 gap-6'>
        <section class='card'>
          <h2 class='font-semibold mb-3 text-lg'>Settings</h2>
          <form method='post' action='/api/settings' class='space-y-4'>
            <div>
              <div class='flex items-baseline justify-between mb-1'>
                <label class='text-sm'>Quality <span class='text-slate-400'>(higher = bigger file)</span></label>
                <span id='vhc-quality-val' class='font-mono text-slate-100 text-sm'>CRF {e.global_quality}</span>
              </div>
              <input type='range' min='0' max='100' step='1'
                     value='{quality_pct}' class='vhc-slider'
                     oninput='vhcQualityUpdate(this)'>
              <input type='hidden' name='global_quality' id='vhc-quality-crf' value='{e.global_quality}'>
              <div class='flex justify-between text-[10px] text-slate-500 mt-1'>
                <span>smaller file</span>
                <span>higher quality</span>
              </div>
            </div>
            <div>
              <div class='flex items-baseline justify-between mb-1'>
                <label class='text-sm'>Preset <span class='text-slate-400'>(slower = better compression)</span></label>
                <span id='vhc-preset-val' class='font-mono text-slate-100 text-sm'>{e.preset}</span>
              </div>
              <input type='range' min='0' max='{len(PRESETS) - 1}' step='1'
                     value='{preset_idx}' class='vhc-slider'
                     oninput="vhcPresetUpdate(this)">
              <input type='hidden' name='preset' id='vhc-preset-hidden' value='{e.preset}'>
              <div class='flex justify-between text-[10px] text-slate-500 mt-1'>
                <span>{PRESETS[0]}</span>
                <span>{PRESETS[-1]}</span>
              </div>
            </div>
            <div>
              <div class='flex items-baseline justify-between mb-1'>
                <label class='text-sm'>Sharpen <span class='text-slate-400'>(compensate encoder smoothing)</span></label>
                <span id='vhc-sharpen-val' class='font-mono text-slate-100 text-sm'>{SHARPEN_NAMES[e.sharpen]}</span>
              </div>
              <input type='range' min='0' max='{len(SHARPEN_NAMES) - 1}' step='1'
                     value='{e.sharpen}' class='vhc-slider'
                     oninput='vhcSharpenUpdate(this)'>
              <input type='hidden' name='sharpen' id='vhc-sharpen-hidden' value='{e.sharpen}'>
              <div class='flex justify-between text-[10px] text-slate-500 mt-1'>
                <span>{SHARPEN_NAMES[0]}</span>
                <span>{SHARPEN_NAMES[-1]}</span>
              </div>
              <p class='text-[11px] text-slate-500 mt-1'>
                Adds an <code>unsharp</code> pass before encoding. Recovers
                apparent detail lost to QSV smoothing; too much causes edge
                halos. QSV full-HW mode is disabled while sharpening is on.
              </p>
            </div>
            <script>
              (function() {{
                const PRESET_NAMES = {PRESETS!r};
                const SHARPEN_NAMES = {SHARPEN_NAMES!r};
                window.vhcPresetUpdate = function(el) {{
                  const name = PRESET_NAMES[parseInt(el.value)];
                  document.getElementById('vhc-preset-val').textContent = name;
                  document.getElementById('vhc-preset-hidden').value = name;
                }};
                window.vhcQualityUpdate = function(el) {{
                  const pct = parseInt(el.value);
                  const crf = Math.round(30 - pct * 12 / 100);
                  document.getElementById('vhc-quality-val').textContent = 'CRF ' + crf;
                  document.getElementById('vhc-quality-crf').value = crf;
                }};
                window.vhcSharpenUpdate = function(el) {{
                  const idx = parseInt(el.value);
                  document.getElementById('vhc-sharpen-val').textContent = SHARPEN_NAMES[idx];
                  document.getElementById('vhc-sharpen-hidden').value = idx;
                }};
              }})();
            </script>
            <div>
              <label class='block text-sm mb-1'>
                Daily sweep time <span class='text-slate-400'>(HH:MM 24-hour local — empty = manual only)</span>
              </label>
              <input type='text' name='sweep_at_time' pattern='^([01]\d|2[0-3]):[0-5]\d$|^$'
                     value='{r.sweep_at_time}' placeholder='03:00' class='w-32'>
            </div>
            <label class='flex items-center gap-2 text-sm'>
              <input type='checkbox' name='delete_original'
                     {"checked" if r.delete_original else ""}>
              Overwrite original after successful validation
            </label>
            <label class='flex items-center gap-2 text-sm'>
              <input type='checkbox' name='dry_run' {"checked" if r.dry_run else ""}>
              Dry run (analyse only, no encoding)
            </label>
            <p class='text-xs text-slate-400'>
              A background sweep (scan + convert) runs daily at
              <b>{r.sweep_at_time or 'never (manual only)'}</b>. Use <b>Scan now</b> or
              <b>Convert queued files</b> for one-shot manual runs.
            </p>
            <div class='pt-2'>
              <button class='btn btn-primary'>Save settings</button>
              <span class='text-xs text-slate-400 ml-2'>
                Takes effect at the start of the next sweep.
              </span>
            </div>
          </form>
        </section>
      </div>

      <section class='card'>
        <h2 class='font-semibold text-lg mb-1'>Container</h2>
        <p class='text-xs text-slate-400 mb-4 max-w-2xl'>
          Runtime settings from <code>docker-compose.yml</code> (read-only).
        </p>
        <div id='container-info' hx-get='/api/container' hx-trigger='load'>…</div>
      </section>
    </section>

    <!-- ==================== TAB 2: CONVERT ==================== -->
    <section id='tab-convert' class='tab-content space-y-6 hidden'>
      <div class='flex items-center gap-3 flex-wrap'>
        <button class='btn btn-primary' hx-post='/api/convert' hx-swap='none'
                title='Encode every file currently in the Pending list'>
          Convert queued files
        </button>
        <button class='btn btn-danger' hx-post='/api/stop' hx-swap='none'
                onclick="return confirm('Stop the current conversion? The file being encoded will be discarded and the rest of the queue kept for later.')"
                title='Kill the running ffmpeg and abort the queue'>
          Stop
        </button>
        <span class='text-slate-400 text-sm'>
          Convert drains the Pending list. Stop kills the current encode
          immediately; unprocessed files stay queued.
        </span>
      </div>

      <section class='card'>
        <h2 class='font-semibold mb-3 text-lg'>Current encode</h2>
        <div id='progress' hx-get='/api/progress' hx-trigger='load, every 2s'>…</div>
      </section>
    </section>

    <!-- ==================== TAB 3: STATUS ==================== -->
    <section id='tab-status' class='tab-content space-y-6 hidden'>
      <div class='flex items-center justify-between'>
        <h2 class='font-semibold text-lg'>Library status</h2>
        <form method='post' action='/api/state/clear_failed' class='inline'>
          <button class='btn btn-ghost' onclick="return confirm('Clear failed rows from state DB so they get retried?')">Retry failed</button>
        </form>
      </div>

      <section class='card'>
        <h2 class='font-semibold mb-3 text-lg'>Intel iGPU</h2>
        <div id='gpu-status' hx-get='/api/gpu/status' hx-trigger='load, every 2s'>…</div>
      </section>

      <section class='card'>
        <div id='pies' hx-get='/api/pies' hx-trigger='load, every 5s'>…</div>
      </section>

      <section class='card'>
        <h2 class='font-semibold mb-3 text-lg'>Recent activity</h2>
        <div id='recent' hx-get='/api/recent' hx-trigger='load, every 10s'>…</div>
      </section>

      <section class='card'>
        <h2 class='font-semibold mb-3 text-lg'>Log tail</h2>
        <div id='logs' hx-get='/api/logs' hx-trigger='load, every 5s'>…</div>
      </section>
    </section>
  </div>

  <script>
    (function () {{
      const tabs = document.querySelectorAll('.tabs-nav .tab');
      const panes = document.querySelectorAll('.tab-content');
      function activate(name) {{
        tabs.forEach(b => b.classList.toggle('tab-active', b.dataset.tab === name));
        panes.forEach(p => p.classList.toggle('hidden', p.id !== 'tab-' + name));
        try {{ localStorage.setItem('activeTab', name); }} catch (e) {{}}
      }}
      tabs.forEach(b => b.addEventListener('click', () => activate(b.dataset.tab)));
      let saved = null;
      try {{ saved = localStorage.getItem('activeTab'); }} catch (e) {{}}
      if (saved && document.querySelector(`.tab[data-tab="${{saved}}"]`)) {{
        activate(saved);
      }}
    }})();
  </script>
  {_BROWSER_MODAL_HTML}
</body>
</html>"""


def _fmt_duration(secs: float | None) -> str:
    if not secs:
        return "—"
    secs = int(secs)
    h, m = divmod(secs, 3600)
    m, s = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def _render_pending() -> str:
    return _render_pending_table(
        state.get_pending(),
        "No files queued. Click <b>Scan now</b> to find candidates.",
    )


def _svg_donut(slices: list[tuple[str, float, str]], size: int = 150,
               hole: float = 0.55, center_text: str = "") -> str:
    """Render a donut chart as inline SVG.

    slices: list of (label, value, color). Zero-value slices are dropped.
    center_text: optional short string drawn in the middle of the hole.
    """
    total = sum(max(0.0, v) for _, v, _ in slices)
    cx = cy = size / 2
    r = size / 2 - 4
    ri = r * hole

    if total <= 0:
        return (
            f"<svg viewBox='0 0 {size} {size}' width='{size}' height='{size}'>"
            f"<circle cx='{cx}' cy='{cy}' r='{r}' fill='#334155'/>"
            f"<circle cx='{cx}' cy='{cy}' r='{ri}' fill='#1e293b'/>"
            f"<text x='{cx}' y='{cy + 4}' text-anchor='middle' fill='#64748b' "
            f"font-size='11'>no data</text>"
            f"</svg>"
        )

    nonzero = [(lbl, v, c) for lbl, v, c in slices if v > 0]
    parts: list[str] = []

    if len(nonzero) == 1:
        # Full-ring: single <circle> ring, path arcs can't span 360° in one arc.
        color = nonzero[0][2]
        parts.append(
            f"<circle cx='{cx}' cy='{cy}' r='{(r + ri) / 2}' fill='none' "
            f"stroke='{color}' stroke-width='{r - ri}'/>"
        )
    else:
        start = -math.pi / 2
        for _, val, color in nonzero:
            angle = (val / total) * 2 * math.pi
            end = start + angle
            large = "1" if angle > math.pi else "0"
            x1, y1 = cx + r * math.cos(start),  cy + r * math.sin(start)
            x2, y2 = cx + r * math.cos(end),    cy + r * math.sin(end)
            xi1, yi1 = cx + ri * math.cos(start), cy + ri * math.sin(start)
            xi2, yi2 = cx + ri * math.cos(end),   cy + ri * math.sin(end)
            d = (
                f"M {x1:.2f} {y1:.2f} "
                f"A {r} {r} 0 {large} 1 {x2:.2f} {y2:.2f} "
                f"L {xi2:.2f} {yi2:.2f} "
                f"A {ri} {ri} 0 {large} 0 {xi1:.2f} {yi1:.2f} Z"
            )
            parts.append(f"<path d='{d}' fill='{color}'/>")
            start = end

    inner = ""
    if center_text:
        inner = (
            f"<text x='{cx}' y='{cy + 5}' text-anchor='middle' fill='#e2e8f0' "
            f"font-size='14' font-weight='600'>{_esc(center_text)}</text>"
        )
    return (
        f"<svg viewBox='0 0 {size} {size}' width='{size}' height='{size}'>"
        f"{''.join(parts)}{inner}</svg>"
    )


def _legend_row(label: str, value: str, color: str) -> str:
    return (
        "<div class='flex items-center gap-2 text-xs'>"
        f"<span class='inline-block w-3 h-3 rounded-sm' style='background:{color}'></span>"
        f"<span class='text-slate-300'>{_esc(label)}</span>"
        f"<span class='ml-auto text-slate-100 font-semibold'>{_esc(value)}</span>"
        "</div>"
    )


def _render_pending_table(items: list[dict], empty_msg: str) -> str:
    """Shared renderer for the Pending / Up-next tables."""
    if not items:
        return f"<div class='text-slate-400 text-sm'>{empty_msg}</div>"

    total_bytes = sum(int(x.get("size") or 0) for x in items)
    header = (
        "<div class='text-slate-300 text-sm mb-3'>"
        f"<b>{len(items)}</b> file(s) · total <b>{_fmt_bytes(total_bytes)}</b>"
        "</div>"
    )
    thead = (
        "<thead><tr class='text-xs text-slate-400 text-left "
        "border-b border-slate-600'>"
        "<th class='py-1 pr-3'>Path</th>"
        "<th class='py-1 pr-3'>Codec</th>"
        "<th class='py-1 pr-3'>Resolution</th>"
        "<th class='py-1 pr-3'>Duration</th>"
        "<th class='py-1 pr-3'>Size</th>"
        "</tr></thead>"
    )
    rows = []
    for it in items[:100]:
        res = f"{it.get('width', 0)}\u00d7{it.get('height', 0)}"
        rows.append(
            "<tr class='border-b border-slate-700'>"
            f"<td class='py-1 pr-3 font-mono text-xs text-cyan-300 truncate max-w-md'>"
            f"{_esc(it.get('path', ''))}</td>"
            f"<td class='py-1 pr-3 text-xs text-slate-300'>{_esc(it.get('codec', ''))}</td>"
            f"<td class='py-1 pr-3 text-xs text-slate-300'>{res}</td>"
            f"<td class='py-1 pr-3 text-xs text-slate-300'>{_fmt_duration(it.get('duration'))}</td>"
            f"<td class='py-1 pr-3 text-xs text-slate-300'>{_fmt_bytes(it.get('size'))}</td>"
            "</tr>"
        )
    footer = ""
    if len(items) > 100:
        footer = (f"<div class='text-xs text-slate-400 mt-2'>"
                  f"+ {len(items) - 100} more not shown\u2026</div>")
    return f"{header}<table class='w-full text-sm'>{thead}<tbody>{''.join(rows)}</tbody></table>{footer}"


def _render_scan_progress() -> str:
    s = state.get_scan_stats()
    phase = s.get("phase", "idle")
    if phase == "enumerating":
        return (
            "<div class='mt-2 flex items-center gap-3 text-sm'>"
            "<span class='badge b-skip'>SCANNING</span>"
            "<span class='text-slate-300'>Enumerating files\u2026</span>"
            "</div>"
        )
    if phase == "probing":
        total = int(s.get("files_examined") or 0)
        done = int(s.get("files_probed") or 0)
        pct = (done / total * 100.0) if total > 0 else 0.0
        return (
            "<div class='mt-2 space-y-1'>"
            "<div class='flex items-center gap-3 text-sm'>"
            "<span class='badge b-skip'>SCANNING</span>"
            f"<span class='text-slate-300'>Probing <b class='text-slate-100'>{done}</b> / {total} files</span>"
            f"<span class='text-slate-100 font-semibold ml-auto'>{pct:.1f}%</span>"
            "</div>"
            "<div class='progress-track'>"
            f"<div class='progress-fill' style='width:{pct:.2f}%'></div>"
            "</div>"
            "</div>"
        )
    return ""


def _render_activity() -> str:
    """Global activity strip shown above the tab nav.

    Collapses to nothing when idle; shows either scan or encode progress
    otherwise. Scan takes precedence when both would appear.
    """
    scan = state.get_scan_stats()
    cur = state.get_current()
    scanning = scan.get("phase") in ("enumerating", "probing")
    active = cur.get("stage", "idle") != "idle" and cur.get("path")

    if not scanning and not active:
        return ""
    body = _activity_scan_body(scan) if scanning else _activity_encode_body(cur)
    return (
        "<section class='card mb-6 !p-4 border border-emerald-500/30 bg-slate-800/80'>"
        f"{body}"
        "</section>"
    )


def _activity_scan_body(s: dict) -> str:
    phase = s.get("phase", "idle")
    if phase == "enumerating":
        return (
            "<div class='flex items-center gap-3 text-sm'>"
            "<span class='badge b-skip'>SCANNING</span>"
            "<span class='text-slate-300'>Enumerating files\u2026</span>"
            "</div>"
        )
    total = int(s.get("files_examined") or 0)
    done = int(s.get("files_probed") or 0)
    pct = (done / total * 100.0) if total > 0 else 0.0
    return (
        "<div class='flex items-center gap-3 text-sm mb-2'>"
        "<span class='badge b-skip'>SCANNING</span>"
        f"<span class='text-slate-300'>Probing <b class='text-slate-100'>{done}</b> / {total} files</span>"
        f"<span class='text-slate-100 font-semibold ml-auto'>{pct:.1f}%</span>"
        "</div>"
        "<div class='progress-track'>"
        f"<div class='progress-fill' style='width:{pct:.2f}%'></div>"
        "</div>"
    )


def _activity_encode_body(cur: dict) -> str:
    p = cur.get("progress") or {}
    duration = float(cur.get("duration") or 0)
    out_time_s = _parse_ffmpeg_time(p.get("out_time"))
    pct = 0.0
    if duration > 0 and out_time_s is not None:
        pct = min(100.0, max(0.0, out_time_s / duration * 100))
    stage_upper = str(cur.get("stage", "idle")).upper()
    stage_badge = {
        "PROBING": "b-skip", "ENCODING": "b-ok",
        "VALIDATING": "b-skip", "REPLACING": "b-skip",
    }.get(stage_upper, "b-skip")
    filename = Path(str(cur.get("path", ""))).name
    speed = _esc(p.get("speed") or "\u2014")
    return (
        "<div class='flex items-center gap-3 text-sm mb-2 flex-wrap'>"
        f"<span class='badge {stage_badge}'>{_esc(stage_upper)}</span>"
        f"<span class='text-slate-300 font-mono truncate max-w-md'>{_esc(filename)}</span>"
        f"<span class='text-slate-400 ml-auto'>speed <b class='text-slate-100'>{speed}</b> \u00b7 "
        f"<b class='text-slate-100'>{pct:.1f}%</b></span>"
        "</div>"
        "<div class='progress-track'>"
        f"<div class='progress-fill' style='width:{pct:.2f}%'></div>"
        "</div>"
    )


def _render_progress() -> str:
    """Tab 2 body: current file being encoded + list of files still waiting."""
    cur = state.get_current()
    all_pending = state.get_pending()

    # "Up next" excludes whatever is actively being encoded — when the current
    # file finishes and is removed from the queue, the next file naturally
    # shifts up and becomes the new "current".
    current_path = cur.get("path")
    up_next = [x for x in all_pending if x.get("path") != current_path]

    idle = cur["stage"] == "idle" or not current_path

    if idle:
        if all_pending:
            current_block = (
                "<div class='py-6 text-center'>"
                f"<div class='text-4xl font-bold text-emerald-400 mb-2'>{len(all_pending)}</div>"
                "<div class='text-slate-300'>file(s) queued for conversion.</div>"
                "<div class='text-slate-400 text-sm mt-3'>"
                "Click <b>Convert queued files</b> above to start.</div>"
                "</div>"
            )
        else:
            current_block = (
                "<div class='py-6 text-center text-slate-400'>"
                "Nothing to convert. Run a scan first (Setup tab)."
                "</div>"
            )
    else:
        p = cur["progress"]
        duration = float(cur.get("duration") or 0)
        out_time_s = _parse_ffmpeg_time(p.get("out_time"))
        pct = 0.0
        if duration > 0 and out_time_s is not None:
            pct = min(100.0, max(0.0, out_time_s / duration * 100))

        started = cur.get("started_at") or 0
        elapsed = time.time() - started if started else 0
        eta_str = "—"
        if pct >= 1.0 and elapsed > 0:
            eta_str = _fmt_elapsed(elapsed * (100 - pct) / pct)

        stage_upper = cur["stage"].upper()
        stage_badge = {
            "PROBING": "b-skip", "ENCODING": "b-ok",
            "VALIDATING": "b-skip", "REPLACING": "b-skip",
        }.get(stage_upper, "b-skip")
        stage_hint = ""
        if cur["stage"] == "validating":
            stage_hint = (
                "<p class='text-xs text-amber-400'>"
                "Full-decoding the output to catch corruption. Uses the iGPU "
                "when possible — expect a minute or two on large 4K files."
                "</p>"
            )
        elif cur["stage"] == "replacing":
            stage_hint = (
                "<p class='text-xs text-slate-400'>"
                "Copying to the source location and swapping in atomically."
                "</p>"
            )

        current_block = f"""
        <div class='space-y-4'>
          <div class='flex items-center gap-3 flex-wrap'>
            <span class='badge {stage_badge}'>{_esc(stage_upper)}</span>
            <span class='text-slate-300 text-sm'>{_esc(cur.get("encoder") or "—")}</span>
            <span class='text-slate-400 text-sm ml-auto'>
              elapsed <b class='text-slate-100'>{_fmt_elapsed(elapsed)}</b>
              · ETA <b class='text-slate-100'>{_esc(eta_str)}</b>
            </span>
          </div>

          <div class='font-mono text-sm text-cyan-300 break-all'>{_esc(current_path)}</div>
          {stage_hint}

          <div>
            <div class='flex justify-between text-xs text-slate-400 mb-1'>
              <span>{_esc(p.get("out_time", "—"))} / {_fmt_hms(duration)}</span>
              <span class='text-slate-100 font-semibold'>{pct:.1f}%</span>
            </div>
            <div class='progress-track'>
              <div class='progress-fill' style='width:{pct:.2f}%'></div>
            </div>
          </div>

          <div class='grid grid-cols-3 gap-3 text-sm text-slate-300'>
            <div class='bg-slate-900 rounded p-3'>
              <div class='text-xs text-slate-400'>Speed</div>
              <div class='text-lg font-semibold text-slate-100'>
                {_esc(p.get("speed", "—"))}
              </div>
            </div>
            <div class='bg-slate-900 rounded p-3'>
              <div class='text-xs text-slate-400'>Bitrate</div>
              <div class='text-lg font-semibold text-slate-100'>
                {_esc(p.get("bitrate", "—"))}
              </div>
            </div>
            <div class='bg-slate-900 rounded p-3'>
              <div class='text-xs text-slate-400'>Output size so far</div>
              <div class='text-lg font-semibold text-slate-100'>
                {_fmt_bytes(int(p.get("total_size", 0) or 0))}
              </div>
            </div>
          </div>
        </div>
        """

    up_next_msg = (
        "Nothing else waiting." if not idle
        else "Nothing in the queue yet."
    )
    up_next_html = _render_pending_table(up_next, up_next_msg)

    return f"""
    {current_block}
    <div class='mt-8 pt-6 border-t border-slate-700'>
      <h3 class='font-semibold text-sm text-slate-300 mb-3 uppercase tracking-wide'>
        Up next
      </h3>
      {up_next_html}
    </div>
    """


def _render_pies(cfg: Config) -> str:
    """Tab 3 body: the two donut charts + legends."""
    stats = _db_stats(cfg.runtime.state_db)

    total_files = stats["ok"] + stats["skipped"] + stats["failed"]
    files_center = str(total_files) if total_files else ""
    files_pie = _svg_donut(
        [
            ("Converted", stats["ok"],      "#10b981"),
            ("Skipped",   stats["skipped"], "#94a3b8"),
            ("Failed",    stats["failed"],  "#ef4444"),
        ],
        center_text=files_center,
    )
    files_legend = (
        _legend_row("Converted", str(stats["ok"]),      "#10b981")
        + _legend_row("Skipped", str(stats["skipped"]), "#94a3b8")
        + _legend_row("Failed",  str(stats["failed"]),  "#ef4444")
    )

    orig = stats["orig_bytes"]
    new = stats["new_bytes"]
    saved = max(orig - new, 0)
    pct = f"−{(saved / orig * 100):.0f}%" if orig else ""
    space_pie = _svg_donut(
        [
            ("New size on disk", new,   "#22d3ee"),
            ("Saved",            saved, "#10b981"),
        ],
        center_text=pct,
    )
    space_legend = (
        _legend_row("On disk", _fmt_bytes(new),   "#22d3ee")
        + _legend_row("Saved", _fmt_bytes(saved), "#10b981")
        + _legend_row("Original total", _fmt_bytes(orig), "#334155")
    )

    return f"""
    <div class='flex gap-8 items-start justify-center flex-wrap'>
      <div class='text-center'>
        <div class='text-sm text-slate-400 mb-2'>Files</div>
        {files_pie}
        <div class='space-y-1 mt-3 w-44 mx-auto'>{files_legend}</div>
      </div>
      <div class='text-center'>
        <div class='text-sm text-slate-400 mb-2'>Space</div>
        {space_pie}
        <div class='space-y-1 mt-3 w-44 mx-auto'>{space_legend}</div>
      </div>
    </div>
    """


def _render_recent(cfg: Config) -> str:
    rows = _recent(cfg.runtime.state_db)
    if not rows:
        return "<div class='text-slate-400 text-sm'>No activity yet.</div>"
    body = []
    for r in rows:
        badge = {
            "ok": "b-ok", "skipped": "b-skip", "failed": "b-fail",
        }.get(r["status"], "b-skip")
        savings = ""
        if r["status"] == "ok" and r["orig_size"] and r["new_size"]:
            pct = (1 - r["new_size"] / r["orig_size"]) * 100
            savings = f"− {pct:.0f}% ({_fmt_bytes(r['orig_size'])} → {_fmt_bytes(r['new_size'])})"
        body.append(
            f"<tr class='border-b border-slate-700'>"
            f"<td class='py-1 pr-3'><span class='badge {badge}'>{_esc(r['status'])}</span></td>"
            f"<td class='py-1 pr-3 font-mono text-xs text-cyan-300 truncate max-w-md'>{_esc(r['path'])}</td>"
            f"<td class='py-1 pr-3 text-xs text-slate-400'>{_esc(r['orig_codec'])}</td>"
            f"<td class='py-1 pr-3 text-xs'>{_esc(savings or r['reason'])}</td>"
            f"<td class='py-1 pr-3 text-xs text-slate-400'>{_fmt_age(r['ts'])}</td>"
            f"</tr>"
        )
    return "<table class='w-full text-sm'>" + "".join(body) + "</table>"


# Order determines display; only these engine families are surfaced.
_GPU_ENGINE_ORDER = ("Video", "VideoEnhance", "Render/3D", "Blitter")


def _read_gpu_status() -> dict:
    if not Path("/dev/dri/renderD128").exists():
        return {"available": False, "reason": "no /dev/dri/renderD128 device"}
    if not shutil.which("intel_gpu_top"):
        return {"available": False, "reason": "intel_gpu_top not installed"}
    stdout = ""
    try:
        # -s 500 = 500ms interval; 1.5s window captures one full sample.
        proc = subprocess.run(
            ["intel_gpu_top", "-J", "-s", "500"],
            capture_output=True, text=True, timeout=1.5,
        )
        stdout = proc.stdout or ""
    except subprocess.TimeoutExpired as e:
        raw = e.stdout or b""
        stdout = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else raw
    except (OSError, subprocess.SubprocessError) as e:
        return {"available": False, "reason": f"intel_gpu_top failed: {e}"}

    engines: dict[str, float] = {}
    for m in re.finditer(r'"([A-Za-z0-9/_]+)":\s*\{\s*"busy":\s*([\d.]+)', stdout):
        name, busy = m.group(1), float(m.group(2))
        family = name.split("/", 1)[0]
        if family in _GPU_ENGINE_ORDER:
            engines[name] = max(engines.get(name, 0.0), busy)

    freq_actual = None
    freq_m = re.search(r'"frequency":\s*\{[^}]*?"actual":\s*([\d.]+)', stdout)
    if freq_m:
        freq_actual = float(freq_m.group(1))

    power = None
    pwr_m = re.search(r'"power":\s*\{[^}]*?"GPU":\s*([\d.]+)', stdout)
    if pwr_m:
        power = float(pwr_m.group(1))

    if not engines:
        return {"available": False, "reason": "no sample captured (privileges?)"}
    return {
        "available": True,
        "engines": engines,
        "frequency_mhz": freq_actual,
        "power_w": power,
    }


def _render_gpu_status() -> str:
    s = _read_gpu_status()
    if not s.get("available"):
        return (
            f"<div class='text-sm text-amber-400'>GPU stats unavailable: "
            f"{_esc(s.get('reason', 'unknown'))}</div>"
            "<p class='text-xs text-slate-400 mt-2'>"
            "On the container, <code>intel_gpu_top</code> may need the host "
            "<code>i915</code> driver and access to <code>/dev/dri</code>."
            "</p>"
        )
    engines = s["engines"]
    # Order: known families first (highest-signal engines up top), then any others alphabetically.
    def rank(name: str) -> tuple:
        family = name.split("/", 1)[0]
        return (_GPU_ENGINE_ORDER.index(family) if family in _GPU_ENGINE_ORDER else 99, name)
    ordered = sorted(engines.items(), key=lambda kv: rank(kv[0]))

    def bar(name: str, pct: float) -> str:
        pct_clamped = max(0.0, min(100.0, pct))
        color = "bg-emerald-500" if pct_clamped >= 50 else ("bg-sky-500" if pct_clamped >= 10 else "bg-slate-600")
        return (
            "<div class='mb-2'>"
            f"<div class='flex justify-between text-xs mb-1'><span class='font-mono text-slate-300'>{_esc(name)}</span>"
            f"<span class='font-mono text-slate-400'>{pct_clamped:.1f}%</span></div>"
            "<div class='h-2 bg-slate-700 rounded'>"
            f"<div class='h-2 {color} rounded' style='width: {pct_clamped:.1f}%'></div>"
            "</div>"
            "</div>"
        )
    bars = "".join(bar(n, p) for n, p in ordered)

    meta = []
    if s.get("frequency_mhz") is not None:
        meta.append(f"<span class='text-slate-400'>Freq</span> <span class='font-mono'>{s['frequency_mhz']:.0f} MHz</span>")
    if s.get("power_w") is not None:
        meta.append(f"<span class='text-slate-400'>Power</span> <span class='font-mono'>{s['power_w']:.1f} W</span>")
    meta_html = ("<div class='mt-3 flex gap-6 text-sm'>" + " · ".join(meta) + "</div>") if meta else ""

    return bars + meta_html + (
        "<p class='text-xs text-slate-500 mt-3'>"
        "<b>Video</b> busy = QSV encode/decode load. If <b>Video</b> is high while CPU is low, hardware acceleration is working."
        "</p>"
    )


def _render_container() -> str:
    info = _container_info()
    cfg = _load()

    qsv_cls = "text-emerald-400" if info["qsv"] == "present" else "text-amber-400"
    auth_cls = "text-emerald-400" if info["ui_auth"].startswith("password") else "text-amber-400"

    def row(label: str, value_html: str) -> str:
        return (
            "<div class='grid grid-cols-[10rem_1fr] gap-2 py-1 border-b border-slate-700 last:border-b-0'>"
            f"<div class='text-xs text-slate-400 uppercase tracking-wide'>{label}</div>"
            f"<div class='text-sm'>{value_html}</div>"
            "</div>"
        )

    readonly_rows = (
        row("Timezone",     f"<span class='font-mono'>{_esc(info['timezone'])}</span>")
        + row("Web UI port",   f"<span class='font-mono'>{_esc(info['ui_port'])}</span>")
        + row("Auth",          f"<span class='{auth_cls}'>{_esc(info['ui_auth'])}</span>")
        + row("PUID / PGID",   f"<span class='font-mono'>{_esc(info['puid'])} / {_esc(info['pgid'])}</span>")
        + row("Intel QSV",     f"<span class='{qsv_cls}'>{_esc(info['qsv'])}</span>")
    )

    # NAS volumes visible under /media at runtime (readonly — set in compose).
    detected = info["media_mounts"]
    if detected:
        mount_html = "".join(
            f"<div class='font-mono text-xs text-cyan-300'>{_esc(m)}</div>"
            for m in detected
        )
    else:
        mount_html = (
            "<div class='text-xs text-amber-400'>"
            "no volumes mounted under /media — edit docker-compose.yml"
            "</div>"
        )
    volumes_html = row("Mounted volumes", mount_html)

    return readonly_rows + volumes_html


def _render_scan_folders() -> str:
    cfg = _load()
    rows_html = "".join(_scan_row_html(p) for p in cfg.scan_paths)
    return f"""
        <div class='flex items-center justify-between mb-2'>
          <h3 class='text-sm font-semibold uppercase tracking-wide text-slate-300'>Scan folders</h3>
          <button type='button' onclick='vhcOpenBrowser(null)' class='btn btn-ghost text-xs'>+ Add folder…</button>
        </div>
        <p class='text-xs text-slate-400 mb-3'>
          Folders under <code class='font-mono'>{_esc(_BROWSE_ROOT)}/</code>
          that the app scans for videos. Changes take effect on the next
          scan — no restart required.
        </p>
        <form id='vhc-scan-form'
              hx-post='/api/scan_paths/save' hx-swap='none'
              hx-on::after-request="if(event.detail.successful) htmx.ajax('GET', '/api/scan_folders', '#scan-folders')"
              oninput='vhcMarkScanDirty()'>
          <div id='scan-rows' class='space-y-2'>{rows_html}</div>
          <div class='mt-4'>
            <button id='vhc-scan-save' class='btn btn-primary disabled:opacity-50 disabled:cursor-not-allowed disabled:bg-slate-600' disabled>
              Save
            </button>
            <span id='vhc-scan-hint' class='text-xs text-slate-400 ml-2'>No changes to apply.</span>
          </div>
        </form>
        <script>
          function vhcMarkScanDirty() {{
            const btn = document.getElementById('vhc-scan-save');
            const hint = document.getElementById('vhc-scan-hint');
            if (btn) {{ btn.disabled = false; }}
            if (hint) {{ hint.textContent = 'Unsaved changes.'; hint.classList.remove('text-slate-400'); hint.classList.add('text-amber-400'); }}
          }}
          function vhcRemoveScanRow(btn) {{ btn.parentElement.remove(); vhcMarkScanDirty(); }}
          function vhcAddScanRow(path) {{
            const c = document.getElementById('scan-rows');
            const row = document.createElement('div');
            row.className = 'flex items-center gap-2';
            const input = document.createElement('input');
            input.type = 'text';
            input.name = 'path';
            input.readOnly = true;
            input.className = 'flex-1 font-mono text-sm bg-slate-900';
            input.value = path || '';
            const rm = document.createElement('button');
            rm.type = 'button';
            rm.className = 'text-red-400 hover:text-red-300 px-2';
            rm.innerHTML = '&times;';
            rm.addEventListener('click', () => vhcRemoveScanRow(rm));
            row.appendChild(input);
            row.appendChild(rm);
            c.appendChild(row);
            vhcMarkScanDirty();
          }}
        </script>
        """


def _scan_row_html(path: str) -> str:
    return (
        "<div class='flex items-center gap-2'>"
        f"<input type='text' name='path' value='{_esc(path)}' readonly class='flex-1 font-mono text-sm bg-slate-900'>"
        "<button type='button' class='text-red-400 hover:text-red-300 px-2' onclick='vhcRemoveScanRow(this)'>&times;</button>"
        "</div>"
    )


# Folder-picker modal. Rendered once inside the Container card; opened by the
# Browse… buttons on each scan-folder row.
_BROWSER_MODAL_HTML = r"""
<div id='vhc-browse-modal' class='hidden fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70'>
  <div class='bg-slate-800 rounded-lg shadow-xl w-full max-w-lg mx-4 p-4'>
    <div class='flex items-center justify-between mb-3'>
      <h3 id='vhc-browse-title' class='text-lg font-semibold'>Choose a folder</h3>
      <button type='button' onclick='vhcCloseBrowser()' class='text-slate-400 hover:text-slate-200 text-xl leading-none'>&times;</button>
    </div>
    <div class='mb-2 flex items-center gap-2'>
      <button id='vhc-browse-up' type='button' onclick='vhcBrowseUp()' class='btn btn-ghost text-xs'>Up</button>
      <div id='vhc-browse-path' class='font-mono text-xs text-cyan-300 flex-1 truncate'></div>
    </div>
    <div id='vhc-browse-list' class='max-h-72 overflow-y-auto border border-slate-700 rounded p-1 mb-3 bg-slate-900'>
      <div class='text-slate-500 text-xs p-2'>Loading…</div>
    </div>
    <div id='vhc-browse-footer' class='flex justify-end gap-2'>
      <button type='button' onclick='vhcCloseBrowser()' class='btn btn-ghost text-xs'>Cancel</button>
      <button id='vhc-browse-select' type='button' onclick='vhcBrowseSelect()' class='btn btn-primary text-xs'>Select this folder</button>
    </div>
  </div>
</div>
<script>
  let vhcBrowseTarget = null;
  let vhcBrowseCurrent = null;
  let vhcBrowseParent = null;
  let vhcBrowseMode = 'folder';   // 'folder' | 'file'
  function vhcOpenBrowser(btn) {
    vhcBrowseMode = 'folder';
    vhcBrowseTarget = (btn && btn.parentElement) ? btn.parentElement.querySelector("input[name='path']") : null;
    const start = (vhcBrowseTarget && vhcBrowseTarget.value.trim()) || '';
    document.getElementById('vhc-browse-title').textContent = 'Choose a folder';
    document.getElementById('vhc-browse-select').classList.remove('hidden');
    document.getElementById('vhc-browse-modal').classList.remove('hidden');
    vhcBrowseLoad(start);
  }
  function vhcOpenFileBrowser() {
    vhcBrowseMode = 'file';
    vhcBrowseTarget = null;
    document.getElementById('vhc-browse-title').textContent = 'Choose a file to convert';
    document.getElementById('vhc-browse-select').classList.add('hidden');
    document.getElementById('vhc-browse-modal').classList.remove('hidden');
    vhcBrowseLoad('');
  }
  function vhcCloseBrowser() {
    document.getElementById('vhc-browse-modal').classList.add('hidden');
    vhcBrowseTarget = null;
  }
  function vhcBrowseUp() { if (vhcBrowseParent) vhcBrowseLoad(vhcBrowseParent); }
  function vhcBrowseSelect() {
    if (vhcBrowseCurrent) {
      if (vhcBrowseTarget) {
        vhcBrowseTarget.value = vhcBrowseCurrent;
        if (typeof vhcMarkScanDirty === 'function') vhcMarkScanDirty();
      } else if (typeof vhcAddScanRow === 'function') {
        vhcAddScanRow(vhcBrowseCurrent);
      }
    }
    vhcCloseBrowser();
  }
  function vhcHumanBytes(n) {
    if (!n) return '';
    const u = ['B','KiB','MiB','GiB','TiB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(n < 10 && i > 0 ? 1 : 0) + ' ' + u[i];
  }
  async function vhcConvertFile(path) {
    try {
      const body = 'path=' + encodeURIComponent(path);
      const r = await fetch('/api/convert_file', {
        method: 'POST', body,
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      });
      if (!r.ok) {
        const err = await r.text();
        alert('Queue failed: ' + (err || ('HTTP ' + r.status)));
        return;
      }
      vhcCloseBrowser();
    } catch (e) {
      alert('Queue failed: ' + e);
    }
  }
  async function vhcBrowseLoad(path) {
    const list = document.getElementById('vhc-browse-list');
    list.innerHTML = "<div class='text-slate-500 text-xs p-2'>Loading…</div>";
    try {
      const params = new URLSearchParams();
      if (path) params.set('path', path);
      if (vhcBrowseMode === 'file') params.set('files', '1');
      const url = '/api/browse' + (params.toString() ? ('?' + params.toString()) : '');
      const r = await fetch(url);
      if (!r.ok) {
        const err = await r.text();
        list.innerHTML = "<div class='text-red-400 text-xs p-2'>" + (err || ('HTTP ' + r.status)) + "</div>";
        return;
      }
      const data = await r.json();
      vhcBrowseCurrent = data.path;
      vhcBrowseParent = data.parent;
      document.getElementById('vhc-browse-path').textContent = data.path;
      document.getElementById('vhc-browse-up').disabled = !data.parent;
      document.getElementById('vhc-browse-up').classList.toggle('opacity-40', !data.parent);
      if (!data.entries.length) {
        list.innerHTML = vhcBrowseMode === 'file'
          ? "<div class='text-slate-500 text-xs p-2'>(no video files or subfolders)</div>"
          : "<div class='text-slate-500 text-xs p-2'>(no subfolders)</div>";
      } else {
        list.innerHTML = '';
        for (const e of data.entries) {
          const b = document.createElement('button');
          b.type = 'button';
          b.className = 'block w-full text-left px-2 py-1 text-sm font-mono hover:bg-slate-800 rounded';
          const isFile = e.type === 'file';
          const icon = isFile ? '🎬' : '📁';
          const suffix = isFile && e.size ? "  <span class='text-slate-500 text-xs'>" + vhcHumanBytes(e.size) + "</span>" : '';
          b.innerHTML = icon + ' ' + e.name.replace(/</g, '&lt;').replace(/>/g, '&gt;') + suffix;
          b.dataset.path = e.path;
          b.dataset.type = e.type || 'dir';
          b.addEventListener('click', () => {
            if (b.dataset.type === 'file') {
              vhcConvertFile(b.dataset.path);
            } else {
              vhcBrowseLoad(b.dataset.path);
            }
          });
          list.appendChild(b);
        }
      }
    } catch (e) {
      list.innerHTML = "<div class='text-red-400 text-xs p-2'>Error: " + e + "</div>";
    }
  }
</script>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(_: Auth) -> str:
    return _render_page(_load())


@app.get("/api/status", response_class=HTMLResponse)
def api_status(_: Auth) -> str:
    # Kept for backwards compat — returns the pies fragment (Tab 3 body).
    return _render_pies(_load())


@app.get("/api/progress", response_class=HTMLResponse)
def api_progress(_: Auth) -> str:
    return _render_progress()


@app.get("/api/pies", response_class=HTMLResponse)
def api_pies(_: Auth) -> str:
    return _render_pies(_load())


@app.get("/api/container", response_class=HTMLResponse)
def api_container(_: Auth) -> str:
    return _render_container()


@app.get("/api/scan_folders", response_class=HTMLResponse)
def api_scan_folders(_: Auth) -> str:
    return _render_scan_folders()


@app.get("/api/gpu/status", response_class=HTMLResponse)
def api_gpu_status(_: Auth) -> str:
    return _render_gpu_status()


@app.get("/api/recent", response_class=HTMLResponse)
def api_recent(_: Auth) -> str:
    return _render_recent(_load())


@app.get("/api/logs", response_class=HTMLResponse)
def api_logs(_: Auth) -> str:
    cfg = _load()
    lines = _tail(cfg.runtime.log_file, 40)
    body = "\n".join(_esc(l) for l in lines) or "No log entries yet."
    return f"<pre class='text-xs text-slate-300 whitespace-pre-wrap'>{body}</pre>"


@app.post("/api/scan")
def api_scan(_: Auth) -> JSONResponse:
    state.request_scan_now()
    return JSONResponse({"ok": True})


@app.get("/api/scan/progress", response_class=HTMLResponse)
def api_scan_progress(_: Auth) -> str:
    return _render_scan_progress()


@app.get("/api/activity", response_class=HTMLResponse)
def api_activity(_: Auth) -> str:
    return _render_activity()


@app.post("/api/convert")
def api_convert(_: Auth) -> JSONResponse:
    n = state.pending_count()
    state.request_convert_now()
    return JSONResponse({"ok": True, "queued": n})


@app.post("/api/stop")
def api_stop(_: Auth) -> JSONResponse:
    state.request_stop()
    return JSONResponse({"ok": True})


@app.get("/api/pending", response_class=HTMLResponse)
def api_pending(_: Auth) -> str:
    return _render_pending()


@app.post("/api/settings")
def api_settings(
    _: Auth,
    global_quality: Annotated[int, Form()] = 21,
    preset: Annotated[str, Form()] = "veryslow",
    sharpen: Annotated[int, Form()] = 0,
    sweep_at_time: Annotated[str, Form()] = "",
    delete_original: Annotated[str | None, Form()] = None,
    dry_run: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    if not 18 <= global_quality <= 30:
        raise HTTPException(400, "global_quality must be 18-30")
    if preset not in PRESETS:
        raise HTTPException(400, f"preset must be one of {PRESETS}")
    if not 0 <= sharpen < len(SHARPEN_NAMES):
        raise HTTPException(400, f"sharpen must be 0..{len(SHARPEN_NAMES) - 1}")
    sweep_at_time = sweep_at_time.strip()
    if sweep_at_time:
        try:
            h_str, m_str = sweep_at_time.split(":", 1)
            if not (0 <= int(h_str) < 24 and 0 <= int(m_str) < 60):
                raise ValueError
        except (ValueError, AttributeError):
            raise HTTPException(400, "sweep_at_time must be HH:MM (24-hour) or empty")

    cfg = _load()
    cfg.encoder.global_quality = global_quality
    cfg.encoder.preset = preset
    cfg.encoder.sharpen = sharpen
    cfg.runtime.sweep_at_time = sweep_at_time
    cfg.runtime.delete_original = bool(delete_original)
    cfg.runtime.dry_run = bool(dry_run)
    save_config(cfg, _config_path, keys={"encoder", "output", "runtime"})
    return RedirectResponse("/", status_code=303)


@app.post("/api/state/clear_failed")
def api_clear_failed(_: Auth) -> RedirectResponse:
    cfg = _load()
    with sqlite3.connect(cfg.runtime.state_db) as c:
        c.execute("DELETE FROM processed WHERE status='failed'")
    return RedirectResponse("/", status_code=303)


@app.post("/api/scan_paths/save")
def api_scan_paths_save(
    _: Auth,
    path: Annotated[list[str], Form()] = [],
) -> JSONResponse:
    root = Path(_BROWSE_ROOT).resolve()
    resolved_paths: list[Path] = []
    seen: set[str] = set()
    for p in path:
        p = p.strip().rstrip("/")
        if not p:
            continue
        try:
            resolved = Path(p).resolve()
        except OSError as e:
            raise HTTPException(400, f"invalid path {p!r}: {e}")
        if resolved != root and root not in resolved.parents:
            raise HTTPException(400, f"path must be under {_BROWSE_ROOT}: {p}")
        s = resolved.as_posix()
        if s in seen:
            continue
        seen.add(s)
        resolved_paths.append(resolved)

    # Drop entries whose parent (or self) is already covered — sorting lexically
    # puts ancestors before descendants, so we only need to keep the first hit.
    resolved_paths.sort(key=lambda p: p.as_posix())
    kept: list[Path] = []
    dropped: list[str] = []
    for p in resolved_paths:
        if any(p == k or k in p.parents for k in kept):
            dropped.append(p.as_posix())
            continue
        kept.append(p)
    cleaned = [p.as_posix() for p in kept]

    cfg = _load()
    cfg.scan_paths = cleaned
    save_config(cfg, _config_path, keys={"scan_paths"})
    state.request_scan_now()
    if dropped:
        log.info("scan_paths updated: %d entrie(s); dropped %d redundant (covered by a parent): %s",
                 len(cleaned), len(dropped), dropped)
    else:
        log.info("scan_paths updated: %d entrie(s)", len(cleaned))
    return JSONResponse({"ok": True, "count": len(cleaned), "dropped": dropped})


@app.get("/api/browse")
def api_browse(_: Auth, path: str = "", files: int = 0) -> JSONResponse:
    root = Path(_BROWSE_ROOT).resolve()
    if not path:
        target = root
    else:
        try:
            target = Path(path).resolve()
        except OSError as e:
            raise HTTPException(400, f"invalid path: {e}")
        if target != root and root not in target.parents:
            raise HTTPException(400, f"path must be under {_BROWSE_ROOT}")
    if not target.is_dir():
        raise HTTPException(404, f"not a directory: {target}")

    cfg = _load()
    video_exts = {e.lower() for e in (cfg.video_extensions or set())}
    entries: list[dict] = []
    try:
        for c in target.iterdir():
            if c.name.startswith("."):
                continue
            if c.is_dir():
                entries.append({"name": c.name, "path": c.as_posix(), "type": "dir"})
            elif files and c.is_file() and c.suffix.lower() in video_exts:
                try:
                    size = c.stat().st_size
                except OSError:
                    size = 0
                entries.append({
                    "name": c.name, "path": c.as_posix(),
                    "type": "file", "size": size,
                })
    except OSError as e:
        raise HTTPException(500, f"cannot list: {e}")
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))

    return JSONResponse({
        "path": target.as_posix(),
        "parent": target.parent.as_posix() if target != root else None,
        "root": root.as_posix(),
        "entries": entries,
    })


@app.post("/api/convert_file")
def api_convert_file(
    _: Auth,
    path: Annotated[str, Form()],
) -> JSONResponse:
    root = Path(_BROWSE_ROOT).resolve()
    try:
        resolved = Path(path).resolve()
    except OSError as e:
        raise HTTPException(400, f"invalid path: {e}")
    if resolved != root and root not in resolved.parents:
        raise HTTPException(400, f"path must be under {_BROWSE_ROOT}")
    if not resolved.is_file():
        raise HTTPException(404, f"not a file: {path}")

    try:
        from probe import probe_video  # local import: avoids ffprobe on module load
        info = probe_video(resolved)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"probe failed: {e}")

    state.append_pending({
        "path": resolved.as_posix(),
        "codec": info.codec,
        "width": info.width,
        "height": info.height,
        "duration": info.duration,
        "size": resolved.stat().st_size,
        "bit_depth": info.bit_depth,
    })
    state.request_convert_now()
    log.info("convert_file: queued %s", resolved)
    return JSONResponse({"ok": True, "queued": resolved.as_posix()})


def _spawn_compose_recreate() -> None:
    """Recreate this container in a background thread via docker compose up -d."""
    def _run() -> None:
        time.sleep(0.5)
        state.request_stop()
        try:
            hostname = os.environ.get("HOSTNAME") or "video-converter"
            project = subprocess.run(
                ["docker", "inspect", "--format",
                 "{{index .Config.Labels \"com.docker.compose.project\"}}", hostname],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip() or "video_hevc_converter"
            log.info("running docker compose up -d for project %r", project)
            subprocess.Popen(
                ["docker", "compose", "-p", project, "-f", _COMPOSE_PATH, "up", "-d"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (subprocess.SubprocessError, OSError) as e:
            log.error("docker compose up failed (%s) — falling back to SIGTERM", e)
            os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_run, daemon=True).start()


@app.post("/api/restart")
def api_restart(_: Auth) -> JSONResponse:
    log.info("restart requested from UI")
    if _can_docker_compose():
        _spawn_compose_recreate()
        return JSONResponse({"ok": True, "message": "recreating"})

    def _sigterm() -> None:
        time.sleep(0.5)
        state.request_stop()
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_sigterm, daemon=True).start()
    return JSONResponse({"ok": True, "message": "restarting (process only)"})


# ---------------------------------------------------------------------------
# Public entrypoint (called from convert.py)
# ---------------------------------------------------------------------------
def serve(config_path: str, host: str = "0.0.0.0", port: int = 8080) -> None:
    global _config_path
    _config_path = config_path
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
