"""Default configuration for PeerReviewAgents.

Mirrors the TradingAgents config pattern: a plain dict that callers copy and
override. A deep model is used for synthesis/judgement, a quick model for the
parallel reviewer pass.
"""

import os

DEFAULT_CONFIG = {
    # --- LLM provider / models ---
    # provider: one of "anthropic", "openai", "google", "openrouter", "ollama"
    "provider": "anthropic",
    "deep_think_llm": "claude-opus-4-7",
    "quick_think_llm": "claude-haiku-4-5-20251001",
    "temperature": 0.3,
    # Base URL override (used by openrouter / ollama / custom gateways)
    "base_url": None,

    # --- Workflow controls ---
    # Which specialist reviewers to run in the parallel pass.
    "reviewer_set": ["methodology", "data_analysis", "novelty", "clarity", "literature"],
    "max_debate_rounds": 2,
    "max_integrity_rounds": 1,

    # --- Research / grounding layer ---
    "research_enabled": True,
    # Tools the research layer may use when research_enabled is True.
    "research_tools": ["web", "arxiv", "scholar"],
    # Auto-detect scientific manuscripts and enable domain MCP grounding.
    "domain_grounding": False,

    # --- Output ---
    "output_dir": os.path.join(os.getcwd(), "reports"),
    "emit_pdf": False,
    # Include an accept/minor/major/reject verdict in the editor decision.
    "emit_verdict": True,

    # --- Persistence ---
    "memory_path": os.path.join(
        os.path.expanduser("~"), ".peerreviewagents", "memory", "review_memory.md"
    ),
    "checkpoint": False,
    "checkpoint_path": os.path.join(
        os.path.expanduser("~"), ".peerreviewagents", "checkpoints", "review.sqlite"
    ),

    # --- Runtime ---
    "debug": False,
}


def get_config(**overrides):
    cfg = DEFAULT_CONFIG.copy()
    cfg.update(overrides)
    return cfg
