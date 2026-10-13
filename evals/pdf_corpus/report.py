"""Assemble evals/results/2026-10-10-pdf-real-corpus/table.md from the eval JSON outputs.

usage: python report.py   (needs data/corpus.json plus the JSONs in the results dir)
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
RES = HERE.parent / "results" / "2026-10-10-pdf-real-corpus"
sys.path.insert(0, str(HERE))
from stats import bootstrap_ratio, wilson  # noqa: E402


def pct(x: float) -> str:
    return f"{100 * x:.2f}%"


def ratio_row(label: str, runs: list[dict], key_n: str, key_d: str) -> str:
    est, lo, hi = bootstrap_ratio([r[key_n] for r in runs], [r[key_d] for r in runs])
    return f"{pct(est)} [{pct(lo)}, {pct(hi)}]"


def ocr_table(runs_by: dict[str, list[dict]]) -> list[str]:
    out = [
        "| group | pages | CER (95% bootstrap CI) | WER (95% bootstrap CI) | word recall, order-free | pages with CER <= 5% (Wilson 95%) |",
        "|---|---|---|---|---|---|",
    ]
    for name, runs in runs_by.items():
        good = sum(r["char_edits"] / r["chars"] <= 0.05 for r in runs)
        lo, hi = wilson(good, len(runs))
        rec = sum(r["bag_hit"] for r in runs) / sum(r["bag_total"] for r in runs)
        out.append(
            f"| {name} | {len(runs)} | {ratio_row(name, runs, 'char_edits', 'chars')} | "
            f"{ratio_row(name, runs, 'word_edits', 'words')} | {pct(rec)} | "
            f"{good}/{len(runs)} = {pct(good / len(runs))} [{pct(lo)}, {pct(hi)}] |"
        )
    return out


def main() -> None:
    manifest = json.loads((HERE / "MANIFEST.json").read_text())
    corpus = json.loads((HERE / "data" / "corpus.json").read_text())
    born = json.loads((RES / "ocr_born.json").read_text())["pages"]
    fr = json.loads((RES / "ocr_fr.json").read_text())["pages"]
    leak = json.loads((RES / "leak.json").read_text())
    L: list[str] = ["# Real-PDF corpus: ingestion, OCR accuracy, table coverage, leak check (2026-10-10)", ""]

    L += [
        "## Corpus (all US federal publications, 17 USC 105; fetched by `evals/pdf_corpus/fetch.py`)",
        "",
        "| publisher | kind | files | bytes |",
        "|---|---|---|---|",
    ]
    grp: dict[tuple, list] = defaultdict(list)
    for e in manifest:
        grp[(e["publisher"], e["kind"])].append(e)
    for (pub, kind), es in sorted(grp.items()):
        L.append(f"| {pub} | {kind} | {len(es)} | {sum(e['bytes'] for e in es):,} |")
    L += [f"| **total** | | **{len(manifest)}** | **{sum(e['bytes'] for e in manifest):,}** |", ""]

    pages = sum(d["stats"]["pages"] for d in corpus)
    chunks = sum(len(s["paragraphs"]) for d in corpus for s in d["sections"])
    L += [
        f"Ingested with `platform/app/pdf_ingest.py` (first 150 pages of each file): {len(corpus)} documents, "
        f"{pages:,} pages, {chunks:,} paragraph units, {sum(d['stats']['ocr_pages'] for d in corpus)} pages through OCR fallback "
        f"(pages with no usable text layer), {sum(d['stats']['running_head_lines_removed'] for d in corpus):,} running head/foot lines removed.",
        "",
    ]

    L += [
        "## OCR accuracy vs the page's own text layer (born-digital pages)",
        "",
        "Ground truth = the characters of each page's text layer (column-aware reading order from `pdf_ingest`). "
        "Pages are rasterized by pdfium at the stated DPI and OCR'd by Tesseract through the production `ocr_page`. "
        "CER/WER = pooled Levenshtein edits / truth length, 95% percentile bootstrap over pages (2,000 resamples, seed 0). "
        "'word recall, order-free' counts truth words found anywhere in the OCR output, so it ignores reading-order differences. "
        "Pages were drawn at random (seed 20261010) from pages with >= 300 characters of text.",
        "",
    ]
    by_dpi: dict[str, list] = defaultdict(list)
    by_pub: dict[str, list] = defaultdict(list)
    pub300: dict[str, list] = defaultdict(list)
    for p in born:
        for r in p["runs"]:
            by_dpi[f"{r['dpi']} DPI (all publishers)"].append(r)
            if r["dpi"] == 300:
                pub300[f"{p['publisher']} @300"].append(r)
            else:
                by_pub[f"{p['publisher']} @200"].append(r)
    L += ["### By DPI", ""] + ocr_table(dict(sorted(by_dpi.items()))) + ["", "### By publisher", ""]
    both = {**dict(sorted(pub300.items())), **dict(sorted(by_pub.items()))}
    L += ocr_table(both) + [""]
    worst = sorted(
        (
            (r["char_edits"] / r["chars"], p["publisher"], p["file"][-22:], p["page"])
            for p in born
            for r in p["runs"]
            if r["dpi"] == 300
        ),
        reverse=True,
    )[:5]
    L += [
        "Worst five pages at 300 DPI (CER, publisher, file, page index): "
        + "; ".join(f"{c:.2f} {pb} {f} p{pg}" for c, pb, f, pg in worst),
        "",
    ]

    fr_runs = [r for p in fr for r in p["runs"]]
    L += [
        "## Federal Register scans vs GPO's embedded OCR layer (NOISY BASELINE, NOT GROUND TRUTH)",
        "",
        "GPO's text layer is itself uncorrected machine OCR of the same scan. Numbers below measure how closely our Tesseract "
        "output *agrees with another OCR engine*; disagreement can be GPO's error or ours, and the metric cannot tell which. "
        "No accuracy claim is made for scans.",
        "",
    ]
    L += ocr_table({"Federal Register, 300 DPI": fr_runs}) + [""]
    issues = len({p["file"] for p in fr})
    L += [f"{len(fr)} pages from {issues} issues (1936-1993).", ""]

    shapes = [(d["publisher"], s) for d in corpus for s in d["stats"]["table_shapes"]]
    L += [
        "## Table extraction: coverage only (no accuracy claim)",
        "",
        "FinTabNet (the labelled table benchmark we looked at) is licensed CDLA-Permissive according to its dataset cards "
        "(Hugging Face `bsmock/FinTabNet.c` states `cdla-permissive-2.0` and relays the original CDLA-Permissive; the "
        "official IBM Data Asset Exchange page is deprecated and the `dax-cdn.cdn.appdomain.cloud` download host no "
        "longer resolves, so the original LICENSE.txt could not be read). The usable PDFs are not obtainable from an official "
        "source (FinTabNet.c ships annotations only and its extract script needs the dead IBM archive), so pdfplumber "
        "table-structure accuracy was **not measured**. No table labels were invented. What follows is coverage on the "
        "federal corpus: pdfplumber's default ruled-line strategy, tables with >= 2 rows and >= 2 columns.",
        "",
        "| publisher | docs | docs with >= 1 table | tables | median rows | median cols | max rows x cols |",
        "|---|---|---|---|---|---|---|",
    ]
    for pub in sorted({d["publisher"] for d in corpus}):
        ds = [d for d in corpus if d["publisher"] == pub]
        sh = [s for q, s in shapes if q == pub]
        L.append(
            f"| {pub} | {len(ds)} | {sum(1 for d in ds if d['stats']['tables'])} | {len(sh)} | "
            f"{statistics.median([r for r, _ in sh]) if sh else '-'} | {statistics.median([c for _, c in sh]) if sh else '-'} | "
            f"{'x'.join(map(str, max(sh, key=lambda t: t[0] * t[1]))) if sh else '-'} |"
        )
    L += [
        f"| **all** | {len(corpus)} | {sum(1 for d in corpus if d['stats']['tables'])} | {len(shapes)} | "
        f"{statistics.median([s[0] for _, s in shapes])} | {statistics.median([s[1] for _, s in shapes])} | |",
        "",
        "Unruled (whitespace-aligned) tables are not detected by the default strategy, so these counts are a floor, not a census.",
        "",
    ]

    lo, hi = leak["wilson95"]
    L += [
        "## Leak check on the real text (ACLs are SYNTHETIC: one group per publisher)",
        "",
        f"{leak['docs']} documents, {leak['chunks']:,} chunks ingested through the platform (`ingest_corpus`). Text is real; "
        "who may read what is invented for the test (FDIC -> `group:examiners`, IRS -> `group:tax`, CFPB -> `group:compliance`, "
        "HUD -> `group:housing`, GAO and Treasury -> `group:policy`, FEMA -> `group:flood`, Federal Register -> `group:archive`; "
        "plus an outsider with no group).",
        "",
        "| check | result |",
        "|---|---|",
        f"| exact-content probes (12-word shingles unique to one document) | {leak['probes']} probes |",
        f"| forbidden attempts (probe x principal who may not read the document) | {leak['forbidden_attempts']:,} |",
        f"| **leaks** (a result from a forbidden document) | **{leak['leaks']}** (Wilson 95% upper bound {pct(hi)}) |",
        f"| authorized control (owning group finds its own document) | {leak['control_hits']}/{leak['control_attempts']} = "
        f"{pct(leak['control_hits'] / leak['control_attempts'])} |",
    ]
    for name, v in leak["isolation"].items():
        L.append(
            f"| isolation diff for `{name}` (results with vs without every unreadable document) | {v['diffs']} differing of {v['queries']} queries ({v['raw_diffs']} before canonicalizing the order of equal-score results) |"
        )
    L += [
        "",
        "## Caveats",
        "",
        "- Born-digital rasterization is clean, noise-free input: OCR numbers are an upper bound for real scans, which add skew, speckle and fading. They say nothing about handwriting or photographs.",
        "- Text-layer 'ground truth' is exact for characters but not for reading order; the order-free word recall column is the order-insensitive view.",
        "- Federal Register agreement is a noisy baseline, as labelled; it is not accuracy.",
        "- The leak check is real text with invented permissions. It exercises ACL enforcement on realistic chunk sizes and vocabulary; it does not validate any real organisation's permission model.",
        "- Document-sourced text is data: nothing extracted from these PDFs was executed or treated as an instruction.",
        "- Isolation: one `group:tax` query returned the same four results in a different order when unreadable documents were removed. The tied scores were ordered by internal chunk id, which depends on corpus composition, not on document text. Content, scores and document ids were identical (`iso_debug.py` shows the case). It is reported rather than hidden, and not treated as a content leak.",
        "- Authorized control is 95%, not 100%: 19 of 384 exact 12-word probes did not return their own document in the top 4 (cause not traced; likely repeated boilerplate across publications and a lexical hash embedder). That is retrieval quality, unrelated to the zero-leak count.",
        "- FinTabNet table accuracy was not measured (see above).",
    ]
    (RES / "table.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
