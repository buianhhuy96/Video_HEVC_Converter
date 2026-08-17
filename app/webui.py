"""FastAPI + HTMX control panel for the video converter.

Runs in a daemon thread alongside the scanner. Uses HTTP Basic auth when
UI_PASSWORD is set; otherwise open (intended for LAN-only exposure).
"""
from __future__ import annotations

import html
import logging
import math
import os
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

_COMPOSE_PATH = "/compose/docker-compose.yml"
_COMPOSE_SERVICE = "video-converter"

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
    media_root = Path("/media")
    if media_root.is_dir():
        try:
            info["media_mounts"] = sorted(
                str(p) for p in media_root.iterdir() if p.is_dir()
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

    <nav class='tabs-nav flex gap-1 border-b border-slate-700 mb-6'>
      <button data-tab='setup'   class='tab tab-active'>Setup</button>
      <button data-tab='convert' class='tab'>Convert</button>
      <button data-tab='status'  class='tab'>Status</button>
    </nav>

    <!-- ==================== TAB 1: SETUP ==================== -->
    <section id='tab-setup' class='tab-content space-y-6'>
      <div class='flex items-center gap-3'>
        <button class='btn btn-primary' hx-post='/api/scan' hx-swap='none'>Scan now</button>
        <span class='text-slate-400 text-sm'>
          Populates the Pending list below without starting any encoding.
        </span>
      </div>

      <section class='card'>
        <h2 class='font-semibold text-lg mb-1'>Container</h2>
        <p class='text-xs text-slate-400 mb-4 max-w-2xl'>
          These settings live in <code>docker-compose.yml</code>. Edit the
          <strong>Media folders</strong> below and click <strong>Save &amp;
          Restart app</strong> to apply — any running encode is killed and
          Docker brings the app back automatically (UI unreachable for ~15
          seconds).
        </p>
        <div id='container-info' hx-get='/api/container' hx-trigger='load'>…</div>
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
            <script>
              (function() {{
                const PRESET_NAMES = {PRESETS!r};
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
        <h2 class='font-semibold mb-3 text-lg'>Pending conversion</h2>
        <div id='pending' hx-get='/api/pending' hx-trigger='load, every 5s'>…</div>
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


def _render_container() -> str:
    info = _container_info()
    editable = _can_edit_compose()
    can_restart = _can_docker_compose()

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

    if editable:
        mounts = _media_mounts_from_compose()
        if not mounts:
            mounts = [{"host": "", "container": "/media"}]
        rows_html = "".join(_mount_row_html(m["host"], m["container"]) for m in mounts)
        restart_note = "" if can_restart else (
            "<p class='text-xs text-amber-400 mt-2'>"
            "Docker socket not available \u2014 saving writes the file but "
            "does <em>not</em> restart the container. Run "
            "<code>docker compose up -d</code> on the NAS host to apply."
            "</p>"
        )
        edit_html = f"""
        <div class='mt-4 pt-4 border-t border-slate-700'>
          <div class='flex items-center justify-between mb-2'>
            <h3 class='text-sm font-semibold uppercase tracking-wide text-slate-300'>Media folders</h3>
            <button type='button' onclick='vhcAddMount()' class='btn btn-ghost text-xs'>+ Add folder</button>
          </div>
          <p class='text-xs text-slate-400 mb-3'>
            Each row is a host path on the NAS. It is bind-mounted into the
            container <em>and</em> added to the scan list in one step. The
            container path is auto-derived from the host folder name; edit it
            manually only if you need to.
          </p>
          <form id='vhc-mounts-form'
                hx-post='/api/container/mounts/save' hx-swap='none'
                oninput='vhcMarkFormDirty()'
                onsubmit="if(!confirm('Save folders and restart the app? Any running encode will be killed and the container recreated. The UI may be unreachable for ~15 seconds.'))return false; setTimeout(()=&gt;location.reload(),15000);">
            <div id='mount-rows' class='space-y-2'>{rows_html}</div>
            {restart_note}
            <div class='mt-4'>
              <button id='vhc-save-btn' class='btn btn-danger disabled:opacity-50 disabled:cursor-not-allowed disabled:bg-slate-600' disabled>
                Save &amp; Restart app
              </button>
              <span id='vhc-dirty-hint' class='text-xs text-slate-400 ml-2'>No changes to apply.</span>
            </div>
          </form>
        </div>
        <script>
          function vhcMarkFormDirty() {{
            const btn = document.getElementById('vhc-save-btn');
            const hint = document.getElementById('vhc-dirty-hint');
            if (btn) {{ btn.disabled = false; }}
            if (hint) {{ hint.textContent = 'Unsaved changes.'; hint.classList.remove('text-slate-400'); hint.classList.add('text-amber-400'); }}
          }}
          function vhcDeriveContainer(hostInput) {{
            const row = hostInput.parentElement;
            const containerInput = row.querySelector("input[name='container']");
            if (containerInput.dataset.userEdited === '1') return;
            const host = hostInput.value.trim().replace(/\\/+$/, '');
            if (!host) {{ containerInput.value = ''; return; }}
            const base = host.split('/').pop() || 'media';
            containerInput.value = '/media/' + base;
          }}
          function vhcMarkEdited(el) {{ el.dataset.userEdited = '1'; }}
          function vhcRemoveRow(btn) {{ btn.parentElement.remove(); vhcMarkFormDirty(); }}
          function vhcAddMount() {{
            const c = document.getElementById('mount-rows');
            const row = document.createElement('div');
            row.className = 'flex items-center gap-2';
            row.innerHTML = "<input type='text' name='host' placeholder='/volume2/Movies' class='flex-1 font-mono text-sm' oninput='vhcDeriveContainer(this)'>"
              + "<span class='text-slate-500'>&rarr;</span>"
              + "<input type='text' name='container' placeholder='/media/<name>' class='flex-1 font-mono text-sm' oninput='vhcMarkEdited(this)'>"
              + "<button type='button' class='text-red-400 hover:text-red-300 px-2' onclick='vhcRemoveRow(this)'>&times;</button>";
            c.appendChild(row);
            vhcMarkFormDirty();
          }}
          document.querySelectorAll("#mount-rows input[name='container']").forEach(el => {{ if (el.value) el.dataset.userEdited = '1'; }});
        </script>
        """
    else:
        detected = info["media_mounts"]
        if detected:
            mount_html = "".join(
                f"<div class='font-mono text-xs text-cyan-300'>{_esc(m)}</div>"
                for m in detected
            )
        else:
            mount_html = "<div class='text-xs text-slate-400'>none detected under /media</div>"
        edit_html = row("Bind mounts", mount_html) + (
            "<p class='text-xs text-amber-400 mt-3'>"
            "docker-compose.yml is not mounted into this container (read-only). "
            "Edit the file on the NAS host to change bind mounts, then run "
            "<code>docker compose up -d</code>."
            "</p>"
        )

    return readonly_rows + edit_html


def _mount_row_html(host: str, container: str) -> str:
    return (
        "<div class='flex items-center gap-2'>"
        f"<input type='text' name='host' value='{_esc(host)}' placeholder='/volume2/Movies' class='flex-1 font-mono text-sm' oninput='vhcDeriveContainer(this)'>"
        "<span class='text-slate-500'>&rarr;</span>"
        f"<input type='text' name='container' value='{_esc(container)}' placeholder='/media/<name>' class='flex-1 font-mono text-sm' oninput='vhcMarkEdited(this)'>"
        "<button type='button' class='text-red-400 hover:text-red-300 px-2' onclick='vhcRemoveRow(this)'>&times;</button>"
        "</div>"
    )


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
    sweep_at_time: Annotated[str, Form()] = "",
    delete_original: Annotated[str | None, Form()] = None,
    dry_run: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    if not 18 <= global_quality <= 30:
        raise HTTPException(400, "global_quality must be 18-30")
    if preset not in PRESETS:
        raise HTTPException(400, f"preset must be one of {PRESETS}")
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


@app.post("/api/container/mounts/save")
def api_container_mounts_save(
    _: Auth,
    host: Annotated[list[str], Form()] = [],
    container: Annotated[list[str], Form()] = [],
) -> JSONResponse:
    if not _can_edit_compose():
        raise HTTPException(500, "docker-compose.yml is not writable from this container")
    if len(host) != len(container):
        raise HTTPException(400, "host and container path counts must match")

    new_entries: list[str] = []
    seen: set[str] = set()
    for h, c in zip(host, container):
        h = h.strip()
        c = c.strip()
        if not h and not c:
            continue
        if not h or not c:
            raise HTTPException(400, "both host and container path required for each row")
        if not (c == "/media" or c.startswith("/media/")):
            raise HTTPException(400, f"container path {c!r} must be /media or /media/*")
        if c in seen:
            raise HTTPException(400, f"container path {c!r} appears more than once")
        seen.add(c)
        new_entries.append(f"{h}:{c}")

    doc = _load_compose()
    if doc is None:
        raise HTTPException(500, "cannot read docker-compose.yml")

    svc = doc.get("services", {}).get(_COMPOSE_SERVICE)
    if svc is None:
        raise HTTPException(500, f"service '{_COMPOSE_SERVICE}' missing from compose file")

    volumes = svc.get("volumes", []) or []
    # Keep non-/media (system) mounts intact, replace all /media entries.
    kept = []
    for v in volumes:
        _, container_path, _mode = _parse_volume(v)
        if not (container_path == "/media" or container_path.startswith("/media/")):
            kept.append(v)
    svc["volumes"] = kept + new_entries

    _save_compose(doc)
    log.info("compose media mounts updated: %d entrie(s)", len(new_entries))

    # Mirror the container-side paths into config.yaml scan_paths so a saved
    # media folder is both bind-mounted and picked up by the discovery pass.
    cfg = _load()
    cfg.scan_paths = [entry.split(":", 1)[1] for entry in new_entries]
    save_config(cfg, _config_path, keys={"scan_paths"})

    if _can_docker_compose():
        _spawn_compose_recreate()
        return JSONResponse({"ok": True, "restarted": True, "count": len(new_entries)})
    return JSONResponse({"ok": True, "restarted": False, "count": len(new_entries)})


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
