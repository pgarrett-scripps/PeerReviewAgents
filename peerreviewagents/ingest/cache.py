"""On-disk cache for parsed manuscripts.

Caches the ``(title, text, sections)`` triple produced by :mod:`.loader`.
Keyed by file content, extension, and the ingest config (see
:func:`.loader.ingest_config`); the same manuscript re-parses instantly on a
second run.

Layout: one directory per entry under
``$XDG_CACHE_HOME/peerreviewagents/manuscripts/<key>/``::

    <key>/
        metadata.json    # schema_version, title, sections, ingest, meta
        manuscript.md    # extracted text
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

# v4: dropped image caching; entries are just text + metadata.
# v5: the key covers the ingest config as well as the file. While a PDF had
# exactly one possible reading that was unnecessary; the caveman level changes
# the text, and keying on bytes alone would serve a compressed manuscript to a
# run that asked for an uncompressed one. Entries from earlier versions are
# invalidated automatically.
# v6: the ingest record carries deterministic text statistics (see
# :mod:`.prose`). A v5 entry has none, and serving one would report a
# manuscript as having no measurable conversion damage when it was simply
# never measured — so those entries re-parse rather than answer the question
# wrongly.
_SCHEMA_VERSION = 6


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
    """sha256 over (ingest config, file extension, file bytes).

    The config slice comes first so that a change to which knobs count is a
    visible edit here rather than a silent collision. Note that this key is
    recorded in a round record and re-derived a round later to recover the
    previous draft — so anything that changes it also invalidates every
    revision baseline, and belongs behind a schema-version bump.
    """
    from .loader import ingest_config

    h = hashlib.sha256()
    p = Path(path)
    h.update(json.dumps(ingest_config(config), sort_keys=True).encode())
    h.update(p.suffix.lower().encode())
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _entry_dir(key: str, config: dict | None = None) -> Path:
    return cache_root(config) / key


def get(key: str, config: dict | None = None):
    """Return the cached :class:`~.loader.Manuscript`, or ``None`` on miss."""
    from .loader import Manuscript

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
    return Manuscript(
        title=meta.get("title", ""),
        text=text,
        sections=meta.get("sections", {}),
        # Cached alongside the text so that a served entry reports the same
        # provenance the original parse did. Without it the second review of
        # a paper would publish "read via pypdf" for text rustypaper produced.
        ingest=meta.get("ingest", {}),
    )


def put(
    key: str,
    manuscript,
    *,
    source_path: str | os.PathLike,
    config: dict | None = None,
) -> Path:
    """Write a cache entry. Returns the entry directory path."""
    entry = _entry_dir(key, config)
    entry.mkdir(parents=True, exist_ok=True)

    meta: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "title": manuscript.title,
        "sections": manuscript.sections,
        "ingest": manuscript.ingest,
        "meta": {
            "source_path": str(source_path),
            "cached_at": _dt.datetime.now().isoformat(timespec="seconds"),
        },
    }

    (entry / "manuscript.md").write_text(manuscript.text, encoding="utf-8")

    # metadata.json (atomic-ish write)
    tmp_meta = entry / "metadata.json.tmp"
    with tmp_meta.open("w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    tmp_meta.replace(entry / "metadata.json")

    # Clean up any stale figures/ dir left over from a v3 entry that
    # somehow survived the schema-version check.
    stale_figs = entry / "figures"
    if stale_figs.is_dir():
        shutil.rmtree(stale_figs, ignore_errors=True)

    return entry


def clear(config: dict | None = None) -> int:
    """Delete every cache entry. Returns the number of entries removed."""
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
