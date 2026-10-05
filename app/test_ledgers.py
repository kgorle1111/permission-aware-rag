"""Ledgers are enforced, not decorative: every cited check must still exist.

DECISIONS (D), THREAT_MODEL (T), and BACKLOG (B) rows cite evidence as
`path` "anchor". Deleting the cited file or the anchor text fails this test,
and so does a code shortcut (kn:/ponytail:) with no backlog row.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
CITE = re.compile(r'`([\w./-]+)` "([^"]+)"')


def rows(doc, prefix):
    """Table rows whose first cell is an id like D01 / T13 / B04."""
    out = []
    for line in (ROOT / doc).read_text().splitlines():
        m = re.match(rf"\|\s*({prefix}\d+)\s*\|", line)
        if m:
            out.append((m.group(1), line))
    return out


def check_citations(doc, prefix, min_rows):
    found = rows(doc, prefix)
    ids = [i for i, _ in found]
    assert len(found) >= min_rows, f"{doc}: expected ≥{min_rows} {prefix} rows, found {len(found)}"
    assert len(ids) == len(set(ids)), f"{doc}: duplicate ids {sorted(i for i in ids if ids.count(i) > 1)}"
    for row_id, line in found:
        cites = CITE.findall(line)
        if prefix == "T" and line.rstrip("| ").endswith("open"):
            continue  # open threats are known gaps; they cite nothing by definition
        assert cites, f'{doc} {row_id}: no `path` "anchor" citation'
        for path, anchor in cites:
            f = ROOT / path
            assert f.is_file(), f"{doc} {row_id}: cited file {path} is gone"
            assert anchor in f.read_text(), f"{doc} {row_id}: anchor {anchor!r} no longer in {path}"
    return found


def test_decisions():
    check_citations("docs/DECISIONS.md", "D", 6)


def test_threat_model():
    check_citations("docs/THREAT_MODEL.md", "T", 15)


def test_backlog_covers_every_shortcut():
    backlog = check_citations("BACKLOG.md", "B", 1)
    anchors = [a for _, line in backlog for _, a in CITE.findall(line)]
    for f in sorted((ROOT / "app").glob("*.py")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"\b(kn|ponytail): ", line):
                assert any(a in line for a in anchors), (
                    f"app/{f.name}:{n} shortcut has no BACKLOG row: {line.strip()}"
                )


def test_ledgers_fail_when_evidence_is_deleted(tmp_path, monkeypatch):
    """The gate itself must be falsifiable: a missing anchor has to fail."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/X.md").write_text('| D01 | accepted | x | y | z | `docs/Y.md` "the anchor" |\n')
    (tmp_path / "docs/Y.md").write_text("evidence was deleted\n")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    try:
        check_citations("docs/X.md", "D", 1)
    except AssertionError:
        return
    raise AssertionError("ledger gate passed on a missing anchor")


def test_readme_numbers_match_the_evals():
    """README figures are recomputed, not remembered: drift between claim and run fails."""
    import io
    from contextlib import redirect_stdout

    import eval_scale
    import run_evals

    readme = (ROOT / "README.md").read_text()
    hits, total, leaks = run_evals.label_gate(run_evals.srv.rag, verbose=False)
    assert f"recall@4: {hits}/{total}" in readme and leaks == 0

    out = io.StringIO()
    with redirect_stdout(out):
        assert eval_scale.main() == 0
    row = out.getvalue()  # "| in-memory BM25 | 210 | 0/1350 | 0.28% | ..."
    must_not, ub = row.split("|")[3].strip().split("/")[1], row.split("|")[4].strip()
    assert f"0/{int(must_not):,} leaks" in readme, f"README leak count stale; eval says 0/{must_not}"
    assert f"upper bound {ub}" in readme, f"README bound stale; eval says {ub}"
