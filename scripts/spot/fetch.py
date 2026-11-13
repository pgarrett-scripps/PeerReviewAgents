"""Fetch the SPOT pilot's papers: candidates with a confirmed error, and controls.

Stage A planted the errors it looked for. This pilot does not: every error here
is one the authors themselves acknowledged, recorded by the `SPOT benchmark
<https://arxiv.org/abs/2505.11855>`_ — 83 papers and 91 errors drawn from
retraction notices and from PubPeer threads an author answered. What SPOT
publishes is the annotation, under CC BY 4.0: the error's category, where in
the paper it is, and a sentence saying what is wrong. It publishes no PDFs, and
the papers stay under their own licences, so the pilot takes the annotations
from SPOT and each paper from its publisher — which is also what makes this a
test of the engine on real PDFs rather than on somebody's parse of one.

**The copy you can fetch today is often the corrected copy.** An erratum does
not always leave the article alone: for two of the first five candidates here
the open-access PDF already carried the fixed equation and the fixed instrument
name, so the annotated error was simply not in the file. Scoring those as
misses would have been scoring the fetch, not the engine. Every candidate
therefore has to be read at its annotated location before it is reviewed, and
``check.py`` is what records that reading. That is why this file holds a pool
of :data:`CANDIDATES` with ``chosen`` set on the ones that survived the check,
rather than a list of five picks.

The two controls are the Stage A papers that were reviewed with nothing planted
in them, copied from the Stage A data directory: they carry the pilot's only
measurement of what the engine alleges about a paper with no known error in it.

Nothing here sends anything about the machine it runs on beyond a generic
client name, and nothing here makes a model call.

    python scripts/spot/fetch.py --root ~/pra-spot            # fetch and verify
    python scripts/spot/fetch.py --root ~/pra-spot --audit    # say what is there
    python scripts/spot/fetch.py --root ~/pra-spot --all      # the whole pool
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'stage_a'))

from corpus import Unreachable, fetch_pdf, get, is_pdf, oa_pdf_url, pdf_url, read_json  # noqa: E402

#: NCBI's id converter, which gives the PMCID for a DOI. ``tool`` is the name
#: NCBI asks a client to send; it names this script and nothing about the
#: machine or the person running it.
IDCONV = 'https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/'
TOOL = 'pra-spot-pilot'

#: The candidates, all from the share-permissive subset of SPOT and all in or
#: near biology and biomedicine, with ``row`` the row of ``amphora/SPOT`` the
#: annotation comes from so that every claim about what the error is can be
#: traced to the benchmark rather than to this file. ``chosen`` marks the five
#: the pilot reviewed; ``kind`` is SPOT's own error category.
CANDIDATES = [
    {'slug': 'medicine-oxytetracycline', 'doi': '10.1038/s41598-024-73921-8', 'row': 66,
     'field': 'Multidisciplinary', 'kind': 'Statistical reporting', 'chosen': True},
    {'slug': 'chemistry-photocatalytic', 'doi': '10.1038/s41598-025-85894-3', 'row': 8,
     'field': 'Multidisciplinary', 'kind': 'Data inconsistency', 'chosen': True},
    {'slug': 'biology-dark-kinome', 'doi': '10.1186/s12964-024-01868-4', 'row': 6,
     'field': 'Biology', 'kind': 'Reagent identity', 'chosen': True},
    {'slug': 'biology-cgrp', 'doi': '10.1007/s10571-024-01456-7', 'row': 53,
     'field': 'Biology', 'kind': 'Figure duplication', 'chosen': True},
    # SPOT names v2, which is the withdrawal: arXiv serves no PDF for a
    # withdrawn version, and the error the withdrawal is about is in v1.
    {'slug': 'network-beamforming', 'doi': '2402.10307v1', 'row': 1,
     'field': 'Computer Science', 'kind': 'Equation / proof', 'chosen': True},
    # Fetched and checked, not reviewed: the annotated error was not in the PDF
    # that is served today, or the PDF could not be had at all. check.py says
    # which, for each one.
    {'slug': 'biology-methylation', 'doi': '10.1186/s13059-024-03222-w', 'row': 24,
     'field': 'Biology', 'kind': 'Figure duplication', 'chosen': False},
    {'slug': 'biology-ebselen', 'doi': '10.1038/s41598-025-91235-1', 'row': 9,
     'field': 'Multidisciplinary', 'kind': 'Equation / proof', 'chosen': False},
    {'slug': 'medicine-exosome', 'doi': '10.1038/s41598-024-56334-5', 'row': 63,
     'field': 'Multidisciplinary', 'kind': 'Data Inconsistency (figure-text)', 'chosen': False},
    {'slug': 'medicine-radiofrequency', 'doi': '10.34172/bi.2023.30064', 'row': 12,
     'field': 'Medicine', 'kind': 'Reagent identity', 'chosen': False},
    {'slug': 'medicine-vitamin-c', 'doi': '10.1186/s13287-023-03615-x', 'row': 38,
     'field': 'Medicine', 'kind': 'Figure duplication', 'chosen': False},
    {'slug': 'biology-antibiofouling', 'doi': '10.1186/s12951-024-03026-x', 'row': 67,
     'field': 'Biology', 'kind': 'Figure duplication', 'chosen': False},
    {'slug': 'biology-ilf3', 'doi': '10.1038/s41467-024-51030-4', 'row': 51,
     'field': 'Multidisciplinary', 'kind': 'Figure duplication', 'chosen': False},
    {'slug': 'environment-net-zero', 'doi': '10.5539/jsd.v17n6p137', 'row': 4,
     'field': 'Environmental Science', 'kind': 'Equation / proof', 'chosen': False},
]

#: The two Stage A papers that were reviewed unaltered and scored for false
#: allegations. They are copied rather than refetched so that the control is
#: the same file Stage A reviewed, byte for byte.
CONTROLS = [
    {'slug': 'control-education-pmc7183109',
     'from': 'candidates/education-pmc7183109.pdf'},
    {'slug': 'control-microbiology-pmc8208984',
     'from': 'candidates/microbiology-pmc8208984.pdf'},
]


def chosen() -> list:
    """The candidates the pilot reviewed."""
    return [c for c in CANDIDATES if c.get('chosen')]


def pmcid_for(doi: str, get=get) -> str:
    """The PMCID for a DOI, or '' when the article is not in PMC."""
    query = urllib.parse.urlencode({'ids': doi, 'format': 'json', 'tool': TOOL})
    try:
        answer = read_json(f'{IDCONV}?{query}', get)
    except Unreachable:
        return ''
    records = answer.get('records') if isinstance(answer, dict) else None
    for record in records if isinstance(records, list) else []:
        if isinstance(record, dict) and record.get('pmcid'):
            return str(record['pmcid'])
    return ''


def publisher_url(doi: str) -> str:
    """The publisher's own PDF for an id, for the forms that have a known path.

    arXiv, Nature and Springer serve open-access PDFs at a path derived from
    the id, and an arXiv id carries its version, which is the only route in
    this file that fetches a *stated* version of a paper rather than whatever
    is current. Nothing else is guessed: the other prefixes go through
    OpenAlex, which is asked where the open copy is rather than told.
    """
    if not doi.startswith('10.'):
        return f'https://arxiv.org/pdf/{doi}'
    prefix, _, suffix = doi.partition('/')
    if prefix == '10.1038':
        return f'https://www.nature.com/articles/{suffix}.pdf'
    if prefix in {'10.1186', '10.1007'}:
        return f'https://link.springer.com/content/pdf/{doi}.pdf'
    return ''


def routes_for(candidate: dict, get=get):
    """``(name, url)`` for one candidate, the bucket first and the rest after.

    The Open Access Subset serves files rather than pages, which is the whole
    reason Stage A fetches from it: twenty of its first forty-one candidates
    came down as "Preparing to download" interstitials served with a 200. The
    publisher and then OpenAlex are the fallbacks, and a body that does not
    begin with ``%PDF`` is not kept by any of the three.
    """
    pmcid = pmcid_for(candidate['doi'], get) if candidate['doi'].startswith('10.') else ''
    if pmcid:
        url = oa_pdf_url(pmcid, get)
        if url:
            yield 'pmc-oa', url
    publisher = publisher_url(candidate['doi'])
    if publisher:
        yield 'publisher', publisher
    openalex = pdf_url(candidate['doi'], get)
    if openalex:
        yield 'openalex', openalex


def fetch(root: Path, stage_a: Path, candidates=None, get=get) -> list:
    """Fetch the candidates and copy the controls, and say what happened."""
    papers = Path(root) / 'papers'
    papers.mkdir(parents=True, exist_ok=True)
    rows = []
    for candidate in candidates if candidates is not None else chosen():
        path = papers / f'{candidate["slug"]}.pdf'
        row = {**candidate, 'path': str(path)}
        if is_pdf(path):
            rows.append({**row, 'state': 'already here', 'route': 'on disk'})
            continue
        problems = []
        for name, url in routes_for(candidate, get):
            problem = fetch_pdf(url, path, get)
            if not problem:
                rows.append({**row, 'state': 'fetched', 'route': name, 'url': url})
                break
            problems.append(f'{name}: {problem}')
        else:
            rows.append({**row, 'state': 'missing', 'problems': problems})
    for control in CONTROLS:
        path = papers / f'{control["slug"]}.pdf'
        source = Path(stage_a) / control['from']
        row = {**control, 'path': str(path), 'kind': 'control'}
        if is_pdf(path):
            rows.append({**row, 'state': 'already here', 'route': 'on disk'})
        elif is_pdf(source):
            shutil.copyfile(source, path)
            rows.append({**row, 'state': 'copied', 'route': str(source)})
        else:
            rows.append({**row, 'state': 'missing',
                         'problems': [f'{source} is not a PDF on this machine']})
    return rows


def audit(root: Path) -> list:
    """Which papers are on disk and are PDFs, without fetching anything."""
    papers = Path(root) / 'papers'
    return [{'slug': entry['slug'], 'chosen': bool(entry.get('chosen')),
             'pdf': is_pdf(papers / f'{entry["slug"]}.pdf'),
             'bytes': (papers / f'{entry["slug"]}.pdf').stat().st_size
             if (papers / f'{entry["slug"]}.pdf').is_file() else 0}
            for entry in [*CANDIDATES, *CONTROLS]]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', required=True, help='the pilot data directory')
    parser.add_argument('--stage-a', default=str(Path.home() / 'pra-stage-a'),
                        help="where Stage A's data directory is, for the controls")
    parser.add_argument('--audit', action='store_true', help='say what is there, fetch nothing')
    parser.add_argument('--all', action='store_true',
                        help='fetch the whole candidate pool, not only the chosen')
    parser.add_argument('--only', default='', help='one slug, for a single retry')
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser()
    if args.audit:
        for row in audit(root):
            mark = 'reviewed' if row['chosen'] else 'pool'
            print(f'{row["slug"]:34} {mark:9} '
                  f'{"pdf" if row["pdf"] else "NOT A PDF":10} {row["bytes"]:>9}')
        return 0
    wanted = CANDIDATES if args.all else None
    if args.only:
        wanted = [c for c in CANDIDATES if c['slug'] == args.only]
        if not wanted:
            print(f'no candidate called {args.only}')
            return 2
    rows = fetch(root, Path(args.stage_a).expanduser(), wanted, get)
    (root / 'papers' / 'fetched.json').write_text(
        json.dumps(rows, indent=2) + '\n', encoding='utf-8')
    for row in rows:
        print(f'{row["slug"]:34} {row["state"]:12} {row.get("route", "")}')
        for problem in row.get('problems') or []:
            print(f'    {problem}')
    return 0 if all(row['state'] != 'missing' for row in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
