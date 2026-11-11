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
from .storage import digest, write

POPPLER_MISSING = ('Poppler (pdftotext) is not installed. Install it with `brew install poppler` '
                   'on macOS, `sudo apt install poppler-utils` on Debian or Ubuntu, or '
                   '`choco install poppler` on Windows, then run the command again.')


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


def import_paper(pdf, output, xml=None, title=None, source_url='', supplements=None):
    import rustypaper
    pdf, output = Path(pdf), Path(output)
    if (output / 'paper.json').exists():
        raise ValueError('Workspace already contains an immutable manuscript')
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf, output / 'original.pdf')
    markdown, doc = rustypaper.convert(str(pdf))
    write(output / 'converter.json', doc)
    (output / 'converter.md').write_text(markdown)
    raw_pages = page_text(pdf)
    if raw_pages and not raw_pages[-1].strip():
        raw_pages.pop()
    normalized_pages = [norm(page) for page in raw_pages]
    blocks, references, assets, warnings = [], [], [], []

    def locate(text):
        clean = norm(text)
        if len(clean) < 35:
            return None
        probes = [clean[i:i+55] for i in range(0, min(len(clean), 500)-54, 55)]
        hits = [(sum(probe in page for probe in probes), number+1) for number, page in enumerate(normalized_pages)]
        best = max(hits, default=(0, None))
        return best[1] if best[0] >= 2 else None

    def add(text, section, kind='paragraph', source_id='', page=None, bbox=None, citations=None, document='main', table=None):
        if not text.strip():
            return
        bid = f'b{len(blocks)+1:04d}'
        located = page or (locate(text) if document=='main' else None)
        blocks.append({'id':bid,'text':text,'sha256':digest(text),'section':section,'kind':kind,
                       'source_id':source_id,'page':located,
                       'page_mapping':'converter' if page else 'text_anchor' if located else 'unavailable',
                       'bbox':bbox,'citations':citations or [],'document':document,'table':table})

    if xml:
        xml = Path(xml)
        raw = xml.read_bytes()
        if re.search(br'<!ENTITY\s', raw, re.I):
            raise ValueError('XML entity declarations are not accepted')
        root = ET.fromstring(raw)
        shutil.copy2(xml, output / 'original.xml')
        source_title = text_of(root.find('.//article-title'))
        title = title or source_title
        for p in root.findall('./front/article-meta/abstract/p'):
            add(text_of(p), 'Abstract', source_id=p.get('id',''))

        def walk(parent, section):
            for e in parent:
                tag = e.tag.rsplit('}',1)[-1]
                if tag == 'sec':
                    heading = text_of(e.find('title')) or section
                    add(heading, heading, 'heading', e.get('id',''))
                    walk(e, heading)
                elif tag == 'p':
                    cites = [rid for x in e.findall('.//xref') if x.get('ref-type')=='bibr' for rid in x.get('rid','').split()]
                    add(text_of(e), section, source_id=e.get('id',''), citations=cites)
                elif tag in ('fig','table-wrap','supplementary-material'):
                    label = text_of(e.find('label'))
                    caption = ' '.join(filter(None,[label,text_of(e.find('caption'))]))
                    rows = [[text_of(c) for c in row if c.tag in ('td','th')] for row in e.findall('.//tr')]
                    body = caption + ('\n' + '\n'.join('\t'.join(row) for row in rows) if rows else '')
                    add(body, section, 'table' if rows else 'caption', e.get('id',''), table=rows or None)
                    assets.append({'id':e.get('id',''),'label':label,'block_id':blocks[-1]['id'] if body.strip() else None,
                                   'kind':tag,'available':'caption_in_manuscript','href':e.get('{http://www.w3.org/1999/xlink}href','')})
                elif tag in ('boxed-text','list','list-item'):
                    walk(e,section)
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
            kind=b['kind']['type']
            if kind=='heading':
                section=b['text']
            rows=b.get('table',{}).get('rows')
            text=b.get('text','') or ('\n'.join('\t'.join(row) for row in rows) if rows else '')
            if kind=='reference':
                ref=b.get('reference',{})
                references.append({'id':f'r{len(references)+1}',**ref,'raw':text})
                continue
            add(text,section,kind,page=b.get('page',0)+1,bbox=b.get('bbox'),table=rows)
        warnings.append('PDF-only references and structure are provisional. Check extraction against the original before review.')
    for ref in references:
        add(ref['raw'],'References','reference',ref['id'])
    if supplements:
        for path in sorted(Path(supplements).glob('*')):
            if path.suffix.lower() not in ('.docx','.png','.tif','.tiff','.jpg','.pdf'):
                continue
            dest=output/'supplements'/path.name
            dest.parent.mkdir(exist_ok=True)
            shutil.copy2(path,dest)
            if path.suffix.lower()=='.docx':
                with zipfile.ZipFile(path) as z:
                    item=z.getinfo('word/document.xml')
                    if item.file_size>5_000_000:
                        raise ValueError('Supplement document exceeds text limit')
                    root=ET.fromstring(z.read(item))
                ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                for e in root.find('w:body',ns):
                    if e.tag.endswith('}tbl'):
                        rows=[[' '.join(text_of(p) for p in cell.findall('w:p',ns)) for cell in row.findall('w:tc',ns)] for row in e.findall('w:tr',ns)]
                        add('\n'.join('\t'.join(row) for row in rows),'Supplement '+path.stem,'table',document=path.name,table=rows)
                    elif e.tag.endswith('}p'):
                        add(text_of(e),'Supplement '+path.stem,'supplement',document=path.name)
            elif path.suffix.lower() in ('.tif','.tiff'):
                from PIL import Image
                image=Image.open(path)
                image.thumbnail((1800,1800))
                image.convert('RGB').save(dest.with_suffix('.png'))
            assets.append({'id':path.stem,'label':path.name,'kind':'supplement','path':'supplements/'+path.name,'preview':'supplements/'+path.with_suffix('.png').name if path.suffix.lower() in ('.tif','.tiff') else None,'available':'file'})
    sections=collections.defaultdict(list)
    for b in blocks:
        sections[b['section']].append(b['id'])
    stats={'total_words':sum(len(words(b['text'])) for b in blocks if b['kind']!='heading'),
           'main_words':sum(len(words(b['text'])) for b in blocks if b['kind'] not in ('heading','reference') and b['document']=='main'),
           'reference_words':sum(len(words(b['text'])) for b in blocks if b['kind']=='reference'),
           'reference_entries':len(references),'blocks':len(blocks),'pdf_pages':len(raw_pages),
           'sections':{name:{'blocks':len(ids),'words':sum(len(words(b['text'])) for b in blocks if b['id'] in ids and b['kind']!='heading')} for name,ids in sections.items()},
           'convention':'Unicode words with internal apostrophes and hyphens. Includes captions and tables. References reported separately. No quality inference from counts.'}
    paper={'schema_version':1,'title':title,'source_url':source_url,'source_sha256':digest(pdf.read_bytes()),
           'parser':'rustypaper '+getattr(rustypaper,'__version__','unknown'),'xml_sha256':digest(Path(xml).read_bytes()) if xml else None,
           'blocks':blocks,'references':references,'assets':assets,'sections':dict(sections),'statistics':stats,'warnings':warnings}
    paper['id']=digest(json.dumps({'pdf':paper['source_sha256'],'xml':paper['xml_sha256']},sort_keys=True))[:16]
    write(output/'paper.json',paper)
    write(output/'references.json',references)
    write(output/'statistics.json',stats)
    (output/'manuscript.md').write_text('\n\n'.join(f"[{b['id']} | {b['section']} | {b['document']} page {b['page'] or '?'}]\n{b['text']}" for b in blocks))
    for i,page in enumerate(raw_pages,1):
        p=output/'pages'/f'{i:03d}.txt'
        p.parent.mkdir(exist_ok=True)
        p.write_text(page)
    write(output/'manifest.json',{'paper_id':paper['id'],'files':{str(p.relative_to(output)):digest(p.read_bytes()) for p in sorted(output.rglob('*')) if p.is_file() and p.name!='manifest.json'}})
    return paper
