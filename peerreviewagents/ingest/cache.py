"""On-disk cache for parsed manuscripts.

Caches **only** the Datalab parse output (the markdown and the extracted
figure images). The vision-model pass that turns figure images into
prose descriptions runs every time, on top of the cached parse — that
way swapping ``vision_model`` doesn't bust the expensive PDF parse, and
each review run gets fresh figure descriptions.

Layout: one directory per entry under
``$XDG_CACHE_HOME/peerreviewagents/manuscripts/<key>/``::

    <key>/
        metadata.json    # schema_version, title, sections, meta
        manuscript.md    # raw Datalab markdown (NO vision annotations)
        figures/         # extracted figure PNGs (filenames as Datalab returned)

The cache key is sha256 over (file bytes, file extension, the slice of
``config`` that actually changes the Datalab output). Vision-model
settings are deliberately excluded from the key.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

# v3: cache layout changed from a single JSON blob to a folder per entry
# (manuscript.md + figures/), and vision-model results are no longer
# cached — they're applied post-cache so each run gets fresh figure
# descriptions. Invalidates v1 and v2 entries automatically.
_SCHEMA_VERSION = 3

# Config keys that change what the *Datalab parse* produces. Anything not
# in here is irrelevant to the cache key — keep this list tight so we
# don't bust the cache on unrelated config changes. Note `vision_model`
# and `vision_max_figures` are intentionally absent: vision runs on top
# of the cached parse output, not inside it.
_CACHE_AFFECTING_KEYS = (
    "pdf_force_ocr",
    "pdf_use_llm",
    "pdf_max_pages",
    "pdf_page_range",
    "pdf_langs",
)


def cache_root(config: dict | None = None) -> Path:
    """Resolve the cache directory. Config overrides env overrides default."""
    config = config or {}
    custom = config.get("cache_dir")
    if custom:
        return Path(custom).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return Path(base) / "peerreviewagents" / "manuscripts"


def cache_key(path: str | os.PathLike, config: dict | None = None) -> str:
    """sha256 over (file bytes, file extension, cache-affecting config)."""
    config = config or {}
    h = hashlib.sha256()
    p = Path(path)
    h.update(p.suffix.lower().encode())
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    slice_ = {k: config.get(k) for k in _CACHE_AFFECTING_KEYS}
    h.update(json.dumps(slice_, sort_keys=True, default=str).encode())
    return h.hexdigest()


def _entry_dir(key: str, config: dict | None = None) -> Path:
    return cache_root(config) / key


def get(
    key: str, config: dict | None = None
) -> tuple[str, str, dict[str, str], dict] | None:
    """Return (title, raw_markdown, sections, images) or None on miss.

    ``images`` is a dict mapping the figure filename to a ``PIL.Image``
    — the same shape :mod:`.vision` expects. The returned markdown is
    the raw Datalab output without vision annotations; the caller is
    responsible for running the vision pass on top of it.
    """
    entry = _entry_dir(key, config)
    meta_path = entry / "metadata.json"
    md_path = entry / "manuscript.md"
    if not meta_path.is_file() or not md_path.is_file():
        return None
    try:
        with meta_path.open("r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    if meta.get("schema_version") != _SCHEMA_VERSION:
        return None
    try:
        with md_path.open("r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    images = _load_figures(entry / "figures")
    return (
        meta.get("title", ""),
        text,
        meta.get("sections", {}),
        images,
    )


def _load_figures(figures_dir: Path) -> dict:
    """Re-hydrate cached figure PNGs as PIL.Image objects."""
    if not figures_dir.is_dir():
        return {}
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        # PIL is an optional dep; without it the vision pass can't run
        # anyway, so returning an empty dict is the right fallback.
        return {}
    out: dict = {}
    for path in sorted(figures_dir.iterdir()):
        if not path.is_file():
            continue
        try:
            # Eagerly materialize the pixels and close the file handle —
            # PIL otherwise lazy-loads from disk and breaks if anything
            # later mutates or rotates the cache directory.
            with Image.open(path) as img:
                out[path.name] = img.copy()
        except Exception:  # noqa: BLE001
            continue
    return out


def put(
    key: str,
    title: str,
    text: str,
    sections: dict[str, str],
    images: dict | None,
    *,
    source_path: str | os.PathLike,
    config: dict | None = None,
) -> Path:
    """Write a cache entry. Returns the entry directory path."""
    entry = _entry_dir(key, config)
    entry.mkdir(parents=True, exist_ok=True)

    meta: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "title": title,
        "sections": sections,
        "meta": {
            "source_path": str(source_path),
            "cached_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "figure_count": len(images or {}),
        },
    }

    # manuscript.md
    (entry / "manuscript.md").write_text(text, encoding="utf-8")

    # metadata.json (atomic-ish write)
    tmp_meta = entry / "metadata.json.tmp"
    with tmp_meta.open("w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    tmp_meta.replace(entry / "metadata.json")

    # figures/
    figures_dir = entry / "figures"
    if images:
        figures_dir.mkdir(exist_ok=True)
        # Clear any stale figures from a prior version of this entry so
        # the directory matches the new image set exactly.
        for stale in figures_dir.iterdir():
            if stale.is_file():
                try:
                    stale.unlink()
                except OSError:
                    pass
        for fname, img in images.items():
            try:
                img.save(figures_dir / fname, format="PNG")
            except Exception:  # noqa: BLE001
                # A single bad image must not nuke the rest of the cache
                # entry; the vision pass will silently skip it next run.
                continue
    return entry


def clear(config: dict | None = None) -> int:
    """Delete every cache entry (and any v1/v2 legacy JSON blobs).
    Returns the number of entries removed."""
    root = cache_root(config)
    if not root.is_dir():
        return 0
    removed = 0
    for entry in root.iterdir():
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
                removed += 1
            elif entry.is_file():
                # Legacy single-file v1/v2 entries.
                entry.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def stats(config: dict | None = None) -> dict[str, Any]:
    """Quick inventory of what's cached. Useful for `just cache-info`."""
    root = cache_root(config)
    if not root.is_dir():
        return {"root": str(root), "entries": 0, "bytes": 0, "legacy_entries": 0}
    entries = [e for e in root.iterdir() if e.is_dir()]
    legacy = [e for e in root.iterdir() if e.is_file() and e.suffix == ".json"]
    total_bytes = 0
    for entry in entries:
        for sub in entry.rglob("*"):
            if sub.is_file():
                try:
                    total_bytes += sub.stat().st_size
                except OSError:
                    pass
    for f in legacy:
        try:
            total_bytes += f.stat().st_size
        except OSError:
            pass
    return {
        "root": str(root),
        "entries": len(entries),
        "bytes": total_bytes,
        "legacy_entries": len(legacy),
    }
