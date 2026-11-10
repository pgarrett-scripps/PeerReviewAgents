"""Evidence-linked review engine: manuscript workspaces, reviewers and synthesis.

This subpackage is the headless engine. Its public, versioned API is the same
surface that used to be ``paper_workbench_core``, so a downstream shim can
re-export it unchanged:

    from peerreviewagents.engine import api

Importing this package starts no model provider and reads no manuscript.
"""
from .api import (
    API_VERSION, ReviewRequest, capabilities, load_manuscript, evidence_registry,
    validate_review, run_review, StructuredRequest, run_structured, usage_summary,
    review_request_from_preset,
    SavedDraftSynthesisRequest, run_saved_draft_synthesis,
)
from .config import CONFIG_VERSION, review_presets, resolved_review_config

# Tracks the distribution version in pyproject.toml.
__version__ = '0.7.0'
__all__ = ['API_VERSION', 'ReviewRequest', 'capabilities', 'load_manuscript',
           'evidence_registry', 'validate_review', 'run_review', 'StructuredRequest',
           'run_structured', 'usage_summary', 'CONFIG_VERSION', 'review_presets',
           'resolved_review_config', 'review_request_from_preset']
__all__ += ['SavedDraftSynthesisRequest', 'run_saved_draft_synthesis']
