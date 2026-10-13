"""PDF -> corpus document: text layer, tables, OCR fallback, section breadcrumbs.

The output is the same {"doc_id", "acl", "sections"} shape the JSON corpus uses,
so chunking, ACL validation/inheritance and PII redaction all stay in
ingest.chunk_document. Nothing here decides who may read a chunk: the document
ACL passed in is stamped on the document and every extracted chunk (body text,
table text, OCR text) inherits it there. Extracted text is untrusted data.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from itertools import groupby
from pathlib import Path

import pdfplumber

OCR_DPI = 300
MAX_CHUNK_CHARS = 1200
REPEAT_PAGES = 3          # a header/footer line must repeat on this many pages
EDGE_FRACTION = 0.08      # ...and sit in the top/bottom 8% of the page
GUTTER_MIN_WIDTH = 8      # points of vertical whitespace that make a column gutter
EDGE_GAP = 12             # points of white space that set a running head/foot apart
MIN_COLUMN_WIDTH = 0.15   # of the page width
MIN_COLUMN_SHARE = 0.12   # a real column holds at least this share of the page's words
LOSS_FALLBACK = 0.99      # share of word characters a column-split read must keep
MIN_USABLE_WORDS = 5      # fewer alphabetic words than this = no usable text layer
_DIGITS = re.compile(r"\d+")
_WORD = re.compile(r"[A-Za-z]{2,}")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


class OcrUnavailable(RuntimeError):
    pass


def _norm(text: str) -> str:
    """Digits collapse (page numbers) and tokens sort (recto/verso footers swap order)."""
    return " ".join(sorted(_DIGITS.sub("#", text).split()))


def has_usable_text(text: str) -> bool:
    return len(_WORD.findall(text)) >= MIN_USABLE_WORDS


def _gutters(words: list[dict], width: float) -> list[float]:
    """x of each vertical whitespace channel splitting the page into columns (maybe none)."""
    if len(words) < 30:
        return []
    tolerance = max(1, len(words) // 200)  # a heading row or two may legitimately span the gutter
    xs = range(int(width * 0.05), int(width * 0.95) + 1)
    clear = [sum(1 for w in words if w["x0"] < x < w["x1"]) <= tolerance for x in xs]
    runs = []
    for is_clear, grp in groupby(zip(xs, clear), key=lambda t: t[1]):
        run = [x for x, _ in grp]
        if is_clear and len(run) >= GUTTER_MIN_WIDTH:
            runs.append((len(run), (run[0] + run[-1]) / 2))
    cuts: list[float] = []
    for _, centre in sorted(runs, reverse=True):  # widest first; an indent gap inside a column is not a gutter
        if all(abs(centre - c) >= width * MIN_COLUMN_WIDTH for c in cuts):
            cuts.append(centre)
    cuts.sort()
    while cuts:  # a channel next to a nearly empty strip is a margin, not a gutter
        edges = [0.0, *cuts, width]
        share = [sum(1 for w in words if edges[i] <= (w["x0"] + w["x1"]) / 2 < edges[i + 1]) / len(words)
                 for i in range(len(edges) - 1)]
        low = min(range(len(share)), key=share.__getitem__)
        if share[low] >= MIN_COLUMN_SHARE:
            break
        del cuts[min(low, len(cuts) - 1)]
    return cuts


def _line(raw: dict, page_height: float) -> dict:
    sizes = [c["size"] for c in raw["chars"]]
    bold = sum("bold" in c["fontname"].lower() for c in raw["chars"]) * 2 > len(raw["chars"])
    return {"text": raw["text"].strip(), "size": round(statistics.median(sizes) * 2) / 2,
            "bold": bold, "top": raw["top"], "bottom": raw["bottom"],
            "edge": raw["top"] < page_height * EDGE_FRACTION
            or raw["bottom"] > page_height * (1 - EDGE_FRACTION)}


def _region_lines(page, bbox: tuple[float, float, float, float]) -> list[dict]:
    px0, ptop, px1, pbottom = page.bbox  # some PDFs have a sub-point negative origin
    x0, top, x1, bottom = max(bbox[0], px0), max(bbox[1], ptop), min(bbox[2], px1), min(bbox[3], pbottom)
    if bottom - top <= 0 or x1 - x0 <= 0:
        return []
    return [_line(r, page.height) for r in page.within_bbox((x0, top, x1, bottom)).extract_text_lines(return_chars=True)
            if r["text"].strip()]


def _columns(page, words: list[dict], top: float, bottom: float) -> list[dict]:
    """Column-by-column reading order between full-width bands; one column if no gutter."""
    w = page.width
    words = [x for x in words if top <= x["top"] and x["bottom"] <= bottom]
    cuts = _gutters(words, w)
    edges = [0.0, *cuts, w]
    spanning = [x for x in words if any(x["x0"] < c < x["x1"] for c in cuts)]
    bands = sorted((min(x["top"] for x in grp), max(x["bottom"] for x in grp))
                   for grp in _spanning_groups(spanning))
    out: list[dict] = []
    cursor = top
    for b_top, b_bottom in [*bands, (bottom, bottom)]:
        for left, right in zip(edges, edges[1:]):
            out += _region_lines(page, (left, cursor, right, b_top))
        out += _region_lines(page, (0, b_top, w, b_bottom))
        cursor = b_bottom
    return out


def _page_lines(page) -> list[dict]:
    """Reading-order lines. Running heads/feet are read full-width, never column-split."""
    x0, y0, x1, y1 = page.bbox  # printers' slugs sit off the page box; they are not content
    words = [x for x in page.extract_words()
             if x["x0"] >= x0 and x["x1"] <= x1 and x["top"] >= y0 and x["bottom"] <= y1]
    w, h = page.width, page.height
    head = [x for x in words if x["top"] < h * EDGE_FRACTION]
    foot = [x for x in words if x["bottom"] > h * (1 - EDGE_FRACTION)]
    top = max((x["bottom"] for x in head), default=0)
    bottom = min((x["top"] for x in foot), default=h)
    # a running head/foot is set apart by white space; body text that starts inside the band is not one
    below = [x["top"] for x in words if x not in head]
    above = [x["bottom"] for x in words if x not in foot]
    if head and min(below, default=h) - top < EDGE_GAP:
        top = 0
    if foot and bottom - max(above, default=0) < EDGE_GAP:
        bottom = h
    if top >= bottom:
        top, bottom = 0, h
    lines = (_region_lines(page, (0, 0, w, top)) + _columns(page, words, top, bottom)
             + _region_lines(page, (0, bottom, w, h)))
    wanted = sum(len(x["text"]) for x in words)
    if sum(len("".join(x["text"].split())) for x in lines) < wanted * LOSS_FALLBACK:
        return _region_lines(page, (0, 0, w, h))  # a cut dropped text: reading order loses to completeness
    return lines


def _spanning_groups(spanning: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for word in sorted(spanning, key=lambda x: x["top"]):
        if groups and word["top"] <= max(x["bottom"] for x in groups[-1]):
            groups[-1].append(word)
        else:
            groups.append([word])
    return groups


def table_to_markdown(rows: list[list[str]]) -> str:
    head, body = rows[0], rows[1:]
    esc = lambda c: c.replace("|", "\\|")  # noqa: E731
    lines = ["| " + " | ".join(esc(c) for c in head) + " |",
             "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in body]
    return "\n".join(lines)


def table_to_sentences(rows: list[list[str]]) -> list[str]:
    """One sentence per data row: '<H0> <v0>: <H1> = <v1>; ...' (empty cells skipped)."""
    head = [h or f"column {i + 1}" for i, h in enumerate(rows[0])]
    out = []
    for r in rows[1:]:
        pairs = [f"{h} = {v}" for h, v in zip(head[1:], r[1:]) if v]
        subject = f"{head[0]} {r[0]}" if r[0] else head[0]
        out.append(f"{subject}: " + "; ".join(pairs).rstrip(".") + "." if pairs else f"{subject}.")
    return out


def _clean_table(raw: list[list[str | None]]) -> list[list[str]] | None:
    rows = [[" ".join((c or "").split()) for c in r] for r in raw]
    rows = [r for r in rows if any(r)]
    if len(rows) < 2 or max(len(r) for r in rows) < 2:
        return None
    width = max(len(r) for r in rows)
    return [r + [""] * (width - len(r)) for r in rows]


def page_tables(page) -> list[tuple[tuple, list[list[str]]]]:
    found = []
    for t in page.find_tables():
        rows = _clean_table(t.extract())
        if rows:
            found.append((t.bbox, rows))
    return found


def ocr_page(page, dpi: int = OCR_DPI) -> str:
    hint = ("page has no usable text layer and OCR is unavailable: install the tesseract "
            "binary (brew/apt tesseract-ocr) and `pip install pytesseract`")
    try:
        import pytesseract
    except ImportError as exc:
        raise OcrUnavailable(hint) from exc
    try:
        return pytesseract.image_to_string(page.to_image(resolution=dpi).original)
    except pytesseract.TesseractNotFoundError as exc:
        raise OcrUnavailable(hint) from exc


def _outside(bboxes: list[tuple]):
    def keep(obj: dict) -> bool:
        if obj["object_type"] != "char":
            return True
        cx, cy = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
        return not any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in bboxes)
    return keep


def _strip_running_heads(pages: list[list[dict]]) -> int:
    """Drop page-edge lines that repeat (modulo digits) on >= REPEAT_PAGES pages."""
    seen: Counter[str] = Counter()
    for lines in pages:
        seen.update({_norm(x["text"]) for x in lines if x["edge"]})
    drop = {k for k, n in seen.items() if n >= REPEAT_PAGES}
    removed = 0
    for lines in pages:
        keep = [x for x in lines if not (x["edge"] and _norm(x["text"]) in drop)]
        removed += len(lines) - len(keep)
        lines[:] = keep
    return removed


def _body_size(pages: list[list[dict]]) -> float:
    weight: Counter[float] = Counter()
    for lines in pages:
        for x in lines:
            weight[x["size"]] += len(x["text"])
    return weight.most_common(1)[0][0] if weight else 10.0


def _is_heading(x: dict, body: float) -> bool:
    text = x["text"]
    # ". " inside the line = run-in head ("Estimated tax. The corporation...") or a dot-leader TOC row
    if len(text) > 100 or text[-1] in ".,;:" or ". " in text or not _WORD.search(text):
        return False
    return x["size"] >= body * 1.15 or (x["bold"] and x["size"] >= body - 0.5)


def _paragraphs(lines: list[dict]) -> list[str]:
    paras: list[str] = []
    prev = None
    for x in lines:
        new = (prev is None or x["top"] - prev["bottom"] > 0.5 * x["size"]
               or x["top"] < prev["top"] or x["text"][0] in "•▪●")
        if new:
            paras.append(x["text"])
        elif paras[-1].endswith("-") and x["text"][0].islower():
            paras[-1] += x["text"]  # keep the hyphen: "five-"+"year" is a compound, "ad-"+"ministration" still tokenizes
        else:
            paras[-1] += " " + x["text"]
        prev = x
    return paras


def _pack(paras: list[str], limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Greedy-merge short paragraphs; split overlong ones on sentence ends."""
    pieces: list[str] = []
    for p in paras:
        if len(p) <= limit:
            pieces.append(p)
            continue
        cur = ""
        for s in _SENTENCE_END.split(p):
            while len(s) > limit:
                if cur:
                    pieces.append(cur)
                    cur = ""
                pieces.append(s[:limit])
                s = s[limit:]
            if cur and len(cur) + 1 + len(s) > limit:
                pieces.append(cur)
                cur = s
            else:
                cur = f"{cur} {s}".strip()
        pieces.append(cur)
    out: list[str] = []
    for p in pieces:
        if out and len(out[-1]) + 1 + len(p) <= limit and len(out[-1]) < limit // 4:
            out[-1] += " " + p
        else:
            out.append(p)
    return out


def extract_pdf(path: str | Path, title: str | None = None, dpi: int = OCR_DPI,
                max_pages: int | None = None) -> dict:
    """-> {"sections": [{"title", "paragraphs": [{"text"}]}], "stats": {...}}."""
    page_lines: list[list[dict]] = []
    page_tables_: list[list[list[list[str]]]] = []
    ocr_texts: dict[int, str] = {}
    shapes: list[tuple[int, int]] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages[:max_pages]):
            tables = page_tables(page)
            body = page.filter(_outside([b for b, _ in tables])) if tables else page
            lines = _page_lines(body)
            if not has_usable_text(" ".join(x["text"] for x in lines)) and not tables:
                text = ocr_page(page, dpi)
                if text.strip():
                    ocr_texts[i] = text
                lines = []
            page_lines.append(lines)
            page_tables_.append([rows for _, rows in tables])
            shapes += [(len(rows), len(rows[0])) for _, rows in tables]
    removed = _strip_running_heads(page_lines)
    body_size = _body_size(page_lines)
    keys = sorted({(x["size"], x["bold"]) for ls in page_lines for x in ls
                   if _is_heading(x, body_size)}, reverse=True)
    root = title or Path(path).stem
    stack: list[str] = []
    sections: list[dict] = []
    buf: list[dict] = []
    heading_run: list[str] = []
    run_level = 0

    def flush() -> None:
        paras = _pack(_paragraphs(buf)) if buf else []
        if paras:
            crumb = " > ".join([root, *stack])
            sections.append({"title": crumb, "paragraphs": [{"text": f"{crumb}\n{p}"} for p in paras]})
        buf.clear()

    def close_heading() -> None:
        nonlocal heading_run
        if heading_run:
            del stack[run_level:]
            stack.append(" ".join(heading_run))
            heading_run = []

    def add_extras(i: int) -> None:
        crumb = " > ".join([root, *stack])
        for rows in page_tables_[i]:
            texts = [table_to_markdown(rows), *table_to_sentences(rows)]
            sections.append({"title": crumb, "paragraphs": [{"text": f"{crumb}\n{t}"} for t in _pack(texts)]})
        if i in ocr_texts:
            paras = [p.replace("\n", " ").strip() for p in re.split(r"\n\s*\n", ocr_texts[i])]
            paras = _pack([p for p in paras if p])
            sections.append({"title": crumb, "paragraphs": [{"text": f"{crumb}\n{p}"} for p in paras]})

    for i, lines in enumerate(page_lines):
        for x in lines:
            if _is_heading(x, body_size):
                level = min(keys.index((x["size"], x["bold"])), 2)
                if heading_run and level != run_level:
                    close_heading()
                if not heading_run:
                    flush()
                    run_level = level
                heading_run.append(x["text"])
            else:
                close_heading()
                buf.append(x)
        close_heading()
        flush()
        add_extras(i)
    return {"sections": sections,
            "stats": {"pages": len(page_lines), "ocr_pages": len(ocr_texts),
                      "tables": len(shapes), "table_shapes": shapes,
                      "running_head_lines_removed": removed}}


def pdf_to_doc(path: str | Path, doc_id: str, acl: list[str], title: str | None = None, **kw) -> dict:
    out = extract_pdf(path, title=title, **kw)
    return {"doc_id": doc_id, "acl": acl, "sections": out["sections"], "stats": out["stats"]}
