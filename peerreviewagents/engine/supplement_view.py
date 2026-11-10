"""Render one verified supplement page without modifying manuscript sources."""
import re
import subprocess
from pathlib import Path
from .storage import read, digest


def supplement_page(root, paper, asset_id, output, page=1):
    root, output = Path(root).resolve(), Path(output)
    if type(page) is not int or not 1 <= page <= 100:
        raise ValueError('Supplement page must be between 1 and 100')
    asset = next((a for a in paper.get('assets', []) if a['id'] == asset_id), None)
    if asset is None:
        raise ValueError('Unknown supplement ID')
    relative = asset.get('path')
    pdf = bool(relative and Path(relative).suffix.lower() == '.pdf')
    relative = relative if pdf else asset.get('preview')
    if not relative:
        raise ValueError('No PDF or image preview is available for this asset')
    source = (root/relative).resolve()
    expected = read(root/'manifest.json')['files'].get(relative)
    if not source.is_relative_to(root) or not expected or not source.is_file() or digest(source.read_bytes()) != expected:
        raise ValueError('Supplement source is not verified')
    if not pdf:
        if page != 1:
            raise ValueError('The image preview has one page')
        return source, {'label': asset['label'], 'source_sha256': expected,
                        'asset_id': asset_id, 'page': 1, 'pages': 1,
                        'limitation': 'This is the supplied image preview.'}
    info = subprocess.run(['pdfinfo', str(source)], check=True, capture_output=True,
                          text=True, timeout=20).stdout
    match = re.search(r'^Pages:\s+(\d+)', info, re.M)
    if not match or not page <= int(match[1]):
        raise ValueError('Requested page is outside the PDF supplement')
    output.mkdir(parents=True, exist_ok=True)
    target = output/f'supplement_{digest(asset_id)[:16]}_{page:03d}.png'
    subprocess.run(['pdftoppm', '-f', str(page), '-l', str(page), '-scale-to', '1500',
                    '-singlefile', '-png', str(source), str(target.with_suffix(''))],
                   check=True, capture_output=True, timeout=30)
    return target, {'label': f"{asset['label']} page {page}", 'source_sha256': expected,
                    'asset_id': asset_id, 'page': page, 'pages': int(match[1]),
                    'limitation': 'Only the requested original PDF page was inspected. Other pages remain unexamined.'}
