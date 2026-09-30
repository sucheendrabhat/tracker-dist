"""
indexer.py
==========

Parses GATE Overflow-style subject PDFs -- the ones with entries like:

    3.6.27 Binary Tree: GATE CSE 2007 | Question: 12

a chapter list at the front like:

    2 Compiler Design (242)

and an "Answer Keys" grid at the end of every chapter, e.g.:

    3.6.25   D   3.6.26   D   3.6.27   C   3.6.28   B   3.6.29   A

build_index() turns a PDF like this into a flat dict:

    {
        "3.6.27": {
            "title": "Binary Tree: GATE CSE 2007 | Question: 12",
            "answer": "C",
            "url": "https://gateoverflow.in/1210/gate-cse-2007-question-12",
            "page": 221,
            "chapter_num": 3,
            "chapter_name": "Programming and DS: Data Structures",
        },
        ...
    }

Only PyMuPDF (pip install pymupdf) is required -- no external binaries.

---------------------------------------------------------------------
How y_top/y_bottom are found (v4 -- fixes half-cut export snapshots)
---------------------------------------------------------------------
Each question's crop box used to be bounded by the position of the
*next* question's clickable GATE Overflow link: y_top came from this
question's own link, y_bottom from the next one's, built by zipping
the list of header lines together with the list of link annotations
on the page in order.

That's fragile: it assumes every question has exactly one link and
that both lists stay in the same order with the same count. Whenever
a single question on a page is missing its link (recently-added
questions sometimes don't have a stable permalink yet -- this showed
up on a GATE CSE 2026 question), the zip silently shifts by one for
every question after it on that page, and the crop boxes for however
many questions follow no longer line up with their actual content --
showing up as a half-cut snapshot in an export.

v4 finds each question's box using only its own text, the same way
the companion question-bank site's extractor does against this same
PDF family: a question's id block (e.g. "3.6.27") paired with a
sibling block containing "Question:" marks its top; scanning forward
from there for that same question's own "Answer key" button marks its
bottom. Neither depends on link annotations existing or being in any
particular order, so a missing link can no longer cascade into wrong
positions for other questions -- it just means that one question's
"Open on GATE Overflow" link is empty, without breaking anyone else's
crop.
"""

import hashlib
import json
import os
import re

import fitz  # PyMuPDF


# A question id like "3.6.27" (chapter.section.number)
ID_RE = re.compile(r"\d+\.\d+\.\d+")

# A block that is ONLY an id, e.g. "3.6.27" with nothing else -- this is
# how each question's header id renders as its own text block.
ID_ONLY_RE = re.compile(r"^\d+\.\d+\.\d+$")

# What an answer-key VALUE can look like: a letter (or letters joined with
# ';' for multi-select), N/A, TBA, True/False, a plain number, or a ratio
# like "5:5" (with or without spaces around the colon).
VALUE_TOKEN = (
    r"(?:N/A|TBA|True|False"
    r"|[A-Za-z](?:;[A-Za-z])*"
    r"|-?\d+(?:\.\d+)?\s*:\s*-?\d+(?:\.\d+)?"
    r"|-?\d+(?:\.\d+)?)"
)
VALUE_ONLY_RE = re.compile(r"^" + VALUE_TOKEN + r"$")

# Matches "<id> <value>" pairs anywhere in the document -- this is how the
# per-chapter "Answer Keys" grids are laid out. The strict VALUE_TOKEN means
# this will NOT accidentally match ordinary header lines like
# "3.6.27 Binary Tree: ..." (the word "Binary" doesn't fit the value shape).
ANSWER_KEY_RE = re.compile(r"(\d+\.\d+\.\d+)\s+(" + VALUE_TOKEN + r")(?=\s|$)")

# The clickable "question" link on gateoverflow.in, with or without the
# SEO slug, e.g. https://gateoverflow.in/1210/gate-cse-2007-question-12
# or the bare https://gateoverflow.in/1210
QLINK_RE = re.compile(
    r"https?://(?:www\.)?gateoverflow\.in/(\d+)(?:/([\w-]+))?/?$", re.IGNORECASE
)

# A chapter/subject heading, e.g. "2 Compiler Design (242)" (optionally
# followed by a table-of-contents page number, e.g. "...  (242)   117").
CHAPTER_RE = re.compile(r"^(\d+)\s+([A-Za-z][^()]*?)\s*\((\d+)\)\s*\d*\s*$")

CACHE_VERSION = 4  # bumped: v4 changes how y_top/y_bottom are computed,
                    # so any on-disk .gateindex.json cache from before
                    # must be treated as stale and rebuilt.


def file_fingerprint(path):
    """Fast fingerprint used only to invalidate the on-disk index cache
    for THIS path (path + size + mtime). Cheap, but NOT stable across a
    file being moved or renamed -- see content_fingerprint() for that."""
    stat = os.stat(path)
    raw = f"{path}|{stat.st_size}|{stat.st_mtime}|{CACHE_VERSION}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def content_fingerprint(path, chunk_size=1024 * 1024):
    """A hash of the file's actual bytes, independent of its path or
    filename. Used to recognize 'the same PDF' in the database even if
    it's been moved, renamed, or re-downloaded -- so progress/notes tied
    to it aren't lost."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _cache_path(pdf_path):
    return os.path.splitext(pdf_path)[0] + ".gateindex.json"


def _find_markers(page_blocks):
    """Every question-start marker on the whole document: a block that is
    JUST a dotted id ('3.6.27') sitting at the same height as a sibling
    block containing 'Question:'. That combination only happens at the
    top of a real question -- never in the table of contents or the
    answer-key grid -- so it's a reliable anchor that doesn't depend on
    link annotations at all."""
    markers = []
    for pno, blocks in enumerate(page_blocks):
        seen_ids = set()
        for b in blocks:
            x0, y0, x1, y1, text, bno, btype = b
            stripped = text.strip()
            if not ID_ONLY_RE.match(stripped) or stripped in seen_ids:
                continue
            sibling = next(
                (b2 for b2 in blocks if abs(b2[1] - y0) < 2 and "Question:" in b2[4]),
                None,
            )
            if sibling is None:
                continue
            title = sibling[4].strip().replace("\n", " ")
            if VALUE_ONLY_RE.match(title):
                continue  # a stray single-entry answer-key row, not a real header
            markers.append({"id": stripped, "page": pno, "y0": y0, "title": title})
            seen_ids.add(stripped)
    markers.sort(key=lambda m: (m["page"], m["y0"]))
    return markers


def _find_question_end(page_blocks, start_page, start_y, max_lookahead_pages=15):
    """Scan forward from (start_page, start_y) for THIS question's own
    'Answer key' button -- the true bottom of its content, independent
    of where the next question (or a chapter's appendix / next chapter's
    intro pages) happens to start. Returns (end_page, end_y) or None if
    no Answer key button turns up within the lookahead window."""
    n_pages = len(page_blocks)
    ordered = []
    for p in range(start_page, min(start_page + max_lookahead_pages, n_pages)):
        lo = start_y if p == start_page else -1
        for b in page_blocks[p]:
            if b[1] >= lo:
                ordered.append((p, b[1], b[3], b[4]))  # page, y0, y1, text
    ordered.sort(key=lambda t: (t[0], t[1]))

    for p, y0, y1, text in ordered:
        if "Answer key" in text:
            return p, y1
    return None


def build_index(pdf_path, progress_cb=None, use_cache=True):
    """
    Parse a GATE Overflow-style subject PDF.

    Returns: { question_id: {"title", "answer", "url", "page",
                              "chapter_num", "chapter_name",
                              "y_top", "y_bottom"} }

    progress_cb(done_pages, total_pages) is called periodically if given.
    """
    cache_file = _cache_path(pdf_path)
    fingerprint = file_fingerprint(pdf_path)

    if use_cache and os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("fingerprint") == fingerprint:
                return cached["index"]
        except Exception:
            pass  # cache unreadable/corrupt -> just rebuild

    doc = fitz.open(pdf_path)
    n_pages = len(doc)

    chapters = {}         # chapter_num (int) -> chapter_name (str)
    page_blocks = []       # per-page list of (x0,y0,x1,y1,text,bno,btype)
    page_qlinks = []       # per-page list of (y0, gid, uri), in top-to-bottom order

    for pno in range(n_pages):
        page = doc[pno]
        # sort=True reconstructs proper left-to-right, top-to-bottom
        # reading order for each block, matching how a person reads
        # the page.
        blocks = page.get_text("blocks", sort=True)
        page_blocks.append(blocks)

        for b in blocks:
            for line in b[4].split("\n"):
                cm = CHAPTER_RE.match(line)
                if cm:
                    chapters.setdefault(int(cm.group(1)), cm.group(2).strip())

        # Question links, kept per-page and only used to fill in "url"
        # for whichever question they spatially sit above -- no longer
        # used to determine crop positions, so a missing or extra link
        # can't shift anything else out of place.
        qlinks = []
        for link in page.get_links():
            if link.get("kind") != 2:  # 2 == URI link
                continue
            uri = link.get("uri", "")
            if "/tag/" in uri:
                continue
            m = QLINK_RE.match(uri)
            if m:
                qlinks.append((link["from"].y0, m.group(1), uri))
        qlinks.sort(key=lambda t: t[0])
        collapsed = []
        for y, gid, uri in qlinks:
            # a question's two links (descriptive-slug + bare) land at
            # essentially the same position -- collapse those together
            if collapsed and collapsed[-1][1] == gid:
                continue
            collapsed.append((y, gid, uri))
        page_qlinks.append(collapsed)

        if progress_cb:
            progress_cb(pno + 1, n_pages)

    markers = _find_markers(page_blocks)

    index = {}
    for i, m in enumerate(markers):
        qid, pno, y0, title = m["id"], m["page"], m["y0"], m["title"]

        found = _find_question_end(page_blocks, pno, y0)
        if found is not None:
            end_page, end_y = found
        elif i + 1 < len(markers) and markers[i + 1]["page"] == pno:
            # fallback: no Answer key button found nearby (shouldn't
            # normally happen) -- bound by the next question on this
            # same page rather than risk running away across pages
            end_page, end_y = pno, max(y0, markers[i + 1]["y0"] - 4)
        else:
            end_page, end_y = pno, doc[pno].rect.height

        # Rendering only ever crops a single page (see
        # render_question_snapshot / export.py's _SnapshotSource), so if
        # a question's own Answer key button landed on a later page
        # (a question that runs long enough to cross a page break),
        # clip to the bottom of its start page rather than under-crop
        # using a boundary from the wrong page.
        if end_page != pno:
            end_y = doc[pno].rect.height

        url = None
        for y, gid, uri in page_qlinks[pno]:
            if y >= y0 - 2:
                url = uri
                break

        index[qid] = {
            "title": title,
            "page": pno + 1,
            "url": url,
            "y_top": max(0, y0 - 6),
            "y_bottom": end_y,
        }

    doc.close()

    # Answer keys can be pulled from the whole document's text at once.
    full_text = "\n".join(b[4] for blocks in page_blocks for b in blocks)
    for qid, value in ANSWER_KEY_RE.findall(full_text):
        entry = index.setdefault(qid, {})
        entry["answer"] = value
        entry.setdefault("title", None)
        entry.setdefault("page", None)
        entry.setdefault("url", None)
        entry.setdefault("y_top", None)
        entry.setdefault("y_bottom", None)

    for qid, entry in index.items():
        entry.setdefault("answer", None)

    # Attach chapter/subject info to every question, based on the first
    # number of its id (e.g. "3.6.27" -> chapter 3).
    for qid, entry in index.items():
        chap_num = int(qid.split(".")[0])
        entry["chapter_num"] = chap_num
        entry["chapter_name"] = chapters.get(chap_num, f"Chapter {chap_num}")

    if use_cache:
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"fingerprint": fingerprint, "index": index}, f)
        except Exception:
            pass  # not fatal if we can't write a cache file

    return index


def render_question_snapshot(pdf_path, page_num, y_top, y_bottom, dpi=200, pad=4):
    """
    Renders a PNG image of just one question's block on the page, cropped
    from the actual source PDF -- this is what lets an export show the
    question exactly as it's typeset (formulas, tables and all) rather
    than just a text title.

    page_num is 1-indexed. Returns PNG bytes, or None if it can't be
    rendered (e.g. missing position data).
    """
    if y_top is None or y_bottom is None:
        return None
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_num - 1]
        rect = page.rect
        clip = fitz.Rect(
            rect.x0 + 2,
            max(rect.y0, y_top - pad),
            rect.x1 - 2,
            min(rect.y1, y_bottom + pad),
        )
        if clip.height <= 2 or clip.width <= 2:
            return None
        pix = page.get_pixmap(dpi=dpi, clip=clip)
        return pix.tobytes("png")
    finally:
        doc.close()
