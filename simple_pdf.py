"""A small PDF writer: text, lines and filled boxes on US Letter pages, in Helvetica.

Enough for a purchase order, with nothing to install: the fonts are PDF's built-in Helvetica
and Helvetica-Bold (every PDF reader has them), so none are embedded. Text is Windows-1252
(Western European); a character outside it is written as "?".

    pdf = Pdf()
    pdf.text(72, 720, "Purchase order", size=18, bold=True)
    data = pdf.render()      # bytes
"""

PAGE_WIDTH, PAGE_HEIGHT = 612.0, 792.0  # US Letter, in points (1/72 inch)

# Character widths of Helvetica and Helvetica-Bold for the printable ASCII characters (space to
# ~), in thousandths of the font size (from the fonts' published metrics).
_REGULAR = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
            556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
            1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
            667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
            333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
            556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
_BOLD = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
         556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
         975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
         667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
         333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
         611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]
_OTHER = 556  # accented letters and symbols: about a lower-case letter


def clean(text):
    """Text as the PDF can show it: one line, in Windows-1252, anything else as "?"."""
    text = " ".join(str(text if text is not None else "").split())
    return text.encode("cp1252", "replace").decode("cp1252")


def width(text, size=10, bold=False):
    """How wide `text` is, in points."""
    table = _BOLD if bold else _REGULAR
    return sum(table[ord(c) - 32] if 32 <= ord(c) <= 126 else _OTHER for c in clean(text)) * size / 1000.0


def wrap(text, max_width, size=10, bold=False):
    """`text` broken into lines no wider than `max_width` (a word too long for a line is cut)."""
    lines = []
    for paragraph in str(text if text is not None else "").replace("\r", "").split("\n"):
        line = ""
        for word in clean(paragraph).split(" "):
            if width(word, size, bold) > max_width:  # a word too long for any line: cut into pieces that fit
                if line:
                    lines.append(line)
                    line = ""
                while width(word, size, bold) > max_width and len(word) > 1:
                    cut = len(word) - 1
                    while cut > 1 and width(word[:cut], size, bold) > max_width:
                        cut -= 1
                    lines.append(word[:cut])
                    word = word[cut:]
            trial = f"{line} {word}".strip()
            if line and width(trial, size, bold) > max_width:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
    return lines or [""]


def _escape(text):
    return clean(text).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("cp1252", "replace")


class Pdf:
    """Pages drawn with their origin at the bottom left, as PDF has it."""

    def __init__(self, title="", author=""):
        self.title, self.author = title, author
        self.pages = [[]]  # each a list of content-stream fragments (bytes)

    @property
    def page_number(self):
        return len(self.pages)

    def new_page(self):
        self.pages.append([])

    def text(self, x, y, text, size=10, bold=False, align="left", gray=0.0, page=None):
        """One line of text with its baseline at y; align "right" or "center" puts x at that edge."""
        if align == "right":
            x -= width(text, size, bold)
        elif align == "center":
            x -= width(text, size, bold) / 2
        ops = b"BT %.2f g /%s %.2f Tf %.2f %.2f Td (" % (gray, b"F2" if bold else b"F1", size, x, y) \
            + _escape(text) + b") Tj ET\n"
        (self.pages[page] if page is not None else self.pages[-1]).append(ops)

    def line(self, x1, y1, x2, y2, thickness=0.5, gray=0.0):
        self.pages[-1].append(b"%.2f G %.2f w %.2f %.2f m %.2f %.2f l S\n" % (gray, thickness, x1, y1, x2, y2))

    def box(self, x, y, w, h, gray=0.93):
        """A filled box, its bottom-left corner at (x, y)."""
        self.pages[-1].append(b"%.2f g %.2f %.2f %.2f %.2f re f\n" % (gray, x, y, w, h))

    def render(self):
        objects = []  # the bodies of objects 1..n

        def add(body):
            objects.append(body)
            return len(objects)

        catalog, pages = add(None), add(None)
        regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        kids = []
        for content in self.pages:
            stream = b"".join(content)
            stream_id = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream")
            kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R /Resources "
                            b"<< /Font << /F1 %d 0 R /F2 %d 0 R >> >> >>"
                            % (pages, PAGE_WIDTH, PAGE_HEIGHT, stream_id, regular, bold)))
        objects[pages - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
            b" ".join(b"%d 0 R" % k for k in kids), len(kids))
        objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages
        info = add(b"<< /Title (" + _escape(self.title) + b") /Author (" + _escape(self.author)
                   + b") /Producer (Yeems Core) >>")
        out, offsets = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"), []
        for number, body in enumerate(objects, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
        out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
            len(objects) + 1, catalog, info, xref)
        return bytes(out)
