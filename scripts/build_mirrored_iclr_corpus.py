#!/usr/bin/env python3
"""Build a deterministic balanced corpus from downloaded public mirrors.

Joins the `smallari/openreview-iclr-peer-reviews` decision/rating Parquet file
to the `Wutaghost/LLMscore-ICLR-OpenReview` extracted-text archive by stable
OpenReview paper ID. It never infers or fabricates a decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from peerreviewagents.eval.corpus import write_corpus_manifest
from peerreviewagents.eval.schema import CorpusItem
from peerreviewagents.ingest.loader import load_manuscript_record

SMALLARI_DATASET = "https://huggingface.co/datasets/smallari/openreview-iclr-peer-reviews"
LLMSCORE_DATASET = "https://huggingface.co/datasets/Wutaghost/LLMscore-ICLR-OpenReview"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sql_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


def select_rows(
    parquet: Path,
    text_index: Path,
    *,
    year: int,
    per_class: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Select explicit decisions with >=3 ratings and clean, substantial text."""
    candidate_count = per_class * 20
    query = f"""
    WITH eligible AS (
      SELECT
        p.paper_id, p.title, p.decision, p.label, to_json(p.reviews) AS reviews_json,
        t.text_path, t.text_chars, t.pages,
        row_number() OVER (
          PARTITION BY p.label
          ORDER BY md5(p.paper_id || ':{seed}')
        ) AS class_rank
      FROM '{_sql_path(parquet)}' p
      JOIN read_json_auto(
        '{_sql_path(text_index)}', format='newline_delimited'
      ) t USING (paper_id)
      WHERE p.year = '{year}'
        AND p.decision IN ('Accept', 'Reject')
        AND ((p.label = 1 AND p.decision = 'Accept')
          OR (p.label = 0 AND p.decision = 'Reject'))
        AND list_count(p.reviews) >= 3
        AND t.conversion_status = 'ok'
        AND list_count(t.quality_flags) = 0
        AND t.text_chars BETWEEN 20000 AND 250000
    )
    SELECT *
    FROM eligible
    WHERE class_rank <= {candidate_count}
    ORDER BY class_rank, label DESC
    """
    result = subprocess.run(
        ["duckdb", "-json", "-c", query],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = json.loads(result.stdout)
    if len(rows) < 2 * per_class:
        raise RuntimeError(
            f"selected only {len(rows)} candidates; need at least {2 * per_class}"
        )
    for row in rows:
        encoded = row.pop("reviews_json")
        row["reviews"] = json.loads(encoded) if isinstance(encoded, str) else encoded
    return rows


def _ratings(reviews: list[dict[str, Any]]) -> list[float]:
    values = []
    for review in reviews:
        try:
            values.append(float(review.get("rating")))
        except (TypeError, ValueError):
            continue
    if len(values) < 3:
        raise ValueError(f"only {len(values)} numeric ratings survived parsing")
    return values


def build(args: argparse.Namespace) -> None:
    source = Path(args.source).resolve()
    out = Path(args.out).resolve()
    parquet = source / "openreview_peer_reviews.parquet"
    text_index = source / "paper_text_index.jsonl.gz"
    archive = source / f"ICLR_{args.year}.tar.gz"
    for path in (parquet, text_index, archive):
        if not path.is_file():
            raise FileNotFoundError(path)

    rows = select_rows(
        parquet,
        text_index,
        year=args.year,
        per_class=args.per_class,
        seed=args.seed,
    )
    manuscript_dir = out / "manuscripts_ingest_clean"
    manuscript_dir.mkdir(parents=True, exist_ok=True)
    items: list[CorpusItem] = []
    human_reviews: list[dict[str, Any]] = []

    selected_per_class = {0: 0, 1: 0}
    skipped_ingest = 0
    with tarfile.open(archive, "r:gz") as bundle, tempfile.TemporaryDirectory() as temp_dir:
        for row in rows:
            label = int(row["label"])
            if selected_per_class[label] >= args.per_class:
                continue
            archive_name, member_name = str(row["text_path"]).split("::", 1)
            if Path(archive_name).name != archive.name:
                raise ValueError(f"unexpected archive locator: {row['text_path']}")
            member = bundle.getmember(member_name)
            extracted = bundle.extractfile(member)
            if extracted is None:
                raise FileNotFoundError(member_name)
            text = extracted.read().decode("utf-8", errors="replace")
            prepared = f"# {row['title']}\n\n{text}"
            candidate = Path(temp_dir) / f"{row['paper_id']}.txt"
            candidate.write_text(prepared, encoding="utf-8")
            parsed = load_manuscript_record(str(candidate), {"conversion_gate": "degraded"})
            if parsed.health() != "clean":
                skipped_ingest += 1
                continue

            manuscript = manuscript_dir / f"{row['paper_id']}.txt"
            manuscript.write_text(
                prepared,
                encoding="utf-8",
            )

            scores = _ratings(row["reviews"])
            decision = "accept" if int(row["label"]) == 1 else "reject"
            selected_per_class[label] += 1
            items.append(CorpusItem(
                id=row["paper_id"],
                title=row["title"],
                pdf_path=os.path.relpath(manuscript, out),
                human_scores=scores,
                human_mean=round(sum(scores) / len(scores), 3),
                human_decision=decision,
                human_decision_raw=row["decision"],
                venue="ICLR.cc/2025/Conference",
                year=str(args.year),
                source_url=f"https://openreview.net/forum?id={row['paper_id']}",
            ))
            human_reviews.append({
                "paper_id": row["paper_id"],
                "decision": row["decision"],
                "label": int(row["label"]),
                "reviews": row["reviews"],
            })

    if selected_per_class != {0: args.per_class, 1: args.per_class}:
        raise RuntimeError(
            f"ingestion preflight produced {selected_per_class}; need "
            f"{args.per_class} papers per class"
        )

    accepts = sorted(
        (item for item in items if item.human_decision == "accept"), key=lambda item: item.id
    )
    rejects = sorted(
        (item for item in items if item.human_decision == "reject"), key=lambda item: item.id
    )
    items = [item for pair in zip(accepts, rejects) for item in pair]
    corpus_path = out / "corpus.jsonl"
    corpus_path.write_text("".join(item.to_json() + "\n" for item in items), encoding="utf-8")
    reviews_path = out / "human_reviews.jsonl"
    human_by_id = {row["paper_id"]: row for row in human_reviews}
    reviews_path.write_text(
        "".join(json.dumps(human_by_id[item.id], ensure_ascii=False) + "\n" for item in items),
        encoding="utf-8",
    )

    manifest_path = Path(write_corpus_manifest(
        str(corpus_path),
        venue="ICLR.cc/2025/Conference",
        selection="deterministic balanced join of two public mirrors by OpenReview paper_id",
        seed=args.seed,
        per_class=args.per_class,
        decision_requirement="explicit Accept or Reject; label must agree",
        rating_requirement="at least three numeric official-review ratings",
        text_requirement="conversion_status=ok, no quality flags, 20k-250k characters",
        pra_ingest_requirement="PeerReviewAgents ingest health=clean before sampling",
        ingest_candidates_per_class=args.per_class * 20,
        ingest_candidates_skipped=skipped_ingest,
        text_preprocessing="authoritative mirrored title prepended as a Markdown H1",
        source_datasets=[SMALLARI_DATASET, LLMSCORE_DATASET],
        source_files={
            parquet.name: _sha256(parquet),
            text_index.name: _sha256(text_index),
            archive.name: _sha256(archive),
        },
        leakage_note=(
            "Retrospective ICLR 2025 corpus evaluated with DeepSeek V4 Flash 0731; "
            "training-data familiarity cannot be excluded. Agreement is characterization, "
            "not evidence of human equivalence."
        ),
    ))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["human_reviews_file"] = {
        "path": reviews_path.name,
        "sha256": _sha256(reviews_path),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(items)} papers ({len(accepts)} accept, {len(rejects)} reject) to {out}")


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser()
    cli.add_argument("--source", required=True)
    cli.add_argument("--out", required=True)
    cli.add_argument("--year", type=int, default=2025)
    cli.add_argument("--per-class", type=int, default=10)
    cli.add_argument("--seed", type=int, default=20260815)
    return cli


if __name__ == "__main__":
    build(parser().parse_args())
