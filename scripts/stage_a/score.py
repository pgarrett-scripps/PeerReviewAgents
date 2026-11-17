"""Score a run against the answer key of the error planted in its paper.

The question Stage A asks is narrow enough to answer by rule, and that is the
point: whether a run caught the planted error is decided here, by the key the
planting script wrote, and not by reading the findings and forming an
impression of them. An impression would drift between arms and between
sessions, which is exactly the comparison Stage A is for.

**The rule.** A finding catches the planted error when it is in the right place
*and* about the right thing:

* **Place** — the finding cites one of the lines the key lists: its own line
  through `end_line`, one of its `evidence_lines`, or the line of its
  corroborating quote. Failing that, the text it quotes is inside one of the
  passages the key records as altered.
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
de-duplicated to one row per ``finding_uid``, so a finding restated during the
run is one catch and not two; a run that was killed
mid-write is read as far as it can be read instead of raising; and every row
carries ``publishable`` and ``problems``, so a score computed from a damaged
folder says so in the table rather than passing as a whole review.

**Which piece of the paper a finding is about.** A run folder holds the
manuscript the review was made against — ``paper.md`` and the ``map.json`` over
it — so the piece behind a finding is in the run and not somewhere else. A
finding cites a line, the map has one row per piece covering a range of lines,
and that row says what the piece is (a paragraph, a table, an equation, a figure
description), what page it came off, and who wrote it: the converter, or a model
that read the page and wrote or repaired it. :func:`brief` carries that row with
every finding when the manuscript is readable, which is what lets a results
table count the findings that rest on a display item and the findings that rest
on text a model wrote. A folder with no manuscript scores its findings with no
piece behind any of them, and says so.

Nothing here makes a model call.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from peerreviewagents import read_manuscript
from peerreviewagents.engine import quotes
from peerreviewagents.engine.paper_base import MAIN_DOCUMENT
from peerreviewagents.issues import lines_of
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


def places_of(finding: dict) -> set:
    """Every place in the manuscript the finding points at, as ``(document, line)``.

    The anchor and its evidence lines sit in the finding's own document, and a
    corroborating passage carries the document it was anchored in, which is the
    same one.
    """
    document = str(finding.get('document') or MAIN_DOCUMENT)
    places = {(document, number) for number in lines_of(finding)}
    corroborating = finding.get('corroborating')
    if isinstance(corroborating, dict):
        second = str(corroborating.get('document') or document)
        places |= {(second, number) for number in lines_of(corroborating)}
    return places


def in_place(finding: dict, key: dict) -> bool:
    """Does the finding point at the altered text?"""
    wanted = {(str(place.get('document') or MAIN_DOCUMENT), int(place['line']))
              for place in key['detect']['lines'] if place.get('line')}
    if places_of(finding) & wanted:
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
        return {'status': 'unknown', 'publishable': False, 'findings': [], 'issues': [],
                'issues_source': None, 'findings_source': 'none', 'counts': {},
                'quarantined': [],
                'guard_status': None, 'cause': str(refusal), 'note': None,
                'not_publishable_reasons': [str(refusal)],
                'runtime': {}, 'usage': {}, 'agents': [], 'problems': [str(refusal)]}


def findings_of(run: Path) -> list:
    """The findings a run delivered, de-duplicated, as :func:`read_run` reads them."""
    return reading_of(run)['findings']


#: The piece kinds that are not prose: a finding that cites one of these is
#: about something the page laid out — a table, an equation, a figure — rather
#: than about a sentence. ``figure-description`` is the reader's own account of
#: a figure the converter could only name, and it is a figure for this purpose.
DISPLAY_KINDS = ('table', 'equation', 'figure', 'figure-description')

#: What a map row's ``source`` says about who wrote the piece a finding cites.
#: ``text`` is the converter's extraction, ``ai`` a piece a model wrote where
#: the converter had nothing, and ``ai-reconstructed`` one a model repaired,
#: with the converter's own reading kept beside it in ``original``.
WROTE = {'text': 'the converter', 'ai': 'written by the reader',
         'ai-reconstructed': 'repaired by the reader'}

#: One loaded manuscript per run folder, as ``(base, refusal)``. A score walks
#: every finding of a run and every finding names a line, so the base is read
#: once and asked many times rather than re-read per finding.
_BASES: dict = {}


def base_of(run: Path):
    """The manuscript the run was reviewed against, or ``None`` with a reason.

    The workspace is the run's own ``manuscript/`` folder, and it is read by
    :func:`peerreviewagents.read_manuscript` rather than by opening
    ``map.json`` here, so a folder in a shape this version does not read — the
    old ``paper.json`` blocks, a ``paper-base-v1`` map — is refused by name
    instead of half-understood. :func:`base_refusal` carries that refusal into
    the results row, because a score with no piece behind any finding and a
    score of a paper nothing was cited in are different results.
    """
    folder = Path(run).expanduser() / 'manuscript'
    key = str(folder)
    if key not in _BASES:
        try:
            _BASES[key] = (read_manuscript(folder), '')
        except (NotARunFolder, FileNotFoundError, NotADirectoryError, ValueError,
                OSError) as refusal:
            _BASES[key] = (None, str(refusal))
    return _BASES[key][0]


def base_refusal(run: Path) -> str:
    """Why this run's manuscript could not be read, or ``''`` when it was."""
    base_of(run)
    return _BASES[str(Path(run).expanduser() / 'manuscript')][1]


def reader_of(base) -> dict:
    """What the Read stage did to this manuscript, from the manifest's ``ai_read``.

    ``actions`` counts pieces that are still in the manuscript, one tally per
    act, and ``gone`` counts pieces that have left it — folded into another,
    merged, withdrawn — kept at the time of the act. The two do not add up, and
    both are the reader's own numbers rather than anything recomputed here.
    """
    read = (base.manifest.get('ai_read') if base is not None else None) or {}
    read = read if isinstance(read, dict) else {}
    return {'read': bool(read.get('read')),
            'reader_model': str(read.get('reader_model') or ''),
            'pages_viewed': read.get('pages_viewed'),
            'pages': read.get('pages'),
            'actions': dict(read.get('actions') or {}),
            'gone': dict(read.get('gone') or {}),
            'tool_calls': read.get('tool_calls'),
            'lost_pages': list(read.get('lost_pages') or []),
            # The reader's note when one read the pages, and the reason nothing
            # did when none did: the two fields that say what happened.
            'note': str(read.get('note') or read.get('reason') or '')}


def piece_at_line(base, line, document=MAIN_DOCUMENT, how='anchor') -> dict:
    """The piece holding this line, as a results row records a location.

    *how* is how the line was arrived at — the finding's anchor, a search for
    its quote, an evidence line, a corroborating passage — and it is kept
    because a piece found by searching for an unanchored quote is a weaker
    location than one the engine anchored when it recorded the finding.
    """
    row = base.piece_at(line, document) if base is not None and line else None
    if not row:
        return {}
    source = str(row.get('source') or 'text')
    original = row.get('original') if isinstance(row.get('original'), dict) else {}
    folded = [one for field in ('absorbed', 'merged')
              for one in (original.get(field) or []) if isinstance(one, dict)]
    piece = {'document': document, 'line': int(line), 'located_by': str(how),
             'lines': [int(row['lines'][0]), int(row['lines'][1])],
             'kind': str(row.get('kind') or ''), 'page': row.get('page'),
             'section': row.get('section'), 'source': source,
             'written': WROTE.get(source, source),
             'from_reader': source != 'text',
             'reader_wrote': source == 'ai',
             'reader_repaired': source == 'ai-reconstructed',
             'display': str(row.get('kind') or '') in DISPLAY_KINDS}
    # What the converter had here before a model repaired it: the kind it read
    # the piece as, which is how a paragraph re-read as an equation shows up,
    # and how many of its pieces were folded into this one.
    if original.get('kind'):
        piece['was'] = str(original['kind'])
    if folded:
        piece['folded_in'] = len(folded)
    return piece


def _quote_lines(base, finding, document) -> list:
    """The line a finding's quote sits on, when the finding carries no anchor.

    A quarantined row, or one from a run that recorded no ``piece_lines``, has
    its quote looked for in the document. A quote that occurs twice is no
    location — the same words in two places do not say which one the finding
    means — and a quote too short to identify a passage is not searched for at
    all, by the same rule :func:`in_place` uses.
    """
    quote = str(finding.get('quote') or finding.get('anchor_text') or '')
    if base is None or len(squash(quote)) < 12:
        return []
    found = quotes.placements(base, quote, document=document)
    return [int(found[0]['line'])] if len(found) == 1 else []


def pieces_cited(base, finding) -> list:
    """Every piece of the manuscript a finding points at, its location first.

    The location is where the cited quote sits: the engine anchored the quote to
    a line when it recorded the finding, so the map row holding that line is the
    piece the finding is about. Evidence lines and a corroborating passage come
    after it, because a finding that quotes a paragraph while pointing at the
    table under it is a finding about the table too, and a count of findings
    that rest on a table would miss it otherwise.
    """
    if base is None:
        return []
    document = str(finding.get('document') or MAIN_DOCUMENT)
    anchored = lines_of({'line': finding.get('line'), 'end_line': finding.get('end_line')})
    places = [(number, document, 'anchor') for number in anchored]
    if not places:
        places = [(number, document, 'quote')
                  for number in _quote_lines(base, finding, document)]
    places += [(number, document, 'evidence')
               for number in lines_of({'evidence_lines': finding.get('evidence_lines')})]
    corroborating = finding.get('corroborating')
    if isinstance(corroborating, dict):
        second = str(corroborating.get('document') or document)
        places += [(number, second, 'corroborating') for number in lines_of(corroborating)]
    out, seen = [], set()
    for number, name, how in places:
        piece = piece_at_line(base, number, name, how)
        if not piece:
            continue
        where = (name, tuple(piece['lines']))
        if where not in seen:
            seen.add(where)
            out.append(piece)
    return out


def piece_text(base, piece) -> str:
    """The manuscript's own lines for one cited piece, as they stand."""
    lines = (piece or {}).get('lines') or []
    if base is None or len(lines) != 2:
        return ''
    return base.range_text(lines[0], lines[1], piece.get('document') or MAIN_DOCUMENT)


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
        # The engine says why a run is not publishable; the matrix prints those
        # words rather than guessing at them from the parts, so one rule is
        # described in one place.
        'not_publishable_reasons': reading.get('not_publishable_reasons') or [],
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
        # What the run asked its core reviewers to be, which is the arm as
        # designed; `models` above is what they reported being. A row from
        # before 0.9.0 has an empty list here, because every slot's model was
        # pinned in the plugin agent files and the run recorded no list.
        'reviewer_models': (reading.get('identity') or {}).get('reviewer_models') or [],
    }


def brief(finding: dict, base=None) -> dict:
    """The part of a finding worth putting in a results table.

    With *base* — the run's own manuscript — the row also says which piece of
    the paper the finding cites and who wrote that piece, in ``piece``, and
    carries the four questions a table counts over as their own fields:
    ``cites_kinds`` is every kind of piece this finding points at,
    ``cites_display`` whether one of them is a table, an equation or a figure,
    and ``cites_reader_text`` whether one of them is text a model wrote or
    repaired rather than the converter's extraction. Without a base the row is
    the finding alone, because an unreadable manuscript is not an answer of
    ``false`` to any of those questions.
    """
    row = {field: finding.get(field) for field in
           ('finding_uid', 'title', 'slot', 'role', 'track', 'severity', 'kind', 'basis',
            'document', 'line', 'quote', 'escalate')}
    if base is None:
        return row
    cited = pieces_cited(base, finding)
    row['piece'] = cited[0] if cited else {}
    row['cites_kinds'] = sorted({piece['kind'] for piece in cited if piece.get('kind')})
    row['cites_display'] = any(piece.get('display') for piece in cited)
    row['cites_reader_text'] = any(piece.get('from_reader') for piece in cited)
    row['cites_reader_written'] = any(piece.get('reader_wrote') for piece in cited)
    row['cites_reader_repaired'] = any(piece.get('reader_repaired') for piece in cited)
    return row


def score(run: Path, key: dict, reading: dict | None = None, base=None) -> dict:
    """Did this run catch the planted error, and what did it cost?

    *base* is the manuscript the run was reviewed against, read from the run
    folder when it is not passed, and ``None`` for a folder that holds none.
    """
    reading = reading if reading is not None else reading_of(run)
    base = base_of(run) if base is None else base
    findings = reading['findings']
    caught, near_place, near_content = [], [], []
    for finding in findings:
        placed, about = in_place(finding, key), about_it(finding, key)
        if placed and about:
            caught.append(brief(finding, base))
        elif placed:
            near_place.append(brief(finding, base))
        elif about:
            near_content.append(brief(finding, base))
    tracks = {}
    for finding in findings:
        tracks[str(finding.get('track') or 'unknown')] = \
            tracks.get(str(finding.get('track') or 'unknown'), 0) + 1
    # A catch the review guard threw out is a catch the arm made and the run did
    # not deliver, which is a different number from a miss and has to be counted
    # separately: it says the brief needs fixing, not that the reviewers failed.
    # The place rule reaches a quarantined row through the line it cites, which
    # the guard leaves in place even when it rejects the quote.
    held = [brief(finding, base) for finding in (reading.get('quarantined') or [])
            if in_place(finding, key) and about_it(finding, key)]
    return {
        'run': str(run),
        'paper': key['paper'],
        'kind': key['kind'],
        'rule': key['rule'],
        'reader': reader_of(base),
        'manuscript_refusal': base_refusal(run) if base is None else '',
        'caught': bool(caught),
        'caught_by': caught,
        'first_catch': caught[0] if caught else None,
        'near_miss_right_place': near_place,
        'near_miss_right_content': near_content,
        'findings_total': len(findings),
        'issues_total': (reading.get('counts') or {}).get('issues'),
        'issues_required': (reading.get('counts') or {}).get('required'),
        'issues_suggestions': (reading.get('counts') or {}).get('suggestions'),
        'findings_by_track': tracks,
        'findings_by_slot': counts(findings, 'slot'),
        'findings_by_severity': counts(findings, 'severity'),
        'escalated': sum(1 for finding in findings if finding.get('escalate')),
        'quarantined': len(reading.get('quarantined') or []),
        'quarantined_catches': len(held),
        'quarantined_caught_by': held,
        **cost_of(reading),
    }


def counts(findings: list, field: str) -> dict:
    out = {}
    for finding in findings:
        value = str(finding.get(field) or 'unknown')
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def piece_counts(rows: list) -> dict:
    """What these findings cite, over briefs made with a base.

    ``unlocated`` is the number whose quote could not be put in a piece, and it
    is reported beside the others because a run whose findings cite nothing
    locatable has to look different from one whose findings all cite prose.
    """
    kinds = {}
    for row in rows:
        kind = str((row.get('piece') or {}).get('kind') or 'nothing')
        kinds[kind] = kinds.get(kind, 0) + 1
    return {
        'by_piece_kind': dict(sorted(kinds.items())),
        'citing_display': sum(1 for row in rows if row.get('cites_display')),
        'on_reader_text': sum(1 for row in rows if row.get('cites_reader_text')),
        'on_reader_written': sum(1 for row in rows if row.get('cites_reader_written')),
        'on_reader_repaired': sum(1 for row in rows if row.get('cites_reader_repaired')),
        'unlocated': sum(1 for row in rows if not (row.get('piece') or {}).get('kind')),
    }


def score_control(run: Path, paper: str = '', reading: dict | None = None, base=None) -> dict:
    """What a run alleged on a paper with nothing planted in it.

    The rule is the same shape as the catching rule: a finding counts as a
    planted-style allegation when its words claim a contradiction, a number
    that does not follow, or a reference that does not exist, and it is at
    major severity or above. A question about an unclear method is not an
    allegation of this sort, however fair it is.
    """
    reading = reading if reading is not None else reading_of(run)
    base = base_of(run) if base is None else base
    findings = reading['findings']
    alleged = [brief(finding, base) for finding in findings
               if ALLEGATION.search(words_of(finding))
               and str(finding.get('severity') or '').lower() in SEVERE]
    return {
        'run': str(run),
        'paper': paper or Path(run).name,
        'kind': 'control',
        'reader': reader_of(base),
        'manuscript_refusal': base_refusal(run) if base is None else '',
        'findings_total': len(findings),
        'issues_total': (reading.get('counts') or {}).get('issues'),
        'issues_required': (reading.get('counts') or {}).get('required'),
        'issues_suggestions': (reading.get('counts') or {}).get('suggestions'),
        'planted_style_allegations': len(alleged),
        'allegations': alleged,
        'findings_major': sum(1 for finding in findings
                              if str(finding.get('severity') or '').lower() in SEVERE),
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
