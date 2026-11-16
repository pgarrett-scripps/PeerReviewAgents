import hashlib
import json
import os
from pathlib import Path, PurePath
import tempfile


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def relative_name(path, root):
    """Where ``path`` sits under ``root``, in the one form every OS reads.

    A path that goes into a published file or into a message is not a path on
    the machine that wrote it any more: it is a name the authors, the next
    round and an integration read, and they read it on whatever OS they have.
    ``str()`` of a relative path writes the separator of the OS that happened
    to run the review, so the same review named ``review/editor-log.jsonl`` on
    Linux and ``review\\editor-log.jsonl`` on Windows, and anything matching
    the one got nothing from the other. POSIX form, always, and the absolute
    path — which names somebody's home directory — never.
    """
    if not isinstance(path, PurePath):
        path = Path(path)
    return path.relative_to(root).as_posix()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        # Every run file is UTF-8, explicitly: the manuscript decides what is in
        # it, and the platform's default encoding has no say in that.
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, indent=2, ensure_ascii=False)
            out.write('\n')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
