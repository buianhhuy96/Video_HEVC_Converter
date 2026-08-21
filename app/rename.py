"""Tree-based rename tool for Jellyfin-compatible library naming.

Users see a nested folder tree; each folder and file node has its own
editable "proposed" name. When Apply runs we walk the tree top-down and
issue OS renames in an order that keeps intermediate paths valid.
Renaming a folder cascades atomically to its contents (that's what the
filesystem does for `mv dir new_dir`), so we don't emit per-file ops
for children whose only change is riding along with a folder rename.

Companion subtitles (same basename, different extension) travel with a
video rename. Applied batches are logged JSONL so a single "undo"
inverts them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

log = logging.getLogger(__name__)


Kind = Literal["movie", "tv", "unknown"]
Confidence = Literal["high", "medium", "low"]


@dataclass
class ParsedMedia:
    kind: Kind = "unknown"
    title: str | None = None
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    episode_title: str | None = None
    confidence: Confidence = "low"


# --------------------------------------------------------------------------
# Filename parsing
# --------------------------------------------------------------------------

_SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}

_RELEASE_STOP = re.compile(
    r"\b(?:"
    r"1080p|2160p|720p|480p|4k|uhd|bluray|blu-?ray|webrip|web-?dl|web|hdtv|"
    r"brrip|dvdrip|hdrip|hevc|x26[45]|h\.?26[45]|aac|ac3|eac3|dts(?:-hd)?(?:\.ma)?|"
    r"truehd|atmos|opus|flac|[257]\.[102]|10-?bit|8-?bit|hdr(?:10)?(?:\+)?|sdr|"
    r"dolby[\.\s]?vision|dv|remux|proper|repack|extended|imax|"
    r"director'?s?[\.\s]?cut|theatrical|unrated|multi|internal|limited|"
    r"criterion|edition|complete|hybrid|dsnp|amzn|nf|atvp|hulu|hmax|mkv|mp4"
    r")\b",
    re.IGNORECASE,
)

_SxxExx = re.compile(
    r"\bS(\d{1,3})[\.\s]?E(\d{1,3})(?:[\.\s]?-?[\.\s]?E(\d{1,3}))?\b",
    re.IGNORECASE,
)

_YEAR_ANY = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
_YEAR_PAREN = re.compile(r"\((19\d{2}|20\d{2})\)")

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _clean_title(raw: str) -> str:
    """Turn `The.Matrix..[Group]` into `The Matrix`."""
    s = raw
    s = re.sub(r"[\[\{][^\]\}]*[\]\}]", " ", s)
    s = re.sub(r"[\[\]\{\}]", " ", s)
    s = re.sub(r"[._]+", " ", s)
    s = re.sub(r"\s*-\s*", " - ", s)
    s = re.sub(r"\s+", " ", s).strip(" -")
    # A left-side clip at a release marker (e.g. "Uno (1080p..." cut at
    # "1080p") can leave a dangling opener behind; strip unmatched brackets
    # so parsed parts don't emit "Uno (" as the episode title.
    s = re.sub(r"\s*[\(\[\{]\s*$", "", s)
    s = re.sub(r"^\s*[\)\]\}]\s*", "", s)
    return s


def _split_at_year(raw: str) -> tuple[str, int | None]:
    """Return (title-part, year). Prefers year-in-parens over any year."""
    m = _YEAR_PAREN.search(raw)
    if not m:
        m = _YEAR_ANY.search(raw)
    if not m:
        stop = _RELEASE_STOP.search(raw)
        title_part = raw[: stop.start()] if stop else raw
        return _clean_title(title_part), None
    year = int(m.group(1))
    return _clean_title(raw[: m.start()]), year


def parse_filename(path: Path) -> ParsedMedia:
    """Best-effort parse of a video filename into title/year/season/episode."""
    name = path.stem
    if not name:
        return ParsedMedia()

    m = _SxxExx.search(name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        episode_end = int(m.group(3)) if m.group(3) else None
        show_part = name[: m.start()].rstrip(" .-_")
        after = name[m.end():].strip(" .-_")

        title, year = _split_at_year(show_part)
        ep_title: str | None = None
        if after:
            stop = _RELEASE_STOP.search(after)
            ep_raw = after[: stop.start()] if stop else after
            ep_title = _clean_title(ep_raw) or None

        return ParsedMedia(
            kind="tv",
            title=title or None,
            year=year,
            season=season,
            episode=episode,
            episode_end=episode_end,
            episode_title=ep_title,
            confidence="high" if title else "low",
        )

    title, year = _split_at_year(name)
    if not title:
        return ParsedMedia(kind="unknown", confidence="low")
    if year:
        return ParsedMedia(kind="movie", title=title, year=year, confidence="high")
    return ParsedMedia(kind="movie", title=title, confidence="medium")


# --------------------------------------------------------------------------
# Subtitle discovery
# --------------------------------------------------------------------------

def _sanitize(component: str) -> str:
    """Make one path component filesystem-safe."""
    s = _ILLEGAL.sub("", component)
    return s.strip().rstrip(".")


def _find_subtitles(video: Path) -> list[Path]:
    """Sibling files with the same stem and any subtitle extension.

    Accepts language / kind suffixes like `.en.srt`, `.forced.en.srt`.
    """
    if not video.parent.exists():
        return []
    stem_lower = video.stem.lower()
    subs: list[Path] = []
    try:
        for sibling in video.parent.iterdir():
            if not sibling.is_file():
                continue
            if sibling.suffix.lower() not in _SUBTITLE_EXTS:
                continue
            if sibling.name.lower().startswith(stem_lower + "."):
                subs.append(sibling)
    except OSError:
        return []
    return subs


def _subtitle_tail(subtitle: Path, video_stem: str) -> str:
    """Return the part of the subtitle filename after the video stem.

    `Inception.eng.forced.srt` with video stem `Inception` returns
    `.eng.forced.srt`.
    """
    name = subtitle.name
    lead = video_stem + "."
    if name.lower().startswith(lead.lower()):
        return "." + name[len(lead):]
    return subtitle.suffix


# --------------------------------------------------------------------------
# Proposed filename generation
# --------------------------------------------------------------------------

def proposed_filename(path: Path, parsed: ParsedMedia | None = None) -> str:
    """Compute the canonical FILE name (no folder path).

    For movies: `Title (YYYY).ext`. For TV: `Show (YYYY) - SxxExx - Title.ext`.
    Unparseable inputs return the original filename unchanged.
    """
    parsed = parsed or parse_filename(path)
    ext = path.suffix.lower() or path.suffix
    if parsed.kind == "movie" and parsed.title:
        base = _sanitize(parsed.title)
        return f"{base} ({parsed.year}){ext}" if parsed.year else f"{base}{ext}"
    if parsed.kind == "tv" and parsed.title and parsed.season and parsed.episode:
        base = _sanitize(parsed.title)
        show = f"{base} ({parsed.year})" if parsed.year else base
        ep_label = f"S{parsed.season:02d}E{parsed.episode:02d}"
        if parsed.episode_end and parsed.episode_end != parsed.episode:
            ep_label += f"-E{parsed.episode_end:02d}"
        if parsed.episode_title:
            return f"{show} - {ep_label} - {_sanitize(parsed.episode_title)}{ext}"
        return f"{show} - {ep_label}{ext}"
    return path.name


# --------------------------------------------------------------------------
# Structured parts (for the per-file 3-column edit UI)
# --------------------------------------------------------------------------

def _sxxexx_label(parsed: ParsedMedia) -> str:
    if not (parsed.season and parsed.episode):
        return ""
    label = f"S{parsed.season:02d}E{parsed.episode:02d}"
    if parsed.episode_end and parsed.episode_end != parsed.episode:
        label += f"-E{parsed.episode_end:02d}"
    return label


_SXXEXX_STRICT = re.compile(r"^S(\d{1,3})E(\d{1,3})(?:-E(\d{1,3}))?$", re.IGNORECASE)
_YEAR_STRICT = re.compile(r"^(19\d{2}|20\d{2})$")
_SEASON_FOLDER = re.compile(r"^(?:season|s)\s*0*\d+$", re.IGNORECASE)

# Tolerant patterns for user-typed episode markers. Accept extra S's,
# alternative separators, and the NxNN shorthand.
_TV_TOLERANT: list[re.Pattern[str]] = [
    re.compile(r"^s+\s*(\d{1,3})\s*[\.\s]?e(\d{1,3})(?:\s*-?\s*e(\d{1,3}))?$", re.IGNORECASE),
    re.compile(r"^(\d{1,3})x(\d{1,3})(?:-(\d{1,3}))?$", re.IGNORECASE),
    re.compile(r"^season\s*(\d{1,3})\s*(?:ep(?:isode)?)?\s*(\d{1,3})$", re.IGNORECASE),
]


def _normalize_tv_marker(s: str) -> str:
    """Turn user-typed episode markers into canonical `S01E05`.

    Accepts `S1E5`, `SS1E3`, `s01.e05`, `1x05`, `Season 1 Episode 5`, and
    multi-episode variants like `S01E01-E02` or `1x01-02`. Falls back to
    the input (upper-cased) when nothing matches so freeform text still
    survives the round-trip.
    """
    s = s.strip()
    if not s:
        return s
    for pat in _TV_TOLERANT:
        m = pat.match(s)
        if not m:
            continue
        season = int(m.group(1))
        episode = int(m.group(2))
        label = f"S{season:02d}E{episode:02d}"
        # Third group is optional multi-episode end; not every pattern has it.
        end = m.group(3) if pat.groups >= 3 else None
        if end and int(end) != episode:
            label += f"-E{int(end):02d}"
        return label
    return s.upper() if s.isascii() and any(c.isalpha() for c in s) else s


def _parts_from_parsed(path: Path, parsed: ParsedMedia) -> dict:
    """Split the parser's guess into three unified fields.

    All files use the same layout: (title, middle, right). The middle box
    holds either a year (movies) or an SxxExx label (TV); the right box
    holds either the version (Director's Cut) or the episode title. The
    parser fills what it can from the filename; the user overrides freely.
    Unknown files land with the raw stem in `title`.
    """
    if parsed.kind == "movie" and parsed.title:
        return {
            "title": parsed.title,
            "middle": str(parsed.year) if parsed.year else "",
            "right": "",
        }
    if parsed.kind == "tv" and parsed.season and parsed.episode:
        show = parsed.title or path.parent.name
        if not parsed.title and _SEASON_FOLDER.match(show):
            show = path.parent.parent.name
        if parsed.year:
            show = f"{show} ({parsed.year})"
        return {
            "title": show,
            "middle": _sxxexx_label(parsed),
            "right": parsed.episode_title or "",
        }
    return {"title": path.stem, "middle": "", "right": ""}


def rebuild_proposed(parts: dict, ext: str, fallback: str) -> str:
    """Assemble a proposed filename from user-edited parts.

    The middle box drives the output format: a plain 4-digit year turns
    the file into Movie style (`Title (YYYY) - Right.ext`); anything else
    is treated as a TV episode marker and formatted as
    `Title - S01E05 - Right.ext` (`_normalize_tv_marker` tolerates common
    typos like `SS1E3`, `1x05`, `Season 1 Ep 5`). Empty middle produces
    just `Title.ext` (+ Right if present).
    """
    title = (parts.get("title") or "").strip()
    middle = (parts.get("middle") or "").strip()
    right = (parts.get("right") or "").strip()

    if not title:
        return fallback

    base = _sanitize(title)
    if middle:
        if _YEAR_STRICT.match(middle):
            base += f" ({middle})"
        else:
            base += f" - {_normalize_tv_marker(middle)}"

    if right:
        base += f" - {_sanitize(right)}"

    return f"{base}{ext}"


def _folder_media_types(node: dict) -> set[Literal["movie", "tv"]]:
    media_types: set[Literal["movie", "tv"]] = set()
    for child in node.get("children") or []:
        if child.get("type") == "folder":
            media_types.update(_folder_media_types(child))
            continue
        child_type, _ = metadata_search_context(child)
        if child_type in ("movie", "tv"):
            media_types.add(child_type)
    return media_types


def metadata_search_context(node: dict) -> tuple[Literal["movie", "tv", "any"], int | None]:
    """Infer a metadata search type and year from a file or folder node."""
    if node.get("type") == "folder":
        proposed = str(node.get("proposed") or node.get("name") or "")
        year_match = re.search(r"\((19\d{2}|20\d{2})\)\s*$", proposed)
        year = int(year_match.group(1)) if year_match else None
        media_types = _folder_media_types(node)
        if len(media_types) == 1:
            return next(iter(media_types)), year
        kind = node.get("kind")
        return (kind if kind in ("movie", "tv") else "any"), year

    parts = node.get("parts") or {}
    middle = str(parts.get("middle") or "").strip()
    if _YEAR_STRICT.match(middle):
        return "movie", int(middle)
    if middle and _SXXEXX_STRICT.match(_normalize_tv_marker(middle)):
        title = str(parts.get("title") or "")
        year_match = re.search(r"\((19\d{2}|20\d{2})\)\s*$", title)
        return "tv", int(year_match.group(1)) if year_match else None
    kind = node.get("kind")
    if kind in ("movie", "tv"):
        return kind, None
    return "any", None


def apply_metadata_match(node: dict, provider: str, provider_id: str,
                         media_type: Literal["movie", "tv"],
                         title: str, year: int | None) -> None:
    """Apply only a selected canonical title/year to a file or folder node."""
    if node.get("type") not in ("file", "folder") or media_type not in ("movie", "tv"):
        return
    title = title.strip()
    if not title:
        return

    if node["type"] == "folder":
        proposed = _sanitize(title)
        if year:
            proposed += f" ({year})"
        node["proposed"] = proposed
        node["kind"] = media_type
        node["metadata_match"] = {
            "provider": provider,
            "id": provider_id,
            "media_type": media_type,
        }
        return

    parts = dict(node.get("parts") or {})
    middle = str(parts.get("middle") or "").strip()
    if media_type == "movie":
        parts["title"] = title
        parts["middle"] = str(year) if year else ""
    else:
        parts["title"] = f"{title} ({year})" if year else title
        if _YEAR_STRICT.match(middle):
            parts["middle"] = ""

    node["parts"] = parts
    node["proposed"] = rebuild_proposed(
        parts, node.get("ext", ""), node["name"],
    )
    node["kind"] = media_type
    node["confidence"] = "high"
    node["note"] = None
    node["metadata_match"] = {
        "provider": provider,
        "id": provider_id,
        "media_type": media_type,
    }


# --------------------------------------------------------------------------
# Tree construction
# --------------------------------------------------------------------------

def _node_id(abs_path: str) -> str:
    return hashlib.md5(abs_path.encode("utf-8")).hexdigest()[:12]


def _common_ancestor(paths: list[Path]) -> Path:
    """Shortest common parent directory. Falls back to `/` on Linux."""
    if not paths:
        return Path("/")
    if len(paths) == 1:
        return paths[0].parent
    parts_lists = [p.parts for p in paths]
    common: list[str] = []
    for tup in zip(*parts_lists):
        if all(x == tup[0] for x in tup):
            common.append(tup[0])
        else:
            break
    if not common:
        return Path("/")
    # `Path(*parts)` on POSIX handles the leading '/' from parts[0] correctly.
    return Path(*common)


def _new_file_node(current_path: Path) -> dict:
    parsed = parse_filename(current_path)
    ext = current_path.suffix
    parts = _parts_from_parsed(current_path, parsed)
    inferred_tv_show = (
        parsed.kind == "tv"
        and parsed.season is not None
        and parsed.episode is not None
        and not parsed.title
    )
    return {
        "id": _node_id(str(current_path)),
        "type": "file",
        "name": current_path.name,
        "proposed": rebuild_proposed(parts, ext, current_path.name),
        "path": str(current_path),
        "confidence": "medium" if inferred_tv_show else (
            parsed.confidence if parsed.title else "low"
        ),
        "kind": parsed.kind,
        "ext": ext,
        "parts": parts,
        "note": "show inferred from parent folder" if inferred_tv_show else (
            None if parsed.title else "could not parse filename"
        ),
    }


def _new_folder_node(current_path: Path) -> dict:
    return {
        "id": _node_id(str(current_path)),
        "type": "folder",
        "name": current_path.name,
        "proposed": current_path.name,
        "path": str(current_path),
        "children": [],
    }


def build_tree(items: list[dict]) -> dict:
    """Nest a flat list of video paths into a folder tree.

    The root node represents the shortest common ancestor of every file
    and is marked with `is_root=True` so the UI knows not to allow
    renaming it (renaming the root would move the whole library).
    """
    if not items:
        return {**_new_folder_node(Path("/")), "is_root": True, "children": []}

    paths = [Path(it["path"]) for it in items]
    root_path = _common_ancestor(paths)
    root = _new_folder_node(root_path)
    root["is_root"] = True

    # Optional metadata carried through by the ui.
    codec_by_path = {it["path"]: it.get("codec") for it in items}
    size_by_path = {it["path"]: it.get("size") for it in items}

    for p in paths:
        try:
            rel = p.relative_to(root_path)
        except ValueError:
            # Path doesn't live under root — skip; shouldn't happen.
            continue
        cur = root
        cur_path = root_path
        parts = rel.parts
        for i, part in enumerate(parts):
            cur_path = cur_path / part
            is_leaf = i == len(parts) - 1
            existing = next(
                (c for c in cur["children"] if c["name"] == part), None
            )
            if existing is not None:
                cur = existing
                continue
            if is_leaf:
                node = _new_file_node(cur_path)
                node["codec"] = codec_by_path.get(str(p))
                node["size"] = size_by_path.get(str(p))
                cur["children"].append(node)
            else:
                node = _new_folder_node(cur_path)
                cur["children"].append(node)
                cur = node

    _sort_tree(root)
    return root


def _sort_tree(node: dict) -> None:
    if node.get("type") != "folder":
        return
    node["children"].sort(
        key=lambda x: (x["type"] != "folder", x["name"].lower())
    )
    for child in node["children"]:
        _sort_tree(child)


def find_node(root: dict, node_id: str) -> dict | None:
    """DFS lookup by stable ID."""
    if root.get("id") == node_id:
        return root
    for child in root.get("children") or []:
        found = find_node(child, node_id)
        if found is not None:
            return found
    return None


def _find_parent(root: dict, target_id: str) -> dict | None:
    """Return the folder node whose direct children include `target_id`."""
    if root.get("type") != "folder":
        return None
    for child in root.get("children") or []:
        if child.get("id") == target_id:
            return root
        if child.get("type") == "folder":
            found = _find_parent(child, target_id)
            if found is not None:
                return found
    return None


def _new_empty_folder(name: str = "New Folder") -> dict:
    """User-created folder node — no disk source, will be `mkdir`ed on apply."""
    return {
        "id": "new-" + uuid.uuid4().hex[:12],
        "type": "folder",
        "name": "",           # nothing to rename from
        "proposed": name,
        "path": None,         # not on disk yet
        "is_new": True,
        "children": [],
    }


def _suggested_folder_name(node: dict) -> str:
    """Use the clicked row's proposal as a new folder's editable name."""
    proposed = str(node.get("proposed") or node.get("name") or "").strip()
    if node.get("type") == "file":
        ext = str(node.get("ext") or Path(str(node.get("name") or "")).suffix)
        if ext and proposed.lower().endswith(ext.lower()):
            proposed = proposed[:-len(ext)].rstrip()
    return proposed or "New Folder"


def insert_folder_above(root: dict, sibling_id: str,
                        name: str = "New Folder") -> dict | None:
    """Insert a new empty folder as a sibling immediately above `sibling_id`.

    Returns the newly created node, or None if the sibling wasn't found.
    """
    parent = _find_parent(root, sibling_id)
    if parent is None:
        return None
    idx = next(
        (i for i, c in enumerate(parent["children"]) if c["id"] == sibling_id),
        None,
    )
    if idx is None:
        return None
    node = _new_empty_folder(name)
    parent["children"].insert(idx, node)
    return node


def _find_chain(root: dict, node_id: str,
                chain: list[dict] | None = None) -> list[dict] | None:
    """Return ancestor chain from root down to `node_id` (inclusive)."""
    chain = (chain or []) + [root]
    if root.get("id") == node_id:
        return chain
    for child in root.get("children") or []:
        found = _find_chain(child, node_id, chain)
        if found:
            return found
    return None


def split_at_ancestor(root: dict, node_id: str, target_depth: int) -> dict | None:
    """Insert a new folder at `target_depth` in the tree relative to `node_id`.

    Depth is 1-based, excluding the invisible root (so target_depth=1 is
    root's direct children, =2 is grandchildren, etc.).

        - If target_depth == node's own depth: replace the node and all following
            siblings with a new folder containing that extracted tail.
    - If target_depth < node's own depth: create a new folder as the sibling
      immediately AFTER the ancestor at `target_depth`. The node's own
      chain-ancestor at (target_depth + 1) — plus every one of its
      subsequent siblings under the target-depth ancestor — gets moved into
      the new folder. This is the "extract everything from S02E01 onward
      into a new Season 2 sibling of Season 1" operation.
    """
    chain = _find_chain(root, node_id)
    if not chain:
        return None
    node_depth = len(chain) - 1
    if target_depth < 1 or target_depth > node_depth:
        return None
    clicked = chain[-1]
    suggested_name = _suggested_folder_name(clicked)

    if target_depth == node_depth:
        parent = chain[target_depth - 1]
        idx = parent["children"].index(clicked)
        extracted = parent["children"][idx:]
        new_folder = _new_empty_folder(suggested_name)
        new_folder["children"] = extracted
        new_folder["_extracted_from"] = parent["id"]
        parent["children"][idx:] = [new_folder]
        return new_folder

    target_ancestor = chain[target_depth]
    child_to_extract = chain[target_depth + 1]
    grand = chain[target_depth - 1]

    idx = target_ancestor["children"].index(child_to_extract)
    extracted = target_ancestor["children"][idx:]
    target_ancestor["children"] = target_ancestor["children"][:idx]

    new_folder = _new_empty_folder(suggested_name)
    new_folder["children"] = extracted
    # Remember where these children came from so `delete_new_folder`
    # can put them back without visual level shift.
    new_folder["_extracted_from"] = target_ancestor["id"]

    parent_idx = grand["children"].index(target_ancestor)
    grand["children"].insert(parent_idx + 1, new_folder)
    return new_folder


def move_into_previous_folder(root: dict, node_id: str) -> bool:
    """Move `node_id` into the sibling folder immediately preceding it.

    Returns True on success, False if there is no previous folder sibling.
    """
    parent = _find_parent(root, node_id)
    if parent is None:
        return False
    children = parent["children"]
    idx = next((i for i, c in enumerate(children) if c["id"] == node_id), None)
    if idx is None or idx == 0:
        return False
    prev_folder = None
    for i in range(idx - 1, -1, -1):
        if children[i]["type"] == "folder":
            prev_folder = children[i]
            break
    if prev_folder is None:
        return False
    node = children.pop(idx)
    prev_folder["children"].append(node)
    return True


def delete_new_folder(root: dict, node_id: str) -> bool:
    """Remove a user-created empty folder; children go back where they came from.

    Only works on `is_new` folders. If the folder was created via a split
    (`_extracted_from` set), the adopted children are moved back into the
    original parent so their tree depth stays identical to before the split.
    Otherwise the orphans are placed at the deleted folder's own position.
    """
    parent = _find_parent(root, node_id)
    if parent is None:
        return False
    idx = next((i for i, c in enumerate(parent["children"]) if c["id"] == node_id), None)
    if idx is None:
        return False
    node = parent["children"][idx]
    if not node.get("is_new"):
        return False

    orphans = node.get("children") or []
    origin_id = node.get("_extracted_from")
    if origin_id and orphans:
        origin = find_node(root, origin_id)
        if origin is not None and origin.get("type") == "folder":
            origin["children"].extend(orphans)
            del parent["children"][idx]
            return True

    # Fallback: replace the folder with its orphans at the same position.
    parent["children"][idx:idx + 1] = orphans
    return True


# --------------------------------------------------------------------------
# Compute + apply rename operations from a tree
# --------------------------------------------------------------------------

def _collect_ops(
    node: dict, parent_dst: Path | None,
    mkdirs: list[str], folder_renames: list[dict], file_ops: list[dict],
) -> None:
    """Walk tree; classify each node as mkdir (new folder), folder rename, or
    file rename. `parent_dst` is the target path of the parent under
    proposed names.
    """
    is_root = node.get("is_root") is True

    if is_root:
        dst = Path(node["path"])
    else:
        assert parent_dst is not None
        proposed_name = node.get("proposed") or node["name"]
        dst = parent_dst / proposed_name

        if node.get("is_new"):
            if node["type"] == "folder":
                mkdirs.append(str(dst))
            # (New files aren't supported.)
        else:
            src = Path(node["path"])
            if src != dst:
                bucket = folder_renames if node["type"] == "folder" else file_ops
                bucket.append({
                    "src": str(src),
                    "dst": str(dst),
                    "type": node["type"],
                    "id": node["id"],
                })

    if node["type"] == "folder":
        for child in node.get("children", []):
            _collect_ops(child, dst, mkdirs, folder_renames, file_ops)


def _rewrite_after_folder_renames(
    ops: list[dict], folder_renames: list[dict],
) -> None:
    """Substitute each op's `src` prefix so it reflects prior folder renames.

    Folder renames execute before mkdirs and file ops, so a file whose src
    lived under a renamed folder now lives under the new folder name.
    """
    for op in ops:
        src = Path(op["src"])
        for r in folder_renames:
            old = Path(r["src"])
            try:
                relative = src.relative_to(old)
            except ValueError:
                continue
            src = Path(r["dst"]) / relative
        op["src"] = str(src)


def compute_ops(tree: dict) -> list[dict]:
    """Ordered list of ops: folder renames first, then mkdirs, then file ops.

    Ops are annotated with a `kind` field: `folder`, `mkdir`, or `file`.
    """
    mkdirs: list[str] = []
    folder_renames: list[dict] = []
    file_ops: list[dict] = []
    _collect_ops(tree, None, mkdirs, folder_renames, file_ops)

    # Rewrite mkdirs so nested new folders' paths reflect the renamed
    # ancestor. They were already computed against proposed names so this
    # is usually a no-op, but it hardens against odd tree shapes.
    mkdir_ops = [{"src": None, "dst": d, "type": "folder", "kind": "mkdir"}
                 for d in mkdirs]
    _rewrite_after_folder_renames(file_ops, folder_renames)

    # Order: folder renames (their src still uses ORIGINAL parent names,
    # execute top-down); then mkdirs (shortest path first so nested new
    # folders get their parents created); then file ops (their srcs have
    # been rewritten to post-folder-rename paths).
    ordered: list[dict] = []
    for op in folder_renames:
        ordered.append({**op, "kind": "folder"})
    for op in sorted(mkdir_ops, key=lambda o: len(Path(o["dst"]).parts)):
        ordered.append(op)
    for op in file_ops:
        ordered.append({**op, "kind": "file"})
    return ordered


def _rename_one(src: Path, dst: Path) -> None:
    """Atomic same-filesystem rename with parent-dir creation."""
    if dst.exists():
        raise OSError(f"destination already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)


def apply_tree(tree: dict, log_path: Path) -> dict:
    """Execute all pending ops: folder renames, mkdirs, then file moves.

    For file ops we also move any sibling subtitles (matched at execution
    time, so a preceding folder rename is transparent).

    Returns a summary dict with `applied`, `failed`, and a list of results.
    """
    ops = compute_ops(tree)
    results: list[dict] = []
    performed: list[dict] = []
    ok_count = 0
    fail_count = 0

    for op in ops:
        kind = op.get("kind", "rename")

        if kind == "mkdir":
            dst = Path(op["dst"])
            try:
                dst.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                fail_count += 1
                log.error("mkdir failed: %s (%s)", dst, e)
                results.append({**op, "ok": False, "error": str(e)})
                continue
            ok_count += 1
            performed.append({"src": None, "dst": str(dst), "kind": "mkdir"})
            results.append({**op, "ok": True, "error": None})
            continue

        src = Path(op["src"])
        dst = Path(op["dst"])
        # Snapshot sibling subtitles BEFORE the video is renamed; otherwise
        # the video's parent might have changed name (from an earlier op)
        # but the subs still sit next to the OLD video basename in the
        # (already-renamed) parent.
        subs = _find_subtitles(src) if kind == "file" else []
        try:
            _rename_one(src, dst)
        except OSError as e:
            fail_count += 1
            log.error("rename failed: %s -> %s (%s)", src, dst, e)
            results.append({**op, "ok": False, "error": str(e)})
            continue

        ok_count += 1
        performed.append({"src": op["src"], "dst": op["dst"], "kind": kind})

        for sub in subs:
            tail = _subtitle_tail(sub, src.stem)
            sub_dst = dst.with_name(dst.stem + tail)
            try:
                _rename_one(sub, sub_dst)
                performed.append({"src": str(sub), "dst": str(sub_dst),
                                  "kind": "subtitle"})
            except OSError as e:
                log.warning("rename: sub failed %s -> %s (%s)", sub, sub_dst, e)

        results.append({**op, "ok": True, "error": None})

    if performed:
        _append_log(log_path, performed)
    return {"applied": ok_count, "failed": fail_count, "results": results}


# --------------------------------------------------------------------------
# Rename log + undo
# --------------------------------------------------------------------------

def _append_log(log_path: Path, moves: list[dict]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), "moves": moves}
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _replace_log(log_path: Path, batches: list[dict]) -> None:
    """Atomically replace the undo log with `batches`."""
    tmp_path = log_path.with_name(f".{log_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for batch in batches:
                f.write(json.dumps(batch) + "\n")
        tmp_path.replace(log_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def undo_last(log_path: Path) -> dict:
    """Reverse the most recent apply batch."""
    if not log_path.exists():
        return {"ok": False, "error": "no rename log", "reverted": 0}

    with open(log_path, "r", encoding="utf-8") as f:
        batches = [json.loads(line) for line in f if line.strip()]
    if not batches:
        return {"ok": False, "error": "log is empty", "reverted": 0}

    last = batches[-1]
    moves = list(last["moves"])
    reverted = 0
    failures: list[dict] = []
    pending_moves: list[dict] = []

    for index in range(len(moves) - 1, -1, -1):
        move = moves[index]
        try:
            if move.get("kind") == "mkdir":
                # Undo folder creation — only if still empty.
                dst = Path(move["dst"])
                if dst.exists():
                    dst.rmdir()
                reverted += 1
            else:
                _rename_one(Path(move["dst"]), Path(move["src"]))
                reverted += 1
        except OSError as e:
            failures.append({"move": move, "error": str(e)})
            pending_moves = moves[:index + 1]
            break

    if pending_moves:
        batches[-1] = {**last, "moves": pending_moves}
    else:
        batches.pop()
    _replace_log(log_path, batches)

    return {
        "ok": not failures,
        "reverted": reverted,
        "failures": failures,
        "batch_ts": last.get("ts"),
    }
