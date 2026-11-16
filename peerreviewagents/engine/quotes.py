"""Where a quote is in the manuscript, or why it is not a quote of it.

A finding gives the words it is about, and the engine works out where they are:
the document, the line, and the offsets within the line. The reviewer is not
asked to count lines, because a model asked for a number alongside its quote
will supply a number whether or not it knows it, and a wrong one points a
rebuttal at the wrong sentence. A line is an argument only where it has to be —
to separate two places the same words appear, or to cite an equation, a table
row or a figure by line.

Matching is over the normal form of the whole document (:mod:`paper_text`), so
a retyped hyphen, a straightened quotation mark, a ligature or a line break
inside the passage does not decide whether a true quote can be cited. Case is
allowed to differ as well — a reviewer lowering a sentence's first letter to
splice it into its own has misquoted nobody — and such a placement is marked
inexact rather than refused. Nothing is fuzzy beyond that: the words themselves
must be the manuscript's words, in its order. A quote that is not in the paper
is refused with the lines that come closest, which is the difference between
"you misquoted" and "no".
"""
from __future__ import annotations

import bisect

from . import paper_base, paper_text

#: How many near misses a refusal names. Enough to show the reviewer the
#: sentence it was reaching for, few enough that the refusal stays readable.
NEAREST = 3

#: How much of a near-miss line a refusal prints.
NEAREST_WIDTH = 200


def longest_run(want, have):
    """``(length, start)``: the longest run of ``want`` that occurs in ``have``.

    Words, not characters. A near miss is a reviewer who has the sentence and
    typed it loosely, and a shared run of words finds that sentence where a
    character measure finds whatever line happens to share its letters.
    """
    best = (0, 0)
    previous = [0] * (len(have) + 1)
    for index, word in enumerate(want, 1):
        current = [0] * (len(have) + 1)
        for other_index, other in enumerate(have, 1):
            if word == other:
                current[other_index] = previous[other_index - 1] + 1
                if current[other_index] > best[0]:
                    best = (current[other_index], index - current[other_index])
        previous = current
    return best


def nearest_lines(base, quote, document=paper_base.MAIN_DOCUMENT, limit=NEAREST):
    """The lines that share the longest runs of words with a quote that is absent."""
    want = paper_text.words(quote)
    if not want:
        return []
    scored = []
    for number, line in enumerate(base.lines.get(document, []), 1):
        if not line.strip():
            continue
        length, start = longest_run(want, paper_text.words(line))
        if length:
            scored.append((length, -number, number, ' '.join(want[start:start + length])))
    scored.sort(reverse=True)
    return [{'line': number, 'shared_words': length, 'shared': shared,
             'text': line_text[:NEAREST_WIDTH]}
            for length, _, number, shared in scored[:limit]
            for line_text in [base.line_text(number, document)]]


def _line_of(starts, offset):
    """The 1-based line holding a file offset."""
    return bisect.bisect_right(starts, offset)


def _found_at(base, document, spans, exactly):
    """Turn normal-form spans into the placements a finding is recorded as."""
    _, _, starts = base.projection(document)
    found = []
    for first_index, last_index in spans:
        start, end = _span(base, document, first_index, last_index - first_index + 1)
        start_line = _line_of(starts, start)
        end_line = _line_of(starts, end - 1)
        piece = base.piece_at(start_line, document) or {}
        found.append({
            'document': document, 'line': start_line, 'end_line': end_line,
            'start': start - starts[start_line - 1],
            'end': end - starts[end_line - 1],
            'text': base.documents[document][start:end], 'quoted_exactly': exactly,
            'page': piece.get('page'), 'bbox': piece.get('bbox'),
            'section': piece.get('section'), 'kind': piece.get('kind'),
            'lines': piece.get('lines'), 'fingerprint': piece.get('fingerprint'),
        })
    return found


def _folded_spans(base, document, quote, low, high):
    """The normal-form spans of a quote that matches except in its case."""
    folded, origins = base.folded(document)
    needle = ''.join(character.casefold() or character
                     for character in paper_text.normalised(quote))
    if not needle:
        return []
    low = bisect.bisect_left(origins, low)
    high = bisect.bisect_left(origins, high)
    spans = []
    at = folded.find(needle, low, high)
    while at != -1:
        spans.append((origins[at], origins[at + len(needle) - 1]))
        at = folded.find(needle, at + 1, high)
    return spans


def _span(base, document, start_index, length):
    """The file offsets a match of ``length`` characters covers, from its normal form."""
    _, offsets, _ = base.projection(document)
    text = base.documents[document]
    first = offsets[start_index]
    last = offsets[start_index + length - 1]
    end = last + 1
    # A combining mark belongs to the character it marks: the normal form may
    # hold one character where the file holds two, and a highlight that stops
    # between them would cut an accent off its letter. A soft hyphen or
    # zero-width character sitting at the end of the match is part of the word
    # the quote ended on for the same reason.
    while end < len(text) and (paper_text.is_mark(text[end]) or text[end] in paper_text.DROPPED):
        end += 1
    return first, end


def placements(base, quote, line=None, end_line=None, document=paper_base.MAIN_DOCUMENT):
    """Every place a quote occurs, as the engine will record it.

    With ``line`` — and ``end_line`` for a range — only matches that lie inside
    those lines are returned, which is how a quote that occurs in three places
    is cited in one of them.
    """
    normal, offsets, starts = base.projection(document)
    needle = paper_text.normalised(quote)
    if not needle:
        return []
    low, high = 0, len(normal)
    if line is not None:
        first, last = int(line), int(end_line if end_line is not None else line)
        if first < 1 or last < first or last > len(base.lines.get(document, [])):
            return []
        raw_from = starts[first - 1]
        raw_to = (starts[last] - 1) if last < len(starts) else len(base.documents[document])
        low = bisect.bisect_left(offsets, raw_from)
        high = bisect.bisect_right(offsets, raw_to)
    spans = []
    at = normal.find(needle, low, high)
    while at != -1:
        spans.append((at, at + len(needle) - 1))
        at = normal.find(needle, at + 1, high)
    if spans:
        return _found_at(base, document, spans, True)
    return _found_at(base, document, _folded_spans(base, document, quote, low, high), False)


def anchor(base, quote, line=None, end_line=None, document=None):
    """``{'placement': …}`` for one position in the manuscript, or a refusal.

    A refusal says which of the three things went wrong — the document is not
    in this manuscript, the words are not in it, or they are in it more than
    once — and carries what the reviewer needs to fix it: the nearest lines, or
    the lines the quote does occur on. It never guesses between two positions,
    because a citation is the one thing in a review that has to be checkable.
    """
    document = document or paper_base.MAIN_DOCUMENT
    if document not in base.documents:
        return {'refused': 'This manuscript has no document named ' + str(document)
                           + '. It holds: ' + ', '.join(base.document_names()) + '.',
                'documents': base.document_names()}
    if not paper_text.normalised(quote):
        return {'refused': 'A finding has to quote the manuscript. The quote was empty.'}
    found = placements(base, quote, line, end_line, document)
    if len(found) == 1:
        return {'placement': found[0]}
    if not found:
        return _not_found(base, quote, line, end_line, document)
    lines = [place['line'] for place in found]
    where = ', '.join(str(number) for number in lines[:8])
    one_line = len(set(lines)) == 1
    return {'refused': 'That quote occurs ' + str(len(found)) + ' times in ' + document
                       + (' — on line ' if one_line else ' — on lines ') + where + '. '
                       + ('Quote more of the sentence so that it occurs once.' if one_line else
                          'Cite it with the line it is on, or quote more of the sentence so '
                          'that it occurs once.'),
            'lines': lines}


def _not_found(base, quote, line, end_line, document):
    """The refusal for a quote the search did not find, with somewhere to look."""
    if line is not None:
        elsewhere = placements(base, quote, document=document)
        if elsewhere:
            where = ', '.join(str(place['line']) for place in elsewhere[:8])
            return {'refused': 'Those words are in ' + document + ', but not on the lines given '
                               + '(' + str(line) + ('' if end_line in (None, line)
                                                    else '-' + str(end_line)) + '). They are on '
                               + ('line ' if len(elsewhere) == 1 else 'lines ') + where + '.',
                    'lines': [place['line'] for place in elsewhere]}
        scope = ' on the lines given'
    else:
        scope = ''
    near = nearest_lines(base, quote, document)
    message = ['That quote is not in ' + document + scope
               + '. Quote the manuscript exactly as it reads — the engine ignores how dashes, '
                 'quotation marks and spacing are typed, but not the words.']
    if near:
        message.append('Nearest:')
        for place in near:
            message.append('  line ' + str(place['line']) + ' shares '
                           + str(place['shared_words']) + ' words ("' + place['shared'] + '"): '
                           + place['text'])
    return {'refused': '\n'.join(message), 'nearest': near}


__all__ = ['NEAREST', 'anchor', 'longest_run', 'nearest_lines', 'placements']
