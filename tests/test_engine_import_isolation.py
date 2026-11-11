"""Importing the engine must not drag a server or a UI in with it.

The engine runs reviews on a coding-agent subscription and needs none of
fastapi, uvicorn or the MCP SDK: those belong to `peerreview serve` and to the
MCP transport, which are two surfaces over it. They arrive only through the
lazy re-exports in ``peerreviewagents/__init__.py`` and through the modules
that actually are a server, so an eager import added back there would multiply
the engine's import cost and its install surface for every caller.

The LangGraph panel's own dependencies are listed too. They are no longer
installed at all, so the probe is also the check that nothing has quietly
brought langgraph, langchain or textual back as a review path.

The check runs in a fresh interpreter because an in-process import would see
whatever the rest of the suite already loaded.
"""

from __future__ import annotations

import json
import subprocess
import sys

# Matched on the first dotted component, so langchain_core and langchain_openai
# are covered by "langchain_core"/"langchain_openai".
NOT_THE_ENGINE = (
    "langgraph",
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langchain_anthropic",
    "textual",
    "fastapi",
    "uvicorn",
    "starlette",
    "mcp",
)

_PROBE = """
import json, sys
import peerreviewagents
import peerreviewagents.engine
import peerreviewagents.engine.api
import peerreviewagents.engine.bundle
roots = {name.split('.')[0] for name in sys.modules}
print(json.dumps(sorted(roots & set(json.loads(sys.argv[1])))))
"""


def _loaded_foreign_modules() -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(list(NOT_THE_ENGINE))],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_the_engine_loads_no_server_or_panel_dependency() -> None:
    assert _loaded_foreign_modules() == []


def test_the_lazy_top_level_names_are_the_one_command_review() -> None:
    """The documented library surface must stay the review the surfaces run."""
    import peerreviewagents

    for name in ("capabilities", "plan_review", "review_options", "run_review_options"):
        assert name in dir(peerreviewagents)
        assert callable(getattr(peerreviewagents, name))
    from peerreviewagents import plan_review, review_options, run_review_options
    from peerreviewagents.engine import bundle

    assert (plan_review, review_options, run_review_options) == (
        bundle.plan_review, bundle.review_options, bundle.run_review_options)


def test_the_panel_names_are_gone_rather_than_broken_imports() -> None:
    """`from peerreviewagents import PeerReviewGraph` must say what is wrong."""
    import peerreviewagents

    for name in ("PeerReviewGraph", "get_config", "write_reports", "not_a_real_export"):
        try:
            getattr(peerreviewagents, name)
        except AttributeError as error:
            assert name in str(error)
        else:  # pragma: no cover
            raise AssertionError(f"expected AttributeError for {name}")
