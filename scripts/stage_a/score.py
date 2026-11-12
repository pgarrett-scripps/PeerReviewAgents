"""Score a run against the answer key of the error planted in its paper.

The question Stage A asks is narrow enough to answer by rule, and that is the
point: whether a run caught the planted error is decided here, by the key the
planting script wrote, and not by reading the findings and forming an
impression of them. An impression would drift between arms and between
sessions, which is exactly the comparison Stage A is for.

**The rule.** A finding catches the planted error when it is in the right place
*and* about the right thing:

* **Place** — the finding names one of the blocks the key lists (its
  `block_id`, or one of its `evidence_blocks` or its corroborating block), or
  the text it quotes is inside one of the key's altered blocks.
* **Content** — the finding's own words, not the passage it quotes, name every
  value the key lists: the altered number together with the number it now
  contradicts. Naming one of the two is not enough, because the number that was
  not altered is still correct and quoting it shows nothing.

Both are decided on digits and letters only (`squash`), because a finding
quotes a paper's text as the model saw it, with its own spacing.

A finding that is about the right thing in the wrong place, or in the right
place about something else, is recorded as a near miss rather than silently
dropped: near misses are what tell a brief it looked in the right section and
missed the check.

**Controls.** An unaltered paper has no key, so there is nothing to catch and
the measurement is the opposite one: does the run allege an error of the
planted sort where none was planted? That is counted by the same kind of
written rule — a finding that claims a contradiction, a number that does not
follow, or a reference that does not exist, at major severity or above.

Nothing here makes a model call.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ALLEGATION = re.compile(
    r'does not match|do not match|does not agree|disagree|inconsisten|contradict'
    r'|does not follow|cannot be reproduced|does not add up|mismatch|miscalculat'
    r'|incorrectly reported|wrong(?:ly)? (?:value|number|figure|percentage)'
    r'|not a (?:valid|registered|real) doi|does not resolve|no such (?:doi|reference)'
    r'|fabricat|transposed|swapped|arithmetic', re.I)
SEVERE = ('major', 'critical', 'blocking', 'high')


def squash(text: str) -> str:
    return re.sub(r'[^0-9a-z]', '', str(text).lower())


def words_of(finding: dict) -> str:
    """Everything the finding says, quotes included."""
    parts = [finding.get(field) or '' for field in
             ('title', 'comment', 'quote', 'replacement', 'counterevidence',
              'calculate_receipt', 'corroborating_quote')]
    return ' '.join(str(part) for part in parts)


def own_words_of(finding: dict) -> str:
    """What the finding says in its own words, with the passages it quotes left out.

    The planted values are nearly always inside the quote, because the quote is
    the altered sentence, so a finding that quotes the sentence while saying
    something else about it would otherwise score as a catch. The claim has to
    be in the words the reviewer wrote.
    """
    parts = [finding.get(field) or '' for field in
             ('title', 'comment', 'replacement', 'counterevidence', 'calculate_receipt')]
    return ' '.join(str(part) for part in parts)


def blocks_of(finding: dict) -> set:
    """Every block the finding points at."""
    named = {finding.get('block_id') or '', finding.get('corroborating_block_id') or ''}
    evidence = finding.get('evidence_blocks') or []
    if isinstance(evidence, str):
        evidence = [evidence]
    return {str(block) for block in named | set(evidence) if block}


def in_place(finding: dict, key: dict) -> bool:
    """Does the finding point at the altered text?"""
    wanted = set(key['detect']['blocks'])
    if blocks_of(finding) & wanted:
        return True
    quote = squash(finding.get('quote') or '')
    if len(quote) < 12:
        return False  # Too short to identify a place on its own.
    return any(quote in squash(passage) for passage in key['detect'].get('quotes') or [])


def about_it(finding: dict, key: dict) -> bool:
    """Do the finding's own words name the planted values?"""
    said = squash(own_words_of(finding))
    mentions = [squash(value) for value in key['detect']['must_mention'] if squash(value)]
    if not mentions:
        return False
    hits = [mention in said for mention in mentions]
    return all(hits) if key['detect'].get('mention_rule') == 'all' else any(hits)


def findings_of(run: Path) -> list:
    """The findings a run delivered, in the order they were recorded."""
    path = Path(run) / 'review' / 'findings.jsonl'
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def runtime_of(run: Path) -> dict:
    path = Path(run) / 'review' / 'runtime.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def progress_of(run: Path) -> dict:
    path = Path(run) / 'review' / 'progress.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def cost_of(run: Path) -> dict:
    """Tokens, minutes, agents and status, as the run itself recorded them."""
    runtime = runtime_of(run)
    agents = runtime.get('agents') or []
    return {
        'status': runtime.get('status'),
        'blocked_reason': runtime.get('blocked_reason'),
        'tokens': runtime.get('tokens_total') or 0,
        'minutes': round((runtime.get('wall_seconds') or 0) / 60.0, 1),
        'agents': len(agents),
        'tool_calls': sum(row.get('toolCalls') or 0 for row in agents),
        'models': sorted({str(row.get('model')) for row in agents if row.get('model')}),
    }


def brief(finding: dict) -> dict:
    """The part of a finding worth putting in a results table."""
    return {field: finding.get(field) for field in
            ('finding_uid', 'title', 'slot', 'role', 'track', 'severity', 'kind', 'basis',
             'block_id', 'quote', 'escalate')}


def score(run: Path, key: dict) -> dict:
    """Did this run catch the planted error, and what did it cost?"""
    findings = findings_of(run)
    caught, near_place, near_content = [], [], []
    for finding in findings:
        placed, about = in_place(finding, key), about_it(finding, key)
        if placed and about:
            caught.append(brief(finding))
        elif placed:
            near_place.append(brief(finding))
        elif about:
            near_content.append(brief(finding))
    tracks = {}
    for finding in findings:
        tracks[str(finding.get('track') or 'unknown')] = \
            tracks.get(str(finding.get('track') or 'unknown'), 0) + 1
    return {
        'run': str(run),
        'paper': key['paper'],
        'kind': key['kind'],
        'rule': key['rule'],
        'caught': bool(caught),
        'caught_by': caught,
        'first_catch': caught[0] if caught else None,
        'near_miss_right_place': near_place,
        'near_miss_right_content': near_content,
        'findings_total': len(findings),
        'findings_by_track': tracks,
        'findings_by_slot': counts(findings, 'slot'),
        'findings_by_severity': counts(findings, 'severity'),
        'escalated': sum(1 for finding in findings if finding.get('escalate')),
        'quarantined': progress_of(run).get('quarantined_findings_count'),
        **cost_of(run),
    }


def counts(findings: list, field: str) -> dict:
    out = {}
    for finding in findings:
        value = str(finding.get(field) or 'unknown')
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def score_control(run: Path, paper: str = '') -> dict:
    """What a run alleged on a paper with nothing planted in it.

    The rule is the same shape as the catching rule: a finding counts as a
    planted-style allegation when its words claim a contradiction, a number
    that does not follow, or a reference that does not exist, and it is at
    major severity or above. A question about an unclear method is not an
    allegation of this sort, however fair it is.
    """
    findings = findings_of(run)
    alleged = [brief(finding) for finding in findings
               if ALLEGATION.search(words_of(finding))
               and str(finding.get('severity') or '').lower() in SEVERE]
    return {
        'run': str(run),
        'paper': paper or Path(run).name,
        'kind': 'control',
        'findings_total': len(findings),
        'planted_style_allegations': len(alleged),
        'allegations': alleged,
        'findings_by_slot': counts(findings, 'slot'),
        'findings_by_severity': counts(findings, 'severity'),
        **cost_of(run),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='score.py', description='Score one run against its answer key.')
    parser.add_argument('run', help='the run folder')
    parser.add_argument('--key', default='', help='the answer key, or omitted for a control')
    parser.add_argument('--paper', default='', help='the paper name, for a control')
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    run = Path(args.run)
    if args.key:
        key = json.loads(Path(args.key).read_text(encoding='utf-8'))
        print(json.dumps(score(run, key), indent=2))
    else:
        print(json.dumps(score_control(run, args.paper), indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
