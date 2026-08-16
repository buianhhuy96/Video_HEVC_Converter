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
import sqlite3
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
    folders_rows = "".join(
        f"<li class='flex items-center justify-between py-1 border-b border-slate-700'>"
        f"<code class='text-cyan-300 text-sm'>{_esc(p)}</code>"
        f"<form method='post' action='/api/folders/remove' class='inline'>"
        f"<input type='hidden' name='path' value='{_esc(p)}'>"
        f"<button class='text-red-400 hover:text-red-300 text-xs'>remove</button>"
        f"</form></li>"
        for p in cfg.scan_paths
    ) or "<li class='text-slate-400 text-sm py-2'>No folders configured yet.</li>"

    e, o, r = cfg.encoder, cfg.output, cfg.runtime
    preset_opts = "".join(
        f"<option value='{p}'{' selected' if p == e.preset else ''}>{p}</option>"
        for p in PRESETS
    )

    return f"""<!DOCTYPE html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Video HEVC Converter</title>
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
  </style>
</head>
<body class='p-6'>
  <div class='max-w-6xl mx-auto'>
    <header class='mb-4'>
      <h1 class='text-2xl font-bold'>Video HEVC Converter</h1>
      <p class='text-slate-400 text-sm'>Ugreen DXP4800 Plus · Intel 8505 QSV</p>
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

      <div class='grid md:grid-cols-2 gap-6'>
        <section class='card'>
          <h2 class='font-semibold mb-3 text-lg'>Folders</h2>
          <ul class='mb-4'>{folders_rows}</ul>
          <form method='post' action='/api/folders/add' class='flex gap-2'>
            <input type='text' name='path' placeholder='/media/Movies'
                   class='flex-1' required>
            <button class='btn btn-primary'>Add</button>
          </form>
          <p class='text-xs text-slate-400 mt-2'>
            Paths are as seen <em>inside the container</em> — edit
            <code>docker-compose.yml</code> to add new bind mounts.
          </p>
        </section>

        <section class='card'>
          <h2 class='font-semibold mb-3 text-lg'>Settings</h2>
          <form method='post' action='/api/settings' class='space-y-3'>
            <div>
              <label class='block text-sm mb-1'>
                Quality <span class='text-slate-400'>(global_quality — lower = higher quality)</span>
              </label>
              <input type='number' name='global_quality' min='18' max='30'
                     value='{e.global_quality}' class='w-24'>
            </div>
            <div>
              <label class='block text-sm mb-1'>Preset</label>
              <select name='preset'>{preset_opts}</select>
            </div>
            <div>
              <label class='block text-sm mb-1'>
                Scan interval <span class='text-slate-400'>(hours — fractional OK, 0 = one-shot)</span>
              </label>
              <input type='number' name='scan_interval_hours' step='0.25' min='0'
                     value='{r.scan_interval_hours:g}' class='w-32'>
            </div>
            <div>
              <label class='block text-sm mb-1'>
                Max size vs original
                <span class='text-slate-400'>
                  (1.0 = keep anything not larger; lower requires actual savings)
                </span>
              </label>
              <input type='number' name='max_size_ratio' step='0.05' min='0.1' max='1.0'
                     value='{o.max_size_ratio}' class='w-24'>
            </div>
            <label class='flex items-center gap-2 text-sm'>
              <input type='checkbox' name='auto_convert'
                     {"checked" if r.auto_convert else ""}>
              Auto-convert after each scan (uncheck to require clicking
              <b>Convert</b>)
            </label>
            <label class='flex items-center gap-2 text-sm'>
              <input type='checkbox' name='delete_original'
                     {"checked" if r.delete_original else ""}>
              Overwrite original after successful validation
            </label>
            <label class='flex items-center gap-2 text-sm'>
              <input type='checkbox' name='dry_run' {"checked" if r.dry_run else ""}>
              Dry run (analyse only, no encoding)
            </label>
            <div class='pt-2'>
              <button class='btn btn-primary'>Save settings</button>
              <span class='text-xs text-slate-400 ml-2'>
                Takes effect at the start of the next scan.
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


@app.post("/api/folders/add")
def api_folders_add(_: Auth, path: Annotated[str, Form()]) -> RedirectResponse:
    p = Path(path.strip()).resolve()
    if not p.is_dir():
        raise HTTPException(400, f"Not a directory: {p}")
    cfg = _load()
    s = str(p)
    if s not in cfg.scan_paths:
        cfg.scan_paths.append(s)
        save_config(cfg, _config_path, keys={"scan_paths"})
    return RedirectResponse("/", status_code=303)


@app.post("/api/folders/remove")
def api_folders_remove(_: Auth, path: Annotated[str, Form()]) -> RedirectResponse:
    cfg = _load()
    cfg.scan_paths = [p for p in cfg.scan_paths if p != path]
    save_config(cfg, _config_path, keys={"scan_paths"})
    return RedirectResponse("/", status_code=303)


@app.post("/api/settings")
def api_settings(
    _: Auth,
    global_quality: Annotated[int, Form()] = 23,
    preset: Annotated[str, Form()] = "slower",
    scan_interval_hours: Annotated[float, Form()] = 1.0,
    max_size_ratio: Annotated[float, Form()] = 1.0,
    delete_original: Annotated[str | None, Form()] = None,
    dry_run: Annotated[str | None, Form()] = None,
    auto_convert: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    if not 18 <= global_quality <= 30:
        raise HTTPException(400, "global_quality must be 18-30")
    if preset not in PRESETS:
        raise HTTPException(400, f"preset must be one of {PRESETS}")
    if not 0.1 <= max_size_ratio <= 1.0:
        raise HTTPException(400, "max_size_ratio must be 0.1-1.0")
    if scan_interval_hours < 0:
        raise HTTPException(400, "scan_interval_hours must be >= 0")

    cfg = _load()
    cfg.encoder.global_quality = global_quality
    cfg.encoder.preset = preset
    cfg.runtime.scan_interval_hours = scan_interval_hours
    cfg.runtime.delete_original = bool(delete_original)
    cfg.runtime.dry_run = bool(dry_run)
    cfg.runtime.auto_convert = bool(auto_convert)
    cfg.output.max_size_ratio = max_size_ratio
    save_config(cfg, _config_path, keys={"encoder", "output", "runtime"})
    return RedirectResponse("/", status_code=303)


@app.post("/api/state/clear_failed")
def api_clear_failed(_: Auth) -> RedirectResponse:
    cfg = _load()
    with sqlite3.connect(cfg.runtime.state_db) as c:
        c.execute("DELETE FROM processed WHERE status='failed'")
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Public entrypoint (called from convert.py)
# ---------------------------------------------------------------------------
def serve(config_path: str, host: str = "0.0.0.0", port: int = 8080) -> None:
    global _config_path
    _config_path = config_path
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
