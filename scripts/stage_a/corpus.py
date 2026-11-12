"""Fetch the Stage A paper corpus: open-access papers with real numbers in them.

Stage A needs papers nobody wrote for this test: published, CC BY, with
statistics and tables a reviewer could actually recompute. This script is how
the corpus was built and how it can be built again, because the papers
themselves are deliberately not in the repository — only the list that names
them.

Two ways in:

* `corpus.py roster` downloads the twelve papers Stage A used, by DOI, through
  whichever open-access host serves them. The roster below carries the field,
  the DOI, the URL that worked, the error planted in each one and the candidate
  index `plant.py` needs to reproduce that plant.
* `corpus.py search` asks Europe PMC for more CC BY candidates in a field, and
  resolves each one's PDF through OpenAlex. That is how the roster was found in
  the first place, and how it would be extended for Stage B.

Every download is verified to start with `%PDF`, because several publishers
answer a PDF request with an HTML interstitial and a 200. Nothing here sends
anything about the machine it runs on beyond a generic client name.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path('/home/ty/pra-stage-a')
AGENT = 'stage-a-corpus/1.0'
SEARCH = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
OPENALEX = 'https://api.openalex.org/works/doi:'

# The papers Stage A ran on. `kind` is the error planted in that paper and
# `candidate` the index plant.py chose, so the altered copies can be rebuilt
# exactly. The two with no kind are the clean controls: nothing is planted in
# them, and what matters is whether a run alleges an error anyway.
ROSTER = (
    {'slug': 'psychology-pmc7241824', 'field': 'psychology', 'kind': 'statistic',
     'candidate': 0, 'doi': '10.1371/journal.pone.0233668',
     'url': 'https://www.medrxiv.org/content/medrxiv/early/2020/05/05/'
            '2020.04.29.20085563.full.pdf'},
    {'slug': 'trial-pmc8053280', 'field': 'trial', 'kind': 'statistic',
     'candidate': 0, 'doi': '10.1186/s13195-021-00813-8',
     'url': 'https://alzres.biomedcentral.com/counter/pdf/10.1186/s13195-021-00813-8'},
    {'slug': 'education-pmc6085180', 'field': 'education', 'kind': 'statistic',
     'candidate': 1, 'doi': '10.1200/jco.2017.77.1097',
     'url': 'https://researchonline.jcu.edu.au/69140/1/69140_Rodin_et_al_2018.pdf'},
    {'slug': 'neuroscience-pmc9046077', 'field': 'neuroscience', 'kind': 'table-cell',
     'candidate': 7, 'doi': '10.1038/s41586-022-04569-5',
     'url': 'https://www.nature.com/articles/s41586-022-04569-5.pdf'},
    {'slug': 'psychology-pmc7609822', 'field': 'psychology', 'kind': 'table-cell',
     'candidate': 2, 'doi': '10.1186/s12889-020-09738-7',
     'url': 'https://bmcpublichealth.biomedcentral.com/counter/pdf/'
            '10.1186/s12889-020-09738-7'},
    {'slug': 'neuroscience-pmc8596853', 'field': 'neuroscience', 'kind': 'table-cell',
     'candidate': 2, 'doi': '10.1038/s41586-021-04103-z',
     'url': 'https://www.nature.com/articles/s41586-021-04103-z.pdf'},
    {'slug': 'microbiology-pmc6192448', 'field': 'microbiology', 'kind': 'sample-size',
     'candidate': 0, 'doi': '10.12688/wellcomeopenres.14826.1', 'url': ''},
    {'slug': 'trial-pmc8938265', 'field': 'trial', 'kind': 'sample-size',
     'candidate': 0, 'doi': '10.1038/s41591-021-01659-1',
     'url': 'https://www.nature.com/articles/s41591-021-01659-1.pdf'},
    {'slug': 'microbiology-pmc7296073', 'field': 'microbiology', 'kind': 'fake-doi',
     'candidate': 0, 'doi': '10.3389/fimmu.2020.00906',
     'url': 'https://www.frontiersin.org/articles/10.3389/fimmu.2020.00906/pdf'},
    {'slug': 'epidemiology-pmc7611074', 'field': 'epidemiology', 'kind': 'fake-doi',
     'candidate': 1, 'doi': '10.1038/s41586-020-2521-4',
     'url': 'https://www.nature.com/articles/s41586-020-2521-4.pdf'},
    {'slug': 'genomics-pmc6927181', 'field': 'genomics', 'kind': '', 'candidate': 0,
     'doi': '10.1186/s13059-019-1874-1',
     'url': 'https://genomebiology.biomedcentral.com/track/pdf/10.1186/s13059-019-1874-1'},
    {'slug': 'materials-pmc8128876', 'field': 'materials', 'kind': '', 'candidate': 0,
     'doi': '10.1038/s41467-021-23115-x',
     'url': 'https://www.nature.com/articles/s41467-021-23115-x.pdf'},
)

# What a field means to Europe PMC, for `corpus.py search`.
FIELDS = {
    'trial': 'randomized controlled trial',
    'psychology': 'psychology questionnaire',
    'education': 'education students learning',
    'neuroscience': 'neuroimaging cohort',
    'microbiology': 'microbiome sequencing',
    'epidemiology': 'seroprevalence cohort',
    'genomics': 'genome variants',
    'materials': 'materials characterisation',
    'ecology': 'species abundance',
    'economics': 'household income survey',
}


def get(url: str, accept: str = '*/*') -> bytes:
    """Fetch a URL with a generic client name and no identifying headers."""
    request = urllib.request.Request(url, headers={'User-Agent': AGENT, 'Accept': accept})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def search(field: str, rows: int = 25, years: str = '2018 TO 2024') -> list:
    """CC BY, open access, has a PDF, most cited first."""
    terms = FIELDS.get(field, field)
    query = (f'LICENSE:"cc by" AND OPEN_ACCESS:Y AND HAS_PDF:Y AND SRC:MED '
             f'AND PUB_YEAR:[{years}] AND ({terms})')
    url = (f'{SEARCH}?query={urllib.parse.quote(query)}&format=json&resultType=core'
           f'&pageSize={rows}&sort=' + urllib.parse.quote('CITED desc'))
    payload = json.loads(get(url, 'application/json'))
    out = []
    for result in payload.get('resultList', {}).get('result', []) or []:
        doi = (result.get('doi') or '').strip()
        if not doi:
            continue
        out.append({
            'field': field,
            'slug': f'{field}-pmc{(result.get("pmcid") or "").lstrip("PMC")}',
            'doi': doi,
            'journal': result.get('journalInfo', {}).get('journal', {}).get('title', ''),
            'title': (result.get('title') or '').strip(),
            'license': result.get('license', ''),
            'year': str(result.get('pubYear') or ''),
        })
    return out


def pdf_url(doi: str) -> str:
    """Ask OpenAlex where the open-access PDF of this DOI is.

    Europe PMC's own `fullTextPDF` endpoint answers 404 for most records and
    its article pages answer 403 to anything that is not a browser, so the
    publisher's own open-access copy is the one that works.
    """
    try:
        payload = json.loads(get(OPENALEX + urllib.parse.quote(doi), 'application/json'))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return ''
    location = payload.get('best_oa_location') or {}
    return (location.get('pdf_url') or '').strip()


def fetch_pdf(url: str, path: Path) -> str:
    """Download a PDF, or say why not. A body that is not a PDF is not kept."""
    try:
        body = get(url, 'application/pdf')
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as error:
        return f'{type(error).__name__}: {error}'
    if not body.startswith(b'%PDF'):
        return f'not a pdf: {len(body)} bytes beginning {body[:16]!r}'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return ''


def download_roster(root: Path, only: str = '') -> list:
    """Fetch every roster paper that is not already on disk."""
    folder = Path(root) / 'candidates'
    out = []
    for entry in ROSTER:
        if only and only not in (entry['slug'], entry['field']):
            continue
        row = dict(entry)
        path = folder / f'{entry["slug"]}.pdf'
        row['path'] = str(path)
        if path.is_file() and path.read_bytes()[:4] == b'%PDF':
            row['state'] = 'already here'
        else:
            # The URL that worked when the corpus was built, and OpenAlex as
            # the fallback for when a publisher moves it.
            url = entry.get('url') or pdf_url(entry['doi'])
            row['url'] = url
            if not url:
                row['state'] = 'no open-access pdf url'
            else:
                problem = fetch_pdf(url, path)
                if problem and entry.get('url'):
                    url = pdf_url(entry['doi'])
                    problem = fetch_pdf(url, path) if url else problem
                    row['url'] = url or row['url']
                row['state'] = problem or 'downloaded'
        out.append(row)
    manifest = folder / 'candidates.json'
    if out and not only:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(out, indent=1), encoding='utf-8')
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='corpus.py', description='Fetch the Stage A corpus of open-access papers.')
    parser.add_argument('what', choices=('roster', 'search'),
                        help='roster: the papers Stage A used. search: find more.')
    parser.add_argument('--root', default=str(ROOT), help='where the corpus lives')
    parser.add_argument('--field', default='', help='one field, for search or a partial roster')
    parser.add_argument('--rows', type=int, default=25, help='search results to ask for')
    parser.add_argument('--with-pdf-urls', action='store_true',
                        help='resolve each search hit through OpenAlex (one request each)')
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.what == 'roster':
        rows = download_roster(Path(args.root), args.field)
        for row in rows:
            print(f'{row["slug"]:28} {row.get("kind") or "control":11} {row["state"]}')
        return 0 if all(row['state'] in ('downloaded', 'already here') for row in rows) else 1
    fields = [args.field] if args.field else sorted(FIELDS)
    for field in fields:
        for row in search(field, args.rows):
            if args.with_pdf_urls:
                row['pdf_url'] = pdf_url(row['doi'])
            print(json.dumps(row))
    return 0


if __name__ == '__main__':
    sys.exit(main())
