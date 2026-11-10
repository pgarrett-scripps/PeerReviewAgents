"""Reduce duplicate source representations without shortening scientific text."""


def text_block(block):
    """Omit a row array only when the complete same rows occur in block text."""
    rows = block.get('table')
    if (not isinstance(rows, list) or not rows or
            any(not isinstance(row, list) or any(not isinstance(cell, str) for cell in row) for row in rows)):
        return dict(block)
    rendered = '\n'.join('\t'.join(row) for row in rows)
    if not rendered.strip() or rendered not in block.get('text', ''):
        return dict(block)
    result = {key: value for key, value in block.items() if key != 'table'}
    result['table_representation'] = 'Complete tab-separated rows are retained in text.'
    return result
