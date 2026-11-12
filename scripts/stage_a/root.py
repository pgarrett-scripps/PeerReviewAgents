"""Where the Stage A data directory is, which this repository does not decide.

The corpus, the altered copies, the answer keys and the run folders all live
outside the repository: they are other people's papers, with one error planted
in a copy, and a key that is only worth having while it stays private. So there
is no default path here. The directory is named once, by whoever runs the
scripts, either with ``--root`` or in the ``PRA_STAGE_A_ROOT`` environment
variable, and a script that is given neither says so and stops rather than
guessing a path on someone's machine and writing into it.
"""

from __future__ import annotations

import os
from pathlib import Path

VARIABLE = 'PRA_STAGE_A_ROOT'
MESSAGE = (f'no Stage A data directory: pass --root or set {VARIABLE} '
           '(it is a directory outside this repository)')


class MissingRoot(Exception):
    """Neither ``--root`` nor the environment named the data directory."""

    def __init__(self, message: str = MESSAGE):
        super().__init__(message)


def resolve_root(given: str | None = None, environ=None) -> Path:
    """The data directory from ``--root``, else from the environment.

    Raises ``MissingRoot`` when neither names one. An empty or blank value
    counts as not named, so ``--root ''`` fails the same way as omitting it
    instead of resolving to the working directory.
    """
    env = os.environ if environ is None else environ
    for value in (given, env.get(VARIABLE, '')):
        if value and value.strip():
            return Path(value.strip()).expanduser()
    raise MissingRoot()
