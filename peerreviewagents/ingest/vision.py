"""Vision-model pass that injects figure descriptions into the parsed markdown.

Runs once at ingest time. For each `![](filename)` image reference in the
markdown returned by the Datalab marker API, the corresponding PIL.Image
is sent to the configured `vision_model` via OpenRouter. Its prose
description is appended below the image reference so downstream
text-only reviewer LLMs can reason about figure content.
"""

from __future__ import annotations

import base64
import io
import re
from typing import Any

from langchain_core.messages import HumanMessage

from ..agents.utils.llm import make_vision_llm
from ..observability import AgentEvent, emit, node_context

# Academic figures are dense and multi-panel. The default prompt asks for
# a thorough, structured description so downstream reviewers have enough
# detail to critique methodology, data, and presentation from text alone.
_VISION_PROMPT = (
    "You are inspecting a figure from a scholarly manuscript. Produce a "
    "thorough, structured description that a text-only reviewer can use "
    "to critique the figure without seeing it. Cover, in this order:\n\n"
    "1. **Figure type and layout.** Single panel or multi-panel? If "
    "multi-panel, list each subpanel (A, B, C…) and its individual type "
    "(bar chart, line plot, scatter, heatmap, photomicrograph, gel image, "
    "western blot, schematic, flowchart, microscopy, structural diagram, "
    "phylogenetic tree, etc.).\n"
    "2. **Axes, scales, and units.** For each plot, state x-axis and "
    "y-axis labels with units, the scale (linear/log), and the visible "
    "range. Note tick spacing if irregular. For images, note magnification "
    "or scale bars if shown.\n"
    "3. **Data series, conditions, and legend.** Enumerate every series, "
    "group, condition, treatment, or label shown. Quote legend text "
    "verbatim. Identify the color/shape encoding.\n"
    "4. **Quantitative observations.** Report the main numbers a reviewer "
    "would want: approximate values, peak/trough locations, fold changes, "
    "ranges, error-bar magnitudes, sample sizes if printed (n=…), "
    "p-values or significance asterisks if shown.\n"
    "5. **Main finding or comparison.** State, in 1-2 sentences, what the "
    "figure is intended to demonstrate.\n"
    "6. **Caption text (if visible).** Quote any visible caption, title, "
    "or annotation text verbatim.\n"
    "7. **Quality and integrity issues.** Flag anything a reviewer should "
    "scrutinize: missing error bars, missing n, missing scale bar, "
    "truncated/non-zero y-axes that exaggerate effects, mislabeled or "
    "swapped axes, inconsistent units, illegible text, suspicious "
    "duplication or splicing in blots/images, low resolution, cropped "
    "legends, overplotting that hides data, broken axes, color choices "
    "inaccessible to colorblind readers. Say 'none observed' if nothing "
    "stands out — do not invent issues.\n\n"
    "Be precise and literal. Quote text shown in the figure verbatim "
    "where possible. Do not speculate about what the data 'means' beyond "
    "what is visible. Do not summarize — be exhaustive within each "
    "section above."
)

_IMG_REF_RE = re.compile(r"!\[\]\(([^)]+)\)")


def describe_figures_inline(markdown: str, images: dict, config: dict) -> str:
    """Append a vision-model description below each image reference in `markdown`."""
    if not images:
        return markdown

    with node_context("vision"):
        llm = make_vision_llm(config)
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
            emit(AgentEvent(
                kind="log",
                node="vision",
                text=f"describing figure {seen}/{min(cap, len(images))}: {fname}",
            ))
            try:
                description = _describe_one(llm, img)
            except Exception as exc:  # noqa: BLE001
                description = f"[vision model failed: {exc}]"
            return f"{match.group(0)}\n\n**Figure visual analysis:** {description}\n"

        return _IMG_REF_RE.sub(replace, markdown)


def _describe_one(llm: Any, pil_image: Any) -> str:
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    content = [
        {"type": "text", "text": _VISION_PROMPT},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]
    resp = llm.invoke([HumanMessage(content=content)])
    if isinstance(resp.content, str):
        return resp.content
    return "".join(
        b.get("text", "") if isinstance(b, dict) else str(b) for b in resp.content
    )
