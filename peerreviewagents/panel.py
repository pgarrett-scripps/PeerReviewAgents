"""The agent roster as configuration sees it: every agent key and model tag.

Config validation (:mod:`peerreviewagents.default_config`) and the CLI's
``--show-panel`` table both need to know which ``[models.<tag>]`` and
``[agent_models.<name>]`` entries can ever be read by the pipeline. That
knowledge lives at the ``make_llm(..., agent=..., default_tag=...)`` call
sites, and a config key that matches none of them is silently inert — the
In Silico journal ran with a ``[models.screen]`` block for weeks before
anyone noticed no agent resolves through a "screen" tag.

Regenerate both sets from the call sites:

    grep -rn 'default_tag=' peerreviewagents/agents/ peerreviewagents/eval/
    grep -rn 'agent='       peerreviewagents/agents/ peerreviewagents/eval/

``tests/test_config_validation.py::test_roster_covers_every_call_site``
re-derives the names from source and fails if this file goes stale.
"""

from __future__ import annotations

from .agents.auditors import ALL_AUDITOR_NAMES
from .agents.reviewers import ALL_REVIEWER_NAMES

# Tags agents resolve through — the `default_tag=` values at the call sites.
# "default" is make_chat_model's own signature fallback, reachable by any
# library caller that passes no tag; no pipeline agent uses it, but a
# [models.default] block is not inert, so it is not warned about.
# NOTE: there is no "screen" tag. The desk screen deliberately shares the
# "reviewer" tag so the prompt cache it warms is the one the panel then reads
# (caches are per-model); the response verifier shares it for the same reason.
KNOWN_TAGS: frozenset[str] = frozenset(
    {"reviewer", "audit", "debate", "synthesis", "default"}
)

# (agent key, default tag, call-site default reasoning effort) in pipeline
# order. The agent key is what an [agent_models.<name>] entry must match; the
# effort is what the call site passes when config leaves `effort` unset.
PIPELINE_AGENTS: tuple[tuple[str, str, str | None], ...] = (
    ("desk_screen", "reviewer", None),
    ("response_verifier", "reviewer", "medium"),
    *[(f"reviewer_{name}", "reviewer", None) for name in ALL_REVIEWER_NAMES],
    *[(f"audit_{name}", "audit", None) for name in ALL_AUDITOR_NAMES],
    ("debate_advocate", "debate", None),
    ("debate_skeptic", "debate", None),
    ("debate_synthesizer", "synthesis", None),
    ("editor", "synthesis", "medium"),
    ("journal_recommender", "synthesis", None),
)

# Agents outside the review graph that still resolve through agent_models
# (the eval harness's single-model baseline).
# Legacy modules remain importable for old integrations, but are not graph
# stages and therefore do not appear in PIPELINE_AGENTS / --show-panel.
EXTRA_AGENTS: frozenset[str] = frozenset(
    {"baseline", "gap_finder", "author_rebuttal"}
)

KNOWN_AGENTS: frozenset[str] = (
    frozenset(name for name, _tag, _effort in PIPELINE_AGENTS) | EXTRA_AGENTS
)
