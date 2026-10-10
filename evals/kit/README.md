# Bring your own retriever: deterministic ACL leak checks

Run from the repository root with Python 3.12+; no package installation or model key is needed:

```sh
python3 -m evals.kit.run --mutants
python3 -m evals.kit.run --fixture evals/kit/public_constitution.json --mutants
python3 -m evals.kit.run --adapter evals/kit/example_adapter.py
python3 -m pytest -q app/test_leak_kit.py
```

Exit 0 means the requested checks passed; exit 1 means a gate failed. With `--mutants`, success requires the correct reference adapter to pass and all eight known faulty retrievers to fail through observed violations, without crashes. JSON reports include denominators, limited failing examples, input/implementation digests and the base Git commit. `--output report.json` saves the same report. These finite fixtures do not certify security.

## Connect a different retriever

Copy [example_adapter.py](example_adapter.py) to your own trusted Python file. It is a runnable, independent term-overlap retriever, with no imports from the application or oracle. Export `factory()` returning a fresh object with three methods:

```python
class Adapter:
    def build(self, chunks): ...
    def retrieve(self, query, principal, k): ...
    def replace_acl(self, chunk_id, levels): ...


def factory():
    return Adapter()
```

`build` replaces the entire test index with exactly the supplied chunks, including an empty list; do not mutate the fixture. Each chunk has `id`, `text`, and the three independent arrays `acl_doc`, `acl_section`, `acl_para`. The fixture is already split at paragraph permission boundaries. Return a list of `{ "id": chunk_id, "score": finite_number }` before generation, in ranked order, with unique IDs and at most k rows. IDs must remain stable when the indexed subset changes. `replace_acl` updates only supplied levels and returns after the update is effective; the next query uses the same adapter without rebuilding. It must invalidate any relevant cached permission/results state.

In a remote adapter, map the fixture principal to test-only authenticated credentials/session state. Never put production principal selection in the query or accept a model's role claim. Use a disposable index: the harness builds full and isolated copies and deliberately changes permissions. Both copies must use the same fixed embedding model and ranker. This interface tests observable retrieval; it does not test your token verifier, ingestion chunker, connector sync SLA, or database credential safety.

Then run:

```sh
python3 -m evals.kit.run --adapter /absolute/path/to/my_adapter.py
python3 -m evals.kit.run --adapter /absolute/path/to/my_adapter.py \
  --fixture evals/kit/public_constitution.json
```

Adapters are executed as local code. For an ANN backend, first establish repeatability; this initial runner requires exact ID/order/score equality and provides no approximate tolerance. Index-layout changes can affect ANN results, so an isolation difference needs investigation rather than automatically proving a disclosure bug. An operation error fails the gate rather than becoming a clean empty result.

## What is checked

- An independent specification, importing no application matcher, computes exact permission membership: OR within each valid nonempty level, AND across all three. Explicit scope/ID expectations in the tests cross-check that specification.
- For every scope and each chunk's exact-content query at k=1, 3, 20, full-corpus results are compared with results from an independently selected visible-only corpus. Forbidden/unknown IDs, ordered ID/score differences, duplicate IDs and nonfinite scores fail.
- Every allowed chunk must appear for its exact-content query at the largest k. An adapter that returns nothing for everyone therefore fails.
- Twelve malformed/empty per-level ACL controls must be rejected at ingest or denied to every fixture principal. One warmed permission-revocation control must stop returning its target without process restart.
- The project's eight deliberately faulty implementations are replayed. A separate flat-ACL `GlobalIdf` control detects score isolation differences with **zero forbidden IDs**, so score testing is not masked by a hierarchy disclosure.

`FlattenIntersection` is a false-denial fault: it is caught by positive recall controls, not by disclosure/isolation. The pre-hierarchy `GlobalIdf` and `PostFilterTopK` implementations also ignore child levels; mixed-hierarchy results do not isolate only their originally named fault. The separate flat score control addresses that confound for IDF.

## Public text and fictional permissions

[public_constitution.json](public_constitution.json) contains 87 paragraph texts extracted from the [National Archives Constitution transcription](https://www.archives.gov/founding-docs/constitution-transcript), including the body's closing scribal/attestation paragraphs, excluding signatures/footer. Only paragraph text is included; no images, biographies, navigation or other holdings. The text is historical, externally authored public input. [NARA's reuse policy](https://www.archives.gov/global-pages/privacy.html#copyright) describes federal/NARA works as public domain and permits worldwide reuse of NARA works under CC0; it also warns that other holdings may carry restrictions.

The eight-pattern ACL overlay is entirely fictional: public, HR, HR+Bob section, executive paragraph, HR-admin, literal `banking*` group, Dana, and HR+Bob+finance paragraph. All source text is public in reality. This tests isolation under a controlled permission assignment; it is not evidence about actual government permissions, underwriting policies or the missing 296-chunk corpus. A canonical fixture chunk is mapped to one paragraph-sized application document so IDs survive subset rebuilding.

The [manifest](public_constitution.manifest.json) records the source URL, downloaded HTML digest, extraction boundaries, fixture digest and rights provenance. The checked-in fixture runs offline. To review a new source snapshot explicitly:

```sh
curl -fsSL https://www.archives.gov/founding-docs/constitution-transcript -o /tmp/constitution.html
python3 -m evals.kit.prepare_public /tmp/constitution.html --output /tmp/public_constitution.json
```

Do not overwrite the checked-in fixture/manifest to make a failing gate pass. Source HTML may change; review new extraction and provenance independently. The snapshot manifest currently records the 2026-10-08 research date.

## Optional promptfoo runner adapter

The supplied provider returns the deterministic report as structured output; the assertion checks `output.passed`. It supplements your answer-level red team tests. With your existing promptfoo installation:

```sh
promptfoo eval -c evals/kit/promptfooconfig.yaml
```

Provider `config` may specify `python`, `adapter`, and `fixture`. The provider shim was exercised directly with Node, including subprocess-error propagation. The full promptfoo SDK/CLI evaluation was **not** executed, and no competitor sensitivity comparison is claimed. Its extension shape follows [custom providers](https://www.promptfoo.dev/docs/providers/custom-api/) and [JavaScript object assertions](https://www.promptfoo.dev/docs/configuration/expected-outputs/javascript/).

No unfamiliar engineer has yet integrated their own retriever unassisted. The standalone example proves a second adapter works locally; it does not establish L4. Results do not cover real embeddings, approximate vector search, all possible queries, production identity, answer jailbreaks or recovered private legal input.
