"""Immutable flat-directory manifests for controlled source evidence."""
import re
from pathlib import Path

from .storage import digest


def source_packet_manifest(path):
    root = Path(path).resolve()
    if not root.is_dir():
        raise ValueError('Source packet directory is missing')
    manifest = {}
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.is_symlink():
            raise ValueError('Source packets cannot contain symlinks')
        if not entry.is_file():
            raise ValueError('Source packets must be flat directories of regular files')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}', entry.name):
            raise ValueError('Source packet filename is invalid')
        manifest[entry.name] = digest(entry.read_bytes())
    if not manifest:
        raise ValueError('Source packet cannot be empty')
    return manifest


def verify_source_packet(path, expected):
    actual = source_packet_manifest(path)
    if actual != dict(expected):
        raise ValueError('Frozen source packet changed')
    return actual
