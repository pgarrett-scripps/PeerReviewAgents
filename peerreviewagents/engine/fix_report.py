"""``FIXES.md``: a readable page of what a new draft did with the problems raised.

Nothing here makes a model call. Everything on the page comes out of
``fix-check.json``, which the server wrote: one row per problem that was
answered, each with its verdict, the quotation of the *new* draft behind it and
the searches it rests on, plus the problems nobody answered.

The page is written for the author, so it is ordered the way an author reads it:
what is still open first, then what the draft did not settle, then what was
fixed, then what was not checked at all. A problem with no verdict is on the
page by name — a fix check that quietly dropped a problem would read as a draft
with fewer problems than it has.
"""
from pathlib import Path

from ..names import FIX_VERDICTS
from .run_report import render_markdown

#: The heading each verdict is printed under, and the order they are printed in:
#: the open problems first, because that is what a revision still has to do.
SECTIONS = (
    ('not-fixed', 'Still open',
     'The new draft still has these problems. Each one is printed with the passage of the new '
     'draft the check read, or with the searches behind it where the problem is something the '
     'draft still does not say.'),
    ('cannot-tell', 'The draft does not settle these',
     'These are neither fixed nor unfixed. The check could not answer them from the manuscript '
     'alone — the fix, if there was one, is in the data, the code or an experiment rather than in '
     'the words. Each one says what was looked for.'),
    ('fixed', 'Fixed',
     'The new draft answers these. Each one is printed with the passage of the new draft that '
     'answers it, or, where the fix was to delete the passage the problem was in, with the '
     'confirmation that those words are gone and the searches that looked for them.'),
)

NOTICE = ('This check was generated automatically. The quotations of the new draft were validated '
          'against it, and so was every claim that an old quotation is gone or still there. '
          'Whether a fix is scientifically adequate was not judged.')

BASIS = {'quote': 'A passage of the new draft, quoted and anchored by the server.',
         'old-quote-gone': 'The passage the problem was raised against is no longer in the draft, '
                           'which the server confirmed.',
         'old-quote-stands': 'The passage the problem was raised against is still in the draft '
                             'word for word, which the server confirmed.'}


def _where(evidence):
    """Where in the new draft one verified quotation sits, as one short phrase."""
    if not isinstance(evidence, dict):
        return ''
    parts = [str(evidence.get('document') or '')]
    line, end = evidence.get('line'), evidence.get('end_line')
    if line:
        parts.append('line ' + str(line) + ('–' + str(end) if end and end != line else ''))
    if evidence.get('section'):
        parts.append(str(evidence['section']))
    return ' · '.join(part for part in parts if part)


def _rows(document):
    """One table row per answered problem: the summary the author reads first."""
    rows = []
    for check in document.get('checks') or ():
        rows.append((check.get('problem_id'), check.get('severity') or '',
                     check.get('verdict'), _where(check.get('draft_evidence'))
                     or ('searches' if check.get('searches') else '')))
    for one in document.get('unchecked') or ():
        rows.append((one.get('id'), one.get('severity') or '', 'not checked', ''))
    return rows


def _check_blocks(check):
    """One answered problem in full: what was raised, what the draft says now."""
    blocks = [('heading3', str(check.get('problem_id')) + ': '
               + ' '.join(str(check.get('statement') or '').split()))]
    old = [one for one in (check.get('old_evidence') or ()) if one.get('quote')]
    for one in old:
        presence = one.get('in_new_draft') or {}
        state = ('still in the new draft' if presence.get('exact') else
                 'in the new draft, but not word for word' if presence.get('present') else
                 'not in the new draft')
        blocks += [('labeled', 'Raised against', str(one.get('document') or '')
                    + ' — ' + state),
                   ('quote', str(one['quote']))]
    if check.get('author_reply'):
        blocks.append(('labeled', 'The authors replied', str(check['author_reply'])))
    evidence = check.get('draft_evidence')
    if isinstance(evidence, dict) and (evidence.get('quote') or evidence.get('text')):
        blocks += [('labeled', 'The new draft', _where(evidence)),
                   ('quote', str(evidence.get('quote') or evidence.get('text')))]
    searched = [str(one) for one in (check.get('searches') or ()) if str(one).strip()]
    if searched:
        blocks.append(('labeled', 'Searched', '; '.join(searched)))
    if check.get('reason'):
        blocks.append(('labeled', 'Why it cannot be told from the draft', str(check['reason'])))
    if check.get('note'):
        blocks.append(('paragraph', str(check['note'])))
    basis = BASIS.get(str(check.get('basis') or ''))
    if basis:
        blocks.append(('labeled', 'Evidence', basis))
    return blocks


def _unchecked_blocks(document):
    """The problems with no verdict, by name, with the reason there is none."""
    rows = list(document.get('unchecked') or ())
    if not rows:
        return []
    over_cap = int(document.get('over_cap') or 0)
    cap = document.get('cap')
    reason = ('No verdict was recorded for these. ')
    if over_cap:
        reason += (str(over_cap) + ' of them were past this check\'s limit of ' + str(cap)
                   + ' problems and were never dealt to a checker; ')
    reason += ('the rest were dealt to a checker that did not answer them. They are neither '
               'fixed nor unfixed: nobody read the draft against them.')
    return [('heading2', 'Not checked'), ('paragraph', reason),
            ('table', ('Problem', 'Severity', 'What was raised'),
             [(one.get('id'), one.get('severity') or '',
               ' '.join(str(one.get('statement') or '').split())) for one in rows])]


def report_blocks(document):
    """Every block of ``FIXES.md``, from the fix check document."""
    counts = document.get('counts') or {}
    title = str(document.get('title') or document.get('paper_id') or 'the new draft')
    blocks = [('title', 'Fix check: ' + title), ('paragraph', NOTICE),
              ('fields', [('Problems given', document.get('problems')),
                          ('Checked', document.get('checked')),
                          *[(verdict.replace('-', ' ').capitalize(), counts.get(verdict, 0))
                            for verdict in FIX_VERDICTS],
                          ('Not checked', document.get('not_checked')),
                          ('Written', document.get('written'))])]
    rows = _rows(document)
    if rows:
        blocks.append(('table', ('Problem', 'Severity', 'Verdict', 'Where in the new draft'),
                       rows))
    checks = list(document.get('checks') or ())
    for verdict, heading, lead in SECTIONS:
        group = [row for row in checks if str(row.get('verdict')) == verdict]
        if not group:
            continue
        blocks += [('heading2', heading), ('paragraph', lead)]
        for check in group:
            blocks += _check_blocks(check)
    return blocks + _unchecked_blocks(document)


def write_fix_report(review, document):
    """Write ``FIXES.md`` beside ``fix-check.json``, and return its path as a string."""
    target = Path(review)
    target.mkdir(parents=True, exist_ok=True)
    path = target / 'FIXES.md'
    path.write_text(render_markdown(report_blocks(document)), encoding='utf-8')
    return str(path)
