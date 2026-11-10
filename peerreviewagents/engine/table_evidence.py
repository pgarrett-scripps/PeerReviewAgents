"""Read-only, source-addressed evidence from reported-results workbooks."""
import json
from pathlib import Path

from .storage import digest


def scalar(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    raise ValueError('Unsupported source cell type: ' + type(value).__name__)


def build_catalog(paths):
    """Retain literal cells and merges without inferring or filling row labels."""
    from openpyxl import load_workbook
    paths = sorted(Path(p).resolve() for p in paths)
    if len(set(paths)) != len(paths):
        raise ValueError('Duplicate workbook source')
    books, blocks = [], []
    for index, path in enumerate(paths, 1):
        source_hash = digest(path.read_bytes())
        values = load_workbook(path, data_only=True, keep_links=False)
        formulas = load_workbook(path, data_only=False, keep_links=False)
        book = {'id':f't{index:02}', 'file':path.name, 'sha256':source_hash, 'sheets':[]}
        try:
            for number, sheet in enumerate(values.worksheets, 1):
                source = formulas[sheet.title]
                prefix = f'{book["id"]}-s{number:02}'
                record = {'id':prefix, 'name':sheet.title,
                          'dimensions':sheet.calculate_dimension(),
                          'merged_ranges':[str(r) for r in source.merged_cells.ranges],
                          'formula_cells':[], 'rows':[]}
                for row in sheet.iter_rows():
                    cells = {}
                    for cell in row:
                        original = source[cell.coordinate]
                        if cell.value is not None or original.data_type == 'f':
                            cells[cell.coordinate] = scalar(cell.value)
                        if original.data_type == 'f':
                            record['formula_cells'].append({
                                'address':cell.coordinate, 'formula':str(original.value),
                                'cached_value_available':cell.value is not None})
                    if not cells:
                        continue
                    bid = f'{prefix}-r{row[0].row:03}'
                    text = ' '.join(k + '=' + json.dumps(v, ensure_ascii=False, allow_nan=False)
                                    for k, v in cells.items())
                    block = {'id':bid, 'kind':'source_table_row', 'text':text,
                             'sha256':digest(text), 'source_sha256':source_hash,
                             'file':path.name, 'sheet':sheet.title, 'row':row[0].row,
                             'cells':cells}
                    record['rows'].append(bid)
                    blocks.append(block)
                book['sheets'].append(record)
        finally:
            values.close()
            formulas.close()
        if digest(path.read_bytes()) != source_hash:
            raise ValueError('Workbook changed during evidence extraction')
        books.append(book)
    return {'version':'table-evidence-v1', 'books':books, 'blocks':blocks,
            'limitations':[
                'Values are cached source cells, not recomputed analysis results.',
                'Omitted coordinates are blank. Blank is not zero or a repeated label.',
                'Headers must be read from their own rows. Merged ranges are recorded separately.',
                'Cell types and values are retained. Display formatting and scientific interpretation are not validated.',
                'Missing formula caches remain null. Source formula text is recorded but never executed.']}


def catalog_text(catalog):
    """Compact delivery with all populated cells, explicit row IDs, and context."""
    by = {b['id']:b for b in catalog['blocks']}
    lines = ['REPORTED TABLE CELL REGISTER', *catalog['limitations']]
    for book in catalog['books']:
        lines.append(f"WORKBOOK {book['id']}: {book['file']} SHA256 {book['sha256']}")
        for sheet in book['sheets']:
            lines.append('SHEET ' + json.dumps({k:v for k,v in sheet.items() if k != 'rows'}, ensure_ascii=False))
            lines.extend(bid + ' ' + by[bid]['text'] for bid in sheet['rows'])
    return '\n'.join(lines)


def evidence_paper(paper, catalog):
    existing = {b['id'] for b in paper['blocks']}
    incoming = [b['id'] for b in catalog['blocks']]
    if len(set(incoming)) != len(incoming) or existing.intersection(incoming):
        raise ValueError('Evidence source IDs collide')
    return {**paper, 'blocks':paper['blocks'] + catalog['blocks']}
