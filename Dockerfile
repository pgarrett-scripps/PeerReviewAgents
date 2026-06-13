# PeerReviewAgents — container image for the FastAPI web UI.
#
# Builds with uv against the committed uv.lock for reproducible installs,
# then runs `peerreview serve` as a non-root user.
#
#   docker build -t peerreviewagents .
#   docker run -p 8765:8765 --env-file .env peerreviewagents
#
# See docker-compose.yml for the recommended deployment (volumes + config).

FROM python:3.13-slim

# uv: fast, lockfile-aware installer. Pulled from the official static image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # Put the cache somewhere the runtime user owns (see HOME below).
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 1) Dependency layer — cached unless pyproject.toml / uv.lock change.
#    README.md is referenced by pyproject's `readme` field.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra research

# 2) Project layer — source code + the editable install of the package.
COPY peerreviewagents ./peerreviewagents
COPY cli ./cli
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra research

# Non-root runtime user. HOME=/app so the manuscript cache lands in
# /app/.cache/peerreviewagents (a declared volume), not a throwaway layer.
RUN useradd --create-home --home-dir /app --shell /bin/bash app 2>/dev/null || true \
    && mkdir -p /app/reports /app/.peerreview-uploads /app/.cache \
    && chown -R app:app /app
ENV HOME=/app
USER app

EXPOSE 8765

# Bind to 0.0.0.0 so the server is reachable from outside the container.
CMD ["peerreview", "serve", "--host", "0.0.0.0", "--port", "8765"]
