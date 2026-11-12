"""The Stage A scripts are scripts, not part of the package, so put them on the path.

They live in `scripts/stage_a/` because they are the harness of one experiment
rather than part of the reviewer, and nothing in `peerreviewagents` imports
them. The repository's own `tests/conftest.py` already strips the API keys and
keeps the `claude` CLI out of the way, so everything here runs offline.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts' / 'stage_a'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
