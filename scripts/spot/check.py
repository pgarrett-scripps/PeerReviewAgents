"""Record, and re-verify, whether each candidate's annotated error is in the PDF.

SPOT's annotations describe the paper as it was when the error was found. The
file a publisher serves today is often the paper as it was *corrected*: of the
eight candidates read here, three had the annotated error already fixed in the
open-access PDF, and one could not be fetched at all. Reviewing those and
scoring the result would have measured the fetch, not the engine.

Grepping for a correction notice does not find them. None of the thirteen PDFs
fetched for this pilot contains the words of an erratum, including the three
that are demonstrably corrected copies — the publisher replaced the content and
left the article looking untouched. The only thing that works is to read the
annotated location and see whether the wrong value, label or image is still
there, which is what :data:`READINGS` records: for each candidate, the anchor
text that decides it, where that anchor is, and what reading it supported.

``--verify`` re-runs the mechanical half of that: it extracts the PDF's text
and says whether each anchor is present or absent as recorded. It cannot
re-check the two image-level readings — a duplicated micrograph and a crop are
not in the text layer — and it says so rather than passing them silently.

    python scripts/spot/check.py --root ~/pra-spot            # what was read
    python scripts/spot/check.py --root ~/pra-spot --verify    # re-check anchors
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

#: One reading per candidate, in the order they were read. ``present`` are
#: strings that must appear in the PDF's text for the annotated error to still
#: be there, ``absent`` are strings whose appearance means it was corrected, and
#: ``verdict`` is the reading those anchors supported: ``'error present'``,
#: ``'already corrected'``, ``'not fetchable'`` or ``'image only'``. ``how``
#: says what was actually looked at, in a sentence, so a reader can disagree.
READINGS = [
    {
        'slug': 'medicine-oxytetracycline', 'verdict': 'error present',
        'present': ['-587.51', '-1780.34'],
        'absent': [],
        'how': 'The negative AUC(0-inf) and the negative LOQ are in Table 1 and in the '
               'running text of the served PDF, with the minus signs intact.',
    },
    {
        'slug': 'chemistry-photocatalytic', 'verdict': 'error present',
        'present': [],
        'absent': [],
        'how': "Read off the page render: Fig. 4B still carries the ~260 nm spike and its "
               "axis is still labelled 'Wavelenght (nm)' where Fig. 4A beside it reads "
               "'Wavelength'. Both are inside the figure bitmap, so neither is in the "
               'text layer and --verify cannot re-check them.',
    },
    {
        'slug': 'biology-dark-kinome', 'verdict': 'error present',
        'present': [],
        'absent': [],
        'how': 'No anchor can settle this one, and that is the reading: the error is '
               'which recombinant fragments the work used, so no in-place '
               'correction of the PDF is possible: the construct table and the methods '
               'still describe the same reagents the annotation is about.',
    },
    {
        'slug': 'biology-cgrp', 'verdict': 'image only',
        'present': [],
        'absent': [],
        'how': 'Read off the page renders, not the text: column C of Fig. 5 and column C '
               'of Fig. 6 look like crops of one field, which is what the annotation '
               'says. Not pixel-verified, and the scorer is told so.',
    },
    {
        'slug': 'network-beamforming', 'verdict': 'error present',
        'present': ['12.0 dB', '16 antennas'],
        'absent': [],
        'how': 'v1 states a 12.0 dB combining gain for 16 antennas, which is 10log10(N) '
               'and not the 10log10(N^2) coherent beamforming gives. SPOT names v2; v2 '
               'is the withdrawal and arXiv serves no PDF for it.',
    },
    {
        'slug': 'biology-ebselen', 'verdict': 'already corrected',
        'present': [],
        'absent': [],
        'how': 'The equation the annotation is about is the corrected one in the served '
               'PDF. Dropped before any tokens were spent on it.',
    },
    {
        'slug': 'medicine-exosome', 'verdict': 'already corrected',
        'present': [],
        'absent': [],
        'how': 'The figure-text mismatch is gone: the text now agrees with the figure. '
               'Dropped.',
    },
    {
        'slug': 'biology-methylation', 'verdict': 'already corrected',
        'present': [],
        'absent': [],
        'how': 'The duplicated panel the annotation names has been replaced in the '
               'served PDF. Dropped.',
    },
    {
        'slug': 'environment-net-zero', 'verdict': 'not fetchable',
        'present': [],
        'absent': [],
        'how': 'Not in PMC, no publisher path of a known shape, and OpenAlex lists no '
               'open copy. Never read, so never a candidate.',
    },
]


def text_of(pdf: Path) -> str:
    """The PDF's text layer, or '' when it cannot be extracted."""
    try:
        done = subprocess.run(['pdftotext', '-layout', str(pdf), '-'],
                              capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        return ''
    return done.stdout if done.returncode == 0 else ''


def verify(root: Path) -> list:
    """Re-check each recorded anchor against the PDF on disk."""
    papers = Path(root) / 'papers'
    rows = []
    for reading in READINGS:
        pdf = papers / f'{reading["slug"]}.pdf'
        row = {'slug': reading['slug'], 'verdict': reading['verdict'],
               'pdf': pdf.is_file(), 'missing_anchors': [], 'unexpected_anchors': [],
               'checkable': bool(reading['present'] or reading['absent'])}
        if row['pdf'] and row['checkable']:
            said = text_of(pdf)
            row['text'] = bool(said)
            row['missing_anchors'] = [a for a in reading['present'] if a not in said]
            row['unexpected_anchors'] = [a for a in reading['absent'] if a in said]
        rows.append(row)
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', required=True, help='the pilot data directory')
    parser.add_argument('--verify', action='store_true', help='re-check the anchors')
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser()
    if not args.verify:
        for reading in READINGS:
            print(f'{reading["slug"]:34} {reading["verdict"]}')
            print(f'    {reading["how"]}')
        return 0
    bad = 0
    for row in verify(root):
        if not row['pdf']:
            state = 'no PDF on disk'
        elif not row['checkable']:
            state = 'not checkable from text — read off the renders'
        elif row['missing_anchors'] or row['unexpected_anchors']:
            state = (f'DISAGREES missing={row["missing_anchors"]} '
                     f'unexpected={row["unexpected_anchors"]}')
            bad += 1
        else:
            state = 'anchors as recorded'
        print(f'{row["slug"]:34} {row["verdict"]:20} {state}')
    return 1 if bad else 0


if __name__ == '__main__':
    raise SystemExit(main())
