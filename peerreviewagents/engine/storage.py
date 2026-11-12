import hashlib
import json
import os
from pathlib import Path
import tempfile


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


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
