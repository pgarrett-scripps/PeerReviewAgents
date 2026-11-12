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

Both ignore spacing, because a finding quotes a paper's text as the model saw
it. Place is decided on digits and letters alone (`squash`). Content keeps the
decimal points (`numeric_form`) and makes a short value sit on its own
(`names_value`), because `.72` is not the `72` of `72%` and not the `72.18` of
an arithmetic receipt.

A finding that is about the right thing in the wrong place, or in the right
place about something else, is recorded as a near miss rather than silently
dropped: near misses are what tell a brief it looked in the right section and
missed the check.

**Controls.** An unaltered paper has no key, so there is nothing to catch and
the measurement is the opposite one: does the run allege an error of the
planted sort where none was planted? That is counted by the same kind of
written rule — a finding that claims a contradiction, a number that does not
follow, or a reference that does not exist, at major severity or above.

**Where the numbers come from.** The run folder is read once, by
:func:`peerreviewagents.runs.read_run`, and never by opening the run's files
here. That matters for a results table in three ways: the findings are
de-duplicated to one row per ``finding_uid``, so a finding the editor placed in
both the body and the appendix is one catch and not two; a run that was killed
mid-write is read as far as it can be read instead of raising; and every row
carries ``publishable`` and ``problems``, so a score computed from a damaged
folder says so in the table rather than passing as a whole review.

Nothing here makes a model call.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from peerreviewagents.runs import NotARunFolder, read_run

ALLEGATION = re.compile(
    r'does not match|do not match|does not agree|disagree|inconsisten|contradict'
    r'|does not follow|cannot be reproduced|does not add up|mismatch|miscalculat'
    r'|incorrectly reported|wrong(?:ly)? (?:value|number|figure|percentage)'
    r'|not a (?:valid|registered|real) doi|does not resolve|no such (?:doi|reference)'
    r'|fabricat|transposed|swapped|arithmetic', re.I)
SEVERE = ('major', 'critical', 'blocking', 'high')


def squash(text: str) -> str:
    return re.sub(r'[^0-9a-z]', '', str(text).lower())


def numeric_form(text: str) -> str:
    """Letters, digits and decimal points, with a leading zero before a point dropped.

    The place rule can throw punctuation away, because a quote is matched
    against a whole passage. The content rule cannot: ``.72`` and ``72`` are
    different numbers, and a p-value written ``P = 0.72`` is the same number as
    ``P = .72``. Anything that is not part of a number becomes a space rather
    than nothing, because deleting it joins numbers to whatever precedes them —
    ``Table 1; 548`` would read as the single run of digits ``1548`` — and a
    comma between digits is dropped, because ``11,548`` is one number.
    """
    text = re.sub(r'(?<=[0-9]),(?=[0-9])', '', str(text).lower())
    spaced = re.sub(r'[^0-9a-z.]+', ' ', text)
    return re.sub(r'(?<![0-9])0\.', '.', spaced)


def names_value(said: str, mention: str) -> bool:
    """Do these words name this value, as a number and not as a run of digits?

    A short number has to sit on its own. The pilot showed why: with ``.72``
    matched as the digits ``72``, a finding that said ``109 of 151, 72%`` about
    another part of the paper, and one whose arithmetic receipt happened to
    contain ``72.185430463``, both counted as naming the planted p-value. A
    long value — a DOI suffix — is specific enough to match wherever it appears,
    and has to be, because it is written inside a longer string of digits.
    """
    mention = numeric_form(mention)
    if not mention:
        return False
    digits = sum(1 for character in mention if character.isdigit())
    if digits == 0 or digits > 5:
        return mention in said
    before = r'(?<![0-9])' if mention.startswith('.') else r'(?<![0-9.])'
    return re.search(before + re.escape(mention) + r'(?![0-9])', said) is not None


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
    said = numeric_form(own_words_of(finding))
    mentions = [value for value in key['detect']['must_mention'] if numeric_form(value)]
    if not mentions:
        return False
    hits = [names_value(said, mention) for mention in mentions]
    return all(hits) if key['detect'].get('mention_rule') == 'all' else any(hits)


def reading_of(run: Path) -> dict:
    """The run folder, read the supported way, or an empty reading for no folder.

    A run that never got as far as writing a review folder is a result too — it
    is the arm's answer for that paper — so it is read as a row with no findings
    and a problem naming it, rather than as a crash in the driver.
    """
    try:
        return read_run(Path(run))
    except (NotARunFolder, FileNotFoundError, ValueError) as refusal:
        return {'status': 'unknown', 'publishable': False, 'findings': [], 'body': [],
                'appendix': [], 'findings_source': 'none', 'counts': {}, 'quarantined': [],
                'guard_status': None, 'cause': str(refusal), 'note': None,
                'runtime': {}, 'usage': {}, 'agents': [], 'problems': [str(refusal)]}


def findings_of(run: Path) -> list:
    """The findings a run delivered, de-duplicated, as :func:`read_run` reads them."""
    return reading_of(run)['findings']


def cost_of(reading: dict) -> dict:
    """Tokens, minutes, agents and status, as the run itself recorded them.

    Two token numbers, because they answer different questions: ``tokens`` is
    the starter's own accounting, which is what the matrix's ceiling is checked
    against, and ``observed_tokens`` is what the usage receipts add up to, which
    is a lower bound when a timed-out attempt spent an amount nobody counted.
    """
    runtime, usage = reading.get('runtime') or {}, reading.get('usage') or {}
    agents = reading.get('agents') or []
    return {
        'status': reading.get('status'),
        'publishable': reading.get('publishable'),
        'problems': reading.get('problems') or [],
        'blocked_reason': reading.get('cause'),
        'note': reading.get('note'),
        'guard_status': reading.get('guard_status'),
        'findings_source': reading.get('findings_source'),
        'tokens': runtime.get('tokens_total') or 0,
        'observed_tokens': usage.get('observed_tokens') or 0,
        'tokens_unknown': bool(usage.get('unknown')),
        'minutes': round((runtime.get('wall_seconds') or 0) / 60.0, 1),
        'agents': len(agents),
        'tool_calls': sum((row.get('toolCalls') or 0) for row in agents),
        'models': sorted({str(row.get('model')) for row in agents if row.get('model')}),
    }


def brief(finding: dict) -> dict:
    """The part of a finding worth putting in a results table."""
    return {field: finding.get(field) for field in
            ('finding_uid', 'title', 'slot', 'role', 'track', 'severity', 'kind', 'basis',
             'block_id', 'quote', 'escalate')}


def score(run: Path, key: dict, reading: dict | None = None) -> dict:
    """Did this run catch the planted error, and what did it cost?"""
    reading = reading if reading is not None else reading_of(run)
    findings = reading['findings']
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
        'findings_body': len(reading['body']),
        'findings_appendix': len(reading['appendix']),
        'findings_by_track': tracks,
        'findings_by_slot': counts(findings, 'slot'),
        'findings_by_severity': counts(findings, 'severity'),
        'escalated': sum(1 for finding in findings if finding.get('escalate')),
        'quarantined': len(reading.get('quarantined') or []),
        **cost_of(reading),
    }


def counts(findings: list, field: str) -> dict:
    out = {}
    for finding in findings:
        value = str(finding.get(field) or 'unknown')
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def score_control(run: Path, paper: str = '', reading: dict | None = None) -> dict:
    """What a run alleged on a paper with nothing planted in it.

    The rule is the same shape as the catching rule: a finding counts as a
    planted-style allegation when its words claim a contradiction, a number
    that does not follow, or a reference that does not exist, and it is at
    major severity or above. A question about an unclear method is not an
    allegation of this sort, however fair it is.
    """
    reading = reading if reading is not None else reading_of(run)
    findings = reading['findings']
    alleged = [brief(finding) for finding in findings
               if ALLEGATION.search(words_of(finding))
               and str(finding.get('severity') or '').lower() in SEVERE]
    return {
        'run': str(run),
        'paper': paper or Path(run).name,
        'kind': 'control',
        'findings_total': len(findings),
        'findings_body': len(reading['body']),
        'findings_appendix': len(reading['appendix']),
        'planted_style_allegations': len(alleged),
        'allegations': alleged,
        'findings_by_slot': counts(findings, 'slot'),
        'findings_by_severity': counts(findings, 'severity'),
        'quarantined': len(reading.get('quarantined') or []),
        **cost_of(reading),
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
