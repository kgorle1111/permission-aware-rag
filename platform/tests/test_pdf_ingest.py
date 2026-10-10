"""PDF ingestion on REAL US-federal PDFs (public domain, see fixtures/pdf/README.md).

No generated documents anywhere: the OCR tests rasterize a page of a committed
real PDF at test time, and table edge cases mutate rows read from a real table.
"""
import hashlib
import json
import sys
from pathlib import Path

import pdfplumber
import pytest

from app import pdf_ingest
from app.identity import Principal
from app.ingest import chunk_document, ingest_corpus
from app.retrieval import clear_cache, retrieve
from app.store import ChunkACL, ChunkText, SessionLocal
from conftest import reingest

FIX = Path(__file__).parent / "fixtures" / "pdf"
FDIC31 = FIX / "fdic-section3-1.pdf"
FDIC171 = FIX / "fdic-section17-1.pdf"
FDIC211 = FIX / "fdic-section21-1.pdf"
FEMA = FIX / "fema-bulletin-w-25004.pdf"
IRS542 = FIX / "irs-p542.pdf"
SHA256 = {
    FDIC31: "f22c5c7cd9df71686b95f0462dde71d2eb9f208ef5c029167776c93a8e9a61ad",
    FEMA: "1d8fd99db19429ac862614c346e52e8efd6f20284ef637ca5ae6792879213380",
    FDIC171: "ceed222dd62efe0c864e4248185ab4b7768d5225a543eecbfa7df10863d02767",
    FDIC211: "5f1a6f2bdab6d1719d69c4cfbfe2982d9f915f4a3bd2af5547faa5db54a59095",
    IRS542: "9bae90126317b0de17313cdae8245209fd9de403a455d52f418f4a5b85c13b30",
}


def _text(doc: dict) -> str:
    return "\n".join(p["text"] for s in doc["sections"] for p in s["paragraphs"])


def test_fixtures_are_the_pinned_real_files():
    for path, digest in SHA256.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_two_column_order_breadcrumbs_and_running_heads():
    out = pdf_ingest.extract_pdf(FDIC31, title="FDIC 3.1")
    text = "\n".join(p["text"] for s in out["sections"] for p in s["paragraphs"])
    # a row-wise (column-blind) read interleaves the two columns on one line
    assert "INTRODUCTION • The adequacy" not in text
    assert ("Asset quality is one of the most critical areas in determining the overall "
            "condition of a bank.") in text
    # header/footer lines repeating on all 3 pages are gone; body text is not
    assert out["stats"]["running_head_lines_removed"] == 12
    assert "Federal Deposit Insurance Corporation" not in text
    assert "Section 3.1" not in text
    assert "EVALUATION OF ASSET QUALITY" in [t.split(" > ")[-1] for t in
                                             (s["title"] for s in out["sections"])]
    assert [s["title"] for s in out["sections"]] == [
        "FDIC 3.1", "FDIC 3.1 > INTRODUCTION", "FDIC 3.1 > EVALUATION OF ASSET QUALITY",
        "FDIC 3.1 > RATING THE ASSET QUALITY FACTOR", "FDIC 3.1 > RATING THE ASSET QUALITY FACTOR"]
    for s in out["sections"]:
        assert all(p["text"].startswith(s["title"] + "\n") for p in s["paragraphs"])
    assert out["stats"]["pages"] == 3 and out["stats"]["ocr_pages"] == 0


def test_single_column_page_is_not_split_and_title_defaults_to_filename():
    out = pdf_ingest.extract_pdf(FEMA)
    assert out["stats"] == {"pages": 1, "ocr_pages": 0, "tables": 0, "table_shapes": [],
                            "running_head_lines_removed": 0}
    text = _text({"sections": out["sections"]})
    assert "MEMORANDUM FOR: Write Your Own (WYO) Company Principal Coordinators" in text
    assert out["sections"][0]["title"] == "fema-bulletin-w-25004"
    with pdfplumber.open(FEMA) as pdf:
        assert pdf_ingest._gutters(pdf.pages[0].extract_words(), pdf.pages[0].width) == []
    with pdfplumber.open(FDIC31) as pdf:
        (gx,) = pdf_ingest._gutters(pdf.pages[1].extract_words(), pdf.pages[1].width)
        assert 290 < gx < 320  # centre of a 612pt two-column page


def _order(lines, *texts):
    pos = [next(i for i, x in enumerate(lines) if x["text"].startswith(t)) for t in texts]
    return pos == sorted(pos) and len(set(pos)) == len(pos)


def test_full_width_bands_split_the_columns_into_zones_and_footers_are_not_duplicated():
    with pdfplumber.open(FDIC211) as pdf:
        lines = pdf_ingest._page_lines(pdf.pages[19])
    # the whole left column is read before the right column; heads/feet are read once, full width
    assert _order(lines, "Pre-Commissioned Trainer Benchmarks", "Other", "LOGISTICAL INFORMATION",
                  "EXAMINATION NUMBERS")
    assert [x["text"] for x in lines if x["text"].startswith("Federal Deposit Insurance")] == [
        "Federal Deposit Insurance Corporation"]
    assert sum(x["text"].startswith("Examination Planning – Point-in-Time Examinations (8/2026)")
               for x in lines) == 1
    assert all(x["text"] for x in lines)


def test_spanning_groups_merge_overlapping_rows():
    with pdfplumber.open(FDIC211) as pdf:
        page = pdf.pages[19]
        words = page.extract_words()
        cuts = pdf_ingest._gutters(words, page.width)
    assert cuts == [399.0]
    groups = pdf_ingest._spanning_groups(
        [w for w in words if any(w["x0"] < c < w["x1"] for c in cuts)])
    assert [[w["text"] for w in g] for g in groups] == [["Technology"]]


def test_spanning_groups_merge_words_on_one_row_and_split_rows():
    with pdfplumber.open(FDIC211) as pdf:
        words = pdf.pages[19].extract_words()
    row = [w for w in words if round(w["top"]) == 145][:2]
    other = next(w for w in words if round(w["top"]) == 157)
    groups = pdf_ingest._spanning_groups([other, *row])
    assert [[round(w["top"]) for w in g] for g in groups] == [[145] * len(row), [157]]


def test_column_split_that_would_drop_text_falls_back_to_a_full_width_read(monkeypatch):
    with pdfplumber.open(FDIC31) as pdf:
        page = pdf.pages[1]
        full = pdf_ingest._region_lines(page, (0, 0, page.width, page.height))
        monkeypatch.setattr(pdf_ingest, "_columns", lambda *a: [])
        assert pdf_ingest._page_lines(page) == full


def test_page_where_edge_bands_overlap_is_read_as_one_region(monkeypatch):
    with pdfplumber.open(FDIC31) as pdf:
        page = pdf.pages[1]
        full = pdf_ingest._region_lines(page, (0, 0, page.width, page.height))
        words = page.extract_words()
        monkeypatch.setattr(pdf_ingest, "EDGE_FRACTION", 0.9)
        monkeypatch.setattr(pdf_ingest, "EDGE_GAP", -1e9)
        assert pdf_ingest._page_lines(page) == pdf_ingest._columns(page, words, 0, page.height)
        assert len(full) == 63  # control: the unsplit read is a different (row-wise) sequence


def test_degenerate_ruled_boxes_are_not_reported_as_tables():
    with pdfplumber.open(FDIC211) as pdf:
        assert len(pdf.pages[3].find_tables()) == 5  # pdfplumber sees five ruled boxes...
        assert pdf_ingest.page_tables(pdf.pages[3]) == []  # ...none is a real multi-row table
        sizes = [(len(r), len(r[0])) for _, r in pdf_ingest.page_tables(pdf.pages[17])]
    assert sizes == [(9, 4), (7, 8), (10, 4), (3, 4), (3, 8)]


def test_regions_are_clamped_to_the_page_box():
    # a Federal Register scan has a sub-point negative origin; pdfplumber raises if a crop leaves the page
    with pdfplumber.open(FDIC31) as pdf:
        page = pdf.pages[1]
        w, h = page.width, page.height
        exact = pdf_ingest._region_lines(page, (0, 0, w, h))
        assert pdf_ingest._region_lines(page, (-5, -0.02, w + 30, h + 9)) == exact
        assert pdf_ingest._region_lines(page, (w + 1, 0, w + 50, h)) == []


def test_printers_slug_outside_the_page_box_is_not_content():
    with pdfplumber.open(IRS542) as pdf:
        page = pdf.pages[2]
        slug = [w["text"] for w in page.extract_words() if w["top"] < 0]
        assert "Fileid:" in slug and "proofs." in slug  # really present in the text layer, off the page
        lines = pdf_ingest._page_lines(page)
    text = " ".join(x["text"] for x in lines)
    assert "Fileid:" not in text and "removed before printing" not in text
    assert lines[0]["text"] == "Business formed after 1996. The following businesses"  # left column first
    assert _order(lines, "Business formed after 1996", "certain publicly traded partnerships).")


def test_a_channel_beside_a_nearly_empty_strip_is_a_margin_not_a_gutter():
    with pdfplumber.open(FDIC31) as pdf:
        page = pdf.pages[1]
        words = page.extract_words()
        (gx,) = pdf_ingest._gutters(words, page.width)
        left_only = [w for w in words if w["x1"] <= gx]
        sliver = left_only + [w for w in words if w["x0"] >= gx][:5]
        assert pdf_ingest._gutters(sliver, page.width) == []  # right strip holds <12% of words
        assert len(pdf_ingest._gutters(words, page.width)) == 1


def test_max_pages_limits_extraction():
    assert pdf_ingest.extract_pdf(FDIC31, max_pages=1)["stats"]["pages"] == 1


def test_table_becomes_markdown_and_natural_language_sentences():
    out = pdf_ingest.extract_pdf(FDIC171, title="RoE")
    assert out["stats"]["tables"] == 1 and out["stats"]["table_shapes"] == [(6, 3)]
    paras = [p["text"] for s in out["sections"] for p in s["paragraphs"]]
    assert any("| MRA Number | Risk Category | Summary |\n|---|---|---|\n| MRA-2027-01 | Capital |" in p
               for p in paras)
    assert any("MRA Number MRA-2027-01: Risk Category = Capital; Summary = Improve the Capital "
               "Position: The Board should increase capital to a level commensurate with the "
               "bank’s elevated risk profile. MRA Number MRA-2027-02:" in p for p in paras)
    # table cells are not also emitted as flowing body text
    body = [p for p in paras if "| " not in p and "Risk Category = " not in p]
    assert not any("MRA-2027-01 Capital Improve the Capital Position" in p for p in body)


def _real_rows():
    with pdfplumber.open(FDIC171) as pdf:
        (_, rows), = pdf_ingest.page_tables(pdf.pages[0])
    return rows


def test_table_formatting_edge_cases_on_real_rows():
    rows = _real_rows()
    assert rows[0] == ["MRA Number", "Risk Category", "Summary"]
    assert len(rows) == 6
    blank_cell = [r[:] for r in rows]
    blank_cell[1][1] = ""
    assert pdf_ingest.table_to_sentences(blank_cell)[0].startswith(
        "MRA Number MRA-2027-01: Summary = Improve the Capital Position")
    assert "Risk Category" not in pdf_ingest.table_to_sentences(blank_cell)[0]
    no_label = [r[:] for r in rows]
    no_label[1][0] = ""
    assert pdf_ingest.table_to_sentences(no_label)[0].startswith("MRA Number: Risk Category = Capital;")
    no_header = [["", rows[0][1], rows[0][2]], *rows[1:]]
    assert pdf_ingest.table_to_sentences(no_header)[0].startswith("column 1 MRA-2027-01:")
    only_label = [rows[0], [rows[1][0], "", ""]]
    assert pdf_ingest.table_to_sentences(only_label) == ["MRA Number MRA-2027-01."]
    pipe = [rows[0], ["a|b", "c", "d"]]
    assert pdf_ingest.table_to_markdown(pipe) == (
        "| MRA Number | Risk Category | Summary |\n|---|---|---|\n| a\\|b | c | d |")


def test_clean_table_rejects_degenerate_tables_and_pads_ragged_rows():
    rows = _real_rows()
    assert pdf_ingest._clean_table([rows[0]]) is None
    assert pdf_ingest._clean_table([[r[0]] for r in rows]) is None
    assert pdf_ingest._clean_table([[None, ""], ["", None]]) is None
    ragged = pdf_ingest._clean_table([rows[0], rows[1][:2], [None, None, None]])
    assert ragged == [rows[0], [rows[1][0], rows[1][1], ""]]
    spaced = pdf_ingest._clean_table([[" a\n b ", "c"], ["d", "e"]])
    assert spaced == [["a b", "c"], ["d", "e"]]


def test_hyphen_wrapped_compound_keeps_hyphen_on_real_lines():
    with pdfplumber.open(FDIC171) as pdf:
        lines = pdf_ingest._page_lines(pdf.pages[6])
    i = next(k for k, x in enumerate(lines) if x["text"].endswith("one to five-"))
    para = pdf_ingest._paragraphs(lines[i:i + 2])[0]
    assert "one to five-year time horizon" in para


def test_pack_splits_long_and_merges_short_paragraphs_losslessly():
    paras = [p["text"].split("\n", 1)[1] for s in pdf_ingest.extract_pdf(FDIC31)["sections"]
             for p in s["paragraphs"]]
    long_para = max(paras, key=len)
    assert len(long_para) > 600
    pieces = pdf_ingest._pack([long_para], limit=300)
    assert all(len(p) <= 300 for p in pieces) and len(pieces) >= 2
    assert " ".join(pieces).split() == long_para.split()
    unbreakable = max(long_para.split(), key=len) * 1 + long_para.split()[0]
    assert "".join(pdf_ingest._pack([unbreakable], limit=7)) == unbreakable
    sentence_free = long_para.replace(".", "")
    first = long_para.split(". ")[0] + "."
    mixed = pdf_ingest._pack([first + " " + sentence_free], limit=len(first) + 40)
    assert mixed[0] == first and "".join(mixed[1:]).replace(" ", "") == sentence_free.replace(" ", "")
    assert pdf_ingest._pack(["a", "b"], limit=10) == ["a b"]
    assert pdf_ingest._pack(["x" * 9, "y" * 9], limit=10) == ["x" * 9, "y" * 9]


def test_usable_text_and_running_head_normalisation():
    assert pdf_ingest.has_usable_text("Asset quality is one of the most critical areas")
    assert not pdf_ingest.has_usable_text("1 2 3 . . ,")
    assert pdf_ingest._norm("RMS Manual 3.1-1 Asset") == pdf_ingest._norm("Asset 3.1-2 RMS Manual")
    assert pdf_ingest._norm("page 7") != pdf_ingest._norm("chapter 7")


def _image_only_pdf(src: Path, page_index: int, dst: Path, dpi: int = 200) -> None:
    """Rasterize one page of a real PDF into an image-only PDF (no text layer)."""
    with pdfplumber.open(src) as pdf:
        pdf.pages[page_index].to_image(resolution=dpi).original.convert("RGB").save(
            dst, "PDF", resolution=dpi)


def test_ocr_fallback_reads_a_rasterized_real_page(tmp_path):
    scan = tmp_path / "scan.pdf"
    _image_only_pdf(FDIC31, 1, scan)
    with pdfplumber.open(scan) as pdf:
        assert pdf.pages[0].extract_text().strip() == ""  # really no text layer
    out = pdf_ingest.extract_pdf(scan, title="scan")
    assert out["stats"]["ocr_pages"] == 1
    text = " ".join(_text({"sections": out["sections"]}).split())
    for phrase in ("Asset quality is one of the most critical areas",
                   "credit administration program", "Uniform Financial Institution Rating System"):
        assert phrase in text
    assert out["sections"][0]["title"] == "scan"


def test_ocr_page_default_dpi_is_300_and_blank_ocr_adds_nothing(tmp_path, monkeypatch):
    seen = {}

    class FakePage:
        def to_image(self, resolution):
            seen["dpi"] = resolution
            return type("I", (), {"original": "img"})()

    import pytesseract
    monkeypatch.setattr(pytesseract, "image_to_string", lambda img: "  \n ")
    assert pdf_ingest.ocr_page(FakePage()) == "  \n "
    assert seen["dpi"] == 300 == pdf_ingest.OCR_DPI


def test_blank_ocr_page_contributes_no_chunks(tmp_path, monkeypatch):
    scan = tmp_path / "scan.pdf"
    _image_only_pdf(FDIC31, 1, scan, dpi=72)
    monkeypatch.setattr(pdf_ingest, "ocr_page", lambda page, dpi: " \n ")
    out = pdf_ingest.extract_pdf(scan)
    assert out["sections"] == [] and out["stats"]["ocr_pages"] == 0


def test_missing_ocr_engine_fails_loudly_not_silently(tmp_path, monkeypatch):
    scan = tmp_path / "scan.pdf"
    _image_only_pdf(FDIC31, 1, scan, dpi=72)
    import pytesseract

    def no_binary(img):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "image_to_string", no_binary)
    with pytest.raises(pdf_ingest.OcrUnavailable, match="install the tesseract binary"):
        pdf_ingest.extract_pdf(scan)
    monkeypatch.setitem(sys.modules, "pytesseract", None)  # import raises ImportError
    with pytest.raises(pdf_ingest.OcrUnavailable, match="pip install pytesseract"):
        pdf_ingest.extract_pdf(scan)


def test_text_pages_never_invoke_ocr(monkeypatch):
    monkeypatch.setattr(pdf_ingest, "ocr_page", lambda *a: pytest.fail("OCR on a text page"))
    pdf_ingest.extract_pdf(FDIC31)


def _chunk_rows(doc_ids):
    with SessionLocal() as s:
        rows = (s.query(ChunkACL, ChunkText).join(ChunkText, ChunkText.chunk_id == ChunkACL.chunk_id)
                .filter(ChunkACL.doc_id.in_(doc_ids)).all())
        return [(a.doc_id, list(a.acl), t.text) for a, t in rows]


def test_every_extracted_chunk_inherits_the_document_acl_and_outsiders_get_nothing(
        client, tmp_path):
    scan = tmp_path / "scan.pdf"
    _image_only_pdf(FDIC31, 1, scan)
    docs = [
        pdf_ingest.pdf_to_doc(FDIC31, "pdf-text", ["group:examiners"], title="FDIC 3.1"),
        pdf_ingest.pdf_to_doc(FDIC171, "pdf-table", ["group:analysts"], title="RoE"),
        pdf_ingest.pdf_to_doc(scan, "pdf-ocr", ["group:legal"], title="scan"),
        pdf_ingest.pdf_to_doc(FEMA, "pdf-public", ["*"], title="FEMA W-25004"),
    ]
    assert docs[2]["stats"]["ocr_pages"] == 1 and docs[1]["stats"]["tables"] == 1
    corpus = tmp_path / "pdfs.json"
    corpus.write_text(json.dumps(docs))
    try:
        n = ingest_corpus(corpus)
        rows = _chunk_rows([d["doc_id"] for d in docs])
        assert n == len(rows) > 20
        want = {"pdf-text": ["group:examiners"], "pdf-table": ["group:analysts"],
                "pdf-ocr": ["group:legal"], "pdf-public": ["*"]}
        assert all(acl == want[doc] for doc, acl, _ in rows)
        by_doc = {d: [t for dd, _, t in rows if dd == d] for d in want}
        assert any("Risk Category = Capital" in t for t in by_doc["pdf-table"])  # table sentence chunk
        assert any(t.startswith("| MRA Number") or "\n| MRA Number" in t for t in by_doc["pdf-table"])
        assert any("critical areas in determining" in " ".join(t.split()) for t in by_doc["pdf-ocr"])

        outsider = Principal(user_id="eve@x.test", groups=("eng",))
        clear_cache()
        probes = {"pdf-text": "asset quality is one of the most critical areas in determining the "
                              "overall condition of a bank",
                  "pdf-table": "MRA Number MRA-2027-01 Risk Category Capital Improve the Capital Position",
                  "pdf-ocr": "asset quality is one of the most critical areas in determining the "
                             "overall condition of a bank"}
        for doc, q in probes.items():
            got = retrieve(q, outsider)
            assert doc not in [r["doc_id"] for r in got["results"]], doc
            assert "critical areas in determining" not in got["answer"] or doc == "pdf-ocr"
        for doc, q, group in [("pdf-text", probes["pdf-text"], "examiners"),
                              ("pdf-table", probes["pdf-table"], "analysts"),
                              ("pdf-ocr", probes["pdf-ocr"], "legal")]:
            got = retrieve(q, Principal(user_id="ann@x.test", groups=(group,)))
            assert doc in [r["doc_id"] for r in got["results"]], doc
        # the outsider still reads the public PDF
        pub = retrieve("Write Your Own WYO Company Principal Coordinators NFIP Direct Servicing Agent", outsider)
        assert "pdf-public" in [r["doc_id"] for r in pub["results"]]
    finally:
        reingest()


def test_pii_redaction_still_applies_to_extracted_pdf_text():
    doc = pdf_ingest.pdf_to_doc(FEMA, "fema", ["*"])
    assert "NFIPWYOMailbox@fema.dhs.gov" in _text(doc)  # the real mailbox is in the PDF text
    chunks = chunk_document(doc)
    joined = "\n".join(c["text"] for c in chunks)
    assert "NFIPWYOMailbox" not in joined and "[REDACTED:EMAIL]" in joined
    assert all(c["acl"] == ["*"] for c in chunks)
