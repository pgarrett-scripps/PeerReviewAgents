"""Score one SPOT pilot run: what it found, what it cost, what it checked.

Stage A's scorer decides a catch by rule, because Stage A planted the error and
knows the exact numbers the finding has to name. SPOT's errors were not planted:
for two of the five the annotated error is a value a finding can be required to
name — a negative LOQ, a 12.0 dB gain — and for the other three it is a
duplicated micrograph, a misspelled axis label and a reagent without a kinase
domain, which no regular expression settles. So this scorer stops where the rule
stops. It reports, per run:

* the **location** findings that land where SPOT says the error is, and the
  **cue** findings whose own words carry the key's phrases or values — both as
  *candidates*, never as a verdict;
* every finding in brief, with the piece of the manuscript it cites: what kind
  of piece it is, what page it came off, and whether the converter extracted it
  or a model that read the page wrote or repaired it — and, when a model did,
  whether it rebuilt a displayed piece or only cut debris out of the
  converter's own sentences, which are not the same contribution;
* what the Read stage did to the manuscript, from its own ``ai_read`` account;
* the citation ledger's counts by state, straight from ``read_run``;
* tokens, minutes, agents, status, guard status and the run's own problems;
* for a control, Stage A's false-allegation count, by Stage A's own rule.

**Location is decided in the manuscript, not in the reviewer's prose.** A
finding cites a quote, the engine anchored that quote to a line, and
``map.json`` says which piece holds that line — so the key's phrases are looked
for in the piece the finding cites and in the quote it took from it, rather than
in the words the reviewer wrote around them. A reviewer who writes "Table 1" in
a comment about Figure 3 is then not at the location, and one who quotes the
table without naming it is.

The yes/partly/no verdict is written by hand into the results document, with the
finding quoted, because that is a reading and not a measurement. What this file
guarantees is that the reading starts from every candidate the run produced,
that every candidate says which piece of the paper it rests on, and that no cost
number is retyped from a terminal.

A run folder is named by whoever made the run — a comparison names a pair of
folders after the arm, not after the paper — so the paper is named with
``--slug`` and the folder name is only a fallback.

    python scripts/spot/score.py <run folder> --slug medicine-oxytetracycline
    python scripts/spot/score.py --root ~/pra-spot --all      # every run on disk

Runs made by a version before ``paper-base-v2`` are not scored here: their
manuscript is blocks with ids rather than lines of ``paper.md``, so no finding
in them cites a piece this file could name.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'stage_a'))

from score import (  # noqa: E402
    ALLEGATION,
    SEVERE,
    base_of,
    base_refusal,
    brief,
    cost_of,
    counts,
    names_value,
    own_words_of,
    piece_counts,
    piece_text,
    pieces_cited,
    reader_of,
    reading_of,
    score_control,
    words_of,
)

#: What SPOT says is wrong, per chosen paper, in the forms a scorer can use:
#: ``where`` are phrases that put a *piece of the manuscript* at the annotated
#: location — they are looked for in the piece a finding cites, not in the
#: reviewer's prose — ``where_kinds`` are the kinds of piece the error lives in,
#: and ``values`` are numbers a finding has to name for the Stage A content rule
#: to hold. ``cues`` are the words that would appear in a finding about this
#: error; a cue hit is a candidate to read, not a catch. ``annotation`` is
#: SPOT's own sentence, kept here so the reading never drifts from the
#: benchmark.
KEYS = {
    'medicine-oxytetracycline': {
        'row': 66, 'kind': 'Statistical reporting', 'where_label': 'Table 1',
        'where': ['table 1', 'table. 1', 'pharmacokinetic parameter', 'loq', 'auc'],
        'where_kinds': ['table'],
        'values': ['587.51', '1780.34'],
        'cues': [r'negative (?:value|auc|loq|concentration|parameter)',
                 r'cannot be negative', r'nonsensical', r'below zero', r'minus sign'],
        'annotation': 'Negative values are reported for pharmacokinetic parameters such as '
                      'AUC, elimination rates, and LOQ in Table 1, which are nonsensical '
                      'since these parameters should always be positive.',
    },
    'chemistry-photocatalytic': {
        'row': 8, 'kind': 'Data inconsistency', 'where_label': 'Fig. 4',
        'where': ['fig. 4', 'figure 4', 'fig 4', 'absorption spectra'],
        'where_kinds': ['figure', 'figure-description', 'caption'],
        'values': [],
        'cues': [r'wavelenght', r'misspel', r'spelling', r'artefact', r'artifact',
                 r'spike', r'off[- ]scale', r'clipp', r'260\s*nm', r'unfiltered'],
        'annotation': 'The UV-Vis spectrum for the chemically synthesized Ag nanoparticles '
                      'displays an artifact — a sharp, anomalous peak at ~260 nm — due to '
                      'unfiltered junk data not being removed before plotting, and the '
                      'horizontal axis label is also misspelled.',
    },
    'biology-dark-kinome': {
        'row': 6, 'kind': 'Reagent identity', 'where_label': 'Overall',
        'where': ['recombinant', 'kinase domain', 'protein fragment', 'reagent'],
        # SPOT's annotation is "Overall": the reagents are named in the methods
        # prose and in a supplementary table, so no kind of piece is the place.
        'where_kinds': [],
        'values': [],
        'cues': [r'kinase domain', r'catalytic domain', r'lacks? the', r'truncat',
                 r'fragment (?:does not|lacks)', r'no intrinsic', r'not a kinase'],
        'annotation': 'The recombinant protein fragments used for at least three kinases do '
                      'not include their respective kinase domains, so the reagents have no '
                      'intrinsic kinase activity and the results do not reflect actual '
                      'kinase function for those proteins.',
    },
    'biology-cgrp': {
        'row': 53, 'kind': 'Figure duplication', 'where_label': 'Fig 5 and Fig 6',
        'where': ['fig. 5', 'figure 5', 'fig 5', 'fig. 6', 'figure 6', 'fig 6'],
        'where_kinds': ['figure', 'figure-description', 'caption'],
        'values': [],
        'cues': [r'duplicat', r'same image', r'identical (?:image|panel|micrograph|field)',
                 r'reused', r're-?use of', r'overlap', r'cropped'],
        'annotation': 'Figure 5 contains duplicated images that are also found in Figure 6, '
                      'specifically enlarged and cropped versions of the same images across '
                      'both figures.',
    },
    'network-beamforming': {
        'row': 1, 'kind': 'Equation / proof', 'where_label': 'beamforming gain',
        'where': ['beamforming gain', 'combining gain', 'antenna element', '12.0 db'],
        'where_kinds': ['equation'],
        'values': ['12.0'],
        'cues': [r'n\^?2', r'n\s*\*\*\s*2', r'squar', r'24(?:\.1)?\s*db', r'coherent',
                 r'gain of n\b', r'underestimat'],
        'annotation': 'The beamforming gain for N antennas is calculated as N, when coherent '
                      'beamforming makes the received power rise with the square of the '
                      'number of antennas (N^2): 16 antennas are stated as 12.0 dB.',
    },
}

#: The controls, which have no key: nothing is known to be wrong in them.
CONTROLS = ('control-education-pmc7183109', 'control-microbiology-pmc8208984')


def where_hits(base, finding: dict, key: dict, cited: list) -> list:
    """Which of the key's location phrases the piece this finding cites carries.

    What is searched is the manuscript and not the review: the piece the cited
    quote sits in — its own text and the section heading above it — together
    with the quote. That is the one piece the finding is anchored to; the
    evidence lines it also names are counted by :func:`at_kind` instead, where a
    table two lines below the quoted sentence is a table the finding points at
    without the paragraph having to mention it. A phrase in the reviewer's own
    sentence is a cue, counted by :func:`mentions_cue`, and not a location —
    naming Table 1 while discussing Figure 3 does not put a finding in Table 1.
    """
    located = cited[0] if cited else {}
    said = ' '.join([piece_text(base, located), str(located.get('section') or ''),
                     str(finding.get('quote') or '')]).lower()
    return [phrase for phrase in key['where'] if phrase in said]


def at_kind(key: dict, cited: list) -> bool:
    """Whether the finding cites a piece of the kind the error lives in."""
    kinds = [str(piece.get('kind') or '') for piece in cited]
    return any(kind in (key.get('where_kinds') or []) for kind in kinds)


def mentions_cue(finding: dict, key: dict) -> list:
    """Which of the key's cues this finding's own words carry."""
    said = own_words_of(finding)
    return [cue for cue in key['cues'] if re.search(cue, said, re.I)]


def names_values(finding: dict, key: dict) -> list:
    """Which of the key's values the finding names, by Stage A's value rule."""
    said = own_words_of(finding)
    return [value for value in key['values'] if names_value(said, value)]


def score_paper(run: Path, slug: str, reading: dict | None = None, base=None) -> dict:
    """Candidates, findings and cost for one paper with a known error."""
    key = KEYS[slug]
    reading = reading if reading is not None else reading_of(run)
    base = base_of(run) if base is None else base
    findings = reading['findings']
    briefs, candidates = [], []
    for finding in findings:
        cited = pieces_cited(base, finding)
        row = brief(finding, base)
        briefs.append(row)
        place = where_hits(base, finding, key, cited)
        cues, values = mentions_cue(finding, key), names_values(finding, key)
        if place or cues or values:
            candidates.append({**row, 'at_location': bool(place), 'where_hits': place,
                               'at_kind': at_kind(key, cited),
                               'cues': cues, 'values_named': values,
                               'stage_a_rule': bool(place and values and
                                                    len(values) == len(key['values']))})
    return {
        'run': str(run), 'paper': slug, 'spot_row': key['row'], 'kind': key['kind'],
        'where': key['where_label'], 'annotation': key['annotation'],
        'findings_total': len(findings),
        'issues_total': (reading.get('counts') or {}).get('issues'),
        'issues_required': (reading.get('counts') or {}).get('required'),
        'issues_suggestions': (reading.get('counts') or {}).get('suggestions'),
        'candidates': candidates,
        'candidates_at_location': sum(1 for one in candidates if one['at_location']),
        'candidates_at_kind': sum(1 for one in candidates if one['at_kind']),
        'candidates_by_cue': sum(1 for one in candidates if one['cues']),
        'caught_by_stage_a_rule': sum(1 for one in candidates if one['stage_a_rule']),
        'candidate_pieces': piece_counts(candidates),
        'severe_allegations': sum(1 for finding in findings
                                  if ALLEGATION.search(words_of(finding))
                                  and str(finding.get('severity') or '').lower() in SEVERE),
        'findings_by_slot': counts(findings, 'slot'),
        'findings_by_severity': counts(findings, 'severity'),
        'findings_major': sum(1 for finding in findings
                              if str(finding.get('severity') or '').lower() in SEVERE),
        'quarantined': len(reading.get('quarantined') or []),
        'reader': reader_of(base),
        'manuscript_refusal': base_refusal(run) if base is None else '',
        'finding_pieces': piece_counts(briefs),
        'citations': citations_of(reading),
        'marker_shapes': marker_shapes(base, reading),
        'citation_causes': citation_causes(run),
        'all_findings': briefs,
        **cost_of(reading),
    }


def citations_of(reading: dict) -> dict:
    """The citation lane's own numbers, as the run recorded them."""
    citations = reading.get('citations') or {}
    return {'enabled': citations.get('enabled'), 'complete': citations.get('complete'),
            'summary': citations.get('summary'), 'counts': citations.get('counts') or {},
            'problems': citations.get('problems') or []}


#: A reference whose raw text looks like something other than a journal article:
#: a standard, a book, an agency report, a thesis, a web page. Crossref and
#: Europe PMC are indexes of articles, so "not located" is the expected answer
#: for one of these and says nothing about the citing paper.
#:
#: A bare URL is deliberately *not* one of these signals. Springer and Nature
#: print a DOI as a URL on every ordinary article in the list, so matching
#: ``https?://`` labelled 35 of 43 resolved references on one paper as
#: non-articles — a count about the publisher's house style, not about the
#: references. Only a URL with the words of a visited web page counts.
NOT_AN_ARTICLE = re.compile(
    r'\b(FAO|WHO|OIE|USDA|EPA|APHA|Association|Organi[sz]ation|Commission|Directive|'
    r'Regulation|Standard Methods|guideline|handbook|manual|Press|thesis|dissertation|'
    r'Publication No|eds?\.|accessed|retrieved|last visited)\b', re.I)

#: Numerals in square brackets — the one in-text marker shape the engine's
#: ledger matches on the numeric side.
BRACKETED = re.compile(r'\[\d{1,3}(?:\s*[,;-]\s*\d{1,3})*\]')

#: A word with digits glued to its end: what a superscript citation marker
#: becomes when the converter flattens it into the text. The same shape is
#: produced by author affiliation markers, which is why counting it is a
#: diagnosis and not a patch.
GLUED_DIGITS = re.compile(r'[A-Za-z]{3,}\d{1,3}(?:[,;-]\d{1,3})*')

#: An author-year marker, the other shape the ledger matches.
AUTHOR_YEAR = re.compile(r'[A-Z][A-Za-z-]{2,}'
                         r'(?:\s+(?:et\s+al\.?|and\s+[A-Z][A-Za-z-]+|&\s+[A-Z][A-Za-z-]+))?'
                         r'[,\s]*\(?(?:1[89]|20)\d{2}\)?')


def marker_shapes(base, reading: dict) -> dict:
    """Why the ledger found the in-text uses it found, or found none.

    A ledger with no use rows means the half of the lane that asks whether a
    source supports the claim never ran, and the reason is a property of the
    manuscript's text rather than of the lookups. So this counts, in the
    manuscript the review was made against, each marker shape the ledger can
    match and the one it cannot: a superscript numeral, which arrives as digits
    glued to the end of the preceding word.
    """
    text = base.text if base is not None else ''
    uses = (reading.get('citations') or {}).get('counts', {}).get('uses')
    bracketed, glued = len(BRACKETED.findall(text)), len(GLUED_DIGITS.findall(text))
    author_year = len(AUTHOR_YEAR.findall(text))
    # Whichever shape is commonest is the paper's style. Two of the three counts
    # over-count on purpose and the over-counting is why the winner is reported
    # rather than the raw number: a glued digit is also what an author
    # affiliation marker looks like, and the author-year pattern also matches a
    # year mentioned in prose. Only the bracketed count is clean.
    shapes = {'bracketed numerals, which the ledger matches': bracketed,
              'author-year, which the ledger matches when the reference list parsed':
                  author_year,
              'superscript numerals flattened onto the preceding word, '
              'which the ledger does not match': glued}
    best = max(shapes, key=lambda name: shapes[name])
    return {'uses': uses, 'bracketed': bracketed, 'glued_superscript': glued,
            'author_year': author_year,
            'likely_shape': ('none — the manuscript carries no marker of any shape'
                             if not shapes[best] else best),
            # The gap is specifically the superscript one: markers are there, in
            # the shape the ledger cannot see, and nothing else is.
            'marker_gap': bool(not uses and glued and not bracketed
                               and glued > author_year)}


def citation_causes(run: Path) -> dict:
    """Why each reference ended unresolved, from the ledger's own resolve rows.

    ``could not check`` and ``not located`` are one state apiece in the counts,
    and they are not one cause apiece: a reference the paper printed no title
    for was never searched, a reference searched and not found in either index
    is usually a book or a standard, and a lookup that did not answer is a
    network result. The three say different things about the engine, so they are
    separated here rather than reported as a single number.
    """
    ledger = Path(run) / 'review' / 'citations' / 'ledger.jsonl'
    if not ledger.is_file():
        return {}
    rows = []
    for line in ledger.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get('kind') == 'reference' and row.get('stage') == 'resolve':
            rows.append(row)
    out = {'references': len(rows), 'printed_doi': 0, 'printed_title': 0, 'by_state': {}}
    for row in rows:
        printed = row.get('printed') or {}
        out['printed_doi'] += bool(printed.get('doi'))
        out['printed_title'] += bool(printed.get('title'))
    for state in ('yes', 'could not check', 'not located', 'no'):
        chosen = [row for row in rows if (row.get('resolution') or {}).get('exists') == state]
        if not chosen:
            continue
        out['by_state'][state] = {
            'total': len(chosen),
            'no_title_extracted': sum(1 for row in chosen
                                      if not (row.get('printed') or {}).get('title')),
            'no_doi_printed': sum(1 for row in chosen
                                  if not (row.get('printed') or {}).get('doi')),
            'lookup_made': sum(1 for row in chosen
                               if (row.get('resolution') or {}).get('requests')),
            'lookup_never_made': sum(1 for row in chosen
                                     if not (row.get('resolution') or {}).get('requests')),
            'not_an_article': sum(1 for row in chosen
                                  if NOT_AN_ARTICLE.search(row.get('raw') or '')),
            'methods': counts([(row.get('resolution') or {}) for row in chosen], 'method'),
        }
    return out


def score_one(run: Path, slug: str) -> dict:
    """One run, scored as a control, against its key, or as neither.

    A slug with no key here is scored anyway, as findings and cost and pieces
    with no candidates: a comparison run of a paper that is not one of the five
    is still a run whose numbers are worth having, and refusing it would stop
    ``--all`` on the first folder it did not recognise.

    A run whose manuscript cannot be read — a folder from before
    ``paper-base-v2``, or one that never got as far as sealing one — is not
    scored for candidates at all. Location is decided in the map over the
    manuscript, so without one every finding would be reported as nowhere, and a
    column of zeros reads like a result. Its findings and its cost are still
    reported, with the refusal beside them.
    """
    reading = reading_of(run)
    base = base_of(run)
    if base is None:
        return unkeyed(run, slug, reading, kind='not scored',
                       annotation='This run\'s manuscript could not be read, so no finding in '
                                  'it could be put in a piece of the paper: '
                                  + base_refusal(run))
    if slug in KEYS:
        return score_paper(run, slug, reading, base)
    row = (score_control(run, slug, reading, base) if slug in CONTROLS
           else unkeyed(run, slug, reading, base))
    row['citations'] = citations_of(reading)
    row['marker_shapes'] = marker_shapes(base, reading)
    row['citation_causes'] = citation_causes(run)
    row['all_findings'] = [brief(finding, base) for finding in reading['findings']]
    row['finding_pieces'] = piece_counts(row['all_findings'])
    return row


def unkeyed(run: Path, slug: str, reading: dict, base=None, kind: str = 'no key',
            annotation: str = '') -> dict:
    """A run with no candidates in it: everything measurable, and why.

    Two runs end here: one of a paper SPOT has no key for, and one whose
    manuscript could not be read. Both are real rows — the findings and the cost
    are the arm's answer for that paper — and both say in ``kind`` and
    ``annotation`` which they are, so a table never shows an empty candidate
    count as a run that found nothing.
    """
    findings = reading['findings']
    return {
        'run': str(run), 'paper': slug, 'kind': kind,
        'annotation': annotation or 'No SPOT key for this paper, so nothing here is '
                                    'a candidate.',
        'findings_total': len(findings),
        'issues_total': (reading.get('counts') or {}).get('issues'),
        'issues_required': (reading.get('counts') or {}).get('required'),
        'issues_suggestions': (reading.get('counts') or {}).get('suggestions'),
        'findings_by_slot': counts(findings, 'slot'),
        'findings_by_severity': counts(findings, 'severity'),
        'findings_major': sum(1 for finding in findings
                              if str(finding.get('severity') or '').lower() in SEVERE),
        'severe_allegations': sum(1 for finding in findings
                                  if ALLEGATION.search(words_of(finding))
                                  and str(finding.get('severity') or '').lower() in SEVERE),
        'quarantined': len(reading.get('quarantined') or []),
        'reader': reader_of(base),
        'manuscript_refusal': base_refusal(run) if base is None else '',
        **cost_of(reading),
    }


#: Words a run folder's name carries that are about the run and not about the
#: paper: a comparison makes one folder per arm — ``cmp-antenna-on`` and
#: ``cmp-antenna-off`` — and the arm is read from the manuscript's own
#: ``ai_read`` rather than from the name, so it is stripped here.
ARM_WORDS = ('cmp-', 'reader-trial-', 'reader-rerun-', 'reader-rerun2-')

#: The short names a run folder is called by, where the name is not part of the
#: slug at all. ``antenna`` is the beamforming paper, named for what it is about
#: rather than for its SPOT row.
ALIASES = {'antenna': 'network-beamforming', 'antibiotic': 'medicine-oxytetracycline',
           'education': 'control-education-pmc7183109',
           'microbiology': 'control-microbiology-pmc8208984'}


def slug_of(run: Path) -> str:
    """The paper a run folder is about, from its own name, as a fallback.

    ``--slug`` is how a run says which paper it is about, because a folder name
    is whatever the person who made the run chose. This is for the runs named
    after the paper: the folder name can be the tail of the slug
    (``runs/beamforming`` for ``network-beamforming``), its head
    (``runs/control-education`` for ``control-education-pmc7183109``), the slug
    itself, or a short name in :data:`ALIASES`, with any arm word stripped
    first. An unmatched name is returned as it is and scored by
    :func:`unkeyed`, which says there is no key rather than guessing one.
    """
    name = Path(run).name
    for word in ARM_WORDS:
        if name.startswith(word):
            name = name[len(word):]
    for word in ('-on', '-off'):
        if name.endswith(word):
            name = name[:-len(word)]
    if name in ALIASES:
        return ALIASES[name]
    for slug in (*KEYS, *CONTROLS):
        if name in {slug} or name.startswith(slug) or slug.endswith(name) \
                or slug.startswith(name):
            return slug
    return name


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('run', nargs='?', help='the run folder to score')
    parser.add_argument('--root', default='', help='the pilot data directory, for --all')
    parser.add_argument('--slug', default='', help='which paper this run is about')
    parser.add_argument('--all', action='store_true', help='score every run under <root>/runs')
    parser.add_argument('--out', default='',
                        help='append the rows to this JSONL file, one row per run')
    args = parser.parse_args(argv)
    if args.all:
        if not args.root:
            print('--all needs --root')
            return 2
        runs = sorted(p for p in (Path(args.root).expanduser() / 'runs').iterdir() if p.is_dir())
    elif args.run:
        runs = [Path(args.run).expanduser()]
    else:
        print('name a run folder, or pass --all with --root')
        return 2
    rows = [score_one(run, args.slug or slug_of(run)) for run in runs]
    out = Path(args.out).expanduser() if args.out else (
        Path(args.root).expanduser() / 'runs' / 'results.jsonl' if args.root else None)
    if out:
        # Appended, because scoring one run and then another is how a comparison
        # is scored, and the second run must not take the first one's line with
        # it. The file is a log of scorings, so the last line for a run wins.
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('a', encoding='utf-8') as handle:
            handle.write(''.join(json.dumps(row) + '\n' for row in rows))
    for row in rows:
        pieces = row.get('finding_pieces') or {}
        print(f"{Path(row['run']).name:22} {row['paper']:30} "
              f"{'reader' if (row.get('reader') or {}).get('read') else 'converter':9} "
              f"findings {row.get('findings_total', 0):>3} "
              f"({row.get('findings_major', 0):>2} major) "
              f"candidates {len(row.get('candidates', row.get('allegations', []))):>3} "
              f"display {pieces.get('citing_display', 0):>3} "
              f"rebuilt {pieces.get('on_reader_rebuilt', 0):>3} "
              f"cut {pieces.get('on_reader_cut', 0):>3} "
              f"tokens {row.get('tokens', 0):>9,} {row.get('minutes', 0):>5} min "
              f"{'publishable' if row.get('publishable') else 'NOT publishable'}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
