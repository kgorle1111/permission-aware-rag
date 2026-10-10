"""EnronQA eval of the reference engine (app/permission_rag.py): leaks, recall, citation validity.

Real emails and real questions we did not write (EnronQA, arXiv:2505.00263). Permissions come from
the corpus structure: an email is readable by the mailbox it was collected from. See README.md.

    python3 evals/enronqa/run.py --no-llm                 # everything except the paid step
    python3 evals/enronqa/run.py --smoke 5                # 5 paid questions: prints cost/question, writes nothing
    python3 evals/enronqa/run.py --llm-n 600 --max-usd 5  # full run, hard spend cap

The key is read from the environment only (llm.ask); this file never prints or opens it.
Needs pyarrow (evals/enronqa/requirements.txt). Downloaded text stays in ./data/ (gitignored).
"""

from __future__ import annotations

import argparse
import ast
import collections
import email as emaillib
import email.utils
import gc
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "evals")]

from metrics import _tokens  # noqa: E402
from permission_rag import PermissionRAG, tokenize  # noqa: E402

SEED = 20261010
KS = (1, 4, 10)
LLM_K = 4  # chunks handed to the LLM
Z = 1.959964
HEADER_SEP = "=====================================\n"
# claude-haiku-4-5 list price, USD per token (cache write 1.25x input, cache read 0.1x input)
PRICE_IN, PRICE_OUT = 1.00 / 1e6, 5.00 / 1e6
MAX_OUT_TOKENS = 600  # llm.MAX_TOKENS
SUPPORT_THRESHOLD = 0.6  # fixed a priori, not tuned
_CITE = re.compile(r"\[([^\[\]\r\n]+)\]")


# ---------- statistics ----------
def wilson(k: int, n: int, z: float = Z) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials; (0, 1) when n == 0."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def bootstrap_ci(values, clusters=None, b: int = 2000, seed: int = SEED):
    """Percentile bootstrap CI of the mean. With `clusters`, resample whole clusters (mailboxes)."""
    values = list(values)
    if not values:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    if clusters is None:
        groups = [[v] for v in values]  # plain bootstrap = every item its own cluster
    else:
        by = collections.defaultdict(list)
        for v, c in zip(values, clusters, strict=True):
            by[c].append(v)
        groups = list(by.values())
    means = []
    for _ in range(b):
        picked = [v for g in rnd.choices(groups, k=len(groups)) for v in g]
        means.append(sum(picked) / len(picked))
    means.sort()
    return (means[int(0.025 * b)], means[int(0.975 * b) - 1])


def prop(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": k / n if n else None, "wilson95": [lo, hi]}


# ---------- data and ACL mapping ----------
def body_of(email: str) -> str:
    return email.split(HEADER_SEP, 1)[-1].strip()


def parse_header(email_text: str) -> tuple[list[str], list[str]]:
    """(sender addresses, recipient addresses), lower-cased, from the header block before the `=====` line.

    Uses the stdlib `email` parser (handles folded headers). EnronQA flattens To/Cc/Bcc into one
    `Recipients: [...]` python-list header, so Bcc cannot be told apart; To/Cc/Bcc headers are also
    read if present. Display names without an address (no '@') are ignored."""
    msg = emaillib.message_from_string(email_text.split(HEADER_SEP, 1)[0])

    def addrs(name: str) -> list[str]:
        items: list[str] = []
        for raw in msg.get_all(name) or []:
            raw = " ".join(str(raw).split())
            try:
                lit = ast.literal_eval(raw)
                items += [str(x) for x in lit] if isinstance(lit, (list, tuple)) else [raw]
            except (ValueError, SyntaxError):
                items.append(raw)
        return sorted({a.lower() for _, a in emaillib.utils.getaddresses(items) if "@" in a})

    rcpt = sorted({a for h in ("Recipients", "To", "Cc", "Bcc") for a in addrs(h)})
    return addrs("Sender") or addrs("From"), rcpt


SENT_FOLDER = re.compile(r"sent", re.I)


def build_address_map(rows: list[dict], min_n: int = 3, min_share: float = 0.5) -> tuple[dict, dict]:
    """address -> user, derived from the data: a user's address is the dominant sender address over
    the emails in that user's own sent folders (folder name contains 'sent'), accepted only with
    >= min_n sent emails and >= min_share of them from that address. An address claimed by two
    users is ambiguous and dropped for both. Nothing is guessed from names."""
    senders: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    users = {r["user"] for r in rows}
    for r in rows:
        parts = r["path"].split("/")
        if len(parts) > 1 and SENT_FOLDER.search(parts[1]):
            for a in parse_header(r["email"])[0]:
                senders[r["user"]][a] += 1
    chosen, no_sent, weak = {}, 0, 0
    for u in sorted(users):
        c = senders.get(u)
        if not c:
            no_sent += 1
            continue
        addr, n = c.most_common(1)[0]
        if n < min_n or n / sum(c.values()) < min_share:
            weak += 1
            continue
        chosen[u] = addr
    by_addr = collections.defaultdict(list)
    for u, a in chosen.items():
        by_addr[a].append(u)
    amap = {a: us[0] for a, us in by_addr.items() if len(us) == 1}
    stats = {
        "rule": f"dominant sender address in the user's own sent folders (folder name contains 'sent'); >= {min_n} sent emails and >= {min_share:.0%} share; addresses claimed by two users dropped",
        "users": len(users),
        "mapped": len(amap),
        "no_sent_emails": no_sent,
        "weak_dominance": weak,
        "ambiguous_addresses": sum(len(us) > 1 for us in by_addr.values()),
        "users_dropped_as_ambiguous": sum(len(us) for us in by_addr.values() if len(us) > 1),
    }
    return amap, stats


def participants(email_text: str, amap: dict, recipients: bool = True) -> set[str]:
    """Mailbox users named in the header (sender, plus recipients if asked) via the address map."""
    sender, rcpt = parse_header(email_text)
    return {amap[a] for a in sender + (rcpt if recipients else []) if a in amap}


def build_acl(rows: list[dict], amap: dict | None = None) -> dict[str, dict]:
    """doc_id -> {"text", "owners", "readers", "paths"}. One doc per distinct body; a body found in
    several mailboxes keeps the first path (sorted) as its id. owners = those mailboxes (union);
    readers = owners plus, when `amap` is given, every header participant that maps to a user."""
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for r in sorted(rows, key=lambda r: r["path"]):
        groups[hashlib.sha256(body_of(r["email"]).encode()).hexdigest()].append(r)
    docs = {}
    for g in groups.values():
        owners = {r["user"] for r in g}
        extra = set().union(*(participants(r["email"], amap) for r in g)) if amap else set()
        docs[g[0]["path"]] = {
            "text": g[0]["email"],
            "owners": owners,
            "readers": owners | extra,
            "paths": [r["path"] for r in g],
        }
    return docs


def acl_for(owners) -> list[str]:
    return sorted(f"user:{u}" for u in owners)


def readable(owners_by_doc: dict[str, set[str]], user: str, doc_id: str) -> bool:
    """The oracle: independent of PermissionRAG. Unknown docs are unreadable (fail closed)."""
    return user in owners_by_doc.get(doc_id, ())


def count_leaks(results: list[dict], user: str, owners_by_doc) -> int:
    bad = 0
    for r in results:
        if not (
            readable(owners_by_doc, user, r["doc_id"])
            and readable(owners_by_doc, user, r["id"].rsplit("#", 1)[0])
        ):
            bad += 1
    return bad


def load_rows(path: Path) -> list[dict]:
    import pyarrow.parquet as pq  # eval-only dependency

    cols = ["email", "path", "user", "questions", "gold_answers", "alternate_answers"]
    return pq.read_table(path, columns=cols).to_pylist()


def sample_mailboxes(rows, n: int, min_emails: int, seed: int) -> list[str]:
    counts = collections.Counter(r["user"] for r in rows)
    eligible = sorted(u for u, c in counts.items() if c >= min_emails)
    return sorted(random.Random(seed).sample(eligible, n))


def make_questions(rows, doc_of_path: dict[str, str], n_q: int, seed: int, keep=None) -> list[dict]:
    qs = []
    for r in rows:
        alts = r.get("alternate_answers") or []
        for i, q in enumerate(r["questions"] or []):
            golds = [r["gold_answers"][i]] + list(alts[i] if i < len(alts) else [])
            qs.append({"user": r["user"], "gold_doc": doc_of_path[r["path"]], "question": q, "golds": golds})
    if keep:
        qs = [q for q in qs if keep(q)]
    qs.sort(key=lambda q: (q["user"], q["gold_doc"], q["question"]))
    random.Random(seed).shuffle(qs)
    return qs[:n_q]


# ---------- retrieval metrics ----------
def hit_ks(results: list[dict], gold_doc: str, ks=KS) -> dict[int, bool]:
    return {k: any(r["doc_id"] == gold_doc for r in results[:k]) for k in ks}


def distinctive_shingle(text: str, df: collections.Counter, n_docs: int, width: int = 8) -> str:
    """The `width`-token window of the body with the highest summed IDF (rare, exact content)."""
    toks = tokenize(body_of(text))
    if len(toks) <= width:
        return " ".join(toks)
    idf = [math.log(n_docs / (1 + df[t])) for t in toks]
    best = max(range(len(toks) - width + 1), key=lambda i: sum(idf[i : i + width]))
    return " ".join(toks[best : best + width])


# ---------- answer scoring (SQuAD-style) ----------
def normalize(s: str) -> str:
    s = re.sub(r"\b(a|an|the)\b", " ", re.sub(r"[^a-z0-9\s]", " ", s.lower()))
    return " ".join(s.split())


def f1(pred: str, gold: str) -> float:
    p, g = normalize(pred).split(), normalize(gold).split()
    common = collections.Counter(p) & collections.Counter(g)
    same = sum(common.values())
    if not p or not g or not same:
        return 0.0
    prec, rec = same / len(p), same / len(g)
    return 2 * prec * rec / (prec + rec)


def best_f1_em(pred: str, golds: list[str]) -> tuple[float, bool]:
    return max(f1(pred, g) for g in golds), any(normalize(pred) == normalize(g) for g in golds)


def supports(cited_text: str, golds: list[str], question: str) -> bool | None:
    """Deterministic: >= SUPPORT_THRESHOLD of a gold answer's content tokens (minus question tokens)
    occur in the cited text. None when no gold answer has content tokens left to test."""
    qt, ct = _tokens(question), _tokens(cited_text)
    scored = [len(a & ct) / len(a) for g in golds if (a := _tokens(g) - qt)]
    return max(scored) >= SUPPORT_THRESHOLD if scored else None


def usage_cost(u: dict) -> float:
    inp = (
        u.get("input_tokens", 0)
        + 1.25 * u.get("cache_creation_input_tokens", 0)
        + 0.1 * u.get("cache_read_input_tokens", 0)
    )
    return inp * PRICE_IN + u.get("output_tokens", 0) * PRICE_OUT


def score_answer(q: dict, answer: str, retrieved: list[dict], owners_by_doc) -> dict:
    """Citation validity for one answer. `retrieved` = the chunks the LLM was given."""
    cites = sorted(set(_CITE.findall(answer)))
    got = {c["doc_id"] for c in retrieved}
    valid = [c for c in cites if c in got and readable(owners_by_doc, q["user"], c)]
    sup = [
        supports(" ".join(c["text"] for c in retrieved if c["doc_id"] == d), q["golds"], q["question"])
        for d in valid
    ]
    clean = _CITE.sub("", answer)
    f, em = best_f1_em(clean, q["golds"])
    return {
        "cites": len(cites),
        "valid": len(valid),
        "support_testable": sum(s is not None for s in sup),
        "supported": sum(bool(s) for s in sup),
        "f1": f,
        "em": em,
        "refusal": "do not answer this" in answer,
    }


def run_llm(qs, results_by_q, owners_by_doc, max_usd: float, ask, log=print) -> dict:
    spent, rows, aborted = 0.0, [], None
    for i, q in enumerate(qs):
        ctx = results_by_q[i][:LLM_K]
        worst = (sum(len(c["text"]) for c in ctx) / 3 + 800) * PRICE_IN + MAX_OUT_TOKENS * PRICE_OUT
        if spent + worst > max_usd:
            aborted = (
                f"stopped before question {i}: spent ${spent:.4f} + worst-case ${worst:.4f} > cap ${max_usd}"
            )
            break
        out = ask(q["question"], ctx)
        if out is None:
            raise SystemExit("ANTHROPIC_API_KEY is not set; rerun with --no-llm or set it in the environment")
        spent += usage_cost(out["usage"])
        rows.append(score_answer(q, out["answer"], ctx, owners_by_doc) | {"user": q["user"]})
        if (i + 1) % 25 == 0:
            log(f"  llm {i + 1}/{len(qs)} spent ${spent:.4f}")
    return {"rows": rows, "spent_usd": spent, "aborted": aborted}


def summarize_llm(res: dict) -> dict:
    rows = res["rows"]
    if not rows:
        return {"status": "no rows", "spent_usd": res["spent_usd"], "aborted": res["aborted"]}
    cites, valid = sum(r["cites"] for r in rows), sum(r["valid"] for r in rows)
    testable, supported = sum(r["support_testable"] for r in rows), sum(r["supported"] for r in rows)
    cl = [r["user"] for r in rows]
    cited_rows = [r for r in rows if r["cites"]]
    return {
        "status": "ok",
        "n_questions": len(rows),
        "spent_usd": round(res["spent_usd"], 4),
        "cost_per_question_usd": round(res["spent_usd"] / len(rows), 5),
        "aborted": res["aborted"],
        "answers_without_citation": prop(
            sum(1 for r in rows if not r["cites"] and not r["refusal"]), len(rows)
        ),
        "refusals": prop(sum(r["refusal"] for r in rows), len(rows)),
        "citation_retrieved_and_readable": prop(valid, cites),
        "answers_with_all_citations_valid": prop(
            sum(r["valid"] == r["cites"] for r in cited_rows), len(cited_rows)
        ),
        "valid_citation_supports_gold": prop(supported, testable),
        "exact_match": prop(sum(r["em"] for r in rows), len(rows)),
        "f1_mean": statistics.mean(r["f1"] for r in rows),
        "f1_mean_ci95_cluster_bootstrap": bootstrap_ci([r["f1"] for r in rows], cl),
    }


# ---------- main ----------
def pctl(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def U(u: str) -> dict:
    return {"id": u, "groups": []}


def build_rag(docs: dict, readers: dict) -> PermissionRAG:
    rag = PermissionRAG()
    for d, v in docs.items():
        rag.add_document(d, v["text"], acl_for(readers[d]))
    return rag


def rec(hits: list[dict], clusters: list[str]) -> dict:
    return {
        f"hit@{k}": prop(sum(h[k] for h in hits), len(hits))
        | {"cluster_bootstrap95": bootstrap_ci([h[k] for h in hits], clusters)}
        for k in KS
    }


def evaluate(rag, readers, qs, sampled, probes, a, seed) -> tuple[dict, list[list[dict]], list[dict]]:
    """All retrieval metrics for one ACL model. Returns (results, owner top-10 per question, owner hits)."""
    rnd = random.Random(seed)
    o_hits, o_res, o_leaks = [], [], 0
    p_hits, p_cl, p_leaks = [], [], 0
    n_leaks = n_att = n_ret = 0
    times = []
    for q in qs:
        t = time.perf_counter()
        res = rag.retrieve(q["question"], U(q["user"]), k=max(KS))
        times.append(time.perf_counter() - t)
        o_leaks += count_leaks(res, q["user"], readers)
        o_res.append(res)
        o_hits.append(hit_ks(res, q["gold_doc"]))
        others = sorted(readers[q["gold_doc"]] - {q["user"]})
        for u in rnd.sample(others, min(a.participants, len(others))):
            res = rag.retrieve(q["question"], U(u), k=max(KS))
            p_leaks += count_leaks(res, u, readers)
            p_hits.append(hit_ks(res, q["gold_doc"]))
            p_cl.append(q["user"])
        nonr = [u for u in sampled if u not in readers[q["gold_doc"]]]
        for u in rnd.sample(nonr, a.non_readers):
            res = rag.retrieve(q["question"], U(u), k=max(KS))
            n_att += 1
            n_ret += bool(res)
            n_leaks += count_leaks(res, u, readers)
    pr_leaks = pr_att = pr_ret = pr_own = 0
    for d, shingle in probes:
        owner = sorted(readers[d])[0]
        pr_own += any(r["doc_id"] == d for r in rag.retrieve(shingle, U(owner), k=1))
        nonr = [u for u in sampled if u not in readers[d]]
        for u in rnd.sample(nonr, a.non_readers):
            res = rag.retrieve(shingle, U(u), k=max(KS))
            pr_att += 1
            pr_ret += bool(res)
            pr_leaks += count_leaks(res, u, readers)
    cl = [q["user"] for q in qs]
    out = {
        "latency_ms_owner_query": {
            "median": statistics.median(times) * 1000,
            "p95": pctl(times, 0.95) * 1000,
        },
        "leaks": {
            "owner_queries": prop(o_leaks, len(qs)),
            "other_allowed_participant_queries": prop(p_leaks, len(p_hits)),
            "non_reader_question_queries": prop(n_leaks, n_att) | {"queries_returning_anything": n_ret},
            "non_reader_exact_content_probes": prop(pr_leaks, pr_att)
            | {"queries_returning_anything": pr_ret, "probe_width_tokens": 8},
            "probe_sensitivity_control_owner_top1": prop(pr_own, len(probes)),
            "all_attempts": prop(
                o_leaks + p_leaks + n_leaks + pr_leaks, len(qs) + len(p_hits) + n_att + pr_att
            ),
            "all_non_reader_attempts": prop(n_leaks + pr_leaks, n_att + pr_att),
        },
        "recall_owner": rec(o_hits, cl),
        "recall_other_participants": rec(p_hits, p_cl) if p_hits else None,
    }
    return out, o_res, o_hits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(HERE / "data" / "test-00000-of-00001.parquet"))
    ap.add_argument("--out", default=str(ROOT / "evals" / "results" / "2026-10-10-enronqa"))
    ap.add_argument("--mailboxes", type=int, default=24)
    ap.add_argument("--min-emails", type=int, default=100)
    ap.add_argument("--questions", type=int, default=600)
    ap.add_argument("--participants", type=int, default=3, help="other allowed readers queried per question")
    ap.add_argument("--non-readers", type=int, default=3, help="non-readers queried per question/probe")
    ap.add_argument("--probes", type=int, default=600)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--llm-n", type=int, default=0, help="questions sent to the paid LLM step")
    ap.add_argument(
        "--smoke", type=int, default=0, help="paid smoke run on N questions; prints cost, writes nothing"
    )
    ap.add_argument("--max-usd", type=float, default=5.0)
    a = ap.parse_args(argv)
    t_start = time.time()

    rows_all = load_rows(Path(a.data))
    amap, map_stats = build_address_map(rows_all)
    (HERE / "data").mkdir(exist_ok=True)
    (HERE / "data" / "address_map.json").write_text(json.dumps(amap, indent=1, sort_keys=True))  # gitignored
    mailboxes = sample_mailboxes(rows_all, a.mailboxes, a.min_emails, SEED)
    rows = [r for r in rows_all if r["user"] in set(mailboxes)]
    docs = build_acl(rows, amap)
    readers = {d: v["readers"] for d, v in docs.items()}
    owners = {d: v["owners"] for d, v in docs.items()}
    sender_only = {d: v["owners"] | participants(v["text"], amap, recipients=False) for d, v in docs.items()}
    doc_of_path = {p: d for d, v in docs.items() for p in v["paths"]}
    qs = make_questions(rows, doc_of_path, a.questions, SEED)
    if a.smoke:
        qs = qs[: a.smoke]
    print(f"{len(mailboxes)} mailboxes, {len(docs)} docs, {len(qs)} questions", flush=True)

    sizes = [len(v) for v in readers.values()]
    sampled_set = set(mailboxes)
    sharing = {
        "readers_per_doc": {
            "1": sum(s == 1 for s in sizes),
            "2": sum(s == 2 for s in sizes),
            "3-5": sum(3 <= s <= 5 for s in sizes),
            "6+": sum(s >= 6 for s in sizes),
        },
        "pct_docs_with_2plus_readers": 100 * sum(s >= 2 for s in sizes) / len(sizes),
        "mean_readers_per_doc": statistics.mean(sizes),
        "max_readers": max(sizes),
        "docs_with_a_reader_outside_the_sampled_mailboxes": sum(
            bool(v - sampled_set) for v in readers.values()
        ),
        "multi_owner_docs": sum(len(v) > 1 for v in owners.values()),
    }
    df = collections.Counter()
    for v in docs.values():
        df.update(set(tokenize(body_of(v["text"]))))
    probe_docs = random.Random(SEED + 2).sample(sorted(docs), min(a.probes, len(docs)))
    probes = [(d, distinctive_shingle(docs[d]["text"], df, len(docs))) for d in probe_docs]

    t0 = time.time()
    rag = build_rag(docs, readers)
    ingest_s = time.time() - t0
    print(f"indexed {len(rag.chunks)} chunks in {ingest_s:.0f}s", flush=True)
    base = PermissionRAG()  # no-ACL baseline: same chunks and statistics, every chunk public
    pub = frozenset({"*"})
    base.chunks = [{**c, "acl": pub, "acl_doc": pub} for c in rag.chunks]
    b_hits = [hit_ks(base.retrieve(q["question"], U(q["user"]), k=max(KS)), q["gold_doc"]) for q in qs]

    main_res, owner_res, o_hits = evaluate(
        rag, readers, qs, mailboxes, [] if a.smoke else probes, a, SEED + 1
    )
    print("participant-model eval done", flush=True)
    has_other = [bool(readers[q["gold_doc"]] - {q["user"]}) for q in qs]
    cl = [q["user"] for q in qs]
    report = {
        "dataset": json.loads((HERE / "MANIFEST.json").read_text()) | {"split_used": "test"},
        "seed": SEED,
        "params": {k: v for k, v in vars(a).items() if k not in ("data", "out")},
        "acl_model": {
            "participants": "readers = mailbox owner(s) + every Sender/Recipients header address that maps to one of the 150 users via address_map; To/Cc/Bcc are flattened into `Recipients` in this release, so they are not separable",
            "address_map": map_stats,
            "full_mapping_location": "evals/enronqa/data/address_map.json (gitignored; not published)",
        },
        "sample": {
            "mailboxes": mailboxes,
            "n_mailboxes": len(mailboxes),
            "n_docs": len(docs),
            "n_chunks": len(rag.chunks),
            "n_words": sum(len(v["text"].split()) for v in docs.values()),
            "n_questions": len(qs),
            "chunk_words": 80,
            "ingest_seconds": round(ingest_s, 1),
        },
        "sharing_structure": sharing,
        "participants_model": main_res,
        "no_acl_baseline_owner_queries": rec(b_hits, cl),
        "no_acl_baseline_questions_with_other_readers": rec(
            [h for h, o in zip(b_hits, has_other, strict=True) if o],
            [c for c, o in zip(cl, has_other, strict=True) if o],
        ),
        "owner_recall_questions_with_other_readers": rec(
            [h for h, o in zip(o_hits, has_other, strict=True) if o],
            [c for c, o in zip(cl, has_other, strict=True) if o],
        ),
    }

    if not a.smoke:
        # --- positive controls: the harness must see a leak (or a false denial) when one exists
        from mutants import NoFilter

        rnd = random.Random(SEED + 3)
        leaky = NoFilter()
        leaky.chunks = rag.chunks
        c_k = c_n = 0
        for q in qs[:100]:
            u = rnd.choice([u for u in mailboxes if u not in readers[q["gold_doc"]]])
            c_n += 1
            c_k += count_leaks(leaky.retrieve(q["question"], U(u), k=max(KS)), u, readers) > 0
        del leaky

        order = sorted(docs)
        shingle_of = dict(probes)
        targets = []  # docs whose next-in-order doc has a reader this doc lacks
        for i in rnd.sample(range(len(order) - 1), len(order) - 1):
            d, nxt = order[i], order[i + 1]
            diff = readers[nxt] - readers[d]
            if diff and len(targets) < 300:
                targets.append((d, sorted(diff)[0]))
        off = PermissionRAG()  # ACL misaligned by one: doc i is also readable by doc i+1's readers
        for i, d in enumerate(order):
            extra = readers[order[i + 1]] if i + 1 < len(order) else set()
            off.add_document(d, docs[d]["text"], acl_for(readers[d] | extra))
        off_k = ok_k = 0
        for d, u in targets:
            sh = shingle_of.get(d) or distinctive_shingle(docs[d]["text"], df, len(docs))
            off_k += count_leaks(off.retrieve(sh, U(u), k=max(KS)), u, readers) > 0
            ok_k += count_leaks(rag.retrieve(sh, U(u), k=max(KS)), u, readers) > 0
        del off
        gc.collect()

        no_rcpt = build_rag(docs, sender_only)  # bug: recipients ignored -> legitimate readers denied
        fd_n = fd_ok = fd_bad = 0
        for q in qs[:300]:
            others = sorted(readers[q["gold_doc"]] - sender_only[q["gold_doc"]])
            if not others:
                continue
            u = others[0]
            fd_n += 1
            fd_ok += hit_ks(rag.retrieve(q["question"], U(u), k=4), q["gold_doc"], (4,))[4]
            fd_bad += hit_ks(no_rcpt.retrieve(q["question"], U(u), k=4), q["gold_doc"], (4,))[4]
        del no_rcpt
        gc.collect()
        report["controls"] = {
            "NoFilter_mutant_non_reader_queries_leaking": prop(c_k, c_n),
            "off_by_one_acl_mutant_targeted_probes_leaking": prop(off_k, len(targets))
            | {"same_probes_on_correct_engine": prop(ok_k, len(targets))},
            "recipients_ignored_mutant_denies_legit_readers_hit@4": {
                "queries": fd_n,
                "correct_engine_hits": fd_ok,
                "mutant_hits": fd_bad,
                "note": "this bug under-grants, so the oracle sees it as lost recall for allowed readers, not as a leak",
            },
        }
        sup = [supports(docs[q["gold_doc"]]["text"], q["golds"], q["question"]) for q in qs]
        testable = [s for s in sup if s is not None]
        report["support_rule_on_gold_email_text"] = prop(sum(testable), len(testable))

        # --- stratum with real sharing: questions whose email has 2+ readers (the random sample has few)
        shared_qs = make_questions(
            rows, doc_of_path, a.questions, SEED + 5, keep=lambda q: len(readers[q["gold_doc"]]) > 1
        )
        shared_res, _, _ = evaluate(rag, readers, shared_qs, mailboxes, [], a, SEED + 6)
        shared_res.pop("latency_ms_owner_query")
        shared_res["n_questions"] = len(shared_qs)
        shared_res["no_acl_baseline"] = rec(
            [hit_ks(base.retrieve(q["question"], U(q["user"]), k=max(KS)), q["gold_doc"]) for q in shared_qs],
            [q["user"] for q in shared_qs],
        )
        report["shared_email_stratum"] = shared_res
        print("shared stratum done", flush=True)

        # --- previous model for comparison: mailbox owner only
        del base
        gc.collect()
        rag_o = build_rag(docs, owners)
        owner_only, _, _ = evaluate(rag_o, owners, qs, mailboxes, probes, a, SEED + 1)
        del rag_o
        report["single_owner_model_comparison"] = owner_only

    # --- paid step (reads the participants-model owner retrievals)
    n_llm = a.smoke or (0 if a.no_llm else a.llm_n)
    if n_llm:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            report["citation_validity"] = {"status": "skipped: ANTHROPIC_API_KEY not set in the environment"}
            print(report["citation_validity"]["status"])
        else:
            import llm

            res = run_llm(qs[:n_llm], owner_res, readers, a.max_usd, llm.ask)
            report["citation_validity"] = summarize_llm(res)
            report["citation_validity"]["model"] = llm.MODEL
            report["citation_validity"]["pricing_usd_per_mtok"] = {
                "input": PRICE_IN * 1e6,
                "output": PRICE_OUT * 1e6,
            }
            report["citation_validity"]["support_threshold"] = SUPPORT_THRESHOLD
    else:
        report["citation_validity"] = {"status": "not run (--no-llm or --llm-n 0)"}

    if a.smoke:
        print(json.dumps(report["citation_validity"], indent=1))
        return 0
    report["runtime_seconds"] = round(time.time() - t_start)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=1) + "\n")
    (out / "table.md").write_text(render_table(report))
    print(f"wrote {out}/report.json and table.md")
    return 0


def fmt(p: dict) -> str:
    lo, hi = p["wilson95"]
    if not p["n"]:
        return "n/a"
    return f"{p['k']}/{p['n']} = {100 * p['rate']:.2f}% (Wilson 95% CI {100 * lo:.2f}-{100 * hi:.2f}%)"


def recall_cell(p: dict) -> str:
    lo, hi = p["wilson95"]
    c = p["cluster_bootstrap95"]
    return f"{100 * p['rate']:.1f}% (Wilson {100 * lo:.1f}-{100 * hi:.1f}; mailbox-bootstrap {100 * c[0]:.1f}-{100 * c[1]:.1f})"


def render_table(r: dict) -> str:
    s, m, sh, am = r["sample"], r["participants_model"], r["sharing_structure"], r["acl_model"]["address_map"]
    lk, ctl, old = m["leaks"], r["controls"], r["single_owner_model_comparison"]
    rd = sh["readers_per_doc"]
    L = [
        "# EnronQA permission-aware retrieval eval (2026-10-10)",
        "",
        f"Dataset `{r['dataset']['dataset']}` @ `{r['dataset']['revision'][:12]}` (test split), seed {r['seed']}. "
        f"{s['n_mailboxes']} mailboxes, {s['n_docs']} emails, {s['n_chunks']} chunks ({s['n_words']} words), "
        f"{s['n_questions']} questions. Engine: `app/permission_rag.py`, BM25 over the visible set, 80-word chunks.",
        f"Median owner query {m['latency_ms_owner_query']['median']:.0f} ms (p95 {m['latency_ms_owner_query']['p95']:.0f} ms) at this index size.",
        "",
        "## Permission structure (header participants)",
        "",
        "Readers of an email = its mailbox owner plus every Sender/Recipients address that maps to one of the 150 users. "
        "EnronQA flattens To/Cc/Bcc into one `Recipients` field, so they are not separated. "
        f"Address map: {am['mapped']}/{am['users']} users mapped from their dominant sent-folder sender address "
        f"({am['no_sent_emails']} have no sent emails, {am['weak_dominance']} fail the dominance rule, "
        f"{am['users_dropped_as_ambiguous']} dropped as ambiguous).",
        "",
        f"Readers per email: 1: {rd['1']}, 2: {rd['2']}, 3-5: {rd['3-5']}, 6+: {rd['6+']} "
        f"({sh['pct_docs_with_2plus_readers']:.1f}% of emails have 2+ readers; mean {sh['mean_readers_per_doc']:.2f}, max {sh['max_readers']}; "
        f"{sh['docs_with_a_reader_outside_the_sampled_mailboxes']} emails have a reader outside the sampled mailboxes).",
        "",
        "## Leaks (lower is better)",
        "",
        "| Attempt type | Result |",
        "|---|---|",
        f"| Owner queries | {fmt(lk['owner_queries'])} |",
        f"| Other allowed participants, EnronQA question (up to 3 per question) | {fmt(lk['other_allowed_participant_queries'])} |",
        f"| Non-readers, EnronQA question (3 per question) | {fmt(lk['non_reader_question_queries'])} |",
        f"| Non-readers, exact 8-token content probe (3 per probe) | {fmt(lk['non_reader_exact_content_probes'])} |",
        f"| **All attempts** | **{fmt(lk['all_attempts'])}** |",
        f"| All non-reader attempts | {fmt(lk['all_non_reader_attempts'])} |",
        "",
        f"Queries by non-readers that returned anything at all: {lk['non_reader_question_queries']['queries_returning_anything']} (questions), "
        f"{lk['non_reader_exact_content_probes']['queries_returning_anything']} (probes); the rest correctly returned nothing.",
        "",
        "Controls (the oracle must see planted bugs):",
        "",
        f"- Probe text retrieves its own email for its owner at top-1: {fmt(lk['probe_sensitivity_control_owner_top1'])}.",
        f"- `NoFilter` mutant (no ACL check): leaks on {fmt(ctl['NoFilter_mutant_non_reader_queries_leaking'])} non-reader queries.",
        f"- Off-by-one ACL mutant (each email also readable by the next email's readers), targeted probes: leaks on {fmt(ctl['off_by_one_acl_mutant_targeted_probes_leaking'])}; same probes on the real engine: {fmt(ctl['off_by_one_acl_mutant_targeted_probes_leaking']['same_probes_on_correct_engine'])}.",
        f"- Recipients-ignored mutant (readers = owner + sender): over-denies instead of leaking, so it shows as lost recall for legitimate readers: hit@4 {ctl['recipients_ignored_mutant_denies_legit_readers_hit@4']['mutant_hits']}/{ctl['recipients_ignored_mutant_denies_legit_readers_hit@4']['queries']} vs {ctl['recipients_ignored_mutant_denies_legit_readers_hit@4']['correct_engine_hits']}/{ctl['recipients_ignored_mutant_denies_legit_readers_hit@4']['queries']} on the real engine.",
        "",
        "## Recall (higher is better)",
        "",
        "| k | Owner, with ACL | Other allowed participants, with ACL | No-ACL baseline (all questions) |",
        "|---|---|---|---|",
    ]
    for k in KS:
        po = m["recall_other_participants"][f"hit@{k}"] if m["recall_other_participants"] else None
        L.append(
            f"| hit@{k} | {recall_cell(m['recall_owner'][f'hit@{k}'])} | {recall_cell(po) if po else 'n/a'} "
            f"| {recall_cell(r['no_acl_baseline_owner_queries'][f'hit@{k}'])} |"
        )
    L += [
        "",
        "Owner recall on only the questions whose email has another reader: "
        + ", ".join(
            f"hit@{k} {recall_cell(r['owner_recall_questions_with_other_readers'][f'hit@{k}'])}" for k in KS
        ),
        "",
        f"## Shared-email stratum ({r['shared_email_stratum']['n_questions']} questions whose email has 2+ readers)",
        "",
        "The random sample has few shared emails, so this stratum is drawn only from emails with 2+ readers "
        "(it is not representative of all questions).",
        "",
        "| Attempt type | Result |",
        "|---|---|",
        f"| Owner queries | {fmt(r['shared_email_stratum']['leaks']['owner_queries'])} |",
        f"| Other allowed participants (up to 3 per question) | {fmt(r['shared_email_stratum']['leaks']['other_allowed_participant_queries'])} |",
        f"| Non-readers (3 per question) | {fmt(r['shared_email_stratum']['leaks']['non_reader_question_queries'])} |",
        f"| **All attempts** | **{fmt(r['shared_email_stratum']['leaks']['all_attempts'])}** |",
        "",
        "| k | Owner | Other allowed participants | No-ACL baseline |",
        "|---|---|---|---|",
        *[
            f"| hit@{k} | {recall_cell(r['shared_email_stratum']['recall_owner'][f'hit@{k}'])} "
            f"| {recall_cell(r['shared_email_stratum']['recall_other_participants'][f'hit@{k}'])} "
            f"| {recall_cell(r['shared_email_stratum']['no_acl_baseline'][f'hit@{k}'])} |"
            for k in KS
        ],
        "",
        "## Comparison: single-owner ACL (mailbox owner only)",
        "",
        f"All attempts: {fmt(old['leaks']['all_attempts'])}. Non-reader attempts: {fmt(old['leaks']['all_non_reader_attempts'])}.",
        "Owner recall: "
        + ", ".join(f"hit@{k} {recall_cell(old['recall_owner'][f'hit@{k}'])}" for k in KS)
        + ".",
        "",
        "## Citation validity (paid LLM step)",
        "",
    ]
    cv = r["citation_validity"]
    if cv.get("status") != "ok":
        L.append(f"Not measured: {cv.get('status')}.")
    else:
        L += [
            f"Model `{cv['model']}`, {cv['n_questions']} questions, spent ${cv['spent_usd']} (${cv['cost_per_question_usd']}/question).",
            "",
            "| Metric | Result |",
            "|---|---|",
            f"| Citations that point to a retrieved AND readable doc | {fmt(cv['citation_retrieved_and_readable'])} |",
            f"| Answers whose every citation is valid | {fmt(cv['answers_with_all_citations_valid'])} |",
            f"| Valid citations whose retrieved text supports the gold answer | {fmt(cv['valid_citation_supports_gold'])} |",
            f"| Exact match vs gold | {fmt(cv['exact_match'])} |",
            f"| Mean token F1 vs gold (mailbox-bootstrap 95% CI) | {cv['f1_mean']:.3f} ({cv['f1_mean_ci95_cluster_bootstrap'][0]:.3f}-{cv['f1_mean_ci95_cluster_bootstrap'][1]:.3f}) |",
            f"| Refusals | {fmt(cv['refusals'])} |",
        ]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
