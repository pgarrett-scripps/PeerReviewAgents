"""Fetch the Stage A paper corpus: open-access papers with real numbers in them.

Stage A needs papers nobody wrote for this test: published, CC BY, with
statistics and tables a reviewer could actually recompute. This script is how
the corpus was built and how it can be built again, because the papers
themselves are deliberately not in the repository — only the list that names
them.

Four ways in:

* `corpus.py roster` downloads the twelve papers Stage A used, by DOI, through
  whichever open-access host serves them. The roster below carries the field,
  the DOI, the URL that worked, the error planted in each one and the candidate
  index `plant.py` needs to reproduce that plant.
* `corpus.py search` asks Europe PMC for more CC BY candidates in a field and
  prints them, resolving each one's PDF through OpenAlex if asked to with
  `--with-pdf-urls`. That is how the roster was found in the first place.
* `corpus.py refill` searches and downloads in one pass, field by field, until
  each field holds as many usable PDFs as it was asked for. That is how the
  pool for a bigger stage gets built.
* `corpus.py audit` says, per field, how many candidates on disk are real PDFs
  and names the ones that are not; `--repair` asks the bucket again for each of
  those, by the PMCID in its own name.

**How a PDF is fetched, and why it changed.** Stage A's first fetch asked the
publisher, or PMC's article page, for the PDF. Twenty of forty-one candidates
came back as a page instead of a paper: PMC's "Preparing to download"
interstitial, or Cloudflare's "Just a moment" challenge, both served with a
200 and saved under a `.pdf` name, where they were found only when the engine
refused to ingest them. The fix is to stop asking a web page for a file. The
PMC Open Access Subset is published as plain objects on AWS Open Data, one
prefix per article, holding the publisher's own PDF:

    https://pmc-oa-opendata.s3.amazonaws.com/PMC7296073.1/PMC7296073.1.pdf

Nothing there challenges a client, redirects to a viewer or renders a page, so
a PMC candidate now comes down from the bucket first and from a publisher only
as a fallback. Every body is checked for `%PDF` before it is kept, each route
is retried once, and a verified body is written to a temporary name and renamed
into place — so a failure writes nothing, and a `.pdf` this script wrote is a
PDF. A candidate already on disk that is not a PDF counts as missing and is
refetched, and `audit` names it until it is. Nothing here sends anything about the machine it runs on beyond a
generic client name.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent))

from root import MissingRoot, resolve_root  # noqa: E402

AGENT = 'stage-a-corpus/1.0'
SEARCH = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
OPENALEX = 'https://api.openalex.org/works/doi:'
# The PMC Open Access Subset on AWS Open Data: one prefix per article version,
# holding the publisher's PDF beside the JATS XML. No challenge, no viewer.
OA_BUCKET = 'https://pmc-oa-opendata.s3.amazonaws.com'
# `PMC7296073.1/PMC7296073.1.pdf` is the article; `mmc1.pdf` beside it is a
# supplement and is not what a review should be given.
ARTICLE_PDF = re.compile(r'^(PMC\d+)\.(\d+)/\1\.\2\.pdf$')

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


# Everything a fetch can fail with. `HTTPException` is in here because a
# truncated chunked response raises `IncompleteRead`, which is not an `OSError`
# and once killed a whole refill from one short answer out of Europe PMC.
NETWORK = (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
           http.client.HTTPException, OSError)


class Unreachable(Exception):
    """A URL did not answer usefully, after being asked more than once."""


def get(url: str, accept: str = '*/*') -> bytes:
    """Fetch a URL with a generic client name and no identifying headers."""
    request = urllib.request.Request(url, headers={'User-Agent': AGENT, 'Accept': accept})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def read_json(url: str, get=get, attempts: int = 3):
    """Fetch and parse JSON, asking again when the answer does not arrive whole.

    A search that fails is not a corpus that fails: raising ``Unreachable``
    lets one field be given up on while the others still get filled.
    """
    problem = 'not attempted'
    for _ in range(max(1, attempts)):
        try:
            return json.loads(get(url, 'application/json'))
        except (*NETWORK, json.JSONDecodeError) as error:
            problem = f'{type(error).__name__}: {error}'
    raise Unreachable(f'{url} -> {problem}')


def search(field: str, rows: int = 25, years: str = '2018 TO 2024', get=get) -> list:
    """CC BY, open access, has a PDF, most cited first."""
    terms = FIELDS.get(field, field)
    query = (f'LICENSE:"cc by" AND OPEN_ACCESS:Y AND HAS_PDF:Y AND SRC:MED '
             f'AND PUB_YEAR:[{years}] AND ({terms})')
    url = (f'{SEARCH}?query={urllib.parse.quote(query)}&format=json&resultType=core'
           f'&pageSize={rows}&sort=' + urllib.parse.quote('CITED desc'))
    payload = read_json(url, get)
    out = []
    for result in payload.get('resultList', {}).get('result', []) or []:
        doi = (result.get('doi') or '').strip()
        if not doi:
            continue
        pmcid = (result.get('pmcid') or '').strip().upper()
        if not pmcid:
            # Without a PMCID there is no Open Access Subset prefix to fetch
            # from, and the slug would not even name the paper.
            continue
        out.append({
            'field': field,
            'slug': f'{field}-pmc{pmcid.removeprefix("PMC").lower()}',
            'pmcid': pmcid,
            'doi': doi,
            'journal': result.get('journalInfo', {}).get('journal', {}).get('title', ''),
            'title': (result.get('title') or '').strip(),
            'license': result.get('license', ''),
            'year': str(result.get('pubYear') or ''),
        })
    return out


def pdf_url(doi: str, get=get) -> str:
    """Ask OpenAlex where the open-access PDF of this DOI is.

    This is the last route tried, not the first: it names a publisher's own
    page, which is where the interstitials and the bot challenges live. It
    stays because it is the only route for a paper that is not in PMC's Open
    Access Subset at all.
    """
    if not doi:
        return ''
    try:
        payload = read_json(OPENALEX + urllib.parse.quote(doi), get)
    except Unreachable:
        return ''
    location = payload.get('best_oa_location') or {}
    return (location.get('pdf_url') or '').strip()


def pmcid_of(entry: dict) -> str:
    """The PMCID this row is about, from its own field or from its slug."""
    pmcid = (entry.get('pmcid') or '').strip().upper()
    if not pmcid:
        found = re.search(r'PMC(\d+)', entry.get('slug', ''), re.IGNORECASE)
        pmcid = f'PMC{found.group(1)}' if found else ''
    return pmcid if pmcid.startswith('PMC') else (f'PMC{pmcid}' if pmcid else '')


def oa_keys(listing: bytes) -> list:
    """The object keys named in one S3 list-objects response."""
    try:
        tree = ElementTree.fromstring(listing)
    except ElementTree.ParseError:
        return []
    return [element.text or '' for element in tree.iter()
            if element.tag.rsplit('}', 1)[-1] == 'Key']


def article_pdf_key(keys, pmcid: str) -> str:
    """This article's own PDF, newest version, ignoring its supplements.

    A prefix holds `PMC7296073.1/PMC7296073.1.pdf`, the paper, and may hold
    `mmc1.pdf` or `media-1.pdf` beside it, which are supplements. Only the
    first is the paper a review should be given.
    """
    best_version, best_key = -1, ''
    for key in keys:
        match = ARTICLE_PDF.match(key or '')
        if match and match.group(1).upper() == pmcid.upper():
            version = int(match.group(2))
            if version > best_version:
                best_version, best_key = version, key
    return best_key


def oa_pdf_url(pmcid: str, get=get) -> str:
    """Where the Open Access Subset keeps this article's PDF, or ''.

    The version number is part of the prefix and cannot be guessed, so the
    prefix is listed and the answer read out of the listing. A paper outside
    the subset lists nothing, which is not an error — it means another route.
    """
    if not pmcid:
        return ''
    query = urllib.parse.urlencode({'list-type': '2', 'prefix': f'{pmcid}.'})
    try:
        listing = get(f'{OA_BUCKET}/?{query}', 'application/xml')
    except NETWORK:
        return ''
    key = article_pdf_key(oa_keys(listing), pmcid)
    return f'{OA_BUCKET}/{urllib.parse.quote(key)}' if key else ''


def routes(entry: dict, get=get):
    """Yield `(route name, url)` for this candidate, best first, lazily.

    The bucket is first because it serves files rather than pages. The URL
    recorded in the roster is second, because it is known to have worked for
    that paper once. OpenAlex is last and is only resolved if it is reached,
    so a candidate that comes down from the bucket costs one listing and one
    download and no third-party lookup at all.
    """
    oa = oa_pdf_url(pmcid_of(entry), get)
    if oa:
        yield 'pmc-oa', oa
    recorded = (entry.get('url') or '').strip()
    if recorded:
        yield 'recorded', recorded
    resolved = pdf_url(entry.get('doi', ''), get)
    if resolved and resolved != recorded:
        yield 'openalex', resolved


def is_pdf(path) -> bool:
    """Whether what is at this path begins with `%PDF`."""
    try:
        with open(path, 'rb') as handle:
            return handle.read(5).startswith(b'%PDF')
    except OSError:
        return False


def fetch_pdf(url: str, path: Path, get=get, attempts: int = 2) -> str:
    """Download a PDF and keep it only if it is one; return '' or the problem.

    Each attempt is checked for `%PDF` before anything touches the target
    path, and a verified body is written beside it and renamed into place. A
    body that is a page, and an attempt that raises, are both retried once:
    an interstitial is often served to the first request and the file to the
    second. Nothing partial and nothing unverified is ever left at `path`.
    """
    problem = 'no url'
    part = Path(path).with_name(Path(path).name + '.part')
    for _ in range(max(1, attempts)):
        try:
            body = get(url, 'application/pdf')
        except NETWORK as error:
            problem = f'{type(error).__name__}: {error}'
            continue
        if not body.startswith(b'%PDF'):
            problem = f'not a pdf: {len(body)} bytes beginning {body[:16]!r}'
            continue
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(body)
        part.replace(path)
        return ''
    return problem


def download_candidate(entry: dict, folder: Path, get=get) -> dict:
    """Fetch one candidate into `folder` by whichever route answers with a PDF.

    A file already there counts only if it is a PDF: the twenty saved HTML
    pages from the first fetch are exactly what this refuses to count, so a
    rerun replaces them instead of trusting the name.
    """
    row = dict(entry)
    path = Path(folder) / f'{entry["slug"]}.pdf'
    row['path'] = str(path)
    if is_pdf(path):
        row['state'] = 'already here'
        return row
    problems = []
    for name, url in routes(entry, get):
        problem = fetch_pdf(url, path, get)
        if not problem:
            row.update({'url': url, 'route': name, 'state': 'downloaded'})
            return row
        problems.append(f'{name}: {problem}')
    row['state'] = '; '.join(problems) or 'no open-access pdf url'
    return row


def download_roster(root: Path, only: str = '', get=get) -> list:
    """Fetch every roster paper that is not already on disk, as a PDF."""
    folder = Path(root) / 'candidates'
    out = [download_candidate(entry, folder, get) for entry in ROSTER
           if not only or only in (entry['slug'], entry['field'])]
    if out and not only:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'candidates.json').write_text(json.dumps(out, indent=1),
                                                encoding='utf-8')
    return out


def field_of(slug: str) -> str:
    """The field a candidate's slug names."""
    return slug.split('-', 1)[0]


def audit(folder) -> tuple:
    """What is on disk: real PDFs per field, and the files that are not PDFs."""
    counts, broken = {}, []
    for path in sorted(Path(folder).glob('*.pdf')):
        if is_pdf(path):
            counts[field_of(path.stem)] = counts.get(field_of(path.stem), 0) + 1
        else:
            broken.append(path)
    return counts, broken


def repair(folder, get=get) -> list:
    """Refetch every file that is not a PDF, and say which ones are still not.

    The twenty pages the first fetch saved are named by their slug, which
    names their PMCID, so each one can be asked for again from the bucket with
    no search and no record of where it came from. One that still will not come
    down is left where it is and reported: it is a page, the audit says so, and
    nothing downstream will mistake it for a paper.
    """
    _, broken = audit(folder)
    out = []
    for path in broken:
        row = download_candidate({'slug': path.stem, 'doi': '', 'url': ''}, folder, get)
        out.append({'slug': path.stem, 'state': row['state'],
                    'route': row.get('route', '')})
    return out


def pmcids_present(folder) -> set:
    """Every PMCID already on disk as a PDF, under whatever field's slug.

    Two fields' searches return the same paper often enough to matter: the
    pool should hold it once, or two planted copies of one paper would be
    counted as two papers.
    """
    return {pmcid_of({'slug': path.stem}) for path in Path(folder).glob('*.pdf')
            if is_pdf(path)} - {''}


def refill(root, fields, per_field: int, rows: int = 50, get=get,
           search=search) -> list:
    """Top each field up to `per_field` usable PDFs, and say what happened.

    Nothing already usable is refetched, nothing is counted twice across
    fields, and a candidate that will not come down as a PDF is dropped and
    named rather than retried in a loop.
    """
    folder = Path(root) / 'candidates'
    counts, _ = audit(folder)
    seen = pmcids_present(folder)
    report = []
    for field in fields:
        have = counts.get(field, 0)
        added, dropped, duplicates, failures = 0, 0, 0, []
        try:
            hits = search(field, rows) if have < per_field else []
        except Unreachable as unreachable:
            hits = []
            failures.append(f'search: {unreachable}')
        for hit in hits:
            if have + added >= per_field:
                break
            pmcid = pmcid_of(hit)
            if pmcid in seen:
                duplicates += 1
                continue
            row = download_candidate(hit, folder, get)
            if row['state'] in ('downloaded', 'already here'):
                added += 1
                seen.add(pmcid)
            else:
                dropped += 1
                failures.append(f'{hit["slug"]}: {row["state"]}')
        report.append({'field': field, 'had': have, 'added': added,
                       'dropped': dropped, 'duplicates': duplicates,
                       'usable': have + added, 'wanted': per_field,
                       'failures': failures})
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='corpus.py', description='Fetch the Stage A corpus of open-access papers.')
    parser.add_argument('what', choices=('roster', 'search', 'refill', 'audit'),
                        help='roster: the papers Stage A used. search: find more. '
                             'refill: top each field up to --per-field usable PDFs. '
                             'audit: count the PDFs on disk, and name what is not one.')
    parser.add_argument('--root', default='',
                        help='where the corpus lives: a directory outside this '
                             'repository, defaulting to $PRA_STAGE_A_ROOT and to '
                             'nothing else')
    parser.add_argument('--field', default='', help='one field, for search or a partial roster')
    parser.add_argument('--rows', type=int, default=25, help='search results to ask for')
    parser.add_argument('--per-field', type=int, default=6,
                        help='usable PDFs wanted in each field, for refill')
    parser.add_argument('--repair', action='store_true',
                        help='with audit: ask the bucket again for every file '
                             'on disk that is not a PDF')
    parser.add_argument('--with-pdf-urls', action='store_true',
                        help='resolve each search hit through OpenAlex (one request each)')
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.what in ('roster', 'refill', 'audit'):
        try:
            root = resolve_root(args.root)
        except MissingRoot as missing:
            print(missing, file=sys.stderr)
            return 2
    if args.what == 'roster':
        rows = download_roster(root, args.field)
        for row in rows:
            print(f'{row["slug"]:28} {row.get("kind") or "control":11} {row["state"]}')
        return 0 if all(row['state'] in ('downloaded', 'already here') for row in rows) else 1
    if args.what == 'audit':
        folder = Path(root) / 'candidates'
        if args.repair:
            for row in repair(folder):
                print(f'{row["slug"]:28} {row["state"]}')
        counts, broken = audit(folder)
        for field in sorted(counts):
            print(f'{field:14} {counts[field]}')
        print(f'{"total":14} {sum(counts.values())}')
        for path in broken:
            print(f'not a pdf: {path}', file=sys.stderr)
        return 0
    if args.what == 'refill':
        fields = [args.field] if args.field else sorted(FIELDS)
        report = refill(root, fields, args.per_field, args.rows)
        for row in report:
            print(f'{row["field"]:14} had {row["had"]:3} added {row["added"]:3} '
                  f'dropped {row["dropped"]:3} usable {row["usable"]:3}'
                  f'{"" if row["usable"] >= row["wanted"] else "  (short)"}')
            for failure in row['failures']:
                print(f'  dropped {failure}', file=sys.stderr)
        print(f'{"total":14} usable {sum(row["usable"] for row in report)}')
        folder = Path(root) / 'candidates'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'refill.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
        return 0 if all(row['usable'] >= row['wanted'] for row in report) else 1
    fields = [args.field] if args.field else sorted(FIELDS)
    for field in fields:
        for row in search(field, args.rows):
            if args.with_pdf_urls:
                row['pdf_url'] = pdf_url(row['doi'])
            print(json.dumps(row))
    return 0


if __name__ == '__main__':
    sys.exit(main())
