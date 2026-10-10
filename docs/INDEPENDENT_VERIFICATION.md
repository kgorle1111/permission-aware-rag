# Independent verification — 2026-10-10

Fresh agents authored contract tests and reviewed the project against its stated
requirements. Test expectations were preregistered and hashed before execution.
They share the same workspace and model family; this is independent authorship
and review context, not sealed hidden evaluation or external-human L4 validation.

## Findings and repairs

| Finding before repair | Resulting behavior | Regression |
|---|---|---|
| Platform security-group auditors could read another user's query text | Every platform audit view redacts query fields, including historical rows | `platform/tests/test_independent_audit.py` |
| New audit records stored query text despite the IDs/counts-only rule | Memory, PostgreSQL and platform SQL record a redacted placeholder; historical records are preserved | `app/test_security.py`, `app/test_pgvector.py`, `platform/tests/test_independent_audit.py` |
| Platform model context had no document framing or escaping | Document IDs and text are escaped inside document tags; the system prompt declares them data | `platform/tests/test_independent_generation.py` |
| Retrieval ignored rebuild_required when pending was false | Both barriers deny cold and cached retrieval | `platform/tests/test_independent_audit.py` |

The rebuild case injects an inconsistent trusted SQL state. No supported transition
or permission bypass was established. The generation test captures mocked outgoing
HTTP JSON; it establishes framing and escaping, not real-model injection resistance.
The platform SQL audit relies on trusted storage and has no hash chain; documentation
now distinguishes it from the reference audit's tamper-evidence mechanisms.

## Authorship and preserved failures

The contract author read requirements and fixture setup, but no runtime source,
existing behavioral tests or mutation results before freezing 15 contract cases.
An initial unsupported k=100 setup assumption was corrected to k=20 after preserving
the first run. Behavioral assertions and expected IDs stayed fixed.

Four audit regressions were authored after the privacy repair, with timing explicitly
recorded. Their initial run exposed two setup assumptions: SQL audit chunk IDs are
serialized strings, and SQLite timestamp reload omits timezone metadata. Preregistered
corrections verify exact IDs through the SQL mirror and compare stored timestamps
before and after presentation; privacy/counts/history/barrier expectations remain fixed.
These four tests and the 15 original contract cases pass. The generation regression
was frozen and failed before repair, then promoted using portable imports with its
original behavioral assertions unchanged.

The reviewer's first private suite reported 7 passed / 3 failed. A supplementary run
reported 20 passed / 4 failed, including the imported original failures and a concrete
cross-user HTTP audit exposure. A separately frozen generation check failed, and five
mutation-validator integrity checks passed. After repairs the unchanged private suites
reported 40 passed, including duplicated imported cases (30 unique checks). Original
failures and frozen artifacts remain in the maintainer's private planning archive.

## Current verification

- Platform: **387 passed**, **98.97%** combined statement/branch coverage.
- Reference suite with actual PostgreSQL: **82 passed**.
- Reference labels: **14/14**, zero leaks; isolation: **0/124** differences.
- Planted retrievers: **8/8** caught.
- Frozen memory and PostgreSQL leak gates: **0/1,350**, digest `db6b5dcb51a5` unchanged.
- Ruff and formatting passed.
- Fresh serial mutation campaign: **1,688/1,708 killed**, **20 reviewed equivalents**,
  zero unresolved outcomes. A fresh complete initial pass killed 1,684 and left 24
  survivors. A separate preregistered quote-handling regression killed four
  non-equivalent survivors in a six-variant targeted follow-up; two default-argument
  omissions remained equivalent. This is not a second fresh whole-catalog pass.
  Runtime source stayed unchanged throughout; equivalent source/AST hashes and
  reasons were independently reviewed and the strict validator passed again in
  the parent checkout.
- Container packaging/restart verification: **passed** on local Linux arm64, with
  isolated names/ports. Authentication, HR/guest behavior, raw-query redaction,
  unchanged preexisting audit rows and signing keys across restart, and
  public-key-only operation passed. Owned containers, volumes and image tag were
  removed. A missing base-image cache digest and two extra verifier assumptions
  were corrected with their original failures preserved; no app failure was found.

Run the public regressions with the regular platform and reference test commands
in `AGENTS.md`. The private reviewer suite is retained locally rather than offered
as a portable public test package. Finite probes do not establish universal security,
real IdP/Drive behavior, production capacity or unassisted external integration.
The earlier 100k benchmark remains historical evidence for its pinned source.

Mutation scope: mutmut 3.8 skips decorated functions other than simple static/class
methods. The generated catalog therefore excludes FastAPI routes/lifecycle,
Pydantic validators, principal properties and the permission transaction context
manager. A complete generated catalog is not exhaustive security mutation coverage.
Separate manual faults in omitted security code were checked against five existing
baseline tests, which all passed on clean code. All four variants failed security
assertions: granting HR to everyone, sharing one principal cache scope, replacing
the query caller with forged HR identity, and removing the audit security guard.
These four selected faults are a separate denominator; no setup/collection errors
were counted as detections. This does not cover every omitted decorated function.

Reproduce the four supplemental faults with the [portable manual runner](../evals/DECORATED_MUTATIONS.md). Their clean baseline and security assertion failures were rerun independently by the parent agent.

The portable decorated-function checks are enforced in both platform Python 3.11/3.12 CI jobs after coverage; drift or an undetected selected fault blocks CI.

## Merged release and Linux confirmation

PRs #6–#12 are merged. Both complete PR12 Linux campaigns passed with the final
test suite: [pull-request run](https://github.com/kgorle1111/permission-aware-rag/actions/runs/38036195914)
and [push run](https://github.com/kgorle1111/permission-aware-rag/actions/runs/38036192853).
Each reported 1,688/1,708 killed and 20 reviewed equivalents, with every other
outcome zero. The parent downloaded both full artifacts and independently
validated them under supported Python 3.12 against current source/waiver pins.
These full Linux runs are separate from the earlier local complete-pass plus
targeted follow-up described above. The generated-catalog scope limits remain.

The [AWS release verification](../evals/results/2026-10-10-aws-release/table.md)
records deployment 2 from the exact merged tree, reference role/privacy probes,
independent CSV verification and the separate platform-upgrade test subset.
It does not establish external-human L4 validation or production authentication.
