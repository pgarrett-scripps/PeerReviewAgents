"""Import source files without rewriting their scientific content."""
import collections
import json
import re
import shutil
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from . import paper_base
from .storage import digest, relative_name, write

POPPLER_MISSING = ('Poppler (pdftotext) is not installed. Install it with `brew install poppler` '
                   'on macOS, `sudo apt install poppler-utils` on Debian or Ubuntu, or '
                   '`choco install poppler` on Windows, then run the command again.')

#: What the pinned converter cannot supply, recorded in the manifest rather
#: than left for a reader to discover. A gap named here is a gap: it is not an
#: omission by the authors and not a piece this import dropped on purpose.
KNOWN_GAPS = {
    'figures': ('The pinned converter returns no figure images, so there is no figures/ folder '
                'and a figure is its caption line in the Markdown.'),
    'equation_confidence': ('Displayed equations carry the converter\'s own confidence in the map. '
                            'Sums and subscripts are the ones it gets wrong, so a low confidence '
                            'means read the page, not that the equation is absent.'),
}


#: ``Figure 3``, ``Fig. 3`` — at the start of a caption, and anywhere in a
#: sentence that points at one.
FIGURE_CAPTION = re.compile(r'^\s*\(?(?:Figure|Fig)\.?\s*(\d{1,3})', re.I)
FIGURE_CITED = re.compile(r'\b(?:Figures|Figure|Figs|Fig)\.?\s*(\d{1,3})', re.I)


def missing_figures(pieces):
    """Figure numbers the text points at that no caption in this import carries.

    A conversion loses a caption now and then — this paper has captions for
    eight figures and the text discusses nine — and the loss is invisible: a
    reviewer asked to check Figure 9 against its caption finds no caption and
    reports the figure as undescribed, which is a finding about the converter
    wearing the clothes of a finding about the manuscript. Counting both sides
    at import is what lets the manifest say so.
    """
    captioned, cited = set(), set()
    for piece in pieces:
        text = str(piece.get('text') or '')
        if piece.get('kind') in ('figure', 'caption'):
            found = FIGURE_CAPTION.match(text)
            if found:
                captioned.add(int(found.group(1)))
        else:
            cited.update(int(number) for number in FIGURE_CITED.findall(text))
    return sorted(cited - captioned), sorted(captioned)


class PopplerMissing(OSError):
    """Poppler is absent from this machine, which is the operator's to fix.

    The import died as ``FileNotFoundError: [Errno 2] ... 'pdftotext'``, which
    reads like a missing manuscript rather than a missing package, so a person
    on a fresh machine had nothing to act on.
    """


def page_text(pdf):
    """The manuscript's text layer, one entry per page, as Poppler lays it out."""
    try:
        extracted = subprocess.run(['pdftotext', '-layout', str(pdf), '-'],
                                   check=True, capture_output=True, text=True).stdout
    except FileNotFoundError as error:
        raise PopplerMissing(POPPLER_MISSING) from error
    return extracted.split('\f')


def text_of(element):
    return re.sub(r'\s+', ' ', ''.join(element.itertext())).strip() if element is not None else ''


def words(text):
    return re.findall(r"\b[^\W_]+(?:[’'-][^\W_]+)*\b", text, re.UNICODE)


def norm(text):
    return re.sub(r'[^\w]', '', unicodedata.normalize('NFKC', text).lower())


def document_name(stem):
    """The ``.md`` a supplement is written to, from its file name.

    A supplement is a document of the base like the paper itself, so it is read
    by name and its name has to survive being written into JSON and read back
    on another machine: anything that is not a letter, a digit, a dot, a dash
    or an underscore becomes a dash.
    """
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '-', str(stem)).strip('-.') or 'supplement'
    return 'supplement-' + cleaned + '.md'


def import_paper(pdf, output, xml=None, title=None, source_url='', supplements=None):
    import rustypaper
    pdf, output = Path(pdf), Path(output)
    if (output / 'map.json').exists():
        raise ValueError('Workspace already contains an immutable manuscript')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'original').mkdir(exist_ok=True)
    shutil.copy2(pdf, output / 'original' / 'original.pdf')
    markdown, doc = rustypaper.convert(str(pdf))
    # The converter's own view of the PDF, kept for diagnosing a conversion
    # rather than for reading the paper: the Markdown a reviewer reads is
    # composed from the pieces below, where every piece has a line and a map
    # row, and `markdown` has neither.
    write(output / 'converter.json', doc)
    raw_pages = page_text(pdf)
    if raw_pages and not raw_pages[-1].strip():
        raw_pages.pop()
    normalized_pages = [norm(page) for page in raw_pages]
    pieces, references, assets, warnings = [], [], [], []
    supplement_pieces = collections.OrderedDict()

    def locate(text):
        clean = norm(text)
        if len(clean) < 35:
            return None
        probes = [clean[i:i+55] for i in range(0, min(len(clean), 500)-54, 55)]
        hits = [(sum(probe in page for probe in probes), number+1) for number, page in enumerate(normalized_pages)]
        best = max(hits, default=(0, None))
        return best[1] if best[0] >= 2 else None

    def add(text, section, kind='paragraph', page=None, bbox=None, citations=None,
            document=paper_base.MAIN_DOCUMENT, table=None, **extra):
        """One piece of one document, in reading order.

        A piece with nothing to render is not a piece: the converter emits
        empty blocks, and a blank line in the Markdown with a map row over it
        would be a citable nothing.
        """
        piece = {'text': text, 'section': section, 'kind': kind, 'page': page, 'bbox': bbox,
                 'citations': citations or [], 'table': table, 'source': 'text',
                 'document': document, **extra}
        if not any(line.strip() for line in paper_base.piece_lines(piece)):
            return
        if document == paper_base.MAIN_DOCUMENT:
            if page is None and extra.get('page_mapping') is None:
                located = locate(text) if kind != 'equation' else None
                piece['page'] = located
                piece['page_mapping'] = 'text_anchor' if located else 'unavailable'
            elif page is not None and extra.get('page_mapping') is None:
                piece['page_mapping'] = 'converter'
            pieces.append(piece)
        else:
            supplement_pieces.setdefault(document, []).append(piece)

    if xml:
        xml = Path(xml)
        raw = xml.read_bytes()
        if re.search(br'<!ENTITY\s', raw, re.I):
            raise ValueError('XML entity declarations are not accepted')
        root = ET.fromstring(raw)
        shutil.copy2(xml, output / 'original' / 'original.xml')
        source_title = text_of(root.find('.//article-title'))
        title = title or source_title
        if title:
            add(title, 'Front matter', 'title')
        for p in root.findall('./front/article-meta/abstract/p'):
            add(text_of(p), 'Abstract')

        def walk(parent, section, depth=1):
            for e in parent:
                tag = e.tag.rsplit('}', 1)[-1]
                if tag == 'sec':
                    heading = text_of(e.find('title')) or section
                    add(heading, heading, 'heading', level=depth)
                    walk(e, heading, min(depth + 1, 5))
                elif tag == 'p':
                    cites = [rid for x in e.findall('.//xref') if x.get('ref-type')=='bibr' for rid in x.get('rid','').split()]
                    add(text_of(e), section, citations=cites)
                elif tag == 'disp-formula':
                    # A publisher's displayed equation is TeX when the XML
                    # carries it and the rendered characters otherwise. Either
                    # way it is a piece of its own, on its own line, so it can
                    # be cited like any other.
                    latex = text_of(e.find('.//tex-math'))
                    add(latex or text_of(e), section, 'equation',
                        latex=latex or text_of(e), number=text_of(e.find('label')))
                elif tag in ('fig','table-wrap','supplementary-material'):
                    label = text_of(e.find('label'))
                    caption = ' '.join(filter(None,[label,text_of(e.find('caption'))]))
                    rows = [[text_of(c) for c in row if c.tag in ('td','th')] for row in e.findall('.//tr')]
                    if rows:
                        add(caption, section, 'caption', label=label)
                        add('', section, 'table', table=rows)
                    else:
                        add(caption, section, 'caption', label=label)
                    assets.append({'id':e.get('id',''),'label':label,
                                   'kind':tag,'available':'caption_in_manuscript','href':e.get('{http://www.w3.org/1999/xlink}href','')})
                elif tag in ('boxed-text','list','list-item'):
                    walk(e, section, depth)
        body = root.find('body')
        if body is not None:
            walk(body,'Body')
        for e in root.findall('.//ref-list/ref'):
            raw_ref = text_of(e)
            doi = next((text_of(x) for x in e.findall('.//pub-id') if x.get('pub-id-type')=='doi'), '')
            if not doi:
                match=re.search(r'(?:doi:\s*|https?://(?:dx\.)?doi\.org/)(10\.\d{4,9}/\S+)',raw_ref,re.I)
                doi=match.group(1).rstrip('.,') if match else ''
            references.append({'id':e.get('id',f'r{len(references)+1}'),'label':text_of(e.find('label')),
                               'raw':raw_ref,'doi':doi,'title':text_of(e.find('.//article-title')),'year':text_of(e.find('.//year'))})
        # Supporting captions can occur in back matter instead of the body.
        existing = {a['id'] for a in assets}
        for e in root.findall('./back//supplementary-material'):
            if e.get('id') not in existing:
                holder=ET.Element('wrapper')
                holder.append(e)
                walk(holder,'Supporting information')
        warnings.append('Publisher XML supplies text and references. PDF page links are conservative text matches, not complete layout reconstruction.')
    else:
        title = title or doc.get('title') or pdf.stem
        section = 'Front matter'
        for b in doc['blocks']:
            kind = b['kind']['type']
            if kind == 'heading':
                section = b['text']
            rows = b.get('table', {}).get('rows')
            text = b.get('text', '') or ''
            if kind == 'reference':
                # The box travels with the reference. A reference is a piece of
                # the paper like any other and a reader following a citation
                # wants it highlighted on the page it is printed on, so where
                # the converter saw it is kept rather than re-found by text.
                ref = b.get('reference', {})
                references.append({'id': f'r{len(references)+1}', **ref, 'raw': text,
                                   'page': b.get('page', 0) + 1, 'bbox': b.get('bbox')})
                continue
            math = b.get('math') or {}
            if kind == 'equation':
                # The converter returns an equation with no text and its LaTeX
                # under `math`, so an importer that reads `text` alone dropped
                # every displayed equation in the paper — eleven of eleven in
                # the antenna manuscript — and the reviewers argued about
                # algebra they had never been shown.
                add(math.get('latex') or '', section, 'equation', page=b.get('page', 0) + 1,
                    bbox=b.get('bbox'), latex=math.get('latex') or '',
                    number=math.get('number') or '', confidence=math.get('confidence'))
                continue
            if rows and not text.strip():
                add('', section, 'table', page=b.get('page', 0) + 1, bbox=b.get('bbox'), table=rows)
                continue
            add(text, section, kind, page=b.get('page', 0) + 1, bbox=b.get('bbox'),
                table=rows, level=b['kind'].get('level'))
        warnings.append('PDF-only references and structure are provisional. Check extraction against the original before review.')
    for ref in references:
        add(ref['raw'], 'References', 'reference', source_id=ref['id'], page=ref.get('page'),
            bbox=ref.get('bbox'))
    if supplements:
        for path in sorted(Path(supplements).glob('*')):
            if path.suffix.lower() not in ('.docx','.png','.tif','.tiff','.jpg','.pdf'):
                continue
            dest=output/'supplements'/path.name
            dest.parent.mkdir(exist_ok=True)
            shutil.copy2(path,dest)
            name = document_name(path.stem)
            if path.suffix.lower()=='.docx':
                with zipfile.ZipFile(path) as z:
                    item=z.getinfo('word/document.xml')
                    if item.file_size>5_000_000:
                        raise ValueError('Supplement document exceeds text limit')
                    root=ET.fromstring(z.read(item))
                ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                section = 'Supplement ' + path.stem
                add(path.stem, section, 'title', document=name, source_file=path.name)
                for e in root.find('w:body',ns):
                    if e.tag.endswith('}tbl'):
                        rows=[[' '.join(text_of(p) for p in cell.findall('w:p',ns)) for cell in row.findall('w:tc',ns)] for row in e.findall('w:tr',ns)]
                        add('', section, 'table', document=name, table=rows, source_file=path.name)
                    elif e.tag.endswith('}p'):
                        add(text_of(e), section, 'supplement', document=name,
                            source_file=path.name)
            elif path.suffix.lower() in ('.tif','.tiff'):
                from PIL import Image
                image=Image.open(path)
                image.thumbnail((1800,1800))
                image.convert('RGB').save(dest.with_suffix('.png'))
            assets.append({'id':path.stem,'label':path.name,'kind':'supplement','path':'supplements/'+path.name,'preview':'supplements/'+path.with_suffix('.png').name if path.suffix.lower() in ('.tif','.tiff') else None,'available':'file','document':name if path.suffix.lower()=='.docx' else None})
    documents = collections.OrderedDict([(paper_base.MAIN_DOCUMENT, pieces)])
    documents.update(supplement_pieces)
    rows = paper_base.write_base(output, documents)
    by_document = collections.defaultdict(list)
    for row in rows:
        by_document[row['document']].append(row)
    # Every piece in these lists rendered — `add` drops the ones that do not —
    # so a document's pieces and its rows are the same sequence, and every row
    # can be counted on the text that actually went into the file rather than
    # the text it was built from. That text is read back out of the written
    # document and kept nowhere else: the Markdown is the manuscript.
    written = []
    for name, document_pieces in documents.items():
        lines = (output / name).read_text(encoding='utf-8').split('\n')
        for row in by_document[name]:
            start, end = row['lines']
            written.append((row, '\n'.join(lines[start - 1:end])))
    main = [(row, text) for row, text in written
            if row['document'] == paper_base.MAIN_DOCUMENT]
    sections = collections.OrderedDict()
    for row, _ in main:
        sections.setdefault(row['section'], []).append(row['lines'][0])
    stats = {
        'total_words': sum(len(words(text)) for row, text in written
                           if row['kind'] != 'heading'),
        'main_words': sum(len(words(text)) for row, text in main
                          if row['kind'] not in ('heading', 'reference', 'title')),
        'reference_words': sum(len(words(text)) for row, text in written
                               if row['kind'] == 'reference'),
        'reference_entries': len(references), 'pieces': len(rows),
        'pdf_pages': len(raw_pages),
        'lines': {name: len((output / name).read_text(encoding='utf-8').splitlines())
                  for name in documents},
        # A section is counted by the lines its pieces start on, because a line
        # is what a reader of this manuscript can point at.
        'sections': {name: {'pieces': len(starts),
                            'words': sum(len(text.split()) for row, text in main
                                         if row['lines'][0] in starts
                                         and row['kind'] != 'heading')}
                     for name, starts in sections.items()},
        'convention': ('Unicode words with internal apostrophes and hyphens. Includes captions, '
                       'tables and equations. References reported separately. No quality '
                       'inference from counts.')}
    uncaptioned, captioned = missing_figures(pieces)
    gaps = {}
    if uncaptioned:
        gaps['figure_captions'] = (
            'The text points at ' + ', '.join('Figure ' + str(n) for n in uncaptioned)
            + ', which no caption in this import carries. Captions were found for '
            + (', '.join(str(n) for n in captioned) or 'no figures')
            + '. A figure with no caption here may still be captioned in the PDF: read the page '
              'before reporting it as undescribed.')
    paper_id = digest(json.dumps({'pdf': digest(pdf.read_bytes()),
                                  'xml': digest(Path(xml).read_bytes()) if xml else None},
                                 sort_keys=True))[:16]
    manifest = {
        'schema_version': paper_base.BASE_SCHEMA_VERSION, 'paper_id': paper_id, 'title': title,
        'source_url': source_url,
        'source': {'file': 'original/original.pdf', 'sha256': digest(pdf.read_bytes()),
                   'pages': len(raw_pages),
                   'xml': 'original/original.xml' if xml else None,
                   'xml_sha256': digest(Path(xml).read_bytes()) if xml else None},
        'parser': 'rustypaper ' + getattr(rustypaper, '__version__', 'unknown'),
        'documents': list(documents),
        # The captioned figures are counted on their own because the kinds do
        # not answer the question: a caption arrives as a `figure` piece or as
        # a `caption` piece depending on what the converter saw around it, so
        # neither count tells a reader how many figures this paper describes.
        'counts': {'pieces': len(rows), 'references': len(references),
                   'figure_captions': len(captioned),
                   **collections.Counter(row['kind'] for row in rows)},
        'pieces_per_source': dict(collections.Counter(row['source'] for row in rows)),
        'source_values': {
            'text': "the document's own text layer, as the converter read it",
            'ai': 'read from a page image by a model: no piece carries this yet',
            'ai-reconstructed': 'not printed in the source, rebuilt from context: none yet'},
        'assets': assets, 'statistics': stats, 'warnings': warnings,
        'known_gaps': dict(KNOWN_GAPS, **gaps),
        'diagnostics': ['converter.json'],
        'fingerprint_rule': ('sha256 of the piece\'s lines in their document, NFKC-normalised, '
                             'whitespace runs collapsed, dash and quote variants folded, soft '
                             'hyphens and zero-width characters dropped, case kept'),
    }
    write(output / 'references.json', references)
    write(output / 'statistics.json', stats)
    # POSIX keys: the scaffold inventory check builds its names with as_posix,
    # so a manifest keyed with the OS separator read as a changed inventory on
    # Windows and refused every run over a scaffold manuscript.
    manifest['files'] = {relative_name(p, output): digest(p.read_bytes())
                         for p in sorted(output.rglob('*'))
                         if p.is_file() and p.name != 'manifest.json'}
    write(output / 'manifest.json', manifest)
    # The manifest, which is the import's own account of what it wrote: the
    # manuscript itself is `paper.md` and its map, and a caller that wants the
    # text reads it with `paper_base.load`.
    return manifest
