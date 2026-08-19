#Requires -Version 5
<#
Windows-native launcher for Video HEVC Converter. No Docker.

Uses NVIDIA NVENC on the local GPU. Config, logs and state live under
$env:APPDATA\VideoHEVCConverter\.

First run: creates the venv, installs Python deps, checks for ffmpeg on PATH.
Subsequent runs: skip setup and launch the app.

    .\run.ps1              # start the app
    .\run.ps1 -Reinstall   # nuke .venv and reinstall from requirements.txt
    .\run.ps1 -Help
#>

param(
  [switch]$Reinstall,
  [switch]$Help
)

$ErrorActionPreference = "Stop"

function Info($msg)  { Write-Host "[+] $msg" -ForegroundColor Green }
function Warn($msg)  { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Err($msg)   { Write-Host "[x] $msg" -ForegroundColor Red }

if ($Help) {
  Write-Host @"
Video HEVC Converter (Windows)
  .\run.ps1              start the app (venv is created on first run)
  .\run.ps1 -Reinstall   recreate the venv and reinstall dependencies
"@
  exit 0
}

$RepoRoot   = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir    = Join-Path $RepoRoot ".venv"
$PyExe      = Join-Path $VenvDir "Scripts\python.exe"
$AppDataDir = Join-Path $env:APPDATA "VideoHEVCConverter"
$ConfigPath = Join-Path $AppDataDir "config.yaml"
$LogDir     = Join-Path $AppDataDir "logs"
$StateDir   = Join-Path $AppDataDir "state"
$WorkDir    = Join-Path $env:LOCALAPPDATA "VideoHEVCConverter\work"

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  Err "python is not on PATH. Install Python 3.11+ (https://www.python.org/downloads/)."
  exit 1
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  Warn "ffmpeg is not on PATH."
  Write-Host "Install with winget (has nvenc + libx265):"
  Write-Host "  winget install Gyan.FFmpeg" -ForegroundColor Cyan
  Write-Host "Or download the 'full' build from https://www.gyan.dev/ffmpeg/builds/ and add bin/ to PATH."
  exit 1
}

# Confirm the ffmpeg build has hevc_nvenc.
$hasNvenc = (ffmpeg -hide_banner -encoders 2>&1 | Select-String -Pattern "hevc_nvenc" -Quiet)
if (-not $hasNvenc) {
  Warn "hevc_nvenc is not enabled in this ffmpeg build."
  Write-Host "Use the 'full' Gyan build (winget install Gyan.FFmpeg) which includes NVENC."
  exit 1
}

# ---------------------------------------------------------------------------
# Virtualenv + deps
# ---------------------------------------------------------------------------
if ($Reinstall -and (Test-Path $VenvDir)) {
  Info "Removing existing venv..."
  Remove-Item -Recurse -Force $VenvDir
}

if (-not (Test-Path $PyExe)) {
  Info "Creating venv at .venv..."
  python -m venv $VenvDir
  Info "Installing dependencies..."
  & $PyExe -m pip install --disable-pip-version-check --quiet -U pip
  & $PyExe -m pip install --disable-pip-version-check --quiet `
      fastapi==0.115.6 "uvicorn[standard]==0.32.1" `
      python-multipart==0.0.19 ruamel.yaml==0.18.6 PyYAML==6.0.2
}

# ---------------------------------------------------------------------------
# App-data layout + starter config
# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Path $AppDataDir, $LogDir, $StateDir, $WorkDir -Force | Out-Null

if (-not (Test-Path $ConfigPath)) {
  Info "Seeding starter config at $ConfigPath"
  $starter = @"
# ---------------------------------------------------------------------------
# Video HEVC Converter — Windows NVENC config
# ---------------------------------------------------------------------------
scan_paths: []   # add folders via the UI

video_extensions: [.mp4, .mkv, .mov, .avi, .wmv, .flv, .m4v, .ts, .mts, .m2ts, .webm, .mpg, .mpeg, .vob, .3gp]
skip_codecs: [hevc, h265, av1, vp9]
raw_codecs: [prores, prores_ks, dnxhd, dnxhr, cfhd, ffv1, huffyuv, rawvideo, mjpeg, magicyuv, lagarith]
raw_extensions: [.braw, .r3d, .ari, .arriraw, .dng, .crm, .mxf]
raw_filename_markers: ["_log", ".log.", "slog", "vlog", "flog", "hlg-raw", "prores", "dnxhr", "master", "intermediate"]
min_size_bytes: 20971520

encoder:
  global_quality: 18
  preset: veryslow
  look_ahead: true
  look_ahead_depth: 80
  allow_10bit: true
  max_bitrate_kbps: 0
  preserve_color_metadata: true
  sharpen: 0
  denoise: 0
  deband: false
  dynamic_crf: false

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
  work_dir: "$($WorkDir -replace '\\','/')"
  log_file: "$(($LogDir + '\converter.log') -replace '\\','/')"
  state_db: "$(($StateDir + '\converter.db') -replace '\\','/')"
  dry_run: false
  stall_timeout_seconds: 300
  stability_check_seconds: 2.0
  sweep_at_time: ""
"@
  $starter | Set-Content -Path $ConfigPath -Encoding UTF8
}

# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------
$env:CONFIG_PATH       = $ConfigPath
$env:VHC_ENCODER       = "nvenc"
# The folder picker walks under this root. C:\ is the whole machine; narrow it
# if you only want the UI to show one drive/folder.
$env:VHC_BROWSE_ROOT   = if ($env:VHC_BROWSE_ROOT) { $env:VHC_BROWSE_ROOT } else { "C:\" }
$env:UI_PORT           = if ($env:UI_PORT) { $env:UI_PORT } else { "8080" }

Info "Encoder: hevc_nvenc"
Info "Config:  $ConfigPath"
Info "Browse root: $env:VHC_BROWSE_ROOT"
Info "UI:      http://127.0.0.1:$($env:UI_PORT)"
Write-Host ""

Push-Location (Join-Path $RepoRoot "app")
try {
  & $PyExe convert.py
} finally {
  Pop-Location
}
