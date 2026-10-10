"""Exact-value tests for the pdf_ingest heuristics, driven by lines/words/chars read from the
committed REAL PDFs (see fixtures/pdf/README.md). Where a boundary needs a specific length or
size, the real text is truncated or a real field is edited; no document is invented."""
import pdfplumber
import pytest

from app import pdf_ingest as P
from test_pdf_ingest import FDIC31, FDIC171, FEMA, IRS542, _image_only_pdf

FDIC171_TITLES = ['RoE > Matters Requiring Attention 1234',
 'RoE > Matters Requiring Attention 1234',
 'RoE > Uniform Financial Institutions Rating System Current Exam Prior Exam Prior Exam',
 'RoE > Composite Rating 3 2 2',
 'RoE > Capital 3 2 2 Asset Quality 3 2 2 Management 3 2 2 Earnings 3 2 2 Liquidity 3 2 2 Sensitivity to '
 'Market Risk 2 2 2 Information Technology 2 2 Compliance1 2 Community Reinvestment Act1 S',
 'RoE > SUMMARY',
 'RoE > CAPITAL - 3',
 'RoE > CAPITAL - 3',
 'RoE > MRA-2027-01: Capital - Improve the Capital Position',
 'RoE > ASSET QUALITY - 3',
 'RoE > ASSET QUALITY - 3',
 'RoE > MRA-2027-02: Asset Quality - Improve Loan Underwriting and Credit Administration',
 'RoE > MRA-2027-03: Asset Quality - Reduce the Volume of Adversely Classified Assets',
 'RoE > MRA-2027-04: Asset Quality - Replenish the Allowance for Credit Losses',
 'RoE > LIQUIDITY - 3',
 'RoE > LIQUIDITY - 3',
 'RoE > MRA-2027-03: Liquidity - Increase On-Balance Sheet Liquidity and Access to Contingent Funding '
 'Sources',
 'RoE > CEO Alpha committed to increase on-balance sheet liquidity by June 30, 2027, and increase access to',
 'RoE > MANAGEMENT - 3',
 'RoE > MANAGEMENT - 3',
 'RoE > EARNINGS - 3',
 'RoE > SENSITIVITY TO MARKET RISK - 2',
 'RoE > SENSITIVITY TO MARKET RISK - 2',
 'RoE > INFORMATION TECHNOLOGY – 2',
 'RoE > MEETING WITH THE BOARD OF DIRECTORS',
 'RoE > DIRECTORATE RESPONSIBILITY',
 'RoE > DIRECTORATE RESPONSIBILITY > Violations of Laws and Regulations 1234',
 'RoE > VIOLATIONS OF LAWS AND REGULATIONS']
IRS542_TITLES = ['P542',
 'P542 > Internal Revenue Service',
 'P542 > Corporations',
 'P542 > Corporations > Future Developments',
 "P542 > Corporations > What's New",
 'P542 > Corporations > Photographs of Missing Children',
 'P542 > Corporations > Introduction',
 'P542 > Corporations > Introduction > Useful Items',
 'P542 > Corporations > Introduction > Publication',
 'P542 > Corporations > Businesses Taxed as Corporations',
 'P542 > Corporations > Businesses Taxed as Corporations',
 'P542 > Corporations > Property Exchanged for Stock',
 'P542 > Corporations > Property Exchanged for Stock > Publication 542 (1-2024) 3',
 'P542 > Corporations > Capital Contributions',
 'P542 > Corporations > Capital Contributions > 4 Publication 542 (1-2024)',
 'P542 > Corporations > Filing and Paying Income Taxes',
 'P542 > Corporations > Filing and Paying Income Taxes > Income Tax Return',
 'P542 > Corporations > Filing and Paying Income Taxes > Penalties',
 'P542 > Corporations > Filing and Paying Income Taxes > Penalties',
 'P542 > Corporations > Filing and Paying Income Taxes > Estimated Tax',
 'P542 > Corporations > Filing and Paying Income Taxes > Annualized income installment method and/or adjus-']


_OPEN: dict = {}


def _page(path, i):
    if path not in _OPEN:  # kept open for the process: pages are lazy
        _OPEN[path] = pdfplumber.open(path)
    return _OPEN[path].pages[i]


def test_section_breadcrumbs_are_pinned_on_real_documents():
    # characterization of today's behaviour on real pages: heading levels, nesting, joining of
    # two-line headings, table sections, title key
    assert [s["title"] for s in P.extract_pdf(FDIC171, title="RoE")["sections"]] == FDIC171_TITLES
    assert [s["title"] for s in P.extract_pdf(IRS542, title="P542", max_pages=6)["sections"]] == IRS542_TITLES


def test_norm_sorts_tokens_and_masks_digits_exactly():
    assert P._norm("RMS Manual of Examination Policies 3.1-1") == "#.#-# Examination Manual Policies RMS of"
    assert P._norm("Asset Quality (10/2025)  3.1-2") == "#.#-# (#/#) Asset Quality"


def test_usable_text_threshold_is_five_words():
    words = "Asset quality is one of the most critical areas".split()
    assert P.has_usable_text(" ".join(words[:5]))
    assert not P.has_usable_text(" ".join(words[:4]))


def _real_lines():
    page = _page(FDIC31, 1)
    raw = page.within_bbox((0, 0, page.width, page.height)).extract_text_lines(return_chars=True)
    return page, raw


def test_line_reads_bold_size_and_edge_from_real_chars():
    page, raw = _real_lines()
    head = P._line(raw[0], page.height)  # "ASSET QUALITY": 15pt bold running head
    assert (head["text"], head["size"], head["bold"], head["edge"]) == ("ASSET QUALITY", 15.0, True, True)
    body = P._line(raw[2], page.height)  # a body line
    assert (body["size"], body["bold"], body["edge"]) == (10.0, False, False)
    assert body["top"] == raw[2]["top"] and body["bottom"] == raw[2]["bottom"]
    # a line that is exactly half bold is not bold (strictly more than half is)
    chars = [dict(c, fontname="Times-Bold") for c in raw[2]["chars"][:4]] + [
        dict(c, fontname="Times-Roman") for c in raw[2]["chars"][4:8]]
    half = dict(raw[2], chars=chars)
    assert P._line(half, page.height)["bold"] is False
    assert P._line(dict(half, chars=chars[:5]), page.height)["bold"] is True
    # font names are matched case-insensitively and only on "bold"
    upper = dict(raw[2], chars=[dict(c, fontname="ARIAL-BOLDMT") for c in raw[2]["chars"]])
    assert P._line(upper, page.height)["bold"] is True
    assert P._line(dict(raw[2], chars=[dict(c, fontname="BLDMT") for c in raw[2]["chars"]]),
                   page.height)["bold"] is False


def test_line_size_is_rounded_to_half_points():
    page, raw = _real_lines()
    for median, want in ((10.2, 10.0), (10.3, 10.5), (9.74, 9.5), (12.0, 12.0)):
        chars = [dict(c, size=median) for c in raw[2]["chars"]]
        assert P._line(dict(raw[2], chars=chars), page.height)["size"] == want


def test_line_edge_flags_follow_the_top_and_bottom_eight_percent():
    page, raw = _real_lines()
    h = page.height
    top_edge = dict(raw[2], top=h * P.EDGE_FRACTION - 0.01)
    assert P._line(top_edge, h)["edge"] is True
    assert P._line(dict(raw[2], top=h * P.EDGE_FRACTION + 1), h)["edge"] is False
    assert P._line(dict(raw[2], bottom=h * (1 - P.EDGE_FRACTION) + 1), h)["edge"] is True
    assert P._line(dict(raw[2], bottom=h * (1 - P.EDGE_FRACTION) - 1), h)["edge"] is False


def test_body_size_is_the_character_weighted_mode():
    page, _ = _real_lines()
    lines = [x for x in P._page_lines(page)]
    assert P._body_size([lines]) == 10.0
    assert P._body_size([]) == 10.0 and P._body_size([[]]) == 10.0
    memo = P._page_lines(_page(FEMA, 0))
    sizes = {x["size"] for x in memo}
    weight = {s: sum(len(x["text"]) for x in memo if x["size"] == s) for s in sizes}
    assert P._body_size([memo]) == max(weight, key=weight.get)
    # weights add up across lines: one long line outweighs a short one of another size
    long_line, short_line = (dict(memo[0], size=7.0, text="x" * 30),
                             dict(memo[0], size=20.0, text="y" * 40))
    assert P._body_size([[long_line, long_line], [short_line]]) == 7.0
    assert P._body_size([[long_line], [long_line], [short_line]]) == 7.0
    default = P._body_size([[dict(memo[0], text="")]])
    assert default == memo[0]["size"]  # a weightless counter still has a mode


def _heading(**kw):
    page, raw = _real_lines()
    base = P._line(raw[2], page.height)  # 10pt non-bold body line
    base.update(text="INTRODUCTION", size=10.0, bold=True)
    base.update(kw)
    return base


def test_is_heading_rules():
    h = P._is_heading
    assert h(_heading(), 10.0)
    assert not h(_heading(bold=False), 10.0)                      # body size, not bold
    assert h(_heading(bold=False, size=11.5), 10.0)               # 15% larger than body: heading
    assert not h(_heading(bold=False, size=11.4), 10.0)
    assert h(_heading(bold=False, size=15.0), 10.0)               # big and not bold
    assert h(_heading(size=9.5), 10.0) and not h(_heading(size=9.0), 10.0)   # bold may be 0.5pt smaller
    assert not h(_heading(size=10.5, bold=False), 10.0)
    real = "Asset quality is one of the most critical areas in determining the overall condition of a bank and the primary"
    exactly = real[:100]
    assert len(exactly) == 100 and exactly[-1].isalpha()
    assert h(_heading(text=exactly), 10.0)
    assert not h(_heading(text=real[:101]), 10.0)
    for end in ".,;:":
        assert not h(_heading(text="INTRODUCTION" + end), 10.0)
    assert h(_heading(text="INTRODUCTION X"), 10.0)               # ends in a letter that is not punctuation
    assert not h(_heading(text="Estimated tax. The corporation"), 10.0)   # run-in head
    assert not h(_heading(text="Filing and Paying Income Taxes . . . . 5"), 10.0)  # dot-leader TOC row
    assert not h(_heading(text="!"), 10.0) and not h(_heading(text="12 34"), 10.0)
    assert h(_heading(text="Ab"), 10.0)


def test_paragraph_breaks_on_gaps_bullets_and_column_resets():
    page, _ = _real_lines()
    lines = [x for x in P._page_lines(page) if not x["edge"]]
    paras = P._paragraphs(lines)
    assert len(paras) == 23
    assert paras[0] == "INTRODUCTION"
    assert paras[8].startswith("• The adequacy of underwriting standards") and paras[9].startswith("• The level, distribution")
    assert paras[1].startswith("Asset quality is one of the most critical areas in determining the overall condition of a bank.")
    assert " " in paras[1] and "\n" not in paras[1]
    a, b = lines[3], lines[4]
    # a gap of 0.5 x font size is still the same paragraph; a larger one starts a new paragraph
    same = dict(b, top=a["bottom"] + 0.5 * b["size"] - 0.01)
    new = dict(b, top=a["bottom"] + 0.5 * b["size"] + 0.01)
    assert len(P._paragraphs([a, same])) == 1 and len(P._paragraphs([a, new])) == 2
    wide = dict(b, top=a["bottom"] + 1.2 * b["size"])
    assert len(P._paragraphs([a, wide])) == 2
    # a line whose top is above the previous one's (next column) starts a new paragraph
    up = dict(b, top=a["top"] - 5, bottom=a["top"] - 1)
    assert len(P._paragraphs([a, up])) == 2
    same_top = dict(b, top=a["top"], bottom=a["top"] + 1)
    assert len(P._paragraphs([a, same_top])) == 1
    for bullet in "•▪●":
        assert len(P._paragraphs([a, dict(b, text=bullet + " x", top=a["bottom"] + 0.1)])) == 2
    assert len(P._paragraphs([a, dict(b, text="x" + "•", top=a["bottom"] + 0.1)])) == 1


def test_hyphen_glue_needs_a_lowercase_first_letter():
    with pdfplumber.open(FDIC171) as pdf:
        lines = P._page_lines(pdf.pages[6])
    i = next(k for k, x in enumerate(lines) if x["text"].endswith("one to five-"))
    first, second = lines[i], lines[i + 1]
    assert second["text"].startswith("year time horizon")
    assert P._paragraphs([first, second])[0].endswith("one to five-" + second["text"])
    cap = dict(second, text="Y" + second["text"][1:])
    assert P._paragraphs([first, cap])[0].endswith("one to five- " + cap["text"])
    odd = dict(second, text="yEAR" + second["text"][4:])
    assert P._paragraphs([first, odd])[0].endswith("one to five-" + odd["text"])
    no_hyphen = dict(first, text="one to five")
    assert P._paragraphs([no_hyphen, second])[0] == "one to five " + second["text"]


def _sentences():
    page, _ = _real_lines()
    paras = P._paragraphs([x for x in P._page_lines(page) if not x["edge"]])
    first = paras[1]
    parts = [s for s in P._SENTENCE_END.split(first) if s]
    return parts


def test_pack_exact_boundaries():
    a, b, c = _sentences()[:3]
    limit = len(a) + 1 + len(b)
    assert P._pack([a + " " + b], limit=limit) == [a + " " + b]        # fits exactly: not split
    assert P._pack([a + " " + b], limit=limit - 1) == [a, b]            # one over: split
    assert P._pack([a + " " + b + " " + c], limit=limit) == [a + " " + b, c]
    assert P._pack([a], limit=len(a)) == [a]
    assert P._pack([a + " " + b], limit=len(a) + len(b)) == [a, b]
    # a sentence exactly as long as the limit is kept whole, with no empty piece around it
    longer, shorter = (a, b) if len(a) >= len(b) else (b, a)
    assert P._pack([shorter + " " + longer], limit=len(longer)) == [shorter, longer]
    assert P._pack([longer + " " + shorter], limit=len(longer)) == [longer, shorter]
    # an over-long sentence is cut at the limit, flushing the sentence collected before it
    big = longer.replace(".", "") + " " + longer.replace(".", "")
    out = P._pack([shorter + " " + big], limit=len(longer))
    assert out[0] == shorter and all(len(p) <= len(longer) for p in out) and "".join(out[1:]).replace(" ", "") == big.replace(" ", "")
    assert P._pack([a, b], limit=len(a) + len(b) + 1) == [a, b]   # long paragraphs are never merged


def test_pack_merges_only_short_neighbours():
    text = " ".join(_sentences()[0].split() * 8)
    limit = 402
    small = text[:limit // 4 - 1]
    other = text[:50]
    assert P._pack([small, other], limit=limit) == [small + " " + other]
    at_quarter = text[:limit // 4]                               # exactly limit // 4 long: not "short"
    assert len(at_quarter) == 100
    assert P._pack([at_quarter, other], limit=limit) == [at_quarter, other]
    assert P._pack([text[:101], other], limit=limit) == [text[:101], other]
    fits = text[:limit // 4 - 1]
    rest = text[: limit - len(fits) - 1]
    assert P._pack([fits, rest], limit=limit) == [fits + " " + rest]
    assert P._pack([fits, text[: limit - len(fits)]], limit=limit) == [fits, text[: limit - len(fits)]]
    assert P._pack([fits, text[: limit - len(fits) - 2]], limit=limit) == [fits + " " + text[: limit - len(fits) - 2]]
    assert P._pack([fits], limit=limit) == [fits]


def test_outside_drops_exactly_the_chars_whose_centre_is_in_a_table_box():
    page = _page(FDIC171, 0)
    ((box, _),) = P.page_tables(page)
    keep = P._outside([box])
    chars = page.chars
    assert len(chars) == 1529
    dropped = [c for c in chars if not keep(c)]
    assert len(dropped) == 915
    x0, top, x1, bottom = box
    for c in chars:
        cx, cy = (c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2
        assert keep(c) == (not (x0 <= cx <= x1 and top <= cy <= bottom))
    assert all(keep(r) for r in page.rects)                        # non-char objects always stay
    assert keep(dict(chars[0], x0=x0 - 1, x1=x0 + 1, top=top + 1, bottom=top + 3)) is False   # centre on the left edge
    assert keep(dict(chars[0], x0=x1 - 1, x1=x1 + 1, top=top + 1, bottom=top + 3)) is False   # centre on the right edge
    assert keep(dict(chars[0], x0=x0 + 1, x1=x0 + 3, top=top - 1, bottom=top + 1)) is False   # centre on the top edge
    assert keep(dict(chars[0], x0=x0 + 1, x1=x0 + 3, top=bottom - 1, bottom=bottom + 1)) is False  # bottom edge
    assert keep(dict(chars[0], x0=x0 + 1, x1=x0 + 3, top=bottom + 1, bottom=bottom + 3)) is True
    assert keep(dict(chars[0], x0=x1 + 1, x1=x1 + 3, top=top + 1, bottom=top + 3)) is True
    assert keep(dict(chars[0], x0=x0 - 5, x1=x0 - 3, top=top + 1, bottom=top + 3)) is True
    assert keep(dict(chars[0], x0=x0 + 1, x1=x0 + 3, top=top - 5, bottom=top - 3)) is True
    assert P._outside([])(chars[0]) is True


def test_table_sentence_does_not_strip_a_trailing_x():
    (_, rows), = P.page_tables(_page(FDIC171, 0))
    rows = [r[:] for r in rows]
    rows[1][2] = rows[1][2].rstrip(".") + " X."
    assert P.table_to_sentences(rows)[0].endswith("elevated risk profile X.")


def test_ocr_unavailable_message_names_both_fixes(monkeypatch):
    class Page:
        def to_image(self, resolution):
            raise AssertionError("not reached when pytesseract is missing")

    monkeypatch.setitem(__import__("sys").modules, "pytesseract", None)
    with pytest.raises(P.OcrUnavailable) as err:
        P.ocr_page(Page())
    assert str(err.value) == ("page has no usable text layer and OCR is unavailable: install the tesseract "
                              "binary (brew/apt tesseract-ocr) and `pip install pytesseract`")


def test_pdf_to_doc_passes_title_and_acl_through():
    doc = P.pdf_to_doc(FEMA, "memo-1", ["group:x"], title="Memo")
    assert doc["doc_id"] == "memo-1" and doc["acl"] == ["group:x"]
    assert doc["sections"][0]["title"] == "Memo"
    assert P.pdf_to_doc(FEMA, "memo-1", ["*"])["sections"][0]["title"] == "fema-bulletin-w-25004"
    assert P.pdf_to_doc(FEMA, "m", ["*"], max_pages=0)["stats"]["pages"] == 0
    assert doc["stats"]["pages"] == 1


def test_ocr_paragraphs_are_split_on_blank_lines_and_unwrapped(tmp_path):
    scan = tmp_path / "scan.pdf"
    _image_only_pdf(FDIC31, 1, scan)
    out = P.extract_pdf(scan, title="scan")
    paras = [p["text"].split("\n", 1)[1] for s in out["sections"] for p in s["paragraphs"]]
    assert len(paras) >= 3
    assert all("\n" not in p and "XX" not in p for p in paras)
    assert any("Asset quality is one of the most critical areas" in p for p in paras)


def test_gutters_need_thirty_words_and_a_real_share_on_each_side():
    page = _page(FDIC31, 1)
    words = page.extract_words()
    left = sorted((w for w in words if w["x1"] <= 306), key=lambda w: w["top"])
    right = sorted((w for w in words if w["x0"] >= 306), key=lambda w: w["top"])
    thirty = left[:15] + right[:15]
    assert len(P._gutters(thirty, page.width)) == 1
    assert P._gutters(thirty[:29], page.width) == []
    n_left = 340
    thin = left[:n_left] + right[:int(n_left * 0.099)]          # right strip = 9% of words
    assert P._gutters(thin, page.width) == []
    fat = left[:n_left] + right[:int(n_left * 0.15)]
    assert len(P._gutters(fat, page.width)) == 1


def test_paragraph_gap_exactly_half_a_font_size_is_not_a_break_and_x_is_not_a_bullet():
    page, _ = _real_lines()
    a, b = [x for x in P._page_lines(page) if not x["edge"]][3:5]
    a = dict(a, top=90.0, bottom=100.0)
    exact = dict(b, size=10.0, top=105.0, bottom=115.0)
    assert len(P._paragraphs([a, exact])) == 1
    assert len(P._paragraphs([a, dict(exact, text="X" + exact["text"])])) == 1
    assert len(P._paragraphs([a, dict(exact, text="•" + exact["text"])])) == 2


def test_ocr_blocks_become_paragraphs_with_newlines_unwrapped(monkeypatch, tmp_path):
    sentences = _sentences()
    block1 = "\n".join(sentences[:3]) + "\n" + sentences[0]
    block2 = "\n".join(sentences[1:5]) + "\n" + sentences[4]
    assert min(len(block1), len(block2)) > 300
    scan = tmp_path / "scan.pdf"
    _image_only_pdf(FDIC31, 1, scan, dpi=72)
    monkeypatch.setattr(P, "ocr_page", lambda page, dpi: block1 + "\n \n" + block2)
    out = P.extract_pdf(scan, title="s")
    paras = [p["text"].split("\n", 1)[1] for s in out["sections"] for p in s["paragraphs"]]
    assert paras == [block1.replace("\n", " "), block2.replace("\n", " ")]


def test_page_with_fewer_than_five_words_goes_to_ocr(monkeypatch):
    page, raw = _real_lines()
    two = [P._line(raw[0], page.height), P._line(raw[0], page.height)]   # "ASSET QUALITY" twice = 4 words
    monkeypatch.setattr(P, "_page_lines", lambda p: list(two))
    calls = []
    monkeypatch.setattr(P, "ocr_page", lambda p, dpi: calls.append(dpi) or "")
    P.extract_pdf(FEMA)
    assert calls == [300]


def test_page_made_only_of_edge_words_reads_without_defaults_crashing(monkeypatch):
    page = _page(FDIC31, 1)
    words = page.extract_words()
    monkeypatch.setattr(P, "EDGE_FRACTION", 1.01)
    assert P._page_lines(page) == P._columns(page, words, 0, page.height)
