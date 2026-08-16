"""Configuration loader."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

try:
    from ruamel.yaml import YAML  # roundtrip that preserves comments
    _RUAMEL = YAML()
    _RUAMEL.preserve_quotes = True
    _RUAMEL.indent(mapping=2, sequence=4, offset=2)
except ImportError:  # optional: PyYAML fallback loses comments on write
    _RUAMEL = None


@dataclass
class EncoderCfg:
    codec: str = "hevc_qsv"
    fallback_codec: str = "libx265"
    global_quality: int = 23
    preset: str = "slower"
    look_ahead: bool = True
    look_ahead_depth: int = 40
    allow_10bit: bool = True
    fallback_crf: int = 22
    max_bitrate_kbps: int = 0


@dataclass
class OutputCfg:
    fallback_container: str = ".mkv"
    max_size_ratio: float = 1.0
    copy_audio: bool = True
    copy_subs: bool = True


@dataclass
class ValidationCfg:
    duration_tolerance_seconds: float = 1.5
    full_decode: bool = True
    check_stream_counts: bool = True


@dataclass
class RuntimeCfg:
    delete_original: bool = True
    work_dir: str = "/tmp/convert"
    log_file: str = "/logs/converter.log"
    state_db: str = "/state/converter.db"
    dry_run: bool = False
    # Kill ffmpeg if no progress line for this many seconds (0 = disabled).
    stall_timeout_seconds: int = 300
    # Skip files whose size/mtime change within this window (still being written).
    stability_check_seconds: float = 2.0
    # Hours between library scans (fractional supported; 0 = one-shot then exit).
    scan_interval_hours: float = 1.0
    # When True, encoding starts immediately after a scan finishes.
    # When False, the scanner just lists candidates and waits for the
    # "Convert" button in the web UI.
    auto_convert: bool = True


@dataclass
class Config:
    scan_paths: list[str] = field(default_factory=list)
    video_extensions: set[str] = field(default_factory=set)
    skip_codecs: set[str] = field(default_factory=set)
    raw_codecs: set[str] = field(default_factory=set)
    raw_extensions: set[str] = field(default_factory=set)
    raw_filename_markers: list[str] = field(default_factory=list)
    min_size_bytes: int = 20 * 1024 * 1024
    encoder: EncoderCfg = field(default_factory=EncoderCfg)
    output: OutputCfg = field(default_factory=OutputCfg)
    validation: ValidationCfg = field(default_factory=ValidationCfg)
    runtime: RuntimeCfg = field(default_factory=RuntimeCfg)


def _lower_set(items) -> set[str]:
    return {str(x).lower() for x in (items or [])}


def load_config(path: str | None = None) -> Config:
    path = path or os.environ.get("CONFIG_PATH", "/config/config.yaml")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    cfg = Config()
    cfg.scan_paths = [str(p) for p in raw.get("scan_paths", [])]
    cfg.video_extensions = _lower_set(raw.get("video_extensions"))
    cfg.skip_codecs = _lower_set(raw.get("skip_codecs"))
    cfg.raw_codecs = _lower_set(raw.get("raw_codecs"))
    cfg.raw_extensions = _lower_set(raw.get("raw_extensions"))
    cfg.raw_filename_markers = [str(m).lower() for m in raw.get("raw_filename_markers", [])]
    cfg.min_size_bytes = int(raw.get("min_size_bytes", cfg.min_size_bytes))

    if "encoder" in raw:
        cfg.encoder = EncoderCfg(**{**cfg.encoder.__dict__, **raw["encoder"]})
    if "output" in raw:
        cfg.output = OutputCfg(**{**cfg.output.__dict__, **raw["output"]})
    if "validation" in raw:
        cfg.validation = ValidationCfg(**{**cfg.validation.__dict__, **raw["validation"]})
    if "runtime" in raw:
        cfg.runtime = RuntimeCfg(**{**cfg.runtime.__dict__, **raw["runtime"]})

    Path(cfg.runtime.work_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.runtime.log_file).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.runtime.state_db).parent.mkdir(parents=True, exist_ok=True)

    return cfg


# ---------------------------------------------------------------------------
# Save back to YAML (used by the web UI)
# ---------------------------------------------------------------------------

# Only these top-level sections can be written from the UI. Anything else in
# config.yaml is left as-is on disk.
_EDITABLE_SECTIONS = ("scan_paths", "encoder", "output", "runtime")


def _cfg_to_dict(cfg: Config) -> dict:
    return {
        "scan_paths": list(cfg.scan_paths),
        "encoder": dict(cfg.encoder.__dict__),
        "output": dict(cfg.output.__dict__),
        "runtime": dict(cfg.runtime.__dict__),
    }


def save_config(cfg: Config, path: str | None = None,
                keys: set[str] | None = None) -> None:
    """Persist `cfg` back to YAML, preserving comments if ruamel.yaml is present.

    `keys` restricts which top-level sections are overwritten. If None, all
    editable sections are updated. Non-editable sections (raw_codecs,
    video_extensions, etc.) are always left untouched.
    """
    path = path or os.environ.get("CONFIG_PATH", "/config/config.yaml")
    updates = _cfg_to_dict(cfg)
    if keys is not None:
        updates = {k: v for k, v in updates.items() if k in keys}

    if _RUAMEL is not None:
        with open(path, "r", encoding="utf-8") as f:
            doc = _RUAMEL.load(f) or {}
        for k, v in updates.items():
            doc[k] = v
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _RUAMEL.dump(doc, f)
        os.replace(tmp, path)
        return

    # PyYAML fallback: comments are lost. Merge with existing on-disk data so
    # unrelated sections still round-trip.
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    for k, v in updates.items():
        doc[k] = v
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, sort_keys=False)
    os.replace(tmp, path)
