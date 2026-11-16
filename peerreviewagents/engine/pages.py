"""Render one page of the original PDF as an image, once, and keep it.

Two stages look at the pages: the reader, which repairs the converted text
against them before the base is sealed, and an investigation, which lets a
reviewer check an equation or a figure against the printed page. They render
the same file at the same scale through this one function, so a reviewer who
disputes an equation sees the image the reader worked from and not a second
rendering of it that might differ in resolution or page numbering.
"""
import subprocess
from pathlib import Path

#: The long edge, in pixels. Big enough to read a subscript in a displayed
#: equation and small enough that a page is a few hundred kilobytes.
SCALE = 1500

ORIGINAL = 'original/original.pdf'


def page_image(root, page, output, pages=0):
    """The PNG of *page* of the manuscript PDF under *root*, cached in *output*.

    *pages* is the page count the folder claims; a page outside it is refused
    rather than rendered, because ``pdftoppm`` answers an impossible page with
    an empty file rather than an error. The render happens once: the second
    call for a page returns the file the first one wrote.
    """
    if type(page) is not int or not 1 <= page <= (pages or 0):
        raise ValueError('Unknown page')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f'page_{page:03d}.png'
    if not target.exists():
        subprocess.run(
            ['pdftoppm', '-f', str(page), '-l', str(page), '-scale-to', str(SCALE),
             '-singlefile', '-png', str(Path(root) / ORIGINAL), str(target.with_suffix(''))],
            check=True, capture_output=True, timeout=30)
    return target


def page_count(manifest):
    """How many pages the original PDF has, as the manifest or draft records it."""
    statistics = manifest.get('statistics') or {}
    return int(statistics.get('pdf_pages') or manifest.get('page_count') or 0)
