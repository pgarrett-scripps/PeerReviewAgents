"""Plant exactly one known error in a copy of a paper, and write the answer key.

Stage A asks a measurable question: given a paper with one error in it, does a
review find that error? That only means anything if the right answer is exact
and private, so the error is planted by this script and the key is written
beside the altered copy, outside the repository.

**The planting layer.** The alteration is made in the PDF's own content
streams, as an equal-length substitution of the characters that are shown.
Everything else about the paper — its figures, its layout, its pagination, its
page images — is the paper the authors published, and the only difference is
the error. That matters twice over: a regenerated PDF would quietly remove the
figures the `figures-tables` specialist is measured on, and a review of a
re-rendered paper would not be a review of a paper. The altered copy then goes
through the ordinary `peerreview review` path like any other manuscript.

**What is planted.** One error per copy, of four kinds:

* `statistic` — a reported percentage that no longer matches the count and
  denominator printed beside it, or a p-value that contradicts the
  significance claimed in its own sentence.
* `table-cell` — two numeric cells of one table column, swapped.
* `sample-size` — one of the places that state the sample size, changed, so it
  disagrees with the other place that still states the original.
* `fake-doi` — a DOI in the references, altered so it resolves to nothing.

**How a target is chosen.** Candidates are found in the *imported* paper, so a
target is a block, a table cell or a reference rather than a byte offset, and
the answer key can name a location the scorer can match against a finding.
The edit itself is then located in the page's content stream by its
surrounding text, and refused when two places match it equally well. Finally
the altered PDF is imported again and the key records the planted text as the
importer sees it: a paper whose planted error does not survive its own import
is dropped rather than scored.

Nothing here makes a model call, and nothing here is specific to one paper.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path

KINDS = ('statistic', 'table-cell', 'sample-size', 'fake-doi')

# Characters of surrounding text kept with an edit, for finding the one place
# in the content stream it belongs to.
CONTEXT = 70
# A target is planted only when the best-matching place beats the runner-up by
# this much of the context. Two equally good matches mean the key could not say
# which occurrence was altered.
MARGIN = 0.12

COUNTED = ('participants', 'patients', 'subjects', 'respondents', 'students', 'mice', 'rats',
           'animals', 'samples', 'cells', 'observations', 'individuals', 'isolates', 'images')

PERCENT = re.compile(r'(?<![\d.])(\d{1,5})\s*(?:/|of|out of)\s*(\d{1,5})\s*\(\s*'
                     r'(\d{1,3}(?:\.\d)?)\s*%\s*\)')
COUNT_PERCENT = re.compile(r'(?<![\d.])(\d{1,6})\s*\(\s*(\d{1,3}(?:\.\d{1,2})?)\s*%\s*\)')
PVALUE = re.compile(r'\b([Pp])\s*([=<])\s*(0?\.\d{2,4})\b')
SAMPLE = re.compile(r'\bn\s*=\s*(\d{2,6})\b', re.I)
COUNTED_BY_NOUN = re.compile(r'\b(\d{2,6})\s+(?:' + '|'.join(COUNTED) + r')\b', re.I)
DOI = re.compile(r'\b10\.\d{4,9}/[^\s,;)\]}>"]+')
# A cell worth swapping: it starts with a digit and holds nothing but a number
# and its usual decoration, so swapping two of them swaps two measurements.
NUMERIC_CELL = re.compile(r'^-?\d[\d.,]*(?:\s*\(\s*-?\d[\d.,]*\s*%?\s*\))?%?$')
SIGNIFICANT = re.compile(r'\bsignifican', re.I)
NOT_SIGNIFICANT = re.compile(r'\b(?:not|no|non-?)\s*significan', re.I)
# A sentence that states a total and its parts, which is where a broken sum is
# an error of reporting rather than a coincidence of numbers.
TOTALLING = re.compile(r'\bof (?:the|these|whom|which)\b|\bin total\b|\btotal of\b|\boverall\b'
                       r'|\bcomprising\b|\bconsisting\b|\bdivided\b|\bincluded\b|\bthere were\b'
                       r'|\bwere (?:randomi[sz]ed|assigned|allocated|split)\b', re.I)
# A number counting something: not a year, not a range, not a percentage, and
# followed by the thing it counts.
PLAIN_NUMBER = re.compile(r'(?<![\d.,\-–])(\d{2,6})(?![\d.,%]|\s*[–-]\s*\d)')
FOLLOWED_BY_NOUN = re.compile(r'^[\s,;)]*(?:[a-z]+\s+){0,2}[a-z]{3,}')
CITING = re.compile(r'\[|\bet al\b|\bdoi\b|\bFig\b|\bTable\b|\bref\b', re.I)


def squash(text: str) -> str:
    """Letters and digits only, lowercased: how two renderings of a passage are compared.

    A PDF's content stream and the importer's text differ in spacing,
    hyphenation and ligatures for the same words, so every comparison here is
    made on this form and never on the raw strings.
    """
    return re.sub(r'[^0-9a-z]', '', unicodedata.normalize('NFKC', text).lower())


def tidy(text: str) -> str:
    """One line of whitespace-normalised text, for a quote in the answer key."""
    return ' '.join(str(text).split())


def core(value: str) -> str:
    """The leading number of a table cell, without any bracketed percentage.

    Cells are often written `3,457,173 (43.12)`. The number that was moved is
    the count; demanding the bracket as well would miss a finding that names
    the count on its own.
    """
    match = re.match(r'-?\d[\d.,]*', str(value).strip())
    return match.group(0).rstrip('.,') if match else tidy(value)


# ---------------------------------------------------------------------------
# Choosing what to plant. These functions read the imported paper only, so
# they are testable without a PDF and without pikepdf.
# ---------------------------------------------------------------------------


def context_of(text: str, start: int, end: int) -> str:
    """The text around a match, which is what locates it in the content stream."""
    return text[max(0, start - CONTEXT):min(len(text), end + CONTEXT)]


def wrong_percentage(correct: float, printed: str) -> str:
    """A percentage that is wrong by at least 8 points and written like the right one.

    Same number of characters, so the substitution leaves the page's layout and
    the PDF's own string lengths untouched.
    """
    decimals = len(printed.split('.')[1]) if '.' in printed else 0
    for shift in (23, 31, 17, 41, 11, 53, 9, 8):
        for direction in (1, -1):
            value = correct + direction * shift
            if not 0.0 <= value <= 99.9:
                continue
            candidate = f'{value:.{decimals}f}'
            if len(candidate) == len(printed) and abs(value - correct) >= 8:
                return candidate
    return ''


def sentences(text: str):
    """The text's sentences, with where each one starts."""
    position = 0
    for piece in re.split(r'(?<=[.;:])\s+', text):
        yield piece, position
        position += len(piece) + 1


def wrong_number(value: str, avoid=()) -> str:
    """A number of the same digit count that is wrong, and not already in use."""
    for shift in (18, 27, 36, 9, 45, 54, 63, 72):
        for direction in (1, -1):
            candidate = str(int(value) + direction * shift)
            if len(candidate) == len(value) and candidate not in avoid:
                return candidate
    return ''


def counts_something(sentence: str, match) -> bool:
    """Is this number counting something, rather than a year or a citation?

    The arithmetic rule only means anything on numbers that are quantities in
    the sentence's own terms. A year, a reference marker and a page range all
    add up by coincidence often enough to swamp the real cases.
    """
    value = int(match.group(1))
    if 1900 <= value <= 2100:
        return False
    return bool(FOLLOWED_BY_NOUN.match(sentence[match.end():match.end() + 40]))


def plan_percentage_of_its_own_count(block: dict) -> list:
    """`24 of 48 (50%)`, with the percentage made wrong: every input is in the sentence."""
    text = block.get('text') or ''
    found = []
    for match in PERCENT.finditer(text):
        count, total, printed = match.group(1), match.group(2), match.group(3)
        if int(total) == 0 or int(count) > int(total):
            continue
        correct = 100.0 * int(count) / int(total)
        if abs(correct - float(printed)) > 0.6:
            continue  # Already inconsistent in the published paper: not ours to claim.
        altered = wrong_percentage(correct, printed)
        if not altered:
            continue
        start, end = match.start(3), match.end(3)
        found.append({
            'kind': 'statistic', 'rule': 'percentage-against-its-own-count',
            'block_id': block['id'], 'page': block.get('page'),
            'edits': [{'original': printed, 'altered': altered,
                       'context': context_of(text, start, end),
                       'offset_in_context': min(start, CONTEXT)}],
            'reason': (f'{count} of {total} is {correct:.1f}%, and the paper now prints '
                       f'{altered}% in the same sentence.'),
            'check': {'count': int(count), 'total': int(total),
                      'correct_percentage': round(correct, 2),
                      'printed_percentage': float(altered)},
            'must_mention': [altered, count],
            'sentence': tidy(context_of(text, start, end)),
        })
    return found


def plan_parts_of_a_total(block: dict) -> list:
    """`Of the 3063 participants, 1327 were men and 1736 women`, with a part made wrong.

    The parts stop adding up to the total the same sentence states, which is
    arithmetic a reviewer can do with `calculate` on the sentence alone. The
    gates are strict on purpose: a sentence has to be stating a total and its
    parts, every number has to be counting something, and the sentence has to
    offer exactly one sum, or the key could not say which number was altered.
    """
    if block.get('kind') != 'paragraph':
        return []
    text = block.get('text') or ''
    found = []
    for sentence, offset in sentences(text):
        if not TOTALLING.search(sentence) or CITING.search(sentence):
            continue
        numbers = [match for match in PLAIN_NUMBER.finditer(sentence)
                   if counts_something(sentence, match)]
        if not 3 <= len(numbers) <= 8:
            continue
        values = [int(match.group(1)) for match in numbers]
        sums = [(one, other, whole)
                for one, other, whole in itertools.permutations(range(len(values)), 3)
                if one < other and values[one] >= 10 and values[other] >= 10
                and values[whole] >= 25 and values[one] + values[other] == values[whole]]
        if len(sums) != 1:
            continue
        one, other, whole = sums[0]
        part = numbers[one] if values[one] >= values[other] else numbers[other]
        printed, total = part.group(1), numbers[whole].group(1)
        if sentence.count(printed) != 1 or text.count(printed) != 1:
            continue
        altered = wrong_number(printed, {match.group(1) for match in numbers})
        if not altered:
            continue
        start = offset + part.start(1)
        found.append({
            'kind': 'statistic', 'rule': 'parts-against-their-stated-total',
            'block_id': block['id'], 'page': block.get('page'),
            'edits': [{'original': printed, 'altered': altered,
                       'context': context_of(text, start, start + len(printed)),
                       'offset_in_context': min(start, CONTEXT)}],
            'reason': (f'The parts now add to {int(altered) + values[whole] - int(printed)} '
                       f'where the same sentence states a total of {total}.'),
            'check': {'stated_total': int(total), 'original_part': int(printed),
                      'printed_part': int(altered),
                      'parts': [values[one], values[other]]},
            'must_mention': [altered, total],
            'sentence': tidy(sentence),
        })
    return found


def plan_percentage_of_a_stated_n(block: dict) -> list:
    """`2808 (57.9%)` with the group's `n = 4850` in the same block, percentage made wrong."""
    text = block.get('text') or ''
    denominators = sorted({int(match.group(1)) for match in SAMPLE.finditer(text)})
    if not denominators:
        return []
    found = []
    for match in COUNT_PERCENT.finditer(text):
        count, printed = int(match.group(1)), match.group(2)
        totals = [total for total in denominators
                  if total and count <= total
                  and abs(100.0 * count / total - float(printed)) < 0.6]
        if len(totals) != 1:
            continue
        correct = 100.0 * count / totals[0]
        altered = wrong_percentage(correct, printed)
        if not altered:
            continue
        start, end = match.start(2), match.end(2)
        found.append({
            'kind': 'statistic', 'rule': 'percentage-against-the-stated-n',
            'block_id': block['id'], 'page': block.get('page'),
            'edits': [{'original': printed, 'altered': altered,
                       'context': context_of(text, start, end),
                       'offset_in_context': min(start, CONTEXT)}],
            'reason': (f'{count} of n = {totals[0]} is {correct:.1f}%, and the paper now '
                       f'prints {altered}%.'),
            'check': {'count': count, 'total': totals[0],
                      'correct_percentage': round(correct, 2),
                      'printed_percentage': float(altered)},
            'must_mention': [altered, str(count)],
            'sentence': tidy(context_of(text, start, end)),
        })
    return found


def plan_p_value(block: dict) -> list:
    """A p-value raised above 0.05 in a sentence that still calls the result significant."""
    text = block.get('text') or ''
    found = []
    for match in PVALUE.finditer(text):
        printed = match.group(3)
        around = text[max(0, match.start() - 160):match.end() + 160]
        if not SIGNIFICANT.search(around) or NOT_SIGNIFICANT.search(around):
            continue
        if float(printed) >= 0.05:
            continue
        digits = printed.split('.')[1]
        altered_digits = ('7' if digits[0] != '7' else '6') + digits[1:]
        altered = '0.' + altered_digits if printed.startswith('0') else '.' + altered_digits
        if len(altered) != len(printed) or float(altered) < 0.05:
            continue
        start, end = match.start(3), match.end(3)
        found.append({
            'kind': 'statistic', 'rule': 'p-value-against-its-own-claim',
            'block_id': block['id'], 'page': block.get('page'),
            'edits': [{'original': printed, 'altered': altered,
                       'context': context_of(text, start, end),
                       'offset_in_context': min(start, CONTEXT)}],
            'reason': (f'The sentence calls the result significant and now reports '
                       f'{match.group(1)} {match.group(2)} {altered}.'),
            'check': {'printed_p': float(altered), 'original_p': float(printed),
                      'threshold': 0.05},
            # The printed value alone. A p-value squashes to two or three
            # digits, which another number could contain by accident, but the
            # scorer only counts a mention inside the altered block, and
            # demanding the 0.05 threshold as well would reject a reviewer who
            # simply writes that .72 is not significant.
            'must_mention': [altered],
            'sentence': tidy(context_of(text, start, end)),
        })
    return found


# Best first: the rules whose inputs are all in one sentence, then the one
# whose denominator is elsewhere in the block, then the one that rests on a
# claim rather than arithmetic.
STATISTIC_RULES = ('percentage-against-its-own-count', 'parts-against-their-stated-total',
                   'percentage-against-the-stated-n', 'p-value-against-its-own-claim')


def plan_statistic(blocks: list) -> list:
    """A printed number that stops following from the numbers printed with it."""
    found = []
    for block in blocks:
        if block.get('kind') in ('heading', 'reference', 'title'):
            continue
        for planner in (plan_percentage_of_its_own_count, plan_parts_of_a_total,
                        plan_percentage_of_a_stated_n, plan_p_value):
            found.extend(planner(block))
    return sorted(found, key=lambda plan: (STATISTIC_RULES.index(plan['rule']),
                                           -len(plan['edits'][0]['original'])))


def plan_table_cell(blocks: list) -> list:
    """Two numeric cells of one table column, swapped.

    Same column and different rows, so the swap is the kind of transcription
    slip a table review is for: the numbers are all still present and only two
    of them are in the wrong rows.

    A swap has to leave something checkable behind, or it is not an error
    anybody could find: two p-values exchanged between rows contradict nothing
    in the paper. So a cell the prose also states is preferred, because the
    swap then disagrees with the text, and a count is preferred over a small
    decimal, because a column of counts has a total.
    """
    prose = ' '.join(block.get('text') or '' for block in blocks
                     if block.get('kind') in ('paragraph', 'caption', 'list_item'))
    found = []
    for block in blocks:
        rows = block.get('table') or []
        if len(rows) < 3:
            continue
        text = block.get('text') or ''
        columns = {}
        for row_index, row in enumerate(rows):
            for column_index, cell in enumerate(row):
                value = tidy(cell)
                if NUMERIC_CELL.match(value) and len(value) >= 3:
                    columns.setdefault(column_index, []).append((row_index, value))
        for column_index, cells in sorted(columns.items()):
            for first in range(len(cells)):
                for second in range(first + 1, len(cells)):
                    one, other = cells[first], cells[second]
                    if one[1] == other[1] or len(one[1]) != len(other[1]):
                        continue
                    if text.count(one[1]) != 1 or text.count(other[1]) != 1:
                        continue  # The key has to name one cell, not a column of equals.
                    starts = (text.index(one[1]), text.index(other[1]))
                    found.append({
                        'kind': 'table-cell', 'rule': 'two-cells-of-one-column-swapped',
                        'block_id': block['id'], 'page': block.get('page'),
                        'edits': [
                            {'original': one[1], 'altered': other[1],
                             'context': context_of(text, starts[0], starts[0] + len(one[1])),
                             'offset_in_context': min(starts[0], CONTEXT)},
                            {'original': other[1], 'altered': one[1],
                             'context': context_of(text, starts[1], starts[1] + len(other[1])),
                             'offset_in_context': min(starts[1], CONTEXT)},
                        ],
                        'reason': (f'Column {column_index + 1} of the table now reads '
                                   f'{other[1]} in row {one[0] + 1} and {one[1]} in row '
                                   f'{other[0] + 1}.'),
                        'check': {'column': column_index + 1, 'rows': [one[0] + 1, other[0] + 1],
                                  'values': [one[1], other[1]],
                                  'stated_in_prose': [value for value in (one[1], other[1])
                                                      if value in prose],
                                  'counts': all(len(value.split('.')[0].replace(',', '')) >= 3
                                                for value in (one[1], other[1]))},
                        # The leading number of each cell, not the whole cell:
                        # a reviewer who names the count without repeating the
                        # percentage in brackets is still naming the value.
                        'must_mention': [core(one[1]), core(other[1])],
                        'sentence': tidy(' | '.join(rows[one[0]]) + ' / '
                                         + ' | '.join(rows[other[0]])),
                    })
    return sorted(found, key=lambda plan: (
        # A cell the prose states too, so the swap contradicts the text.
        -len(plan['check']['stated_in_prose']),
        # Then counts, whose column has a total, over small decimals.
        not plan['check']['counts'],
        -len(plan['edits'][0]['original'])))


def sample_sizes(blocks: list) -> dict:
    """Every place a sample size is stated, as ``{value: [(block, span, text)]}``."""
    places = {}
    for block in blocks:
        if block.get('kind') in ('heading', 'reference'):
            continue
        text = block.get('text') or ''
        for pattern, group in ((SAMPLE, 1), (COUNTED_BY_NOUN, 1)):
            for match in pattern.finditer(text):
                value = match.group(group)
                places.setdefault(value, []).append((block, match.span(group), text))
    return places


def plan_sample_size(blocks: list) -> list:
    """One statement of the sample size changed, so it disagrees with another.

    The error needs two places to exist: the altered one and the one left
    alone. A number stated once can be wrong but cannot contradict anything,
    and a reviewer would have nothing to check it against.
    """
    places = sample_sizes(blocks)
    taken = set(places)
    found = []
    for value, occurrences in places.items():
        if len({block['id'] for block, _, _ in occurrences}) < 2:
            continue
        altered = next((candidate for candidate in
                        (str(int(value) + delta) for delta in (18, 27, 36, 9, 45, 54))
                        if len(candidate) == len(value) and candidate not in taken), '')
        if not altered:
            continue
        for block, (start, end), text in occurrences:
            if text.count(value) != 1:
                continue  # One occurrence per block, or the key cannot name which.
            others = [other['id'] for other, _, _ in occurrences if other['id'] != block['id']]
            found.append({
                'kind': 'sample-size', 'rule': 'one-statement-of-n-against-another',
                'block_id': block['id'], 'page': block.get('page'),
                'edits': [{'original': value, 'altered': altered,
                           'context': context_of(text, start, end),
                           'offset_in_context': min(start, CONTEXT)}],
                'reason': (f'This place now says {altered} where {", ".join(others)} still '
                           f'say {value}.'),
                'check': {'original_n': int(value), 'printed_n': int(altered),
                          'agreeing_blocks': others},
                'must_mention': [altered, value],
                'sentence': tidy(context_of(text, start, end)),
                'other_blocks': others,
            })
    return sorted(found, key=lambda plan: -len(plan['check']['agreeing_blocks']))


def dois_in(blocks: list, references: list) -> list:
    """Every DOI the paper prints once, as ``[(doi, block)]``, references first.

    The importer parses a reference list when it can find one, and plenty of
    papers give it nothing to parse, so the DOIs are taken from the text as
    well. A DOI printed twice is skipped: the key has to name one place.
    """
    reference_dois = {tidy(reference.get('doi') or '') for reference in references}
    out, seen = [], set()
    for block in sorted(blocks, key=lambda block: block.get('kind') != 'reference'):
        text = block.get('text') or ''
        for match in DOI.finditer(text):
            doi = match.group(0).rstrip('.,;')
            if doi in seen:
                continue
            seen.add(doi)
            printed = sum((block.get('text') or '').count(doi) for block in blocks)
            if printed != 1:
                continue
            out.append((doi, block, doi in reference_dois))
    return out


def plan_fake_doi(blocks: list, references: list) -> list:
    """A DOI in the references, altered so that it resolves to nothing.

    The registrant prefix is left alone and the suffix's digits are changed, so
    the reference still looks like a DOI of its own publisher and the check is
    a lookup rather than a format test.
    """
    found = []
    for doi, block, in_references in dois_in(blocks, references):
        holder, text = block['id'], block.get('text') or ''
        prefix, _, suffix = doi.partition('/')
        digits = [index for index, character in enumerate(suffix) if character.isdigit()]
        if len(digits) < 3:
            continue
        altered_suffix = list(suffix)
        for index in digits[-3:]:
            altered_suffix[index] = str((int(suffix[index]) + 4) % 10)
        altered = prefix + '/' + ''.join(altered_suffix)
        if altered == doi:
            continue
        start = text.index(doi)
        found.append({
            'kind': 'fake-doi', 'rule': 'doi-digits-changed-in-one-reference',
            'block_id': holder, 'page': block.get('page'),
            'edits': [{'original': doi, 'altered': altered,
                       'context': context_of(text, start, start + len(doi)),
                       'offset_in_context': min(start, CONTEXT)}],
            'reason': f'The reference now cites {altered}, which is not a registered DOI.',
            'check': {'original_doi': doi, 'printed_doi': altered,
                      'in_parsed_reference_list': in_references,
                      'block_kind': block.get('kind')},
            # Either suffix shows the reviewer resolved this DOI: the printed
            # one, which is the value that does not exist, or the real one,
            # which is nowhere in the altered paper and can only come from a
            # lookup. The pilot's citations specialist reported exactly the
            # second shape — "the DOI printed for ref 6 returns 404 … the
            # preprint with this title is 10.1101/2020.04.23.20076042 (the last
            # digits differ)" — and demanding the fake digits scored that as a
            # miss.
            'must_mention': [altered.split('/', 1)[1], doi.split('/', 1)[1]],
            'mention_rule': 'any',
            'sentence': tidy(text[max(0, start - 300):start + 200]),
        })
    # A parsed reference first, then the longest DOI, which is the one least
    # likely to be a prefix of another.
    return sorted(found, key=lambda plan: (not plan['check']['in_parsed_reference_list'],
                                           -len(plan['edits'][0]['original'])))


def plans_for(paper: dict, kind: str) -> list:
    """Every plantable error of one kind in an imported paper, best first."""
    blocks = paper.get('blocks') or []
    if kind == 'statistic':
        return plan_statistic(blocks)
    if kind == 'table-cell':
        return plan_table_cell(blocks)
    if kind == 'sample-size':
        return plan_sample_size(blocks)
    if kind == 'fake-doi':
        return plan_fake_doi(blocks, paper.get('references') or [])
    raise ValueError('kind is one of ' + ', '.join(KINDS))


def plantable(paper: dict) -> dict:
    """Which kinds this paper can carry, as ``{kind: how many candidates}``."""
    return {kind: len(plans_for(paper, kind)) for kind in KINDS}


# ---------------------------------------------------------------------------
# The PDF layer. Text is shown by a content stream's string operands, so an
# edit is a substitution inside those strings, of the same length.
# ---------------------------------------------------------------------------


def flatten(strings: list) -> tuple:
    """The page's shown text, and the map from its offsets back to the strings.

    ``strings`` is ``[(place, text)]`` in drawing order. A target regularly
    spans several of them, because a line is often one `TJ` array of kerned
    fragments, so the match is made on the concatenation and written back
    through the map.
    """
    flat, index = [], []
    position = 0
    for place, text in strings:
        flat.append(text)
        index.append((position, position + len(text), place))
        position += len(text)
    return ''.join(flat), index


def locate(flat: str, needle: str, context: str, offset: int) -> int:
    """Where in the page's text this edit belongs, or ``-1`` if it is not clear.

    Every occurrence of the target is scored by how much of the surrounding
    text matches the context the plan carries, and the best one has to win by
    ``MARGIN``. A page that says the same thing twice is refused rather than
    guessed at.
    """
    wanted = squash(context)
    if not wanted:
        return -1
    scores = []
    start = flat.find(needle)
    while start != -1:
        window = flat[max(0, start - offset - CONTEXT):start + len(needle) + CONTEXT * 2]
        seen = squash(window)
        shared = sum(1 for size in range(6, min(len(wanted), 240), 6)
                     if wanted[size - 6:size] in seen)
        total = max(1, len(range(6, min(len(wanted), 240), 6)))
        scores.append((shared / total, start))
        start = flat.find(needle, start + 1)
    if not scores:
        return -1
    scores.sort(reverse=True)
    if len(scores) > 1 and scores[0][0] - scores[1][0] < MARGIN:
        return -1
    return scores[0][1] if scores[0][0] >= 0.5 else -1


def splice(strings: list, index: list, start: int, replacement: str) -> list:
    """The strings again, with ``replacement`` written over the span at ``start``.

    The replacement is the same length as what it replaces, so every later
    offset still means what it meant and the page's spacing does not move.
    """
    end = start + len(replacement)
    out = []
    for (place, text), (begin, finish, _) in zip(strings, index):
        if finish <= start or begin >= end:
            out.append((place, text))
            continue
        local = list(text)
        for position in range(max(begin, start), min(finish, end)):
            local[position - begin] = replacement[position - start]
        out.append((place, ''.join(local)))
    return out


def page_strings(page) -> tuple:
    """A page's text operands, as ``[(place, text)]`` plus the parsed instructions.

    A place is ``(instruction, element)`` — the element is the position inside a
    ``TJ`` array, or ``None`` for the single string of ``Tj``, ``'`` and ``"``.
    Only operands that are plain ASCII are offered: a string encoded through a
    font's own CMap cannot be edited character by character, and a paper whose
    numbers are all drawn that way is dropped instead of corrupted.
    """
    import pikepdf

    instructions = list(pikepdf.parse_content_stream(page, ''))
    strings = []
    for number, instruction in enumerate(instructions):
        operator = str(instruction.operator)
        if operator in ('Tj', "'", '"'):
            operands = [(len(instruction.operands) - 1, instruction.operands[-1])]
        elif operator == 'TJ':
            array = instruction.operands[0]
            operands = [(position, item) for position, item in enumerate(array)
                        if isinstance(item, pikepdf.String)]
        else:
            continue
        for position, operand in operands:
            if not isinstance(operand, pikepdf.String):
                continue
            raw = bytes(operand)
            if not raw or any(byte < 32 or byte > 126 for byte in raw):
                continue
            strings.append(((number, position), raw.decode('ascii')))
    return strings, instructions


def write_strings(pdf, page, instructions: list, strings: list) -> None:
    """Put the edited strings back into the page's content stream."""
    import pikepdf

    edited = dict(strings)
    for (number, position), text in edited.items():
        instruction = instructions[number]
        operator = str(instruction.operator)
        if operator == 'TJ':
            instruction.operands[0][position] = pikepdf.String(text)
        else:
            instruction.operands[position] = pikepdf.String(text)
    page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(instructions))


def apply_plan(source: Path, target: Path, plan: dict) -> dict:
    """Write the altered PDF, and report where each edit landed.

    The plan's page is tried first and the rest of the paper after it, because
    the importer's page number comes from a text anchor and can be a page out.
    """
    import pikepdf

    with pikepdf.open(source) as pdf:
        order = list(range(len(pdf.pages)))
        if plan.get('page'):
            first = plan['page'] - 1
            order = [number for number in (first, first - 1, first + 1) if 0 <= number < len(order)]
            order += [number for number in range(len(pdf.pages)) if number not in order]
        landed = []
        for edit in plan['edits']:
            for number in order:
                page = pdf.pages[number]
                strings, instructions = page_strings(page)
                flat, index = flatten(strings)
                start = locate(flat, edit['original'], edit['context'],
                               edit.get('offset_in_context', 0))
                if start < 0:
                    continue
                write_strings(pdf, page, instructions,
                              splice(strings, index, start, edit['altered']))
                landed.append({**edit, 'pdf_page': number + 1})
                break
            else:
                raise LookupError('No single place on any page shows ' + repr(edit['original'])
                                  + ' in its context: nothing was altered.')
        pdf.save(target)
    return {**plan, 'edits': landed}


# ---------------------------------------------------------------------------
# The answer key, written only once the altered paper has been imported again.
# ---------------------------------------------------------------------------


def import_into(pdf: Path, workspace: Path) -> dict:
    """Import a paper the way a review does, and return the imported document."""
    from peerreviewagents.engine.ingest import import_paper

    if (workspace / 'paper.json').is_file():
        return json.loads((workspace / 'paper.json').read_text(encoding='utf-8'))
    return import_paper(str(pdf), str(workspace))


def where_altered(paper: dict, plan: dict) -> list:
    """The blocks of the altered paper that now show each planted string."""
    out = []
    for edit in plan['edits']:
        holders = [block for block in paper['blocks'] if edit['altered'] in (block.get('text') or '')]
        best = None
        if holders:
            wanted = squash(edit['context'])
            best = max(holders, key=lambda block: sum(
                1 for size in range(6, min(len(wanted), 240), 6)
                if wanted[size - 6:size] in squash(block.get('text') or '')))
        out.append({**edit, 'block_id_after': best['id'] if best else '',
                    'page_after': best.get('page') if best else None,
                    'quote_after': tidy((best.get('text') or '')[:600]) if best else ''})
    return out


def answer_key(slug: str, kind: str, original: Path, altered: Path, before: dict, after: dict,
               plan: dict) -> dict:
    """Everything the scorer needs, including the rule it is allowed to use."""
    from peerreviewagents.engine.storage import digest

    edits = where_altered(after, plan)
    blocks = sorted({edit['block_id_after'] for edit in edits if edit['block_id_after']}
                    | {plan['block_id']})
    return {
        'paper': slug,
        'kind': kind,
        'rule': plan['rule'],
        'reason': plan['reason'],
        'original_pdf': str(original),
        'altered_pdf': str(altered),
        'original_pdf_sha256': digest(original.read_bytes()),
        'altered_pdf_sha256': digest(altered.read_bytes()),
        'original_paper_id': before['id'],
        'altered_paper_id': after['id'],
        'title': after.get('title'),
        'block_id_before': plan['block_id'],
        'edits': edits,
        'check': plan['check'],
        'sentence': plan['sentence'],
        # The scoring rule, written down with the key rather than decided later:
        # a finding catches this error when it points at one of these blocks (or
        # quotes the planted text) and its words name the planted values.
        'detect': {
            'blocks': blocks,
            'quotes': [edit['quote_after'] for edit in edits if edit['quote_after']],
            'must_mention': list(dict.fromkeys(plan['must_mention'])),
            # Every planted value by default, not any of them: the numeric
            # rules carry the altered number together with the number it now
            # contradicts, and a finding that names only one of the two has not
            # shown the error. A rule says `any` when its values are two ways
            # of showing the same single error, as a fake DOI's printed suffix
            # and real suffix are.
            'mention_rule': plan.get('mention_rule', 'all'),
        },
    }


def plant(pdf: Path, slug: str, kind: str, out_dir: Path, keys_dir: Path,
          imports_dir: Path, candidate: int = 0) -> dict:
    """Plant one error of one kind in a copy of ``pdf`` and write its answer key."""
    before = import_into(pdf, imports_dir / slug)
    candidates = plans_for(before, kind)
    if not candidates:
        raise LookupError(f'{slug} carries nothing of kind {kind}')
    plan = candidates[candidate]
    out_dir.mkdir(parents=True, exist_ok=True)
    keys_dir.mkdir(parents=True, exist_ok=True)
    altered = out_dir / f'{slug}--{kind}.pdf'
    landed = apply_plan(pdf, altered, plan)
    workspace = Path(tempfile.mkdtemp(prefix='stage-a-verify-'))
    try:
        after = import_into(altered, workspace / 'manuscript')
        key = answer_key(slug, kind, pdf, altered, before, after, landed)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    missing = [edit for edit in key['edits'] if not edit['block_id_after']]
    if missing:
        altered.unlink(missing_ok=True)
        raise LookupError(f'{slug}: the planted {kind} does not survive its own import')
    (keys_dir / f'{slug}--{kind}.json').write_text(json.dumps(key, indent=2) + '\n',
                                                   encoding='utf-8')
    return key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='plant.py',
        description='Plant one known error in a copy of a paper and write the answer key.')
    parser.add_argument('pdf', help='the original PDF')
    parser.add_argument('--kind', choices=KINDS, help='which error to plant')
    parser.add_argument('--slug', default='', help='the name used for the copy and the key')
    parser.add_argument('--root', default='/home/ty/pra-stage-a',
                        help='where the altered copies, keys and imports live')
    parser.add_argument('--candidate', type=int, default=0,
                        help='which candidate of that kind to use (default: the best)')
    parser.add_argument('--survey', action='store_true',
                        help='report what this paper can carry and plant nothing')
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    pdf = Path(args.pdf).resolve()
    slug = args.slug or pdf.stem
    root = Path(args.root)
    if args.survey:
        paper = import_into(pdf, root / 'imports' / slug)
        print(json.dumps({'paper': slug, 'title': paper.get('title'),
                          'pages': paper['statistics']['pdf_pages'],
                          'plantable': plantable(paper)}, indent=2))
        return 0
    if not args.kind:
        raise SystemExit('--kind is required unless --survey is given')
    key = plant(pdf, slug, args.kind, root / 'altered', root / 'keys', root / 'imports',
                args.candidate)
    print(json.dumps(key, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
