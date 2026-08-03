"""Deterministic text statistics over a converted manuscript.

No model, no API call, no judgment. Everything here is a count over the text
the panel is about to read, produced at ingest time and published alongside
the rest of the provenance on ``state["ingest"]``.

Three groups, in descending order of how much they can be trusted:

* **Health** (:class:`Health`) — how well the PDF converted. Valid on every
  document, and the gate for everything else. A manuscript that arrives as
  ``well-definedsitecanbeengaged`` has not been read, and the panel should
  not be the thing that discovers that.
* **Counts** (:class:`Counts`) — size and shape. Valid on every document,
  though ``main_text_words`` needs the reference list to have been found (see
  :attr:`Counts.references_separable`).
* **Density** (:class:`Density`) — style and rhetoric per 1000 words. Valid
  only when the text is the author's own prose, which rules out any run with
  ``caveman`` compression on.

Calibration for the thresholds below comes from a 16-paper corpus of real
converted submissions (ML, proteomics, neuroscience). Where a number looks
arbitrary it is usually a measured one, and the comment says so.

Deliberately *not* here, because measuring them on converted Markdown
produced more false statements than true ones:

* inline statistical-test recomputation (statcheck / GRIM) — zero of 27
  cached documents carried a parseable ``t(24) = 2.13, p = .04``; the format
  is an APA-style convention, and outside psychology the numbers live in
  tables and figure panels that do not survive conversion as statistics;
* undefined-acronym lists — a hardened detector still reported a median of
  ~12 "undefined" acronyms per paper, because field-standard acronyms are
  genuinely used undefined;
* readability indices (Flesch, Fog) — calibrated on general prose, and
  technical vocabulary inflates them mechanically. Every paper scores
  "graduate level," which is not a finding.

Figure/table cross-reference integrity and per-section statistics are viable
but need the section map, so they are not in this module yet.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# ---------------------------------------------------------------------------
# Health thresholds
# ---------------------------------------------------------------------------

# A word this long is not a word. Real English tops out around 20 characters
# outside chemical nomenclature; a run of 25+ alphabetic characters is two or
# more words that lost the space between them.
FUSED_MIN_CHARS = 25

# Fused tokens per 1000 words. On the calibration corpus a healthy conversion
# scores 0.00 — eleven of sixteen papers had literally none — while the two
# genuinely broken ones scored 22.8 and 22.6. There is no middle ground in the
# data, so these are set well inside the gap rather than at its edges.
FUSED_DEGRADED = 1.0
FUSED_BROKEN = 10.0

# Hyphen-at-line-break survivals per 1000 words. Higher tolerance than the
# fused-token rate because this is often cosmetic: the word is recoverable and
# only whole-token matching suffers. One corpus paper (BERT) scores 33.7 while
# reading perfectly well, which is why this can degrade but never break.
HYPHEN_DEGRADED = 20.0

# Sentence-boundary spaces lost per 1000 words ("end.Next"). Tracked because
# it is what silently corrupts the sentence statistics below.
MISSING_SPACE_DEGRADED = 10.0

# Citations per 1000 words below which the style label is withheld. Papers
# that genuinely cite land between 3.8 and 17.6 on the calibration corpus;
# everything below ~1 is a detection failure, not a sparse bibliography.
CITATION_DETECTION_FLOOR = 2.0

# Share of the document's words that landed in no known section. Not a
# conversion failure on its own — the loader's section matching is a heuristic
# over heading text — but past this point nothing section-shaped can be
# trusted, and callers should read it before using `main_text_words`.
PREAMBLE_UNUSABLE = 0.5

CLEAN, DEGRADED, BROKEN = "clean", "degraded", "broken"


# ---------------------------------------------------------------------------
# Lexicons
# ---------------------------------------------------------------------------

# Abbreviations whose trailing period is not a sentence end. Scientific prose
# is dense with them, and getting this wrong is not a rounding error: naive
# splitting on [.!?] overcounted sentences by 10% at the median and 2.3x on the
# worst corpus paper, which would have made every sentence-length number below
# a fiction.
_ABBREVIATIONS = (
    "et al", "e.g", "i.e", "cf", "vs", "viz", "etc", "approx", "resp", "ca",
    "Fig", "Figs", "Eq", "Eqs", "Tab", "Ref", "Refs", "Sec", "Ch", "App",
    "No", "Nos", "pp", "p", "Vol", "ed", "eds", "al", "St",
    "Dr", "Prof", "Mr", "Mrs", "Ms", "Jr", "Sr",
    "Inc", "Ltd", "Co", "Corp", "Univ", "Dept",
    "min", "max", "avg", "std", "var", "sd", "se", "hr", "hrs", "sec", "wt",
)

# Hedges: language that limits the strength of a claim.
_HEDGES = (
    "may", "might", "could", "possibly", "perhaps", "potentially", "suggests",
    "suggest", "suggesting", "appears", "appear", "seems", "seem", "likely",
    "unlikely", "presumably", "arguably", "tends", "tend", "somewhat",
    "relatively", "approximately", "roughly", "largely", "generally",
    "typically", "often", "sometimes", "assume", "assumed", "putative",
    "plausible", "probable", "suggestive", "indicative", "consistent with",
)

# Boosters: language that amplifies one. Tracked next to hedges because the
# ratio between them, and where in a paper each concentrates, is a measurable
# proxy for overclaiming that no single count captures.
_BOOSTERS = (
    "clearly", "obviously", "evidently", "undoubtedly", "certainly",
    "definitely", "conclusively", "unequivocally", "strongly", "highly",
    "dramatically", "substantially", "remarkably", "striking", "strikingly",
    "novel", "unprecedented", "groundbreaking", "breakthrough", "unique",
    "superior", "outperforms", "state-of-the-art", "significantly",
    "demonstrates", "demonstrate", "proves", "prove", "establishes", "must",
)

# Auxiliaries that, followed by a past participle, mark the passive voice.
_BE_FORMS = ("is", "are", "was", "were", "be", "been", "being", "am")


def _word_boundary_pattern(terms: tuple[str, ...]) -> re.Pattern[str]:
    """Case-insensitive alternation over multi-word terms, longest first.

    Longest-first matters: without it ``consistent with`` is consumed by a
    shorter alternative and the phrase never matches as one hedge.
    """
    ordered = sorted(terms, key=len, reverse=True)
    body = "|".join(re.escape(t).replace(r"\ ", r"\s+") for t in ordered)
    return re.compile(rf"\b(?:{body})\b", re.IGNORECASE)


_HEDGE_RE = _word_boundary_pattern(_HEDGES)
_BOOSTER_RE = _word_boundary_pattern(_BOOSTERS)
_PASSIVE_RE = re.compile(
    rf"\b(?:{'|'.join(_BE_FORMS)})\s+(?:\w+ly\s+)?\w+(?:ed|en)\b", re.IGNORECASE
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
_NUMBER_RE = re.compile(r"(?<![A-Za-z\w.])[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?")

# Citation styles. Numeric covers "[3]", "[3, 5]" and "[3-7]"; author-year
# covers "(Smith, 2020)", "(Smith and Jones 2020)", "(Smith et al., 2020)".
_NUMERIC_CITE_RE = re.compile(r"\[(\d{1,3}(?:\s*[,;\-–]\s*\d{1,3})*)\]")
_AUTHOR_YEAR_CITE_RE = re.compile(
    r"\([A-Z][A-Za-z’'\-]+"
    r"(?:\s+(?:et\s+al\.?|and|&)\s+[A-Za-z’'\-]+)?"
    r",?\s+(?:1[89]|20)\d{2}[a-z]?\)"
)

# p-values, split by whether an exact value or only a threshold was reported.
_P_EXACT_RE = re.compile(r"\bp\s*=\s*(0?\.\d+|\d+(?:\.\d+)?[eE][-+]?\d+)", re.IGNORECASE)
_P_THRESHOLD_RE = re.compile(r"\bp\s*[<>≤≥]\s*(0?\.\d+|\d+(?:\.\d+)?[eE][-+]?\d+)", re.IGNORECASE)

# Conversion-damage signatures.
_FUSED_RE = re.compile(rf"[A-Za-z]{{{FUSED_MIN_CHARS},}}")
_HYPHEN_BREAK_RE = re.compile(r"[a-z]-[ \t]*\n[ \t]*[a-z]")
_MISSING_SPACE_RE = re.compile(r"[a-z]{2}\.[A-Z][a-z]{2}")

# Markdown / LaTeX furniture stripped before prose is measured.
_FENCED_CODE_RE = re.compile(r"^```.*?^```", re.M | re.S)
_DISPLAY_MATH_RE = re.compile(r"\$\$.+?\$\$|\\\[.+?\\\]|^\s*\\begin\{equation\}.*?\\end\{equation\}",
                              re.S | re.M)
_INLINE_MATH_RE = re.compile(r"\$[^$\n]{1,200}\$")
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.M)
_HEADING_MARK_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.M)
_EMPHASIS_RE = re.compile(r"[*_`]{1,3}")
_MD_LINK_RE = re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)")

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])[\"'”’)\]]*\s+(?=[\"'“‘(\[]*[A-Z0-9])")
_BLOCK_SPLIT_RE = re.compile(r"\n\s*\n")

# Placeholder standing in for a period that must not end a sentence. Chosen
# because it cannot occur in text the loader produces — it strips lone
# surrogates and normalizes line endings, but never emits control characters.
_DOT = "\x00"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Health:
    """How well the file converted. Valid on every document."""

    fused_per_1k: float
    hyphen_breaks_per_1k: float
    missing_space_per_1k: float
    markdown_headings: int
    # None when no section map was supplied.
    preamble_share: float | None
    verdict: str
    # Plain-language reasons behind a non-clean verdict, for the report.
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """False when the panel would be reviewing conversion damage."""
        return self.verdict != BROKEN

    @property
    def sections_usable(self) -> bool:
        """Whether anything keyed on the section map can be trusted."""
        return self.preamble_share is not None and self.preamble_share < PREAMBLE_UNUSABLE


@dataclass(frozen=True)
class Counts:
    """Size and shape of the manuscript."""

    chars: int
    words: int
    sentences: int
    # Blank-line-separated blocks, which is not the same thing as paragraphs
    # and is reported as what it is. Across the calibration corpus this ranged
    # from 2.8 to 656 words per block on comparable papers — blank lines
    # simply do not survive conversion consistently, so a "mean paragraph
    # length" derived from it would describe the converter, not the author.
    # Kept because the number is a useful fingerprint of how a file converted.
    blocks: int
    display_math: int
    table_rows: int
    reference_words: int
    # False when the reference list could not be located, which makes
    # `main_text_words` unknowable rather than merely imprecise: on the
    # calibration corpus a reference list was 19% of a paper's words at the
    # median and 48% at the worst, so guessing here would put a venue word
    # limit off by up to half.
    references_separable: bool
    main_text_words: int | None


@dataclass(frozen=True)
class Density:
    """Style and rhetoric, per 1000 words except where noted.

    ``None`` for a whole run when the text is not the author's own prose —
    see :func:`analyze`.
    """

    sentence_len_mean: float
    sentence_len_median: float
    sentence_len_p90: float
    long_sentence_share: float
    numbers_per_1k: float
    citations: int
    citation_style: str
    citations_per_1k: float
    hedges_per_1k: float
    boosters_per_1k: float
    mattr: float
    # Regex approximation (be-verb + past participle), counted per sentence
    # rather than as a share because a sentence can hold several. Labelled
    # approximate wherever it is shown: it cannot tell "was performed" from
    # "was tired".
    passive_per_sentence_approx: float
    p_values_exact: int
    p_values_threshold: int


@dataclass(frozen=True)
class ProseStats:
    """Everything this module measures about one manuscript."""

    health: Health
    counts: Counts
    # None when the run compressed the text (see `caveman`), because sentence
    # length, hedging and lexical diversity all describe prose the author did
    # not write.
    density: Density | None
    caveman: str | None

    def to_dict(self) -> dict:
        """JSON-safe form, as stored on the ingest record and cached."""
        return {
            "health": asdict(self.health),
            "counts": asdict(self.counts),
            "density": asdict(self.density) if self.density else None,
            "caveman": self.caveman,
        }


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------


def _protect_periods(text: str) -> str:
    """Mask every period that does not end a sentence.

    Four sources, all common in a manuscript and all capable of splitting one
    sentence into three: known abbreviations, decimals, single-letter initials
    in author names, and ellipses.
    """
    for abbr in _ABBREVIATIONS:
        text = re.sub(
            rf"(?<![A-Za-z]){re.escape(abbr)}\.",
            abbr + _DOT,
            text,
        )
    text = re.sub(r"(\d)\.(\d)", rf"\1{_DOT}\2", text)
    text = re.sub(r"\b([A-Z])\.", rf"\1{_DOT}", text)
    text = text.replace("...", _DOT * 3)
    return text


def split_sentences(text: str) -> list[str]:
    """Split ``text`` into sentences, tuned for scientific prose.

    Exposed because it is the load-bearing assumption behind every sentence
    statistic, and worth testing directly.
    """
    if not text.strip():
        return []
    masked = _protect_periods(text)
    parts = _SENTENCE_SPLIT_RE.split(masked)
    return [p.replace(_DOT, ".").strip() for p in parts if p.strip()]


def prose_only(text: str) -> str:
    """``text`` with Markdown and LaTeX furniture removed.

    Word counts are supposed to describe what the author wrote, and a pipe
    table or a display equation is neither prose nor absent from the Markdown.
    Headings keep their text and lose their hashes: a heading is words the
    author chose.
    """
    text = _FENCED_CODE_RE.sub(" ", text)
    text = _DISPLAY_MATH_RE.sub(" ", text)
    text = _INLINE_MATH_RE.sub(" ", text)
    text = _TABLE_ROW_RE.sub(" ", text)
    text = _HEADING_MARK_RE.sub("", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    return _EMPHASIS_RE.sub("", text)


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def _per_1k(count: int, words: int) -> float:
    return round(count / words * 1000, 2) if words else 0.0


def _percentile(values: list[int], pct: float) -> float:
    """Nearest-rank percentile. No numpy dependency for four call sites."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(pct / 100 * len(ordered) + 0.5)) - 1)
    return float(ordered[max(0, idx)])


def _median(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def _mattr(tokens: list[str], window: int = 500) -> float:
    """Moving-average type-token ratio.

    Plain TTR is used for this and should not be: it falls as a document gets
    longer, so it would rank a 20-page paper as less lexically varied than a
    5-page one on length alone. MATTR averages the ratio over a fixed window
    and is comparable across manuscripts.
    """
    lowered = [t.lower() for t in tokens]
    if len(lowered) <= window:
        return round(len(set(lowered)) / len(lowered), 4) if lowered else 0.0
    ratios = []
    # Stride rather than slide: a step of 1 is O(n * window) for a number that
    # moves in the fourth decimal place.
    step = max(1, window // 10)
    for start in range(0, len(lowered) - window + 1, step):
        ratios.append(len(set(lowered[start:start + window])) / window)
    return round(sum(ratios) / len(ratios), 4) if ratios else 0.0


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def _health(text: str, words: int, sections: dict[str, str] | None) -> Health:
    fused = _per_1k(len(_FUSED_RE.findall(text)), words)
    hyphen = _per_1k(len(_HYPHEN_BREAK_RE.findall(text)), words)
    missing = _per_1k(len(_MISSING_SPACE_RE.findall(text)), words)
    headings = len(re.findall(r"^[ \t]*#{1,6}[ \t]+\S", text, re.M))

    preamble_share = None
    if sections:
        total = sum(len(v) for v in sections.values())
        if total:
            preamble_share = round(len(sections.get("_preamble", "")) / total, 4)

    notes: list[str] = []
    verdict = CLEAN
    if fused >= FUSED_BROKEN:
        verdict = BROKEN
        notes.append(
            f"{fused:.1f} fused tokens per 1000 words — words have lost the "
            "spaces between them and the text is not what the authors wrote"
        )
    elif fused >= FUSED_DEGRADED:
        verdict = DEGRADED
        notes.append(f"{fused:.1f} fused tokens per 1000 words")
    if hyphen >= HYPHEN_DEGRADED:
        verdict = BROKEN if verdict == BROKEN else DEGRADED
        notes.append(f"{hyphen:.1f} hyphenated line breaks per 1000 words")
    if missing >= MISSING_SPACE_DEGRADED:
        verdict = BROKEN if verdict == BROKEN else DEGRADED
        notes.append(f"{missing:.1f} lost sentence spaces per 1000 words")
    if preamble_share is not None and preamble_share >= PREAMBLE_UNUSABLE:
        verdict = BROKEN if verdict == BROKEN else DEGRADED
        notes.append(
            f"{preamble_share:.0%} of the text matched no known section — "
            "section-keyed statistics are unavailable"
        )
    return Health(
        fused_per_1k=fused,
        hyphen_breaks_per_1k=hyphen,
        missing_space_per_1k=missing,
        markdown_headings=headings,
        preamble_share=preamble_share,
        verdict=verdict,
        notes=notes,
    )


def _counts(
    text: str, prose: str, tokens: list[str], sections: dict[str, str] | None
) -> Counts:
    sentences = split_sentences(prose)
    blocks = [p for p in _BLOCK_SPLIT_RE.split(prose) if p.strip()]

    reference_words = 0
    separable = False
    if sections:
        refs = sections.get("references", "")
        if refs.strip():
            reference_words = len(_words(prose_only(refs)))
            separable = reference_words > 0

    main_text = len(tokens) - reference_words if separable else None
    return Counts(
        chars=len(text),
        words=len(tokens),
        sentences=len(sentences),
        blocks=len(blocks),
        display_math=len(_DISPLAY_MATH_RE.findall(text)),
        table_rows=len(_TABLE_ROW_RE.findall(text)),
        reference_words=reference_words,
        references_separable=separable,
        main_text_words=main_text,
    )


def _density(prose: str, tokens: list[str]) -> Density:
    n = len(tokens)
    sentences = split_sentences(prose)
    lengths = [len(_words(s)) for s in sentences]
    lengths = [x for x in lengths if x]

    numeric = _NUMERIC_CITE_RE.findall(prose)
    author_year = _AUTHOR_YEAR_CITE_RE.findall(prose)
    citations = len(numeric) + len(author_year)
    if _per_1k(citations, n) < CITATION_DETECTION_FLOOR:
        # Not "none". Several venues set citations as superscript numerals,
        # which convert to bare digits indistinguishable from any other number,
        # so the count collapses to near zero on papers that plainly cite
        # heavily — two hits across 15 000 words on one corpus paper. Reporting
        # that as "no citations" would be a false statement about the
        # manuscript rather than a true one about the converter.
        style = "undetected"
    elif len(numeric) >= len(author_year):
        style = "numeric"
    else:
        style = "author_year"

    long_sentences = sum(1 for x in lengths if x > 40)
    return Density(
        sentence_len_mean=round(sum(lengths) / len(lengths), 2) if lengths else 0.0,
        sentence_len_median=_median(lengths),
        sentence_len_p90=_percentile(lengths, 90),
        long_sentence_share=round(long_sentences / len(lengths), 4) if lengths else 0.0,
        numbers_per_1k=_per_1k(len(_NUMBER_RE.findall(prose)), n),
        citations=citations,
        citation_style=style,
        citations_per_1k=_per_1k(citations, n),
        hedges_per_1k=_per_1k(len(_HEDGE_RE.findall(prose)), n),
        boosters_per_1k=_per_1k(len(_BOOSTER_RE.findall(prose)), n),
        mattr=_mattr(tokens),
        passive_per_sentence_approx=(
            round(len(_PASSIVE_RE.findall(prose)) / len(sentences), 4) if sentences else 0.0
        ),
        p_values_exact=len(_P_EXACT_RE.findall(prose)),
        p_values_threshold=len(_P_THRESHOLD_RE.findall(prose)),
    )


def analyze(
    text: str,
    sections: dict[str, str] | None = None,
    caveman: str | None = None,
) -> ProseStats:
    """Measure ``text``. Pure, deterministic, and never raises on real input.

    ``sections`` is the loader's section map; without it the reference list
    cannot be separated and ``preamble_share`` is unknown, so the affected
    fields report themselves unavailable rather than guessing.

    ``caveman`` is the run's compression level. When it is on, :attr:`Density`
    is withheld entirely: the compressor strips articles and function words,
    so the text reads *"We designed two-player game ... in presence of both
    experiential and observational evidence"*. Sentence length, hedging,
    passive voice and lexical diversity measured on that describe the
    compressor, not the author.
    """
    prose = prose_only(text)
    tokens = _words(prose)
    compressed = bool(caveman) and caveman != "off"
    return ProseStats(
        health=_health(text, len(tokens), sections),
        counts=_counts(text, prose, tokens, sections),
        density=None if compressed else _density(prose, tokens),
        caveman=caveman if compressed else None,
    )
