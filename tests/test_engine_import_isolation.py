"""The engine must not drag the legacy panel's dependencies in with it.

``peerreviewagents.engine`` runs reviews on a coding-agent subscription and
needs none of langgraph, langchain, textual, fastapi or uvicorn. Those arrive
only through the lazy re-exports in ``peerreviewagents/__init__.py``, so an
eager import added back there would quietly multiply the engine's import cost
and its install surface. The check runs in a fresh interpreter because an
in-process import would see whatever the rest of the suite already loaded.
"""

from __future__ import annotations

import json
import subprocess
import sys

# Top-level distributions the legacy LangGraph panel and web UI need and the
# engine does not. Matched on the first dotted component, so langchain_core
# and langchain_openai are covered by "langchain_core"/"langchain_openai".
LEGACY_ONLY = (
    "langgraph",
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langchain_anthropic",
    "textual",
    "fastapi",
    "uvicorn",
)

_PROBE = """
import json, sys
import peerreviewagents.engine
import peerreviewagents.engine.api
roots = {name.split('.')[0] for name in sys.modules}
print(json.dumps(sorted(roots & set(json.loads(sys.argv[1])))))
"""


def _loaded_legacy_modules() -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(list(LEGACY_ONLY))],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_the_engine_loads_no_legacy_dependency() -> None:
    assert _loaded_legacy_modules() == []


def test_the_lazy_top_level_names_still_import() -> None:
    """The lazy __getattr__ must keep the documented library surface intact."""
    import peerreviewagents

    for name in ("PeerReviewGraph", "get_config", "write_reports"):
        assert name in dir(peerreviewagents)
        assert getattr(peerreviewagents, name) is not None
    from peerreviewagents import PeerReviewGraph, get_config, write_reports

    assert all(callable(obj) for obj in (PeerReviewGraph, get_config, write_reports))


def test_an_unknown_top_level_name_still_raises_attribute_error() -> None:
    import peerreviewagents

    try:
        peerreviewagents.not_a_real_export
    except AttributeError as error:
        assert "not_a_real_export" in str(error)
    else:  # pragma: no cover
        raise AssertionError("expected AttributeError for an unknown attribute")
