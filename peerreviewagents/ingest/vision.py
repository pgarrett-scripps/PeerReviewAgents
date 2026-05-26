"""Vision-model pass that injects figure descriptions into marker's markdown.

Runs once at ingest time. For each `![](filename)` image reference that
marker emitted, the corresponding PIL.Image is sent to a configured vision
model whose short prose description is appended below the reference so
downstream text-only reviewer LLMs can reason about figure content.
"""

from __future__ import annotations

import base64
import io
import re
from typing import Any

from langchain_core.messages import HumanMessage

_DEFAULT_PROMPT = (
    "You are inspecting a figure from a scholarly manuscript. In 3-6 sentences, describe:\n"
    "1. Figure type (bar/line/scatter plot, photomicrograph, schematic, gel image, etc.)\n"
    "2. What is on each axis or what is depicted, with units if visible\n"
    "3. The main visual trend, comparison, or finding shown\n"
    "4. Any obvious issues — mislabeled axes, missing error bars, suspicious patterns, "
    "low resolution, cropped legends. Say nothing if there is nothing to flag.\n"
    "Be precise. Do not speculate beyond what the image shows."
)

_IMG_REF_RE = re.compile(r"!\[\]\(([^)]+)\)")


def describe_figures_inline(markdown: str, images: dict, config: dict) -> str:
    """Append a vision-model description below each image reference in `markdown`."""
    if not images:
        return markdown

    llm = _make_vision_llm(config)
    prompt = config.get("vision_prompt") or _DEFAULT_PROMPT
    provider = config.get("vision_provider") or config["provider"]
    cap = int(config.get("vision_max_figures", 10))
    seen = 0

    def replace(match: re.Match) -> str:
        nonlocal seen
        if seen >= cap:
            return match.group(0)
        fname = match.group(1)
        img = images.get(fname)
        if img is None:
            return match.group(0)
        seen += 1
        try:
            description = _describe_one(llm, prompt, img, provider)
        except Exception as exc:  # noqa: BLE001
            description = f"[vision model failed: {exc}]"
        return f"{match.group(0)}\n\n**Figure visual analysis:** {description}\n"

    return _IMG_REF_RE.sub(replace, markdown)


def _describe_one(llm: Any, prompt: str, pil_image: Any, provider: str) -> str:
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    content = [
        {"type": "text", "text": prompt},
        _image_block(b64, provider),
    ]
    resp = llm.invoke([HumanMessage(content=content)])
    if isinstance(resp.content, str):
        return resp.content
    return "".join(
        b.get("text", "") if isinstance(b, dict) else str(b) for b in resp.content
    )


def _image_block(b64: str, provider: str) -> dict:
    if provider == "anthropic":
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        }
    # openai, openrouter, google, ollama: OpenAI-style data URL.
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}


def _make_vision_llm(config: dict) -> Any:
    from ..agents.utils.llm import _PROVIDERS

    provider = config.get("vision_provider") or config["provider"]
    model = config.get("vision_model")
    if not model:
        raise ValueError(
            "vision_enabled=true but vision_model is not set in config"
        )
    if provider not in _PROVIDERS:
        raise ValueError(
            f"Unknown vision_provider '{provider}'. Options: {sorted(_PROVIDERS)}"
        )
    base_url = config.get("vision_base_url") or config.get("base_url")
    temperature = float(config.get("vision_temperature", 0.2))
    return _PROVIDERS[provider](model, temperature, base_url)
