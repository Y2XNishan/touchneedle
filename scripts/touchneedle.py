#!/usr/bin/env python3
"""Verify that the citations in a document are real, correctly described, and
consistently used.

Reads a prose reference list out of a Markdown or .docx document -- author-date
(Harvard, APA, Chicago author-date), numeric (IEEE, Vancouver/AMA), MLA, or
footnote styles (Chicago notes, MHRA) -- routes each entry to whichever
authority can actually confirm it -- arXiv, Crossref, OpenAlex, the IETF
datatracker, or the live web -- and reports what does not line up. Also
cross-checks in-text citations against the list in both directions.

Standard library only. Python 3.11+.

  touchneedle.py check  DOC [--out report.md] [--json data.json]
  touchneedle.py claims DOC [--out claims.md]

Exit status is 2 when the run found problems worth a human look, 0 when clean.
"""

import argparse
import concurrent.futures
import dataclasses
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from typing import Any

__version__ = "0.3.0"
# Goes out in the User-Agent below, so it has to point somewhere a rate-limited
# API operator can actually reach a human. The same URL is in pyproject.toml,
# the README and the plugin manifests; if the repository moves, they all move.
REPO_URL = "https://github.com/nicoleman0/touchneedle"
UA = f"touchneedle/{__version__} (+{REPO_URL}; academic reference verification)"
CACHE_TTL = 7 * 24 * 3600

# Verification outcomes, worst first -- the report sorts by this order.
SEVERITY = {
    "MISMATCH": 0,
    "NOT_FOUND": 1,
    "LINK_DEAD": 2,
    "STALE": 3,
    "PARTIAL": 4,
    "LINK_MOVED": 5,
    "UNVERIFIABLE": 6,
    "VERIFIED": 7,
}
PROBLEM_STATUSES = {"MISMATCH", "NOT_FOUND", "LINK_DEAD", "STALE"}


# --------------------------------------------------------------------------
# text loading and cleanup
# --------------------------------------------------------------------------

def load_text(path: str) -> str:
    """Return document text as Markdown, converting .docx through pandoc."""
    if path.lower().endswith((".docx", ".odt", ".rtf")):
        if not shutil.which("pandoc"):
            sys.exit(f"error: {path} needs pandoc to convert, and pandoc is not on PATH")
        out = subprocess.run(
            ["pandoc", path, "-t", "markdown", "--wrap=none"],
            capture_output=True, text=True, encoding="utf-8"
        )
        if out.returncode != 0:
            sys.exit(f"error: pandoc failed on {path}:\n{out.stderr}")
        return out.stdout
    with open(path, encoding="utf-8") as fh:
        return fh.read()


LATEX_NOISE = re.compile(r"\\(?:allowbreak|linebreak|newline|,|;|!)\{?\}?")
MARKBOTH = re.compile(r"\\markboth\{[^}]*\}\{[^}]*\}")


def clean(s: str) -> str:
    """Strip the LaTeX/pandoc debris that survives a docx -> markdown pass."""
    s = LATEX_NOISE.sub("", s)
    s = MARKBOTH.sub("", s)
    s = re.sub(r"\\([\'\"`^~$&%#_{}])", r"\1", s)   # pandoc escapes
    s = s.replace("\u00a0", " ").replace("\u2011", "-")
    s = re.sub(r"[ \t]*\n[ \t]*", " ", s)
    return re.sub(r"\s{2,}", " ", s).strip()


FENCE = re.compile(r"(`{3,})[\s\S]*?\1|(~{3,})[\s\S]*?\2")
INLINE_CODE = re.compile(r"`[^`\n]+`")


def strip_code(body: str) -> str:
    """Drop fenced and inline code before scanning for citations: an array
    index like arr[1] or a signature like foo(Date, 2020) inside code is not
    a citation, and in a numeric-style document the false positives would be
    relentless."""
    return INLINE_CODE.sub(" ", FENCE.sub(" ", body))


TITLES = r"references|bibliography|works cited|reference list"
HEADING = re.compile(r"^(#{1,4})\s*(?:\d+[.)]?\s*)?(?:" + TITLES + r")\b.*$", re.I | re.M)
# A .docx whose Word style never mapped to a heading level leaves the word
# sitting on a line of its own, sometimes bold or underlined.
BARE_HEADING = re.compile(r"^[ \t]*[*_]{0,2}(?:" + TITLES + r")[*_:]{0,2}[ \t]*$", re.I | re.M)
# A Works Cited or Bibliography list may hold undated entries -- an MLA web
# source has no year to carry -- while a References list is expected to date
# everything, so the year gate stays shut there.
YEARLESS_HEADING = re.compile(r"works cited|bibliography", re.I)


def split_document(text: str) -> tuple[str, str, str, list[str]]:
    """Split into (body, reference block, list heading, parsed entries).

    Later candidates win -- the word appears in running prose long before the
    list itself -- but a candidate only wins if entries can actually be parsed
    below it, so a stray mention does not swallow the real list.
    """
    candidates = list(HEADING.finditer(text)) or list(BARE_HEADING.finditer(text))
    if not candidates:
        sys.exit("error: no 'References' / 'Bibliography' heading found in the document")

    for match in reversed(candidates):
        level = len(match.group(1)) if match.re is HEADING else 1
        rest = text[match.end():]
        nxt = re.search(r"^#{1," + str(level) + r"}\s+\S", rest, re.M)
        block = rest[: nxt.start()] if nxt else rest
        entries = split_entries(block, bool(YEARLESS_HEADING.search(match.group(0))))
        if len(entries) >= 3:
            heading = match.group(0).strip("#*_ \t")
            return text[: match.start()], block, heading, entries
    sys.exit("error: found a References heading but could not parse any entries under it")


SKIP_ENTRY = re.compile(r"^(\\markboth|\[\^|:::|<!--|!\[|\||\s*$)")

# A numbered entry opens with '[1] ', '1. ' or '1) '. The whitespace after the
# marker is what keeps a DOI ('10.1145/...') or a decimal from masquerading as
# a list number.
NUM_MARKER = re.compile(r"^\[?(\d{1,3})[\].)]\s+")

# An entry needs a year -- parenthesised (author-date) or standing on its own
# between periods (Chicago author-date) -- unless it is numbered, in which
# case the number alone makes it an entry, or the list is a Works Cited /
# Bibliography and the entry carries a quoted title, in which case an
# undated web source is legitimate. The leading period in the Chicago branch
# is what keeps prose out: 'fieldwork ran across 2020' has no period before
# the year, but 'Smith, John. 2020. "Title."' always does.
ANY_YEAR = re.compile(r"\((?:19|20)\d\d[a-z]?\)|\.\s*(?:19|20)\d\d[a-z]?(?=$|[.\s])")


def split_entries(block: str, allow_yearless: bool = False) -> list[str]:
    """Blank-line-separated entries, with a fallback for one-per-line lists."""

    def is_entry(e: str) -> bool:
        return bool(len(e) > 25
                    and (NUM_MARKER.match(e) or ANY_YEAR.search(e)
                         or (allow_yearless and QUOTED.search(e))))

    chunks = [c.strip() for c in re.split(r"\n[ \t]*\n", block)]
    entries = [clean(c) for c in chunks if c and not SKIP_ENTRY.match(c)]
    entries = [e for e in entries if is_entry(e)]
    if len(entries) <= 1 and block.count("\n") > 3:
        # Single-spaced list: start a new entry at each line that opens with a
        # capitalised author or organisation and carries a year, or with a
        # number marker.
        entries, current = [], None
        for line in block.splitlines():
            line = line.strip()
            if NUM_MARKER.match(line) or (re.match(r"^[A-Z\u00c0-\u00dd]", line)
                                          and (ANY_YEAR.search(line)
                                               or (allow_yearless
                                                   and QUOTED.search(line)))):
                if current is not None:
                    entries.append(clean(current))
                current = line
            elif current is not None:
                # Anything before the first author-year line is preamble --
                # a table row, a caption, a stray heading -- not an entry.
                current += " " + line
        if current is not None:
            entries.append(clean(current))
        entries = [e for e in entries
                   if len(e) > 25 and not SKIP_ENTRY.match(e)]
    return entries


# --------------------------------------------------------------------------
# citation-style detection
# --------------------------------------------------------------------------

# Styles, in the order detect_style() considers them. Each has an entry
# grammar in parse_entry's dispatch table and an in-text finder; the string
# is also what --style accepts and what the report prints.
STYLES = ("author-date", "numeric", "mla", "notes")


def detect_style(body: str, entries: list[str], heading: str = "",
                 notes: dict[int, str] | None = None) -> str:
    """Infer the document's citation style from shape, not first impression.

    `body` must already have been through strip_code(), and `entries` are the
    parsed reference-list entries split_document returned.

    Follows split_document's philosophy: a candidate only wins if the
    entries under it actually parse. A numbered list whose body carries more
    bracket markers than author-year parentheticals is numeric; a numbered
    list cited author-date is still author-date. Footnote definitions whose
    notes read like citations make a notes document; a Works Cited heading
    with author-page parentheticals in the body is MLA.
    """
    numbered = sum(1 for e in entries if NUM_MARKER.match(e))
    if entries and numbered >= max(3, (len(entries) + 1) // 2):
        brackets = (len(BRACKET_CITE.findall(body)) + len(PANDOC_SUP.findall(body))
                    + len(SUPERSCRIPT_RUN.findall(body)))
        if brackets > len(PAREN.findall(body)):
            return "numeric"
    if notes:
        # Aside footnotes ('[^1]: a clarification, really') are not citations;
        # require most definitions to read like one before claiming notes.
        citationish = sum(1 for d in notes.values()
                          if QUOTED.search(d) or ANY_YEAR.search(d))
        if citationish >= max(2, len(notes) // 2) and NOTE_REF.search(body):
            return "notes"
    if "works cited" in heading.lower() and find_mla_citations(body):
        return "mla"
    return "author-date"


# --------------------------------------------------------------------------
# reference parsing
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Reference:
    raw: str
    key: str = ""
    number: int = 0            # position in a numbered list, 0 when there is none
    style: str = ""            # parse strategy that produced the entry
    name: str = ""             # first-author surname, or organisation name
    is_org: bool = False
    year: str = ""
    suffix: str = ""          # the 'a' / 'b' in 2025a
    title: str = ""
    container: str = ""
    url: str = ""
    doi: str = ""
    arxiv: str = ""
    rfc: str = ""
    draft: str = ""
    draft_rev: str = ""
    accessed: str = ""
    kind: str = "unknown"
    status: str = "UNVERIFIABLE"
    unchecked: bool = False    # an authority went unreachable, so nothing was decided
    notes: list[str] = dataclasses.field(default_factory=list)
    evidence: list[str] = dataclasses.field(default_factory=list)
    cited_by: list[str] = dataclasses.field(default_factory=list)


YEAR = re.compile(r"\((19|20)(\d\d)([a-z]?)\)")
# Chicago author-date puts the year on its own, unparenthesised, between the
# author segment and the title: 'Smith, John. 2020. "Title." Journal.' The
# leading period is required -- it is what separates an entry from prose that
# merely mentions a year -- and the trailing one is consumed so the title does
# not begin with the year's period.
CHICAGO_YEAR = re.compile(r"\.\s*((?:19|20)\d\d)([a-z]?)[.\s]?")
# A quoted title may contain an apostrophe ("what you've signed up for"), so the
# closing quote is only the one followed by punctuation or the end of the entry
# -- or preceded by it, because Chicago and IEEE put the comma and period
# inside the quotes: 'Title.' Journal rather than 'Title', Journal.
QUOTED = re.compile(
    r"(?:^|[\s(])[\u2018'\"\u201c](.{8,300}?)[.,;:]?[\u2019'\"\u201d](?=[\s,.;:]|$)")
URL_RE = re.compile(r"<?(https?://[^\s>)\]]+)>?")
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s,;>\)\]]+)")
ARXIV_RE = re.compile(r"arXiv[:\s]\s*(\d{4}\.\d{4,5})(v\d+)?", re.I)
# arXiv's own DOI, which is the form doi.org recommends for citing a preprint
# and the form an AI-drafted bibliography reaches for. It is registered with
# DataCite, so asking Crossref about it returns nothing and the entry would be
# reported NOT_FOUND -- a coverage gap accusing a real paper. The id inside it
# is what the arXiv API answers to.
ARXIV_DOI_RE = re.compile(r"\b10\.48550/arXiv\.(\d{4}\.\d{4,5})(v\d+)?", re.I)
RFC_RE = re.compile(r"\bRFC\s*(\d{3,5})\b", re.I)
DRAFT_RE = re.compile(r"\b(draft-[a-z0-9][a-z0-9\-]*[a-z0-9])\b", re.I)
# 'Accessed: 3 June 2026' comes parenthesised in Harvard and bare at the end
# of an MLA entry. Either way it is not a publication date, so parse_entry
# lifts it out before any grammar goes looking for a year.
# The date has to start like a date -- a day number or a month name -- so that
# 'accessed from the 2019 census' in running entry text is left alone.
ACCESSED_RE = re.compile(
    r"[(\[]?\s*Accessed:?\s+((?:\d|[A-Z][a-z]{2})[^)\]]{0,18}?(?:19|20)\d\d)[.)\]]*")
# A person's author segment opens 'Surname, Given' -- the given name may be an
# initial ('Smith, J.') or spelled out ('Smith, John'), because Chicago and MLA
# spell it out. Organisations have no comma after a single-token surname. The
# particle list is closed on purpose: 'van der Waals' is a person and so is its
# capitalised spelling (APA 9.8, Chicago), but an open '[a-z]\w*' prefix would
# take 'the Archives, Kew' with it.
PARTICLE = r"van|von|de(?:[rnm]|ll?a?)?|di|da|dos|du|la|le|el|al|bin|ibn|mac|mc|st|ter|ten|op|zu"
# (?i:) scopes the fold to the particles, so 'Le' and 'le' are one rule while
# the surname keeps its uppercase anchor -- re.I over the whole pattern makes a
# person of 'arXiv, Cornell University'. Concatenated rather than an f-string
# for the reason in CONTRIBUTING: the {0,3} would need doubling.
PERSON_RE = re.compile(
    r"^(?:(?i:" + PARTICLE + r")\s+){0,3}"
    r"[A-Z\u00c0-\u00dd][\w\u00c0-\u017e'\u2019\-]+,"
    r"\s*[A-Z\u00c0-\u00dd][\w\u00c0-\u017e'\u2019\-]*\.?")
# IEEE inverts the author: initials first ('J. Smith', 'A.-B. Smith').
IEEE_PERSON = re.compile(r"^[A-Z]\.(?:-[A-Z]\.)?\s+[A-Z\u00c0-\u00dd]")
# Vancouver/AMA glues initials to the surname with no comma ('Smith JA').
VANCOUVER_PERSON = re.compile(r"^[A-Z\u00c0-\u00dd][\w\u00c0-\u017e'\u2019\-]+\s+[A-Z]{1,3}\b")
# The whole Vancouver author block, ended by a period: 'Smith JA, Jones KB. '
VANCOUVER_BLOCK = re.compile(
    r"^[A-Z\u00c0-\u00dd][\w\u00c0-\u017e'\u2019\-]+(?:\s+[A-Z]{1,3})+"
    r"(?:,\s*[A-Z\u00c0-\u00dd][\w\u00c0-\u017e'\u2019\-]+(?:\s+[A-Z]{1,3})+)*"
    r"(?:,\s*(?:et\s+al|and\s+[A-Z\u00c0-\u00dd][\w\u00c0-\u017e'\u2019\-]+"
    r"(?:\s+[A-Z]{1,3})+))?"
    r"\.\s+")
# Vancouver's year follows the container's period: '. 2020;15(2):123-45.'
# NLM qualifies a monthly journal's date -- '. 2020 Jan;15(2)' -- so an
# optional month and day sit between the year and the delimiter.
VANCOUVER_YEAR = re.compile(
    r"\.\s*((?:19|20)\d\d)(?:\s+[A-Za-z]{3,9}\.?(?:\s+\d{1,2})?)?\s*(?=[;:.]|$)")

ACADEMIC = re.compile(
    r"\b(proceedings|conference|symposium|workshop|journal|transactions|advances in|"
    r"findings of|arxiv|preprint|acm|ieee|usenix|neurips|iclr|icml)\b", re.I)


def first_author(segment: str) -> tuple[str, bool]:
    """(name, is_org) from the author segment of an entry.

    The name is the first author's surname, or the organisation's whole name.
    """
    segment = segment.strip()
    if IEEE_PERSON.match(segment):
        first = re.split(r",", segment)[0]
        first = re.sub(r"\s+et\s+al\.?\s*$", "", first).strip()
        first = re.split(r"\s+and\s+", first)[0].strip()
        tokens = first.split()
        return (tokens[-1] if len(tokens) > 1 else first, False)
    if VANCOUVER_PERSON.match(segment):
        return (segment.split(",")[0].split()[0], False)
    if PERSON_RE.match(segment):
        return (segment.split(",")[0].strip(), False)
    return (segment.strip(" .,:"), True)


def set_title(ref: Reference, tail: str) -> bool:
    """Read the title out of the text after the author/year, quoted or not."""
    qm = QUOTED.search(tail)
    if qm:
        ref.title = qm.group(1).strip()
        ref.container = tail[qm.end():].lstrip(" ,.").strip()
        return True
    # Unquoted title: everything up to the first sentence break that is not
    # part of an initial or a URL.
    head = re.split(r"\.\s+(?=[A-Z])|\.\s*Available at", tail, maxsplit=1)
    ref.title = head[0].strip(" .,")
    ref.container = (head[1] if len(head) > 1 else "").strip()
    return False


def parse_author_date(ref: Reference, rest: str) -> bool:
    """'Smith, J. (2020) 'Title', Journal.' -- the year in parentheses."""
    ym = YEAR.search(rest)
    if not ym:
        return False
    ref.style = "author-date"
    ref.year, ref.suffix = ym.group(1) + ym.group(2), ym.group(3)
    authors = rest[: ym.start()].strip().rstrip(",")
    ref.name, ref.is_org = first_author(authors)
    set_title(ref, rest[ym.end():].strip())
    return True


def parse_quoted(ref: Reference, rest: str) -> bool:
    """Authors, then a quoted title, then a bare year somewhere after it.

    IEEE ('J. Smith, "Title," Journal, 2020.') and MLA ('Smith, John. "Title."
    Journal, 2020.') share the shape; the author segment tells them apart.
    The year must not sit before the title -- parenthesised before it is
    author-date, bare between periods is Chicago, and those strategies own
    such entries -- and neither may the segment carry a year, a slash or a
    URL, because then it is a locator, not a name.
    """
    qm = QUOTED.search(rest)
    if not qm:
        return False
    pre = rest[: qm.start()].rstrip()
    if (not pre or YEAR.search(pre) or CHICAGO_YEAR.search(pre)
            or re.search(r"/|\b(?:19|20)\d\d\b", pre) or VANCOUVER_BLOCK.match(rest)):
        return False
    ref.style = "ieee" if IEEE_PERSON.match(pre) else "mla"
    ref.name, ref.is_org = first_author(pre)
    # The year is the last bare one after the title: a year inside the title
    # is not the publication year, so the search starts where the title ends.
    # An access date is already out of the entry by now.
    years = re.findall(r"\b((?:19|20)\d\d)([a-z]?)\b", rest[qm.end():])
    ref.year, ref.suffix = years[-1] if years else ("", "")
    ref.title = qm.group(1).strip()
    ref.container = rest[qm.end():].lstrip(" ,.").strip()
    return True


def parse_vancouver(ref: Reference, rest: str) -> bool:
    """'Smith JA. Title. Journal. 2020;15(2):123-45.' -- surname plus glued
    initials, unquoted title, year after the container's period."""
    bm = VANCOUVER_BLOCK.match(rest)
    if not bm:
        return False
    ym = VANCOUVER_YEAR.search(rest, bm.end())
    if not ym:
        return False
    ref.style = "vancouver"
    authors = rest[: bm.end()].strip().rstrip(".")
    ref.name, ref.is_org = first_author(authors)
    ref.year = ym.group(1)
    mid = rest[bm.end(): ym.start()].strip()
    set_title(ref, mid)
    return True


def parse_chicago(ref: Reference, rest: str) -> bool:
    """'Smith, John. 2020. "Title." Journal.' -- the year on its own between
    periods."""
    bm = CHICAGO_YEAR.search(rest)
    if not bm:
        return False
    ref.style = "chicago-ad"
    ref.year, ref.suffix = bm.group(1), bm.group(2)
    authors = rest[: bm.start()].strip().rstrip(".,")
    ref.name, ref.is_org = first_author(authors)
    set_title(ref, rest[bm.end():].strip())
    return True


def parse_entry(raw: str) -> Reference:
    ref = Reference(raw=raw)
    rest = raw.strip()
    if nm := NUM_MARKER.match(rest):
        ref.number = int(nm.group(1))
        rest = rest[nm.end():].strip()
    if m := ACCESSED_RE.search(rest):
        ref.accessed = m.group(1).strip(" .,")
        rest = (rest[: m.start()] + " " + rest[m.end():]).strip()

    # First strategy whose shape fits the entry wins. A numbered list of
    # author-date entries still parses as author-date -- the marker is
    # recorded, it does not own the grammar.
    if not (parse_quoted(ref, rest)
            or parse_author_date(ref, rest)
            or parse_vancouver(ref, rest)
            or parse_chicago(ref, rest)):
        # Nothing claimed it. Keep the text where the title would be so the
        # report can quote the entry; the empty name is a coverage gap, not
        # an organisation.
        ref.is_org = True
        ref.notes.append("no citation grammar fit this entry; verify it by eye")
        set_title(ref, rest)

    ref.key = f"{normalise(ref.name)}|{ref.year}{ref.suffix}"
    if ref.key == "|":
        # No name and no year to key on. The list position, or failing that the
        # entry's own text, keeps two unparsed entries from collapsing into one
        # key -- and one of them from standing in for the other in the report.
        ref.key = f"#{ref.number}|" if ref.number else f"{normalise(raw)[:60]}|"

    if m := URL_RE.search(rest):
        ref.url = m.group(1).rstrip(".,;")
    if m := DOI_RE.search(rest):
        ref.doi = m.group(1).rstrip(".")
    if m := ARXIV_RE.search(rest) or ARXIV_DOI_RE.search(rest):
        ref.arxiv = m.group(1)
    if m := RFC_RE.search(rest):
        ref.rfc = m.group(1)
    if m := DRAFT_RE.search(rest):
        # Take the whole token greedily, then split a trailing -NN revision off
        # it -- 'draft-ietf-oauth-v2-1-15' is v2-1 at revision 15.
        full = m.group(1)
        rm = re.match(r"^(.*?)-(\d{2})$", full)
        ref.draft, ref.draft_rev = (rm.group(1), rm.group(2)) if rm else (full, "")
    ref.title = re.sub(r"\s*Available at:?.*$", "", ref.title, flags=re.I)
    # An unquoted title often absorbs the series identifier that follows it;
    # trim it so title matching compares like with like.
    ref.title = re.sub(
        r",?\s*(RFC\s*\d+|BCP\s*\d+|Internet-Draft\s+draft-\S+|IETF)\b", "",
        ref.title, flags=re.I).strip(" .,")

    if ref.arxiv:
        ref.kind = "arxiv"
    elif ref.doi:
        ref.kind = "doi"
    elif ref.rfc:
        ref.kind = "rfc"
    elif ref.draft:
        ref.kind = "ietf-draft"
    elif ref.title and ref.style and ACADEMIC.search(ref.container):
        ref.kind = "paper"
    elif ref.url:
        ref.kind = "web"
    return ref


# --------------------------------------------------------------------------
# in-text citations
# --------------------------------------------------------------------------

# APA and Harvard carry a page locator after the year: '(Smith, 2020, p. 5)',
# '(Smith 2020, 25)'. Anything else trailing the year disqualifies the match.
LOCATOR = r"(?:\s*[,;:]\s*((?:pp?\.\s*)?[\d\u2013,\- ]+))?"

PAREN = re.compile(r"\(([^()]{3,120}?(?:19|20)\d\d[a-z]?[^()]{0,40}?)\)")
NARRATIVE = re.compile(
    r"\b([A-Z\u00c0-\u00dd][\w\u00c0-\u017e'\u2019\-]*"
    r"(?:\s+(?:and|&)\s+[A-Z\u00c0-\u00dd][\w\u00c0-\u017e'\u2019\-]*"
    r"|\s+et\s+al\.?"
    r"|\s+[A-Z\u00c0-\u00dd][\w\u00c0-\u017e'\u2019\-]*){0,3})"
    r"\s+\(((?:19|20)\d\d)([a-z]?)" + LOCATOR + r"\)")
NOT_A_CITATION = re.compile(
    r"^(accessed|figure|table|chapter|section|appendix|see|eq|equation|n\.?d\.?)\b", re.I)


@dataclasses.dataclass
class Citation:
    name: str
    year: str
    suffix: str
    form: str                 # 'parenthetical', 'narrative', 'numeric', 'note', ...
    context: str
    key: str = ""
    number: int = 0           # [n] marker or footnote number
    page: str = ""            # MLA author-page locator


def find_citations(body: str) -> list[Citation]:
    """Author-date in-text citations: parenthetical and narrative forms."""
    out: list[Citation] = []
    for m in PAREN.finditer(body):
        inner = m.group(1)
        if re.search(r"accessed", inner, re.I):
            continue
        for part in re.split(r";", inner):
            part = part.strip()
            cm = re.match(
                r"^(.{2,80}?)[,\s]+((?:19|20)\d\d)([a-z]?)" + LOCATOR + r"$",
                part.replace("et al.", "et al"))
            if not cm:
                continue
            name = cm.group(1).strip(" ,")
            if NOT_A_CITATION.match(name) or not re.match(r"^[A-Z\u00c0-\u00dd]", name):
                continue
            out.append(Citation(name, cm.group(2), cm.group(3), "parenthetical",
                                context(body, m.start()),
                                page=(cm.group(4) or "").strip()))
    for m in NARRATIVE.finditer(body):
        name = m.group(1).strip()
        if NOT_A_CITATION.match(name):
            continue
        out.append(Citation(name, m.group(2), m.group(3), "narrative",
                            context(body, m.start()),
                            page=(m.group(4) or "").strip()))
    return out


# Numeric in-text markers: '[1]', '[2, 5]', '[5-7]', '[1]–[3]', and the two
# superscript forms pandoc emits for a .docx -- '^8^' and the Unicode digits.
# The lookarounds keep markdown that merely contains bracketed digits out:
# link definitions ('[1]: url'), inline links ('[1](url)'), reference uses
# ('[text][1]') and exponents ('x^2^'). The lookbehind rejects only a bracket
# closing on a non-digit, so the second of two adjacent markers ('[1][2]', a
# normal IEEE pair) still counts; a reference link whose text ends in a digit
# is the documented price of that.
BRACKET_CITE = re.compile(
    r"(?<![^\d]\])\[(\d{1,3}(?:\s*[,;\u2013\-]\s*\d{1,3})*)\](?![:(])")
DASHED_BRACKETS = re.compile(r"\[(\d{1,3})\]\s*[\u2013\-]\s*\[(\d{1,3})\]")
PANDOC_SUP = re.compile(r"(?<![A-Za-z0-9=])\^(\d{1,3})\^(?![A-Za-z0-9])")
SUPERSCRIPT_RUN = re.compile(
    r"(?<![A-Za-z0-9=])[\u00b9\u00b2\u00b3\u2070\u2074-\u2079]{1,3}(?![A-Za-z0-9])")
SUP_DIGITS = str.maketrans("\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079",
                           "0123456789")


def expand_numbers(spec: str) -> list[int]:
    """The numbers a bracket citation covers: '[2, 5-7]' -> [2, 5, 6, 7].

    A reversed range ('[7-3]') is a typo, not a citation of 7 and 3, and is
    dropped; a range is also capped so one marker cannot claim half the list.
    """
    out: list[int] = []
    for part in re.split(r"[,;]", spec):
        m = re.fullmatch(r"\s*(\d{1,3})\s*[\u2013\-]\s*(\d{1,3})\s*", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if 0 < lo <= hi < lo + 50:
                out.extend(range(lo, hi + 1))
                continue
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def find_numeric_citations(body: str) -> list[Citation]:
    """Bracket and superscript markers: '[1]', '[5-7]', '^8^', '²'."""
    out: list[Citation] = []
    dashed = list(DASHED_BRACKETS.finditer(body))
    for m in dashed:
        lo, hi = int(m.group(1)), int(m.group(2))
        if 0 < lo <= hi < lo + 50:
            for n in range(lo, hi + 1):
                out.append(Citation("", "", "", "numeric", context(body, m.start()),
                                    number=n))
    for m in BRACKET_CITE.finditer(body):
        if any(d.start() <= m.start() < d.end() for d in dashed):
            continue            # already covered by the dashed-pair range
        for n in expand_numbers(m.group(1)):
            out.append(Citation("", "", "", "numeric", context(body, m.start()),
                                number=n))
    for m in PANDOC_SUP.finditer(body):
        out.append(Citation("", "", "", "numeric", context(body, m.start()),
                            number=int(m.group(1))))
    for m in SUPERSCRIPT_RUN.finditer(body):
        out.append(Citation("", "", "", "numeric", context(body, m.start()),
                            number=int(m.group(0).translate(SUP_DIGITS))))
    return out


# MLA cites by author and page, with no year: '(Smith 42)', '(Smith and
# Jones 12)', '(Smith 42; Lee 3)'. The name is one surname, or two joined by
# 'and'; a spelled-out given name ('Karen Smith 55') deliberately does not
# match, because matching is by surname alone and a false precision would be
# worse than a documented limit. A page that is itself a year is left to the
# author-date finder: '(Smith 2020)' is not MLA with page 2020.
PAREN_ANY = re.compile(r"\(([^()]{2,120}?)\)")
MLA_INNER = re.compile(
    r"^([A-Z\u00c0-\u00dd][\w\u00c0-\u017e'\u2019\-]*"
    r"(?:\s+(?:and|&)\s+[A-Z\u00c0-\u00dd][\w\u00c0-\u017e'\u2019\-]*)?"
    r"(?:\s+et\s+al\.?)?)"
    r",?\s+((?:pp?\.\s*)?\d{1,4}(?:\s*[\u2013,\-]\s*\d{1,4})*)$")


def find_mla_citations(body: str) -> list[Citation]:
    """MLA author-page parentheticals: '(Smith 42)', '(Jones and Patel 12)'."""
    out: list[Citation] = []
    for m in PAREN_ANY.finditer(body):
        inner = m.group(1)
        if re.search(r"accessed", inner, re.I):
            continue
        for part in re.split(r";", inner):
            mm = MLA_INNER.match(part.strip())
            if not mm:
                continue
            name, page = mm.group(1).strip(), mm.group(2).strip()
            if NOT_A_CITATION.match(name):
                continue
            if re.fullmatch(r"(19|20)\d\d", page):
                continue
            out.append(Citation(name, "", "", "mla", context(body, m.start()),
                                page=page))
    return out


# Footnote markers in the body and their definitions at the end. pandoc turns
# .docx footnotes into exactly this shape: 'text[^1] more' in the body and
# '[^1]: the note' blocks at the end, indented continuation lines included.
NOTE_REF = re.compile(r"\[\^(\d{1,3})\]")
NOTE_DEF = re.compile(r"(?m)^\[\^(\d{1,3})\]:[ \t]*([^\n]*(?:\n[ \t]+\S[^\n]*)*)")
IBID = re.compile(r"^ibid\b", re.I)


def harvest_notes(text: str) -> tuple[str, dict[int, str]]:
    """Pull '[^n]: definition' footnote blocks out of the text.

    Returns (text without the definitions, note number -> note text). The
    definitions leave the text before the reference list is looked for, so
    they can never be glued onto a bibliography entry.
    """
    notes: dict[int, str] = {}
    spans: list[tuple[int, int]] = []
    for m in NOTE_DEF.finditer(text):
        notes.setdefault(int(m.group(1)), clean(m.group(2)))
        spans.append((m.start(), m.end()))
    for start, end in reversed(spans):
        text = text[:start] + text[end:]
    return text, notes


def find_note_citations(body: str) -> list[Citation]:
    """Footnote markers in the body, one citation per marker."""
    return [Citation("", "", "", "note", context(body, m.start()),
                     number=int(m.group(1)))
            for m in NOTE_REF.finditer(body)]


def note_surname(text: str) -> str:
    """First surname in a note, inverted ('Greshake, Kai,') or natural
    ('Kai Greshake,'). A one-token head is an inverted surname; a multi-token
    head with no comma inside it is a given name plus surname."""
    # An opening quote follows a space or a bracket; an apostrophe inside a
    # surname ("O'Brien") does not, and must not cut the name in half.
    before_title = re.split(r"(?:^|[\s(\[])[\u2018'\"\u201c]", text, maxsplit=1)[0]
    head = before_title.split(",")[0].strip()
    tokens = head.split()
    if len(tokens) == 1:
        return tokens[0]
    return tokens[-1]


def is_short_note(ref: Reference) -> bool:
    """A Chicago/MHRA shortened note: author, short title, page -- no year,
    nothing checkable, and short."""
    return (bool(ref.title) and not ref.year
            and not (ref.doi or ref.arxiv or ref.rfc or ref.draft or ref.url)
            and len(ref.raw) < 90)


def find_note_target(ref: Reference, pool: list[Reference]) -> Reference | None:
    """The entry a note cites, if one is already in hand: same surname and a
    title that matches outright, or that the full title starts with -- a
    shortened note quotes the first words of the title, and a similarity
    score alone would call 'Not What' and 'Not What You've Signed Up For' a
    poor match."""
    surname = normalise(note_surname(ref.raw)).split(" ")[-1]
    if not surname or not ref.title:
        return None
    for cand in pool:
        # Token match, as author_present does it: 'Lee' is not 'Leeson'.
        if not cand.title or surname not in normalise(cand.name).split():
            continue
        full, short = normalise(cand.title), normalise(ref.title)
        if (title_score(ref.title, cand.title) >= 0.60
                or full.startswith(short) or short.startswith(full)):
            return cand
    return None


def build_note_refs(notes: dict[int, str], bib: list[Reference]
                    ) -> tuple[list[Reference], dict[int, str]]:
    """Footnote definitions as references.

    A full note becomes an entry unless the bibliography already lists the
    work; a shortened note or an Ibid. links to the citation it repeats and
    becomes no entry of its own. Returns (new entries, note number -> key).
    """
    out: list[Reference] = []
    note_map: dict[int, str] = {}
    last = ""
    for n in sorted(notes):
        if IBID.match(notes[n]):
            # Ibid. repeats whatever the previous note cited.
            if last:
                note_map[n] = last
                continue
            ref = parse_entry(notes[n])
            ref.notes.insert(0, "Ibid. with no earlier citation to refer to")
            note_map[n] = ref.key
            last = ref.key
            out.append(ref)
            continue
        # No ref.number: that field is a position in a numbered reference
        # list, and a footnote number is a different numbering space.
        ref = parse_entry(notes[n])
        target = find_note_target(ref, bib + out)
        if target:
            note_map[n] = target.key
            last = target.key
            continue
        if is_short_note(ref):
            ref.notes.insert(0, "shortened note; could not link it to a fuller "
                                "citation, so verify it against the source")
        note_map[n] = ref.key
        last = ref.key
        out.append(ref)
    return out, note_map


# A sentence ends at .!? followed by whitespace or end of line -- or by a
# citation marker trailing the sentence it documents, which the boundary then
# consumes whole so it does not leak into the next context: '... here.^8^',
# 'shown.[1]', 'text.[^1]', 'style.²'. Bare digits are deliberately excluded
# so a decimal ('3.14') never looks like a sentence break.
SENT_END = re.compile(
    r"(?<![A-Z])(?<!\bet al)(?<!\bvol)(?<!\bpp)[.!?]"
    r"(?![ \t]*\d)"          # 'Fig. 4', 'No. 14': an abbreviation, not a stop
    r"(?:\s|$"
    r"|\^\d{1,3}\^"
    r"|\[\^?\d{1,3}\]"
    r"|[\u00b9\u00b2\u00b3\u2070\u2074-\u2079]{1,3}"
    r")")
# A marker abutting the punctuation ('shown.[1]') documents the sentence just
# closed, so the forward half of the quote is dropped. The abbreviation guards
# are SENT_END's: 'Smith et al.[3] we extend' is one sentence, not two.
TRAILING_STOP = re.compile(r"(?<!\bet al)(?<!\bvol)(?<!\bpp)[.!?\u2026]$")


def context(body: str, pos: int, width: int = 420) -> str:
    """Approximate sentence around a position -- good enough for a worklist."""
    start = max(0, pos - width)
    end = min(len(body), pos + width)
    left = body[start:pos]
    right = body[pos:end]
    headings = list(re.finditer(r"(?m)^#{1,6}[^\n]*$", left))
    if headings:                  # don't drag the section title into the quote
        left = left[headings[-1].end():]
    bounds = list(SENT_END.finditer(left))
    if bounds and not left[bounds[-1].end():].strip():
        # The marker trails a finished sentence ('... appears here.^8^'), so
        # the sentence it documents is the one just closed, not whatever
        # follows the marker: keep that sentence instead of dropping it.
        bounds = bounds[:-1]
    if bounds:
        left = left[bounds[-1].end():]
    if TRAILING_STOP.search(left):
        right = ""            # a trailing marker documents the closed sentence
    else:
        fwd = SENT_END.search(right)
        if fwd:
            right = right[: fwd.end()]
    return clean(left + right)


def normalise(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\bet\s+al\.?", "", s, flags=re.I)
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def citation_key(name: str, year: str, suffix: str) -> str:
    # Split the ampersand off before normalise(), which strips punctuation and
    # would otherwise leave 'Smith & Jones' as the single token 'smith jones'.
    n = normalise(name.split("&")[0])
    n = re.split(r"\band\b", n)[0].strip()
    return f"{n}|{year}{suffix}"


def match_citations(refs: list[Reference], cites: list[Citation],
                    note_map: dict[int, str] | None = None) -> list[Citation]:
    """Attach each in-text citation to a reference where one can be found.

    Footnote markers resolve through note_map -- note number to the key of
    the work it cites -- because a shortened note or an Ibid. cites an entry
    another note already produced, and no number-to-entry table can express
    that.
    """
    by_key = {r.key: r for r in refs}
    by_number = {r.number: r for r in refs if r.number}
    by_surname: dict[str, list[Reference]] = {}
    for r in refs:
        first = normalise(r.name).split(" ")[0] if not r.is_org else normalise(r.name)
        by_surname.setdefault(first, []).append(r)

    for c in cites:
        if c.form == "note" and note_map is not None:
            target = by_key.get(note_map.get(c.number, ""))
            if target:
                c.key = target.key
                target.cited_by.append(c.form)
            continue
        if c.form in ("numeric", "note"):
            # A marker resolves by position or not at all -- no fuzzy matching
            # between a number and a name.
            hit = by_number.get(c.number)
            if hit:
                c.key = hit.key
                hit.cited_by.append(c.form)
            continue
        if c.form == "mla":
            # Author-page: no year to key on, so the surname must be unique
            # in the list. Two Smiths leave the citation unresolved rather
            # than guessed.
            candidates = by_surname.get(citation_key(c.name, "", "").split("|")[0], [])
            if len(candidates) == 1:
                c.key = candidates[0].key
                candidates[0].cited_by.append(c.form)
            continue
        key = citation_key(c.name, c.year, c.suffix)
        if key in by_key:
            c.key = key
            by_key[key].cited_by.append(c.form)
            continue
        # organisation names cite in full; person names cite by surname only
        stem = key.split("|")[0]
        head = stem.split(" ")[0]
        for candidate_stem in (stem, head):
            for r in by_surname.get(candidate_stem, []):
                if r.year == c.year and (not c.suffix or c.suffix == r.suffix):
                    c.key = r.key
                    r.cited_by.append(c.form)
                    break
            if c.key:
                break
    return cites


# --------------------------------------------------------------------------
# HTTP with an on-disk cache
# --------------------------------------------------------------------------

# A server answering 404 or 410 has told us the record is not there, and that
# is evidence about the citation. Anything else that goes wrong -- a rate limit,
# a timeout, a 5xx, a TLS failure -- is our side of the conversation failing,
# and says nothing at all about the entry. Reporting it as a finding accuses a
# citation of being wrong because someone else's server was busy.
DEFINITIVE_ABSENCE = (404, 410)


def lookup_failed(rec: dict[str, Any]) -> bool:
    """Did this request fail in a way that says nothing about the target?"""
    if rec["ok"]:
        return False
    status = rec.get("status")
    if status in DEFINITIVE_ABSENCE:
        return False
    if status is None:
        # Transport level. A hostname that does not resolve is a real finding;
        # a timeout or a TLS error is not. Cache records written before this
        # flag existed are read the safe way -- as a failure, not a finding.
        transient: bool = rec.get("transient", True)
        return transient
    return True                 # 429, 5xx, and anything else unexpected


class Fetcher:
    def __init__(self, cache_dir: str, timeout: int = 25, offline: bool = False,
                 mailto: str | None = None, delay: float = 0.4):
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.offline = offline
        self.mailto = mailto
        self.delay = delay
        self._rate_guard = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._last_by_host: dict[str, float] = {}
        self._cache_guard = threading.Lock()
        self._cache_locks: dict[str, threading.Lock] = {}
        os.makedirs(cache_dir, exist_ok=True)

    def _cache_lock(self, path: str) -> threading.Lock:
        with self._cache_guard:
            return self._cache_locks.setdefault(path, threading.Lock())

    @staticmethod
    def _host_key(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.hostname or parsed.netloc or url).lower()

    def _wait_for_host(self, url: str) -> None:
        """Wait until this host may receive another request.

        The lock is held only while reserving the next request slot. Network
        I/O happens after it is released, so different hosts can be fetched in
        parallel while requests to one host remain spaced by ``delay``.
        """
        host = self._host_key(url)
        with self._rate_guard:
            host_lock = self._host_locks.setdefault(host, threading.Lock())
        with host_lock:
            now = time.monotonic()
            last = self._last_by_host.get(host)
            if last is not None:
                gap = self.delay - (now - last)
                if gap > 0:
                    time.sleep(gap)
            self._last_by_host[host] = time.monotonic()

    def get(self, url: str, accept: str | None = None,
            max_bytes: int = 400_000) -> dict[str, Any]:
        path = os.path.join(self.cache_dir, hashlib.sha256(
            (url + (accept or "")).encode()).hexdigest() + ".json")
        with self._cache_lock(path):
            if os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL:
                with open(path, encoding="utf-8") as fh:
                    cached: dict[str, Any] = json.load(fh)
                if not lookup_failed(cached):
                    return cached
            if self.offline:
                return {"ok": False, "status": None, "error": "offline", "body": "",
                        "final_url": url}

            self._wait_for_host(url)

            ua = UA if not self.mailto else f"{UA} mailto:{self.mailto}"
            req = urllib.request.Request(url, headers={
                "User-Agent": ua,
                "Accept": accept or "text/html,application/json;q=0.9,*/*;q=0.8",
            })
            rec: dict[str, Any]
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read(max_bytes)
                    charset = resp.headers.get_content_charset() or "utf-8"
                    rec = {"ok": True, "status": resp.status,
                           "body": raw.decode(charset, "replace"),
                           "final_url": resp.geturl(), "error": None}
            except urllib.error.HTTPError as e:
                rec = {"ok": False, "status": e.code, "body": "", "final_url": url,
                       "error": f"HTTP {e.code}"}
            except Exception as e:                      # timeouts, DNS, TLS, redirect loops
                reason = getattr(e, "reason", None)
                rec = {"ok": False, "status": None, "body": "", "final_url": url,
                       "error": f"{type(e).__name__}: {e}",
                       # A name that does not resolve is a dead link. A timeout is
                       # our problem, not the link's.
                       "transient": not isinstance(reason, socket.gaierror)}
            if not lookup_failed(rec):
                temp = f"{path}.{threading.get_ident()}.tmp"
                try:
                    with open(temp, "w", encoding="utf-8") as fh:
                        json.dump(rec, fh)
                    os.replace(temp, path)
                finally:
                    if os.path.exists(temp):
                        os.remove(temp)
            return rec

    def json(self, url: str) -> tuple[Any | None, str | None]:
        """(parsed body, the reason the lookup did not happen).

        A reason of None means the server answered -- with the JSON, or with a
        definitive 'no such record'. A reason means the question was never put,
        and the caller must not turn that into a finding about the citation.
        """
        rec = self.get(url, accept="application/json")
        if rec["ok"]:
            try:
                return json.loads(rec["body"]), None
            except json.JSONDecodeError:
                return None, "unreadable response"
        if lookup_failed(rec):
            return None, rec["error"] or f"HTTP {rec['status']}"
        return None, None


# --------------------------------------------------------------------------
# similarity
# --------------------------------------------------------------------------

def title_score(a: str, b: str) -> float:
    na, nb = normalise(a), normalise(b)
    if not na or not nb:
        return 0.0
    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jac = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    contain = 1.0 if (na in nb or nb in na) and min(len(na), len(nb)) > 20 else 0.0
    return max(seq, jac, contain)


def author_present(surname: str, authors: Iterable[str]) -> bool:
    s = normalise(surname).split(" ")[-1]
    return any(s and s in normalise(a).split() for a in authors)


# --------------------------------------------------------------------------
# per-source verification
# --------------------------------------------------------------------------

def check_metadata(ref: Reference, found_title: str, found_authors: list[str],
                   found_year: str | None, source: str,
                   authoritative: bool = True) -> None:
    """Compare a retrieved record against the reference and set status/notes.

    `authoritative` says whether the record is the cited work by construction,
    as it is when fetched by DOI or arXiv id. A record merely found by title
    search is not: an index holds reprints, duplicate entries and later
    editions, so a disagreeing date there is worth a look rather than a verdict.
    """
    score = title_score(ref.title, found_title)
    ref.evidence.append(f"{source}: \u201c{found_title[:140]}\u201d"
                        + (f" ({found_year})" if found_year else ""))
    problems = []
    if score < 0.60:
        problems.append(f"title differs from {source} record (similarity {score:.2f})")
    elif score < 0.85:
        ref.notes.append(f"title only partly matches {source} (similarity {score:.2f})")
    if found_authors and not ref.is_org and not author_present(ref.name, found_authors):
        problems.append(f"first author '{ref.name}' not among {source} authors "
                        f"({', '.join(found_authors[:4])})")
    dated_oddly = False
    if found_year and ref.year and abs(int(found_year) - int(ref.year)) > 1:
        if authoritative:
            problems.append(f"year {ref.year} vs {found_year} in {source}")
        else:
            ref.notes.append(f"year {ref.year} vs {found_year} in {source}; a search "
                             "index may hold a reprint or a duplicate record, so "
                             "confirm the date rather than trusting either")
            dated_oddly = True

    if problems:
        ref.status = "MISMATCH"
        ref.notes.extend(problems)
    elif score >= 0.85 and not dated_oddly:
        ref.status = "VERIFIED"
    else:
        ref.status = "PARTIAL"


def verify_arxiv(ref: Reference, f: Fetcher) -> bool:
    url = f"http://export.arxiv.org/api/query?id_list={ref.arxiv}&max_results=1"
    rec = f.get(url, accept="application/atom+xml")
    if not rec["ok"]:
        ref.unchecked = True
        ref.notes.append(f"arXiv API unreachable ({rec['error']})")
        return False
    try:
        root = ET.fromstring(rec["body"])
    except ET.ParseError:
        return False
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = root.find("a:entry", ns)
    if entry is None or entry.findtext("a:title", "", ns).strip() in ("", "Error"):
        ref.status = "NOT_FOUND"
        ref.notes.append(f"arXiv has no paper with id {ref.arxiv}")
        return True
    title = " ".join(entry.findtext("a:title", "", ns).split())
    authors = [a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)]
    published = entry.findtext("a:published", "", ns)[:4] or None
    check_metadata(ref, title, authors, published, f"arXiv:{ref.arxiv}")
    return True


def crossref_record(item: dict[str, Any]) -> tuple[str, list[str], str | None]:
    title = (item.get("title") or [""])[0]
    authors = [" ".join(filter(None, [a.get("given"), a.get("family")]))
               for a in item.get("author", [])]
    parts = (item.get("issued") or {}).get("date-parts") or [[None]]
    year = str(parts[0][0]) if parts and parts[0] and parts[0][0] else None
    return title, authors, year


def verify_doi(ref: Reference, f: Fetcher) -> bool:
    data, unreachable = f.json(
        f"https://api.crossref.org/works/{urllib.parse.quote(ref.doi)}")
    if unreachable:
        # No verdict: hand the entry to the next route rather than accuse it.
        ref.unchecked = True
        ref.notes.append(f"Crossref could not be reached ({unreachable}), "
                         f"so DOI {ref.doi} was not checked")
        return False
    if not data or "message" not in data:
        ref.status = "NOT_FOUND"
        ref.notes.append(f"Crossref has no record for DOI {ref.doi}")
        return True
    title, authors, year = crossref_record(data["message"])
    check_metadata(ref, title, authors, year, f"Crossref {ref.doi}")
    return True


def confident_match(ref: Reference, score: float, authors: list[str]) -> bool:
    """Is this candidate the cited work, or merely near it?

    A title search returns neighbours, and a shared phrase scores high: the
    title of "Attention is all you need" sits inside book chapters that are not
    it. So a person's entry has to agree on the surname as well before the
    candidate is treated as the work. An organisation's entry, with no surname
    to check, and a record that lists no authors at all, have to lean on the
    title alone and are held to a higher bar.
    """
    if ref.is_org or not authors:
        return score >= 0.85
    return score >= 0.60 and author_present(ref.name, authors)


def verify_by_title(ref: Reference, f: Fetcher) -> bool:
    """No identifier: search Crossref then OpenAlex by bibliographic title."""
    if not ref.title:
        return False
    q = urllib.parse.quote(ref.title[:250])
    Candidate = tuple[float, str, list[str], str | None, str]
    found: list[Candidate] = []
    answered: list[str] = []            # authorities that actually searched
    silent: list[str] = []              # and those that could not be reached

    def rank(c: Candidate) -> tuple[bool, float]:
        """Corroborated candidates first, then by title.

        Title score alone breaks down exactly where it matters: a title that
        contains the cited one scores 1.00, so "Is Attention All You Need?"
        ties with the paper it is asking about, and whichever index answered
        first would win. A candidate carrying the cited surname is the better
        answer at the same score.
        """
        corroborated = (not ref.is_org and c[0] >= 0.60
                        and author_present(ref.name, c[2]))
        return (corroborated, c[0])

    def best_of() -> Candidate:
        return max(found, key=rank, default=(0.0, "", [], None, ""))

    data, unreachable = f.json(
        f"https://api.crossref.org/works?query.bibliographic={q}&rows=5"
        + (f"&mailto={urllib.parse.quote(f.mailto)}" if f.mailto else ""))
    if unreachable:
        silent.append(f"Crossref ({unreachable})")
    else:
        answered.append("Crossref")
        for item in ((data or {}).get("message", {}) or {}).get("items", []):
            title, authors, year = crossref_record(item)
            found.append((title_score(ref.title, title), title, authors, year,
                          f"Crossref ({item.get('DOI', 'no DOI')})"))

    best = best_of()
    if not confident_match(ref, best[0], best[2]):
        # Ask the second index whenever the first has not produced the work --
        # a high title score alone is not that, so it must not skip this.
        data, unreachable = f.json(
            f"https://api.openalex.org/works?per-page=5&filter=title.search:{q}"
            + (f"&mailto={urllib.parse.quote(f.mailto)}" if f.mailto else ""))
        if unreachable:
            silent.append(f"OpenAlex ({unreachable})")
        else:
            answered.append("OpenAlex")
            for item in (data or {}).get("results", []):
                title = item.get("display_name") or ""
                authors = [a.get("author", {}).get("display_name", "")
                           for a in item.get("authorships", [])]
                year = str(item.get("publication_year")) if item.get("publication_year") else None
                found.append((title_score(ref.title, title), title, authors, year,
                              "OpenAlex"))
        best = best_of()

    if silent:
        # Say which search did not run, whatever the outcome: a reader deciding
        # how much to trust this line needs to know what was actually asked.
        ref.unchecked = True
        ref.notes.append("could not search " + " and ".join(silent))
    # A search returns candidates; a DOI lookup returns the work. Nothing
    # guarantees the best hit here is the paper being cited, so a weak candidate
    # is evidence that the search missed -- never evidence that the entry is
    # wrong. Only a candidate confident enough to *be* the work earns a metadata
    # comparison, which means MISMATCH on this route needs the authors to agree
    # first; a real title over the wrong authors reads the same as a search that
    # landed next door, and is reported as not found, with the neighbour named.
    if not confident_match(ref, best[0], best[2]):
        if silent:
            return False        # a search that never ran is not evidence
        ref.status = "NOT_FOUND"
        ref.notes.append(
            f"no confident match in {' or '.join(answered)}"
            + (f" (closest {best[0]:.2f}: \u201c{best[1][:90]}\u201d)" if best[1] else "")
            + "; a venue neither index covers reads the same way here as a work "
              "that was never published, so check this one by eye")
        return True
    # Nothing beyond this point can condemn the entry: a candidate only gets
    # here by carrying the cited surname at a title score the neighbours do not
    # reach, and a disagreeing date on a searched record is advisory. The search
    # route therefore reports VERIFIED, PARTIAL or NOT_FOUND, and MISMATCH is
    # left to the routes that fetch the work by its identifier.
    check_metadata(ref, best[1], best[2], best[3], best[4], authoritative=False)
    return True


def verify_rfc(ref: Reference, f: Fetcher) -> bool:
    data, unreachable = f.json(
        f"https://datatracker.ietf.org/api/v1/doc/document/rfc{ref.rfc}/?format=json")
    if data and data.get("title"):
        check_metadata(ref, data["title"], [], None, f"IETF datatracker RFC {ref.rfc}")
        if data.get("std_level"):
            ref.evidence.append(f"status: {data['std_level']}")
        return True
    rec = f.get(f"https://www.rfc-editor.org/rfc/rfc{ref.rfc}.txt", accept="text/plain")
    if not rec["ok"]:
        why = [w for w in (unreachable,
                           rec["error"] if lookup_failed(rec) else None) if w]
        if why:
            ref.unchecked = True
            ref.notes.append(f"RFC {ref.rfc} could not be checked ({'; '.join(why)})")
            return False
        ref.status = "NOT_FOUND"
        ref.notes.append(f"RFC {ref.rfc} not retrievable from datatracker or rfc-editor")
        return True
    head = rec["body"][:4000]
    if (ref.title and title_score(ref.title, head) < 0.10
            and normalise(ref.title)[:40] not in normalise(head)):
        ref.status = "PARTIAL"
        ref.notes.append(f"RFC {ref.rfc} exists but its text does not obviously "
                         "contain the cited title")
    else:
        ref.status = "VERIFIED"
    ref.evidence.append(f"rfc-editor: rfc{ref.rfc}.txt retrieved")
    return True


def verify_draft(ref: Reference, f: Fetcher) -> bool:
    data, unreachable = f.json(
        f"https://datatracker.ietf.org/api/v1/doc/document/{ref.draft}/?format=json")
    if unreachable:
        ref.unchecked = True
        ref.notes.append(f"the IETF datatracker could not be reached ({unreachable}), "
                         f"so {ref.draft} was not checked")
        return False
    if not data or not data.get("title"):
        ref.status = "NOT_FOUND"
        ref.notes.append(f"IETF datatracker has no draft named {ref.draft}")
        return True
    check_metadata(ref, data["title"], [], None, f"IETF datatracker {ref.draft}")
    current = str(data.get("rev") or "")
    if current:
        ref.evidence.append(f"current revision: -{current}")
        if ref.draft_rev and ref.draft_rev != current:
            ref.status = "STALE"
            ref.notes.append(
                f"cited as -{ref.draft_rev} but the current revision is -{current}; "
                "an Internet-Draft is a moving target, so confirm the cited text survived")
    if str(data.get("state") or "").lower() in {"expired", "dead", "replaced"}:
        ref.notes.append(f"datatracker state: {data['state']}")
    return True


TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
OG_TITLE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']', re.I)
SOFT_404 = re.compile(r"\b(404|not found|page (?:not|no longer) (?:found|available)|"
                      r"deleted|does not exist)\b", re.I)
BLOCK_INTERSTITIAL = re.compile(
    r"\b(just a moment|attention required|checking your browser|access denied|"
    r"403 forbidden|are you a robot|security check|please enable javascript)\b", re.I
)


def verify_web(ref: Reference, f: Fetcher) -> bool:
    if not ref.url:
        return False
    rec = f.get(ref.url)
    if not rec["ok"]:
        # Our own offline switch is not evidence about the link, and neither is
        # a timeout or a rate limit. Only a server saying the page is gone, or
        # a hostname that does not resolve, is evidence.
        if rec["error"] == "offline" or lookup_failed(rec):
            ref.status = "UNVERIFIABLE"
            ref.unchecked = rec["error"] != "offline"
            ref.notes.append(f"{ref.url} could not be reached ({rec['error']}); "
                             "unconfirmed rather than dead")
        else:
            ref.status = "LINK_DEAD"
            ref.notes.append(f"{ref.url} -> {rec['error']}")
        return True

    page_title = ""
    if m := OG_TITLE.search(rec["body"]):
        page_title = m.group(1)
    elif m := TITLE_TAG.search(rec["body"]):
        page_title = re.sub(r"<[^>]+>", " ", m.group(1))
    page_title = clean(page_title)[:200]
    ref.evidence.append(f"page title: \u201c{page_title}\u201d" if page_title
                        else f"HTTP {rec['status']}, no <title>")

    final = rec["final_url"]
    if final.rstrip("/") != ref.url.rstrip("/"):
        ref.notes.append(f"redirects to {final}")

    if page_title and SOFT_404.search(page_title):
        ref.status = "LINK_DEAD"
        ref.notes.append(f"page resolves but looks like an error page: \u201c{page_title}\u201d")
        return True

    if page_title and BLOCK_INTERSTITIAL.search(page_title):
        ref.status = "PARTIAL"
        ref.notes.append(
            f"page resolves but a bot/security block prevented reading the article: "
            f"\u201c{page_title}\u201d; confirm by eye"
        )
        return True

    if not page_title:
        ref.status = "PARTIAL"
        ref.notes.append("URL resolves but no title could be read (PDF or JS-rendered page); "
                         "confirm by eye")
        return True

    score = max(title_score(ref.title, page_title),
                title_score(ref.title, page_title + " " + ref.container))
    if score >= 0.55:
        ref.status = "VERIFIED"
    elif normalise(ref.name) and normalise(ref.name).split(" ")[0] in normalise(page_title):
        ref.status = "PARTIAL"
        ref.notes.append(f"page title \u201c{page_title}\u201d does not match the cited title, "
                         "though the publisher matches")
    else:
        ref.status = "MISMATCH"
        ref.notes.append(f"cited title vs page title mismatch (similarity {score:.2f}): "
                         f"\u201c{page_title}\u201d")
    if final.rstrip("/") != ref.url.rstrip("/") and ref.status == "VERIFIED":
        ref.status = "LINK_MOVED"
    return True


def verify(ref: Reference, f: Fetcher) -> None:
    if f.offline:
        # Absence of a lookup is not a finding about the citation.
        ref.status = "UNVERIFIABLE"
        ref.notes.append("offline mode: no source was contacted")
        return
    done = False
    asked = False               # was any authority actually consulted?
    if ref.arxiv:
        asked, done = True, verify_arxiv(ref, f)
    if not done and ref.doi:
        asked, done = True, verify_doi(ref, f)
    if not done and ref.rfc:
        asked, done = True, verify_rfc(ref, f)
    if not done and ref.draft:
        asked, done = True, verify_draft(ref, f)
    if not done and ref.kind == "paper":
        asked, done = True, verify_by_title(ref, f)
    if not done and ref.url:
        done = verify_web(ref, f)
    elif done and ref.url and ref.status in {"VERIFIED", "PARTIAL"} and ref.kind != "web":
        # Identifier checked out; still make sure the link the reader follows works.
        probe = f.get(ref.url)
        if not probe["ok"]:
            ref.notes.append(f"companion link {ref.url} -> {probe['error']}")
            if ref.status == "VERIFIED" and not lookup_failed(probe):
                ref.status = "LINK_DEAD"
    if not done and not ref.url:
        ref.status = "UNVERIFIABLE"
        if not asked:
            ref.notes.append("no DOI, arXiv id, RFC number or URL to check against")
        # If an authority was asked and still produced no verdict, its own note
        # already says which one went unanswered; repeating "nothing checkable"
        # over the top of that would be false.


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

# What 'an in-text citation with no matching entry' probably is, per style,
# when it is not a real orphan: the known false-positive shapes.
UNRESOLVED_CAVEATS = {
    "author-date": "_Expect false positives here: parenthetical years such as "
                   "`(ICLR 2023)` look like citations to a regex._",
    "numeric": "_Expect false positives here: a bracketed number may reference "
               "a figure, an equation or a table rather than a list entry._",
    "mla": "_Expect false positives here: parenthetical name-page pairs such as "
           "`(Smith 42)` in ordinary prose look like citations to a regex._",
    "notes": "_A marker here has no footnote definition, or its note could not "
             "be linked to any entry -- both are worth a look._",
}


def cite_label(c: Citation) -> str:
    """How a citation reads in the report, whatever form it was found in."""
    if c.form in ("numeric", "note"):
        return f"[{c.number}]"
    if c.form == "mla":
        return f"{c.name} {c.page}".strip()
    return f"{c.name} ({c.year}{c.suffix})"


def ref_label(r: Reference) -> str:
    prefix = f"[{r.number}] " if r.number else ""
    if not r.name:
        # No grammar claimed the entry; the title or raw text is all it has.
        return f"{prefix}{r.title[:60] or r.raw[:60]}"
    return f"{prefix}{r.name} ({r.year}{r.suffix})"


@dataclasses.dataclass
class CrossReferenceFindings:
    uncited: list[Reference]
    unresolved: list[Citation]
    ambiguous: list[str]

    @property
    def needs_attention(self) -> bool:
        # Unresolved citations have known false positives and do not gate CI.
        return bool(self.uncited or self.ambiguous)


def cross_reference_findings(refs: list[Reference],
                             cites: list[Citation]) -> CrossReferenceFindings:
    return CrossReferenceFindings(
        uncited=[r for r in refs if not r.cited_by],
        unresolved=[c for c in cites if not c.key],
        ambiguous=ambiguous_suffixes(refs, cites),
    )


def build_report(refs: list[Reference], cites: list[Citation], doc: str,
                 offline: bool, style: str = "", forced: bool = False,
                 polite: bool = False,
                 findings: CrossReferenceFindings | None = None) -> str:
    counts: dict[str, int] = {}
    for r in refs:
        counts[r.status] = counts.get(r.status, 0) + 1

    if findings is None:
        findings = cross_reference_findings(refs, cites)
    uncited, unresolved, ambiguous = findings.uncited, findings.unresolved, findings.ambiguous

    L = [f"# Citation check \u2014 {os.path.basename(doc)}", "",
         f"Run {time.strftime('%Y-%m-%d %H:%M')}"
         + ("  \u2014 **offline mode, nothing was verified against a live source**"
            if offline else ""), ""]
    if style:
        L += [f"Style: {style} ({'forced' if forced else 'detected'})", ""]
    L += [f"{len(refs)} reference entries, {len(cites)} in-text citation instances.", "",
          "## Summary", "", "| Status | Count | Meaning |", "|---|---:|---|"]
    meaning = {
        "VERIFIED": "matched an authoritative record",
        "PARTIAL": "found, but the match is loose \u2014 eyeball it",
        "MISMATCH": "**found something that disagrees with what you wrote**",
        "NOT_FOUND": "**searched and could not find it at all**",
        "LINK_DEAD": "**URL does not resolve**",
        "LINK_MOVED": "URL redirects elsewhere",
        "STALE": "**cited revision superseded**",
        "UNVERIFIABLE": "nothing checkable in the entry, or the check could not be run",
    }
    for status in sorted(counts, key=lambda s: SEVERITY[s]):
        L.append(f"| {status} | {counts[status]} | {meaning[status]} |")

    problems = [r for r in refs if r.status in PROBLEM_STATUSES]
    L += ["", f"**{len(problems)} entries need attention.**" if problems
          else "**No entry failed verification.**", ""]

    # An entry nobody could look up is not a clean entry, and burying that in a
    # per-entry note lets a run of them read as a quiet pass.
    unchecked = [r for r in refs if r.unchecked]
    if unchecked:
        L += [f"**{len(unchecked)} entries could not be checked at all**, because an "
              "authority was unreachable. They are recorded as unverified rather than "
              "as findings, so a clean run above does not mean these are sound."
              + ("" if polite else
                 " Crossref and OpenAlex both keep a separate queue for callers who "
                 "identify themselves, and OpenAlex rate-limits anonymous search when "
                 "it is busy: `--mailto you@example.com` avoids most of this."), ""]

    L += ["## Entries needing attention", ""]
    if not problems:
        L.append("_None._")
    for r in sorted(problems, key=lambda r: SEVERITY[r.status]):
        L += [f"### {r.status} \u2014 {ref_label(r)}", "",
              f"> {r.raw}", ""]
        for n in r.notes:
            L.append(f"- {n}")
        for e in r.evidence:
            L.append(f"- _{e}_")
        L.append("")

    soft = [r for r in refs if r.status in {"PARTIAL", "LINK_MOVED", "UNVERIFIABLE"}]
    L += ["## Worth a glance", ""]
    if not soft:
        L.append("_None._")
    for r in sorted(soft, key=lambda r: SEVERITY[r.status]):
        note = r.notes[0] if r.notes else (r.evidence[0] if r.evidence else "")
        L.append(f"- **{r.status}** \u2014 {ref_label(r)}: {note}")
    L.append("")

    L += ["## Verified", ""]
    ok = [r for r in refs if r.status == "VERIFIED"]
    for r in ok:
        L.append(f"- {ref_label(r)} \u2014 {r.title[:80]}"
                 + (f" \u2014 _{r.evidence[0][:100]}_" if r.evidence else ""))
    if not ok:
        L.append("_None._")

    L += ["", "## Cross-reference consistency", "",
          "### Reference-list entries never cited in the text", ""]
    L += [f"- {ref_label(r)} \u2014 {r.title[:90]}" for r in uncited] or ["_None._"]

    L += ["", "### In-text citations with no matching reference entry", "",
          UNRESOLVED_CAVEATS.get(style, UNRESOLVED_CAVEATS["author-date"]), ""]
    seen = set()
    rows = []
    for c in unresolved:
        k = (c.form, c.number, c.name, c.year, c.suffix)
        if k in seen:
            continue
        seen.add(k)
        rows.append(f"- `{cite_label(c)}` \u2014 \u2026{c.context[:150]}\u2026")
    L += rows or ["_None._"]

    L += ["", "### Ambiguous year suffixes", ""]
    L += ambiguous or ["_None._"]

    L += ["", "## What this run did not check", "",
          "- Whether each source **supports the claim it is attached to**. That needs "
          "reading, not fetching \u2014 run `touchneedle.py claims` and work the list.",
          "- Page numbers, edition, and publisher details.",
          "- Anything behind a paywall or a JS-rendered page, which comes back PARTIAL.", ""]
    return "\n".join(L)


def ambiguous_suffixes(refs: list[Reference], cites: list[Citation]) -> list[str]:
    """Same author+year split across a/b in the list must be cited with a suffix.

    Only entries cited by name carry suffixes; in a numbered list the marker
    disambiguates, so IEEE and Vancouver entries are not considered.
    """
    groups: dict[str, list[Reference]] = {}
    for r in refs:
        if r.style not in ("author-date", "chicago-ad", "mla"):
            continue
        groups.setdefault(f"{normalise(r.name)}|{r.year}", []).append(r)
    out = []
    for stem, group in groups.items():
        if len(group) < 2:
            continue
        name, year = group[0].name, group[0].year
        bare = [c for c in cites
                if citation_key(c.name, c.year, "") == stem and not c.suffix]
        if bare:
            out.append(f"- `{name} ({year})` is cited without a suffix "
                       f"{len(bare)}\u00d7, but the list has "
                       f"{', '.join(year + r.suffix for r in group)}")
        if any(not r.suffix for r in group):
            out.append(f"- reference list has {len(group)} entries for {name} ({year}) "
                       "but not all carry an a/b suffix")
    return out


def build_claims(refs: list[Reference], cites: list[Citation], doc: str) -> str:
    by_key = {r.key: r for r in refs}
    L = [f"# Claim-support worklist \u2014 {os.path.basename(doc)}", "",
          "One row per in-text citation. For each, read the source and decide whether it "
          "supports the sentence: **SUPPORTED / PARTIAL / UNSUPPORTED / INACCESSIBLE**. "
          "Do not guess \u2014 if the source cannot be read, say INACCESSIBLE.", ""]
    # Numeric documents cite constantly -- a review article can carry sixty
    # markers over twenty sources -- so collapse to one row per unique
    # source-and-sentence pair and keep the worklist finishable.
    seen: set[tuple[str, str]] = set()
    rows: list[Citation] = []
    for c in cites:
        if c.form in ("numeric", "note"):
            marker = (c.key or f"#{c.number}", c.context)
            if marker in seen:
                continue
            seen.add(marker)
        rows.append(c)
    ordered = sorted(rows, key=lambda c: (c.key or "zzz", c.year))
    for n, c in enumerate(ordered, 1):
        r = by_key.get(c.key)
        L += [f"## {n}. {cite_label(c)} \u2014 {c.form}", ""]
        if r:
            src = r.url or (f"arXiv:{r.arxiv}" if r.arxiv else "") or (
                f"doi:{r.doi}" if r.doi else "") or (f"RFC {r.rfc}" if r.rfc else "")
            L += [f"- **Source**: {r.title or r.raw[:100]}",
                  f"- **Locate at**: {src or '_no locator in the reference entry_'}"]
        else:
            L.append("- **Source**: _no matching reference-list entry_")
        L += ["- **Claim in the text**:", f"  > \u2026{c.context}\u2026", "",
              "- **Verdict**: ", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def collect(doc: str, style: str = "auto") -> tuple[list[Reference], list[Citation], str]:
    """Parse a document, returning (references, citations, effective style).

    An explicit style other than 'auto' overrides detection; the entries are
    still parsed by whichever grammar fits them, so a forced style changes
    the gates and the in-text finders, not the per-entry dispatch.
    """
    text, notes = harvest_notes(load_text(doc))
    if notes and not (HEADING.search(text) or BARE_HEADING.search(text)):
        # A notes-only document: the footnotes are the reference list, and
        # there is no heading to find.
        entries: list[str] = []
        body, heading = text, "Notes"
    else:
        body, _, heading, entries = split_document(text)
    body = strip_code(body)
    effective = style if style in STYLES else detect_style(body, entries, heading, notes)
    refs = [parse_entry(e) for e in entries]
    if not refs and not notes:
        sys.exit("error: found a References heading but could not parse any entries under it")
    note_map: dict[int, str] = {}
    if effective == "notes" and notes:
        note_refs, note_map = build_note_refs(notes, refs)
        refs = refs + note_refs
    elif notes:
        # Not a notes document, but harvest_notes took the definitions out of
        # the text and the citations inside them are still citations: put them
        # back where the finders can see them.
        body += "\n\n" + "\n\n".join(notes[n] for n in sorted(notes))
    found = find_citations(body)
    if effective == "numeric":
        # A bracketed number is only a citation in a numeric document;
        # elsewhere '[3]' is a row, an equation or a figure.
        found += find_numeric_citations(body)
    if effective == "mla":
        # Author-page parentheticals are only citations in an MLA document;
        # elsewhere '(Smith 42)' is prose, and finding it would be noise.
        found += find_mla_citations(body)
    if note_map:
        found += find_note_citations(body)
    cites = match_citations(refs, found, note_map)
    return refs, cites, effective


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("check", "claims"):
        p = sub.add_parser(name)
        p.add_argument("doc", help="Markdown or .docx document")
        p.add_argument("--out", help="write the report here instead of stdout")
        p.add_argument("--style", default="auto", choices=["auto", *STYLES],
                       help="citation style of the document; the default, auto, "
                            "infers it from the reference list and the in-text "
                            "markers")
    chk = sub.choices["check"]
    chk.add_argument("--json", dest="json_out", help="also write machine-readable results")
    chk.add_argument("--offline", action="store_true",
                     help="parse and cross-check only; make no network calls")
    chk.add_argument("--mailto", default=os.environ.get("CITATION_CHECK_MAILTO"),
                     help="contact address sent to Crossref/OpenAlex for their polite "
                          "rate-limit pool (optional; also read from CITATION_CHECK_MAILTO)")
    chk.add_argument("--cache", default=".touchneedle-cache", help="HTTP cache directory")
    chk.add_argument("--timeout", type=int, default=25)

    args = ap.parse_args()
    refs, cites, style = collect(args.doc, args.style)
    findings: CrossReferenceFindings | None = None

    if args.cmd == "claims":
        out = build_claims(refs, cites, args.doc)
    else:
        f = Fetcher(args.cache, timeout=args.timeout, offline=args.offline,
                    mailto=args.mailto)
        for i, r in enumerate(refs, 1):
            print(f"[{i}/{len(refs)}] {ref_label(r)} …",
                  file=sys.stderr, flush=True)
        if refs:
            workers = min(8, len(refs))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(verify, r, f) for r in refs]
                for i, (r, future) in enumerate(zip(refs, futures, strict=True), 1):
                    future.result()
                    print(f"    [{i}/{len(refs)}] {r.status}",
                          file=sys.stderr, flush=True)
        findings = cross_reference_findings(refs, cites)
        out = build_report(refs, cites, args.doc, args.offline, style,
                           args.style != "auto", polite=bool(args.mailto),
                           findings=findings)
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as fh:
                json.dump({"style": style,
                           "references": [dataclasses.asdict(r) for r in refs],
                           "citations": [dataclasses.asdict(c) for c in cites]},
                          fh, indent=2, ensure_ascii=False)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(out)

    if args.cmd == "check" and findings is not None and (
        findings.needs_attention or any(r.status in PROBLEM_STATUSES for r in refs)
    ):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
