"""Build a labeled corpus from OpenReview: PDFs + human scores + decisions.

OpenReview's note schema drifts across venues and API versions (content
values are bare in v1, wrapped as ``{"value": ...}`` in v2; rating fields are
variously ``rating`` / ``recommendation`` / ``overall_assessment``). The
fragile extraction is isolated into small pure functions (:func:`parse_rating`,
:func:`normalize_decision`, :func:`extract_scores`, :func:`extract_decision`)
that are unit-tested without the network; :func:`fetch_corpus` is the only
part that talks to OpenReview and it lazily imports the client so the rest of
the package doesn't depend on ``openreview-py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import warnings
from pathlib import Path
from typing import Any

from .schema import CorpusItem

# Candidate content fields, most-specific first. Different ICLR years and
# sibling venues label the same thing differently.
_RATING_FIELDS = ("rating", "overall_assessment", "recommendation", "final_rating")
_DECISION_FIELDS = ("decision", "recommendation", "final_decision")

_API2_BASE = "https://api2.openreview.net"
_CORPUS_MANIFEST = "corpus_manifest.json"


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_corpus_manifest(corpus_path: str, **metadata: Any) -> str:
    """Fingerprint the selected labels and PDFs after sampling.

    Every batch run verifies these hashes before spending model tokens, so an
    overwritten PDF or re-fetched label set cannot be silently pooled with
    earlier results.
    """
    corpus_file = Path(corpus_path).resolve()
    items = load_corpus(str(corpus_file))
    pdfs = {}
    for item in items:
        pdf = Path(item.pdf_path)
        if not pdf.is_file():
            raise FileNotFoundError(f"corpus PDF is missing: {item.pdf_path}")
        pdfs[item.id] = {
            "path": os.path.relpath(pdf, corpus_file.parent),
            "sha256": _sha256(pdf),
        }
    doc = {
        "schema_version": 1,
        "corpus_file": corpus_file.name,
        "corpus_sha256": _sha256(corpus_file),
        "n_papers": len(items),
        "class_counts": {
            label: sum(item.human_decision == label for item in items)
            for label in ("accept", "reject")
        },
        "papers": pdfs,
        "sampling": metadata,
    }
    out = corpus_file.parent / _CORPUS_MANIFEST
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(out)


def verify_corpus_manifest(
    corpus_path: str, *, warn_missing: bool = False
) -> dict[str, Any] | None:
    """Verify a frozen corpus, returning its manifest or raising on drift."""
    corpus_file = Path(corpus_path).resolve()
    manifest_path = corpus_file.parent / _CORPUS_MANIFEST
    if not manifest_path.is_file():
        if warn_missing:
            warnings.warn(
                f"No {_CORPUS_MANIFEST} beside {corpus_file.name}; run "
                "`python -m peerreviewagents.eval freeze --dir ...` before a "
                "publication run.",
                UserWarning,
                stacklevel=2,
            )
        return None
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = []
    if doc.get("corpus_sha256") != _sha256(corpus_file):
        errors.append(f"{corpus_file.name} changed after the corpus was frozen")
    for paper_id, rec in (doc.get("papers") or {}).items():
        pdf = (corpus_file.parent / rec["path"]).resolve()
        if not pdf.is_file():
            errors.append(f"{paper_id}: missing PDF {rec['path']}")
        elif rec.get("sha256") != _sha256(pdf):
            errors.append(f"{paper_id}: PDF changed after the corpus was frozen")
    if errors:
        raise RuntimeError("Corpus manifest verification failed:\n- " + "\n- ".join(errors))
    return doc


# ---------------------------------------------------------------------------
# Pure extraction helpers (unit-tested, no network)
# ---------------------------------------------------------------------------


def cval(content: dict[str, Any], key: str) -> Any:
    """Read a content field across API v1 (bare) and v2 (``{"value": ...}``)."""
    if key not in content:
        return None
    v = content[key]
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return v


def parse_rating(value: Any) -> float | None:
    """Pull a numeric rating out of an OpenReview rating value.

    Handles ``8``, ``"8"``, ``"8: accept, good paper"``, ``"6.5"`` and the
    occasional ``"8/10"``. Returns ``None`` when there's no leading number.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.match(r"\s*(\d+(?:\.\d+)?)", str(value))
    return float(m.group(1)) if m else None


def normalize_decision(raw: Any) -> str | None:
    """Collapse a free-text decision to ``"accept"`` / ``"reject"`` /
    ``"withdrawn"`` / ``"desk_reject"`` / ``None``.

    Withdrawn and desk-rejected are their own statuses, not ``"reject"``: a
    withdrawal is the authors' act, not the reviewers' verdict, and folding
    them into "reject" labeled papers the panel never judged as ground-truth
    rejections — while the submission-field path was correctly skipping the
    same papers. They come first because venue strings like "Desk Rejected
    Submission" also contain "reject".
    """
    if not raw:
        return None
    s = str(raw).strip().lower()
    if "withdraw" in s:
        return "withdrawn"
    if "desk" in s:
        return "desk_reject"
    if "accept" in s or "oral" in s or "poster" in s or "spotlight" in s:
        return "accept"
    if "reject" in s:
        return "reject"
    return None


def _invitations(note: Any) -> list[str]:
    """Invitation id(s) for a note across API versions."""
    invs = getattr(note, "invitations", None)
    if invs:
        return list(invs)
    inv = getattr(note, "invitation", None)
    return [inv] if inv else []


def _is_review(invs: list[str]) -> bool:
    return any("Official_Review" in i or i.endswith("/Review") for i in invs)


def _is_final_decision(invs: list[str]) -> bool:
    return any("Decision" in i for i in invs)


def _is_meta_review(invs: list[str]) -> bool:
    return any("Meta_Review" in i for i in invs)


def _is_decision(invs: list[str]) -> bool:
    """Decision-shaped notes: the venue's Decision, or a Meta_Review.

    A meta-review's recommendation is the Area Chair's input to the decision,
    not the decision — the two can differ, so :func:`extract_decision` only
    consults Meta_Review notes when no Decision note yields a label.
    """
    return _is_final_decision(invs) or _is_meta_review(invs)


def extract_scores(replies: list[Any], fields: tuple[str, ...] = _RATING_FIELDS) -> list[float]:
    """Numeric ratings from the official reviews in a forum's replies.

    ``fields`` is the ordered list of content fields to try per review (first
    that yields a number wins). Pass a single-element tuple to pin one field
    once you've confirmed the venue's schema with :func:`summarize_fields`.
    """
    scores: list[float] = []
    for r in replies:
        if not _is_review(_invitations(r)):
            continue
        content = getattr(r, "content", {}) or {}
        for fld in fields:
            score = parse_rating(cval(content, fld))
            if score is not None:
                scores.append(score)
                break
    return scores


def extract_decision(
    replies: list[Any], fields: tuple[str, ...] = _DECISION_FIELDS
) -> tuple[str | None, str]:
    """Normalized + raw decision from the decision/meta note in a forum.

    Decision notes are consulted before Meta_Review notes: the meta-review's
    recommendation is advice to the program chairs and can differ from what
    they decided, so it only counts when no Decision note yields a label.

    May return the ``"withdrawn"`` / ``"desk_reject"`` sentinels — callers
    skip those papers, mirroring :func:`decision_from_submission`, rather
    than shopping other notes for a reviewer verdict that never happened.
    """
    decisions = [r for r in replies if _is_final_decision(_invitations(r))]
    metas = [r for r in replies
             if _is_meta_review(_invitations(r)) and r not in decisions]
    for r in decisions + metas:
        content = getattr(r, "content", {}) or {}
        for fld in fields:
            raw = cval(content, fld)
            norm = normalize_decision(raw)
            if norm:
                return norm, str(raw)
    return None, ""


def submission_status(sub: Any) -> tuple[str, str]:
    """Fine-grained outcome from the submission's ``venue``/``venueid`` fields.

    Modern OpenReview venues (e.g. ICLR 2025) encode the outcome on the
    submission, not a public Decision note: accepted papers get ``venue`` like
    ``"ICLR 2025 Poster/Spotlight/Oral"``; withdrawn/desk-rejected ones carry
    those words; a still-"Submitted to …" paper post-decision was rejected.

    Returns ``(status, raw)`` where status is one of ``accept``, ``reject``,
    ``withdrawn``, ``desk_reject``, ``unknown``. Only meaningful *after*
    decisions are released.
    """
    content = getattr(sub, "content", {}) or {}
    venue = str(cval(content, "venue") or "").strip()
    venueid = str(cval(content, "venueid") or "").strip()
    raw = venue or venueid
    low = f"{venue} {venueid}".lower()

    if "withdraw" in low:
        return "withdrawn", raw
    if "desk" in low:
        return "desk_reject", raw
    if any(w in low for w in ("poster", "oral", "spotlight")) or normalize_decision(venue) == "accept":
        return "accept", raw
    if "reject" in low:
        return "reject", raw
    if low.startswith("submitted to"):
        return "reject", raw          # post-decision, not accepted
    return "unknown", raw


def decision_from_submission(sub: Any) -> tuple[str | None, str]:
    """Binary accept/reject label from the submission, or ``None``.

    Withdrawn / desk-rejected / unknown submissions return ``None`` — they're
    not clean reviewer accept-vs-reject decisions, so they're excluded from the
    labeled corpus rather than mislabeled.
    """
    status, raw = submission_status(sub)
    if status == "accept":
        return "accept", raw
    if status == "reject":
        return "reject", raw
    return None, raw


def summarize_fields(replies: list[Any]) -> dict[str, Any]:
    """What content fields actually exist on a forum's reviews / decision.

    Returns the field names (with a sample value) seen on review notes and on
    decision notes, plus counts. This is the ground truth for picking the
    right ``--rating-field`` / ``--decision-field`` for a venue, instead of
    trusting the hardcoded guesses.
    """
    review_fields: dict[str, Any] = {}
    decision_fields: dict[str, Any] = {}
    n_reviews = n_decisions = 0
    for r in replies:
        invs = _invitations(r)
        content = getattr(r, "content", {}) or {}
        if _is_review(invs):
            n_reviews += 1
            for k in content:
                review_fields.setdefault(k, cval(content, k))
        elif _is_decision(invs):
            n_decisions += 1
            for k in content:
                decision_fields.setdefault(k, cval(content, k))
    return {
        "n_reviews": n_reviews,
        "n_decisions": n_decisions,
        "review_fields": review_fields,
        "decision_fields": decision_fields,
    }


# ---------------------------------------------------------------------------
# Network fetch
# ---------------------------------------------------------------------------


def _client():
    import openreview  # lazy: only needed for network calls

    return openreview.api.OpenReviewClient(
        baseurl=_API2_BASE,
        username=os.environ.get("OPENREVIEW_USERNAME"),
        password=os.environ.get("OPENREVIEW_PASSWORD"),
    )


def inspect_venue(venue: str, *, scan_cap: int = 40) -> dict[str, Any]:
    """Print the review/decision content fields for the first paper in ``venue``
    that has reviews, so you can pick the right --rating-field/--decision-field.

    Returns the :func:`summarize_fields` dict for that paper (or an empty
    summary if none scanned had reviews)."""
    client = _client()
    submissions = client.get_all_notes(invitation=f"{venue}/-/Submission")
    submissions.sort(key=lambda n: getattr(n, "cdate", 0) or 0, reverse=True)

    for sub in submissions[:scan_cap]:
        replies = [r for r in client.get_all_notes(forum=sub.id)
                   if getattr(r, "id", None) != sub.id]
        summary = summarize_fields(replies)
        if summary["n_reviews"] == 0:
            continue
        print(f"Venue: {venue}")
        print(f"Sample paper: {sub.id}  ({summary['n_reviews']} reviews, "
              f"{summary['n_decisions']} decision notes)\n")
        print("REVIEW fields (candidate --rating-field):")
        for k, v in summary["review_fields"].items():
            guess = "  <- numeric?" if parse_rating(v) is not None else ""
            print(f"  {k!r}: {str(v)[:80]!r}{guess}")
        print("\nDECISION note fields (candidate --decision-field):")
        if summary["decision_fields"]:
            for k, v in summary["decision_fields"].items():
                print(f"  {k!r}: {str(v)[:80]!r}")
        else:
            print("  (none — decision is read from the submission's venue field below)")

        # Submission-level fallback: where the accept/reject actually lives
        # when there's no public Decision note (e.g. ICLR 2025).
        sc = getattr(sub, "content", {}) or {}
        dec, raw = decision_from_submission(sub)
        print("\nSUBMISSION decision source (venue / venueid):")
        print(f"  'venue':   {str(cval(sc, 'venue'))[:80]!r}")
        print(f"  'venueid': {str(cval(sc, 'venueid'))[:80]!r}")
        print(f"  -> parsed decision: {dec!r} (from {raw!r})")
        return summary
    print(f"No reviews found in the first {scan_cap} submissions of {venue}.")
    return {"n_reviews": 0, "n_decisions": 0, "review_fields": {}, "decision_fields": {}}


def fetch_corpus(
    venue: str,
    *,
    limit: int = 10,
    out_dir: str,
    scan_cap: int = 80,
    leakage_note: str = "",
    rating_fields: tuple[str, ...] = _RATING_FIELDS,
    decision_fields: tuple[str, ...] = _DECISION_FIELDS,
    balance: bool = True,
) -> list[CorpusItem]:
    """Fetch up to ``limit`` complete papers (PDF + scores + decision) from a venue.

    ``venue`` is an OpenReview venue id, e.g. ``"ICLR.cc/2025/Conference"``.
    A paper is kept only if it has at least one numeric review score and a
    clean accept/reject label (withdrawn/desk-rejected papers are skipped —
    they aren't reviewer decisions). With ``balance`` (default), each of
    accept/reject is capped at ~half of ``limit`` so the corpus isn't all one
    class — otherwise accuracy/kappa are meaningless. ``rating_fields`` /
    ``decision_fields`` override the field-name guesses (see
    :func:`inspect_venue`). PDFs go under ``out_dir/pdfs/`` and labels to
    ``out_dir/corpus.jsonl``. Credentials come from ``OPENREVIEW_USERNAME`` /
    ``OPENREVIEW_PASSWORD`` if set, else anonymous (public notes only).
    """
    client = _client()

    pdf_dir = os.path.join(out_dir, "pdfs")
    os.makedirs(pdf_dir, exist_ok=True)

    submissions = client.get_all_notes(invitation=f"{venue}/-/Submission")
    submissions.sort(key=lambda n: getattr(n, "cdate", 0) or 0, reverse=True)

    year = _venue_year(venue)
    per_class_cap = (limit + 1) // 2 if balance else limit
    buckets: dict[str, list[CorpusItem]] = {"accept": [], "reject": []}
    skipped: dict[str, int] = {}
    scanned = 0
    for sub in submissions:
        if sum(len(b) for b in buckets.values()) >= limit or scanned >= scan_cap:
            break
        scanned += 1
        try:
            replies = [r for r in client.get_all_notes(forum=sub.id)
                       if getattr(r, "id", None) != sub.id]
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {sub.id}: cannot fetch replies ({exc})")
            continue

        scores = extract_scores(replies, rating_fields)
        if not scores:
            skipped["no_scores"] = skipped.get("no_scores", 0) + 1
            continue
        decision, decision_raw = extract_decision(replies, decision_fields)
        if decision in ("withdrawn", "desk_reject"):
            # Same exclusion as the submission-field path below: a withdrawal
            # or desk rejection is not a reviewer accept-vs-reject verdict,
            # whichever note it was announced in.
            skipped[decision] = skipped.get(decision, 0) + 1
            continue
        if decision is None:
            # No public Decision note (e.g. ICLR 2025) — outcome lives on the
            # submission's venue field; withdrawn/unknown come back as None.
            status, decision_raw = submission_status(sub)
            decision = {"accept": "accept", "reject": "reject"}.get(status)
            if decision is None:
                skipped[status] = skipped.get(status, 0) + 1
                continue
        if balance and len(buckets[decision]) >= per_class_cap:
            continue  # this class is full; keep scanning for the other

        content = getattr(sub, "content", {}) or {}
        title = str(cval(content, "title") or "Untitled").strip()
        pdf_path = _download_pdf(client, sub.id, pdf_dir)
        if not pdf_path:
            continue

        buckets[decision].append(CorpusItem(
            id=sub.id, title=title, pdf_path=os.path.relpath(pdf_path, out_dir),
            human_scores=scores,
            human_mean=round(sum(scores) / len(scores), 3),
            human_decision=decision, human_decision_raw=decision_raw,
            venue=venue, year=year,
            source_url=f"https://openreview.net/forum?id={sub.id}",
        ))
        n = sum(len(b) for b in buckets.values())
        print(f"  [{n}/{limit}] {title[:64]}  "
              f"(n={len(scores)}, mean={buckets[decision][-1].human_mean}, {decision})")

    # Interleave so the file isn't all-accepts-then-all-rejects.
    acc, rej = buckets["accept"], buckets["reject"]
    items: list[CorpusItem] = []
    for i in range(max(len(acc), len(rej))):
        if i < len(acc):
            items.append(acc[i])
        if i < len(rej):
            items.append(rej[i])
    items = items[:limit]

    _write_corpus(out_dir, items)
    write_corpus_manifest(
        os.path.join(out_dir, "corpus.jsonl"),
        venue=venue,
        requested_limit=limit,
        scan_cap=scan_cap,
        balance=balance,
        rating_fields=list(rating_fields),
        decision_fields=list(decision_fields),
        leakage_note=leakage_note,
    )
    print(f"\nWrote {len(items)} papers to {os.path.join(out_dir, 'corpus.jsonl')} "
          f"(accept={len(buckets['accept'])}, reject={len(buckets['reject'])}; "
          f"scanned {scanned}).")
    if skipped:
        print(f"Skipped: {skipped}")
    if not buckets["accept"] or not buckets["reject"]:
        print("⚠️  Only one decision class found — accuracy/kappa won't be "
              "meaningful. Try a larger --scan-cap or a different venue/year.")
    if leakage_note:
        print(f"Leakage note recorded: {leakage_note}")
    return items


def _download_pdf(client: Any, note_id: str, pdf_dir: str) -> str | None:
    dest = os.path.join(pdf_dir, f"{note_id}.pdf")
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    try:
        data = client.get_attachment(id=note_id, field_name="pdf")
    except Exception as exc:  # noqa: BLE001
        print(f"  skip {note_id}: pdf download failed ({exc})")
        return None
    if not data:
        return None
    with open(dest, "wb") as fh:
        fh.write(data)
    return dest


def _venue_year(venue: str) -> str:
    m = re.search(r"(20\d{2})", venue)
    return m.group(1) if m else ""


def _write_corpus(out_dir: str, items: list[CorpusItem]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "corpus.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for it in items:
            fh.write(it.to_json() + "\n")


def load_corpus(path: str) -> list[CorpusItem]:
    from .schema import read_jsonl

    base = Path(path).resolve().parent
    items = [CorpusItem.from_dict(d) for d in read_jsonl(path)]
    for item in items:
        pdf = Path(item.pdf_path)
        if pdf.is_absolute():
            continue
        # New corpora store paths relative to corpus.jsonl. Preserve support
        # for older files that stored a path relative to the process cwd.
        if pdf.is_file():
            item.pdf_path = str(pdf.resolve())
        else:
            item.pdf_path = str((base / pdf).resolve())
    return items
