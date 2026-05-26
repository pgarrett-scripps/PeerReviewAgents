"""On-disk cache for parsed manuscripts.

`load_manuscript()` is the expensive bit of the pipeline — marker can take
minutes on a typical PDF, and the optional vision pass adds one LLM call
per figure. Because the output is deterministic given (file contents,
vision config), we hash both and store the resulting (title, text,
sections) triple as JSON. Subsequent runs on the same file with the same
settings return the cached triple instantly.

Cache lives under ``$XDG_CACHE_HOME/peerreviewagents/manuscripts/`` (or
``~/.cache/peerreviewagents/manuscripts/``). One JSON file per entry,
named by the cache key, so it's easy to inspect or wipe individual
entries by hand.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1

# Config keys that change what `load_manuscript` produces. Anything not
# in here is irrelevant to the cache key — keep this list tight so we
# don't bust the cache on unrelated config changes (e.g. provider).
_CACHE_AFFECTING_KEYS = (
    "vision_enabled",
    "vision_provider",
    "vision_model",
    "vision_prompt",
    "vision_max_figures",
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
    # File extension matters: a .pdf and a .md with identical bytes (silly,
    # but possible) take different code paths.
    h.update(p.suffix.lower().encode())
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    # Cache-affecting config slice, deterministically serialized.
    slice_ = {k: config.get(k) for k in _CACHE_AFFECTING_KEYS}
    h.update(json.dumps(slice_, sort_keys=True, default=str).encode())
    return h.hexdigest()


def _entry_path(key: str, config: dict | None = None) -> Path:
    return cache_root(config) / f"{key}.json"


def get(key: str, config: dict | None = None) -> tuple[str, str, dict[str, str]] | None:
    """Return the cached (title, text, sections) triple, or None on miss."""
    path = _entry_path(key, config)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    if payload.get("schema_version") != _SCHEMA_VERSION:
        return None
    return payload["title"], payload["text"], payload["sections"]


def put(
    key: str,
    title: str,
    text: str,
    sections: dict[str, str],
    *,
    source_path: str | os.PathLike,
    config: dict | None = None,
) -> Path:
    """Write a cache entry. Returns the path written."""
    root = cache_root(config)
    root.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "title": title,
        "text": text,
        "sections": sections,
        "meta": {
            "source_path": str(source_path),
            "cached_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "vision_enabled": bool((config or {}).get("vision_enabled")),
            "vision_model": (config or {}).get("vision_model"),
        },
    }
    path = _entry_path(key, config)
    # Atomic-ish write: temp file + rename.
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    tmp.replace(path)
    return path


def clear(config: dict | None = None) -> int:
    """Delete every cache entry. Returns the number of entries removed."""
    root = cache_root(config)
    if not root.is_dir():
        return 0
    removed = 0
    for entry in root.glob("*.json"):
        try:
            entry.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def stats(config: dict | None = None) -> dict[str, Any]:
    """Quick inventory of what's cached. Useful for `just cache-info`."""
    root = cache_root(config)
    if not root.is_dir():
        return {"root": str(root), "entries": 0, "bytes": 0}
    entries = list(root.glob("*.json"))
    total_bytes = sum(p.stat().st_size for p in entries)
    return {"root": str(root), "entries": len(entries), "bytes": total_bytes}
