"""Importing the package must not drag a server or a UI in with it.

A review now runs in a headless Claude Code session, so nothing in this
package needs fastapi, uvicorn or the MCP SDK to start one: the plugin's own
MCP server is launched by Claude Code, in its own process, and imports the SDK
there. The web UI and the standalone MCP transport that used to need them are
gone with the Python orchestration they drove, so an eager import added back
here would multiply the install surface for every caller and bring back a
dependency that no review path uses.

The LangGraph panel's dependencies are listed too. They are not installed at
all, so the probe is also the check that nothing has quietly brought langgraph,
langchain or textual back as a review path.

The check runs in a fresh interpreter because an in-process import would see
whatever the rest of the suite already loaded.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

# Matched on the first dotted component, so langchain_core and langchain_openai
# are covered by "langchain_core"/"langchain_openai".
NOT_A_REVIEW = (
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

# What a consumer imports: the package, the engine, the plugin that runs the
# review and the report writer that renders it.
_PROBE = """
import json, sys
import peerreviewagents
import peerreviewagents.engine
import peerreviewagents.engine.run_report
import peerreviewagents.plugin.server
import peerreviewagents.plugin.starter
import peerreviewagents.cli.main
roots = {name.split('.')[0] for name in sys.modules}
print(json.dumps(sorted(roots & set(json.loads(sys.argv[1])))))
"""

# What 0.8 shipped. Each one is deleted, not deprecated, so an import of it
# must fail rather than find a shim.
GONE = (
    "peerreviewagents.engine.api",
    "peerreviewagents.engine.bundle",
    "peerreviewagents.engine.ensemble",
    "peerreviewagents.engine.ensemble_debate",
    "peerreviewagents.engine.ensemble_references",
    "peerreviewagents.engine.reviewer",
    "peerreviewagents.engine.model_runtime",
    "peerreviewagents.engine.models",
    "peerreviewagents.engine.review_policy",
    "peerreviewagents.engine.review_schema",
    "peerreviewagents.engine.investigation_policy",
    "peerreviewagents.engine.manuscript_tools",
    "peerreviewagents.engine.discovery",
    "peerreviewagents.engine.baseline",
    "peerreviewagents.engine.config",
    "peerreviewagents.review_service",
    "peerreviewagents.web",
    "peerreviewagents.web.server",
    "peerreviewagents.mcp",
    "peerreviewagents.mcp.server",
)


def _loaded_foreign_modules() -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(list(NOT_A_REVIEW))],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_a_review_loads_no_server_or_panel_dependency() -> None:
    assert _loaded_foreign_modules() == []


def test_the_lazy_top_level_names_are_the_review_the_plugin_runs() -> None:
    """The documented library surface must stay what the command itself runs."""
    import peerreviewagents
    from peerreviewagents.engine.ingest import import_paper
    from peerreviewagents.engine.run_report import write_reports
    from peerreviewagents.plugin.starter import review

    for name in ("review", "import_paper", "write_reports"):
        assert name in dir(peerreviewagents)
        assert callable(getattr(peerreviewagents, name))
    assert (peerreviewagents.review, peerreviewagents.import_paper,
            peerreviewagents.write_reports) == (review, import_paper, write_reports)


@pytest.mark.parametrize("name", [
    "PeerReviewGraph", "get_config", "run_review", "ReviewRequest", "plan_review",
    "review_options", "run_review_options", "capabilities", "not_a_real_export"])
def test_an_old_top_level_name_says_what_is_wrong(name: str) -> None:
    """``from peerreviewagents import run_review`` must name what it asked for."""
    import peerreviewagents

    with pytest.raises(AttributeError, match=name):
        getattr(peerreviewagents, name)


@pytest.mark.parametrize("module", GONE)
def test_the_orchestration_is_deleted_rather_than_shimmed(module: str) -> None:
    with pytest.raises(ImportError):
        __import__(module)
