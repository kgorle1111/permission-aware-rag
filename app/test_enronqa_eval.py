"""Tests for evals/enronqa/run.py: ACL mapping, leak oracle, recall and answer-scoring code.

TEST SCAFFOLDING, NOT EVIDENCE. The rows below are a tiny hand-written fixture in the EnronQA
schema (email, path, user, questions, gold_answers, alternate_answers). They exist only so the
metric code can fail in CI without the 160 MB dataset; no reported number comes from them.
"""

import importlib.util
import pathlib

from permission_rag import PermissionRAG

_spec = importlib.util.spec_from_file_location(
    "enronqa_run", pathlib.Path(__file__).resolve().parent.parent / "evals" / "enronqa" / "run.py"
)
eq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eq)

SEP = "=====================================\n"


def row(user, n, body, q="what is the ticket code?", gold="The code is zebra7."):
    path = f"{user}/inbox/{n}."
    return {
        "email": f"Subject: s\nFile: {path}\n{SEP}{body}",
        "path": path,
        "user": user,
        "questions": [q],
        "gold_answers": [gold],
        "alternate_answers": [["zebra7"]],
    }


ROWS = [
    row("alice-a", 1, "The ticket code is zebra7 for the offsite."),
    row("bob-b", 1, "Quarterly gas swap volumes were revised upward."),
    row("bob-b", 2, "Shared memo text sent to both mailboxes."),
    row("carol-c", 7, "Shared memo text sent to both mailboxes."),  # same body, second mailbox
    row("dave-d", 1, "Lunch menu for friday includes salmon."),
]


def test_duplicate_body_gets_union_acl_and_first_path_id():
    docs = eq.build_acl(ROWS)
    assert len(docs) == 4
    shared = docs["bob-b/inbox/2."]
    assert shared["owners"] == {"bob-b", "carol-c"}
    assert sorted(shared["paths"]) == ["bob-b/inbox/2.", "carol-c/inbox/7."]
    assert eq.acl_for(shared["owners"]) == ["user:bob-b", "user:carol-c"]
    assert docs["alice-a/inbox/1."]["owners"] == {"alice-a"}


def _engine(docs):
    rag = PermissionRAG()
    for d, v in docs.items():
        rag.add_document(d, v["text"], eq.acl_for(v["owners"]))
    return rag


def test_engine_matches_oracle_and_non_owner_gets_nothing():
    docs = eq.build_acl(ROWS)
    owners = {d: v["owners"] for d, v in docs.items()}
    rag = _engine(docs)
    mine = rag.retrieve("ticket code zebra7", {"id": "alice-a", "groups": []}, k=10)
    assert [r["doc_id"] for r in mine] == ["alice-a/inbox/1."]
    assert eq.count_leaks(mine, "alice-a", owners) == 0
    theirs = rag.retrieve("ticket code zebra7", {"id": "dave-d", "groups": []}, k=10)
    assert theirs == []
    carol = rag.retrieve("shared memo", {"id": "carol-c", "groups": []}, k=10)
    assert [r["doc_id"] for r in carol] == ["bob-b/inbox/2."]  # union ACL lets the second mailbox read it


def test_oracle_flags_a_leak_and_fails_closed_on_unknown_docs():
    owners = {"d1": {"alice-a"}}
    leaky = [{"id": "d1#0", "doc_id": "d1"}]
    assert eq.count_leaks(leaky, "bob-b", owners) == 1
    assert eq.count_leaks(leaky, "alice-a", owners) == 0
    assert eq.count_leaks([{"id": "ghost#0", "doc_id": "ghost"}], "alice-a", owners) == 1
    # engine-reported doc_id and chunk id disagreeing is also a leak
    assert eq.count_leaks([{"id": "d9#0", "doc_id": "d1"}], "alice-a", owners) == 1


def test_oracle_catches_the_no_acl_mutant():
    from mutants import NoFilter

    docs = eq.build_acl(ROWS)
    owners = {d: v["owners"] for d, v in docs.items()}
    bad = NoFilter()
    for d, v in docs.items():
        bad.add_document(d, v["text"], eq.acl_for(v["owners"]))
    res = bad.retrieve("ticket code zebra7", {"id": "dave-d", "groups": []}, k=10)
    assert eq.count_leaks(res, "dave-d", owners) >= 1


def test_hit_ks_is_prefix_based():
    res = [{"doc_id": "a"}, {"doc_id": "b"}, {"doc_id": "c"}]
    assert eq.hit_ks(res, "c") == {1: False, 4: True, 10: True}
    assert eq.hit_ks(res, "a")[1] is True
    assert eq.hit_ks([], "a") == {1: False, 4: False, 10: False}


def test_wilson_known_values():
    lo, hi = eq.wilson(0, 100)
    assert lo == 0.0 and abs(hi - 0.0370) < 5e-4  # standard Wilson upper bound for 0/100
    lo, hi = eq.wilson(50, 100)
    assert abs(lo - 0.4038) < 5e-4 and abs(hi - 0.5962) < 5e-4
    assert eq.wilson(0, 0) == (0.0, 1.0)
    assert abs(eq.wilson(10, 10)[1] - 1.0) < 1e-9


def test_bootstrap_ci_brackets_mean_and_cluster_widens():
    vals = [1.0] * 30 + [0.0] * 30
    lo, hi = eq.bootstrap_ci(vals, b=500)
    assert lo < 0.5 < hi
    clusters = ["a"] * 30 + ["b"] * 30  # perfectly clustered: resampling clusters is far noisier
    clo, chi = eq.bootstrap_ci(vals, clusters, b=500)
    assert chi - clo > hi - lo
    assert eq.bootstrap_ci([1.0, 1.0, 1.0], b=50) == (1.0, 1.0)


def test_squad_scoring():
    assert eq.normalize("The Cat, sat!") == "cat sat"
    assert eq.f1("a cat sat", "the cat sat") == 1.0
    assert eq.f1("dog", "cat") == 0.0
    f, em = eq.best_f1_em("zebra7", ["The code is zebra7.", "zebra7"])
    assert em and f == 1.0


def test_supports_is_deterministic_and_ignores_question_words():
    golds = ["The code is zebra7."]
    assert eq.supports("ticket code zebra7 offsite", golds, "what is the ticket code?") is True
    assert eq.supports("lunch menu salmon", golds, "what is the ticket code?") is False
    assert eq.supports("anything", ["The ticket code."], "what is the ticket code?") is None


def test_score_answer_flags_unretrieved_and_unreadable_citations():
    owners = {"d1": {"alice-a"}, "d2": {"bob-b"}}
    q = {"user": "alice-a", "question": "what is the ticket code?", "golds": ["zebra7"]}
    ctx = [{"id": "d1#0", "doc_id": "d1", "text": "code zebra7"}, {"id": "d2#0", "doc_id": "d2", "text": "x"}]
    s = eq.score_answer(q, "It is zebra7 [d1]. Also [d2] and [d3].", ctx, owners)
    assert (s["cites"], s["valid"]) == (3, 1)  # d2 retrieved but unreadable, d3 never retrieved
    assert s["supported"] == 1 and s["support_testable"] == 1
    assert s["em"] is False and s["f1"] > 0  # extra words: not exact, still partial credit


def test_spend_cap_aborts_before_overshooting():
    owners = {"d1": {"alice-a"}}
    q = {"user": "alice-a", "question": "what is the ticket code?", "golds": ["zebra7"]}
    ctx = [[{"id": "d1#0", "doc_id": "d1", "text": "code zebra7"}]]
    calls = []

    def fake_ask(question, chunks):
        calls.append(question)
        return {"answer": "zebra7 [d1]", "usage": {"input_tokens": 1000, "output_tokens": 100}}

    res = eq.run_llm([q] * 5, ctx * 5, owners, max_usd=0.004, ask=fake_ask, log=lambda *_: None)
    assert len(calls) == 1 and res["aborted"] and len(res["rows"]) == 1  # worst-case guard stops call 2
    assert abs(res["spent_usd"] - (1000 * eq.PRICE_IN + 100 * eq.PRICE_OUT)) < 1e-12
    full = eq.run_llm([q] * 5, ctx * 5, owners, max_usd=5, ask=fake_ask, log=lambda *_: None)
    summary = eq.summarize_llm(full)
    assert full["aborted"] is None and summary["citation_retrieved_and_readable"]["k"] == 5
    assert abs(summary["citation_retrieved_and_readable"]["wilson95"][1] - 1.0) < 1e-9


def hrow(user, n, sender, recipients_header, folder="inbox", body="memo body"):
    path = f"{user}/{folder}/{n}."
    head = f"Subject: s\nSender: {sender}\n{recipients_header}\nFile: {path}\n{SEP}{body} {user}{n}"
    return {
        "email": head,
        "path": path,
        "user": user,
        "questions": [],
        "gold_answers": [],
        "alternate_answers": [],
    }


def test_parse_header_handles_folded_multi_address_display_names_case_and_bcc():
    text = (
        "Subject: s\nSender: Alice.A@Enron.com\n"
        "Recipients: ['Bob.B@enron.com', 'Smith, John <jsmith@x.com>',\n    'David', 'c@d.com, e@f.com']\n"
        "Bcc: hidden@enron.com\nFile: a/b/1.\n" + SEP + "To: ignored@in.body"
    )
    sender, rcpt = eq.parse_header(text)
    assert sender == ["alice.a@enron.com"]
    assert rcpt == ["bob.b@enron.com", "c@d.com", "e@f.com", "hidden@enron.com", "jsmith@x.com"]
    assert eq.parse_header("Subject: s\nSender: x\nRecipients: []\nFile: f\n" + SEP + "b") == ([], [])


def test_address_map_uses_dominant_sent_sender_and_drops_ambiguous_and_weak():
    none = "Recipients: []"
    rows = (
        [hrow("alice-a", i, "alice@enron.com", none, "sent_items") for i in range(4)]
        + [hrow("bob-b", i, "bob@enron.com", none, "_sent_mail") for i in range(3)]
        + [hrow("bob-b", 9, "alias@enron.com", none, "sent")]  # 3/4 still dominant
        + [hrow("carol-c", i, "shared@enron.com", none, "sent") for i in range(3)]
        + [hrow("dave-d", i, "shared@enron.com", none, "sent") for i in range(3)]  # ambiguous
        + [hrow("erin-e", 1, "erin@enron.com", none, "sent")]  # too few
        + [hrow("fay-f", 1, "fay@enron.com", none, "inbox")]  # no sent folder
    )
    amap, st = eq.build_address_map(rows)
    assert amap == {"alice@enron.com": "alice-a", "bob@enron.com": "bob-b"}
    assert (st["users"], st["mapped"], st["no_sent_emails"], st["weak_dominance"]) == (6, 2, 1, 1)
    assert st["ambiguous_addresses"] == 1 and st["users_dropped_as_ambiguous"] == 2


def test_readers_are_owner_plus_mapped_participants_and_union_over_duplicates():
    amap = {"alice@enron.com": "alice-a", "bob@enron.com": "bob-b", "carol@enron.com": "carol-c"}
    r1 = hrow("alice-a", 1, "alice@enron.com", "Recipients: ['bob@enron.com', 'stranger@x.com', 'David']")
    r2 = hrow("alice-a", 1, "alice@enron.com", "Recipients: ['carol@enron.com']") | {
        "user": "carol-c",
        "path": "carol-c/inbox/5.",
    }
    docs = eq.build_acl([r1, r2], amap)
    assert len(docs) == 1
    d = docs["alice-a/inbox/1."]
    assert d["owners"] == {"alice-a", "carol-c"} and d["readers"] == {"alice-a", "bob-b", "carol-c"}
    solo = eq.build_acl([r1], amap)["alice-a/inbox/1."]
    assert solo["readers"] == {"alice-a", "bob-b"}  # unmapped address and bare display name grant nothing
    assert eq.participants(r1["email"], amap, recipients=False) == {"alice-a"}
    assert eq.build_acl([r1])["alice-a/inbox/1."]["readers"] == {"alice-a"}  # no map = owner only


def test_recipient_can_retrieve_shared_email_and_stranger_cannot():
    amap = {"alice@enron.com": "alice-a", "bob@enron.com": "bob-b"}
    r1 = hrow("alice-a", 1, "alice@enron.com", "Recipients: ['bob@enron.com']", body="ticket zebra7 offsite")
    docs = eq.build_acl([r1], amap)
    readers = {d: v["readers"] for d, v in docs.items()}
    rag = eq.build_rag(docs, readers)
    for user in ("alice-a", "bob-b"):
        res = rag.retrieve("ticket zebra7", {"id": user, "groups": []}, k=5)
        assert [r["doc_id"] for r in res] == ["alice-a/inbox/1."]
        assert eq.count_leaks(res, user, readers) == 0
    assert rag.retrieve("ticket zebra7", {"id": "mallory", "groups": []}, k=5) == []


def test_usage_cost_prices_cache_tiers():
    u = {
        "input_tokens": 100,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 1000,
        "output_tokens": 10,
    }
    assert abs(eq.usage_cost(u) - ((100 + 125 + 100) * eq.PRICE_IN + 10 * eq.PRICE_OUT)) < 1e-12
