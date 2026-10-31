"""A QR code as a standalone SVG (0.5.1: the Signal link code), drawn on the server with the vendored
qrcodegen.py (Project Nayuki, MIT). The page shows it as an <img> from a data: URL, so nothing in the SVG
is ever parsed as HTML on the page, and the page needs no SVG namespace or drawing code of its own.

The SVG is built only from the module grid (numbers), never from the encoded text, so the text can't
inject markup. Black on white with a 4-module quiet zone, as phone cameras expect.
"""
from vendor import qrcodegen

BORDER = 4


def matrix(text, ecc="medium"):
    """The QR code for text: a list of rows of booleans (True = dark), without the quiet zone."""
    level = {"low": qrcodegen.QrCode.Ecc.LOW, "medium": qrcodegen.QrCode.Ecc.MEDIUM,
             "quartile": qrcodegen.QrCode.Ecc.QUARTILE, "high": qrcodegen.QrCode.Ecc.HIGH}[ecc]
    code = qrcodegen.QrCode.encode_text(text, level)
    n = code.get_size()
    return [[code.get_module(x, y) for x in range(n)] for y in range(n)]


def svg(text, ecc="medium", border=BORDER):
    """A standalone SVG document (str) for text: one <rect> background and one <path> of 1x1 squares."""
    grid = matrix(text, ecc)
    n = len(grid) + 2 * border
    parts = [f"M{x + border},{y + border}h1v1h-1z" for y, row in enumerate(grid) for x, dark in enumerate(row) if dark]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{n * 6}" height="{n * 6}" '
            f'shape-rendering="crispEdges"><rect width="{n}" height="{n}" fill="#ffffff"/>'
            f'<path fill="#000000" d="{"".join(parts)}"/></svg>')
