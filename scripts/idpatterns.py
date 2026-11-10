"""Identifier / address patterns shared by crops.py (masking scan lines) and build_site.py (text redaction + guard).

These are the things the archive's human transcripts mostly leave out: prison ID numbers, cell numbers, P.O. boxes,
mailing addresses. Louis approved blacking them out of everything we publish, even where the archive's own text contains
them (2026-11-10): `redact` replaces each matched span of our public text with REDACTION, crops.py paints the same lines
black on the scans. Patterns tolerate dash variants (em/en dashes) and OCR confusions (l/I for 1, O for 0) inside IDs.
A hit is a *backstop*: Vision misreads handwriting, so the main defence is cropping and publishing only the text span
the archive itself published (docs/DATA_CONTRACTS.md sections 4 and 6).
"""
import re

REDACTION = "███"
US_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
             "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
DASH = "[-‐‑‒–—―−]"
DIG = "[\\dlIO]"  # a digit or its common OCR confusions; every ID below still needs several true digits


def _state(st):
    """Regex for one state abbreviation: first letter capital, second either case, optional dots ('N.C.', 'In.')."""
    return r"\.?".join(c if i == 0 else f"[{c}{c.lower()}]" for i, c in enumerate(st)) + r"\.?"


_ST = "(?:" + "|".join(_state(s) for s in US_STATES) + ")"
_ZIP = r"(?<![\w-])\d{5}(?:-\d{4})?(?!\w)"

ID_PATTERNS = [
    re.compile(r"#\s*\w*\d{4,}"),                                                            # #111204, #93A9739
    re.compile(r"\b[A-Z]{1,2}" + DASH + r"?(?=" + DIG + r"*\d" + DIG + r"*\d" + DIG + r"*\d)" + DIG + r"{4,}(?!\w)"),  # E-22600, H-4577l
    re.compile(r"\b[A-Z]{1,2}\d{0,2}" + DASH + r"\d{3,}\b"),                                 # A2-206 (cell)
    re.compile(r"\b\d{6,}\b"),                                                               # bare 6+ digit ID
    re.compile(r"\bP\.?\s*O\.?\s*Box", re.I),                                                # P.O. Box
    re.compile(r"\bBox\s+[A-Z]?" + DASH + r"?\d+", re.I),                                    # Box 730, Box E-22600
    re.compile(r"\bBox\s+[A-Z]\b"),                                                          # Box E
    # "City, ST 12345" (city words optional): the mailing-address tail
    re.compile(r"(?:\b[A-Z][a-z]+\s+){0,2}(?:\b[A-Z][a-z]+,\s*)?" + _ST + r",?\s*\d{5}(?:-\d{4})?(?!\w)"),
    re.compile(r"(?<![\w-])\d{5}(?:-\d{4})?\s*,?\s+" + _ST + r"(?!\w)"),                      # 12788 NY
]
# Crop-only: a header line naming the facility (a text guard cannot tell a header from body text).
FACILITY_PATTERN = re.compile(r"\b(?:Correctional\s+Facility|Facility)\b", re.I)
# Crop-only whole-line address rules (a scan line is blacked out entirely): a standalone ZIP, or "City, ST".
ADDRESS_LINE_PATTERNS = [
    re.compile(_ZIP),
    # "City, ST" only when the state is all-caps ("NY", "N.C.") or capital+dot ("In."), and the line/number follows:
    # "masses, In left reflecting" (ordinary prose) must not match.
    re.compile(r"[A-Za-z]{2,},\s*(?:" + "|".join(r"\.?".join(st) + r"\.?" for st in US_STATES) + r"|[A-Z][a-z]\.)"
               r"(?![A-Za-z])(?=\s*(?:\d|$|[.,;]))"),
]


def is_address_line(text):
    """True if a scan line looks like a mailing-address line (standalone 5-digit/ZIP+4 token, or 'City, ST')."""
    return any(p.search(text) for p in ADDRESS_LINE_PATTERNS)


def _spans(text):
    """Merged [start, end) spans of every ID_PATTERNS match in text."""
    raw = sorted((m.start(), m.end()) for p in ID_PATTERNS for m in p.finditer(text) if m.end() > m.start())
    out = []
    for a, b in raw:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def find_hits(text):
    """The merged matched strings of ID_PATTERNS in text, in order (empty list = clean)."""
    return [text[a:b] for a, b in _spans(text)]


def redact(text):
    """Replace every merged ID/address span of text with REDACTION. Returns (new text, number of spans replaced)."""
    spans, out, pos = _spans(text), [], 0
    for a, b in spans:
        out += [text[pos:a], REDACTION]
        pos = b
    out.append(text[pos:])
    return "".join(out), len(spans)
