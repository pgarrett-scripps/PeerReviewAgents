"""Explicit subscription model catalog. Unknown selections never fall back."""
CODEX_MODELS = ("gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra", "gpt-5.6-luna", "gpt-5.5")
# Claude Code subscription models. Fable is excluded because some plans bill it from paid usage credits.
CLAUDE_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5")
PROVIDER_MODELS = {"codex": CODEX_MODELS, "claude-code": CLAUDE_MODELS}
SUBSCRIPTION_MODELS = CODEX_MODELS + CLAUDE_MODELS
# Selecting a provider without a model uses its default. Claude defaults to the latest Sonnet
# because Opus safeguards can refuse biology manuscripts at the first request.
DEFAULT_MODELS = {"codex": "gpt-5.6-terra", "claude-code": "claude-sonnet-5-5"}


def provider_for(model):
    """Return the subscription transport that serves one explicitly selected model."""
    for provider, models in PROVIDER_MODELS.items():
        if model in models:
            return provider
    raise ValueError('Select a supported subscription model explicitly')
