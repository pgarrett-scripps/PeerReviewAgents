"""The SPOT scorer is a script, so put it on the path — under a name of its own.

``scripts/spot/score.py`` and ``scripts/stage_a/score.py`` are both called
``score``, and the spot one imports the Stage A one as ``score``. A test that
imported the spot file as ``score`` would take that name from the library it
depends on, and whichever test module ran first would decide which file the
other one got. So the spot script is loaded here under ``spot_score``, by path,
after its own imports are made reachable. Nothing else in the suite is affected.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
for folder in (SCRIPTS / 'stage_a', SCRIPTS / 'spot'):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

if 'spot_score' not in sys.modules:
    SPEC = importlib.util.spec_from_file_location('spot_score', SCRIPTS / 'spot' / 'score.py')
    MODULE = importlib.util.module_from_spec(SPEC)
    sys.modules['spot_score'] = MODULE
    SPEC.loader.exec_module(MODULE)
