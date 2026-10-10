# External validation protocol

**Status: preparation only.** No unfamiliar engineer has completed an unassisted
integration, no domain reviewer has supplied feedback under this protocol, and
L4 remains unproven. Local example-adapter results do not count as external use.
Use this document with the [kit contract](../evals/kit/README.md),
[public-input evidence](../evals/results/2026-10-08-public-kit/table.md), and
[threat model](THREAT_MODEL.md).

## What counts as L4

An engineer unfamiliar with this implementation independently connects the kit
to their own separate retriever and produces inspectable reports. They use the
published documentation without live maintainer or project-agent integration
help. They may use their normal development tools; record any assistance,
including AI tools, existing familiarity, and copied adapter code. Running only
the reference or supplied example adapter establishes setup, not L4.

For a successful L4 result, retain the tested commit, the integration code or an
inspectable redacted equivalent, both fixture reports, process exit codes, and
the reviewer's account of the unassisted run. The reports must pass without
weakening fixtures, expected permissions, recall controls, or the oracle. Report
an unsuccessful or assisted attempt as such; it still provides useful usability
and security feedback. A later successful retry after maintainer coaching is an
assisted integration, not evidence of the original unassisted criterion.

A passing finite test set establishes this adapter's observed behavior on those
inputs. It does not certify production security, identity validation, semantic
recall, connector latency, or approximate ranking isolation.

## Instructions for the independent engineer

1. Choose a disposable local or remote index owned by you. The runner rebuilds
   full and visible-only indexes and changes ACLs. Use test-only credentials and
   canonical fixture text; do not connect a production corpus or publish secrets.
2. Clone the repository and check out the exact published evidence commit or
   branch. While the work is unmerged, `codex/validation-preparation` contains these instructions, the kit and
   journal evidence. Record `git rev-parse HEAD`; a branch name alone is not a pin.
   Use Python 3.12+ for the stdlib kit. No model key is required for its controls.
3. Read the kit contract. Run the reference controls and public fixture first.
   Save failures as well as successes. Do not change checked-in inputs to obtain
   a pass.
4. Implement a trusted Python adapter exporting `factory()`, with `build`,
   `retrieve`, and `replace_acl`. Preserve stable IDs, three independent ACL
   levels, caller-bound principal mapping, and finite ordered scores. `build`
   must replace the index, including for an empty corpus. A warmed revocation
   must take effect before `replace_acl` returns without rebuilding or restarting.
5. Run both fixtures against your adapter, capture reports and exits, and return
   the feedback form below. Stop and report undocumented setup or contract
   problems rather than accepting project-specific help during the attempt.

Run from the repository root. These commands write only a local evidence folder;
replace the adapter path with your implementation. The output directory must
exist before using `--output`.

```sh
mkdir -p external-validation
python3 --version > external-validation/python.txt
git rev-parse HEAD > external-validation/commit.txt
git status --short > external-validation/tree-status.txt

python3 -m evals.kit.run --mutants \
  --output external-validation/reference-controls.json \
  > external-validation/reference-controls.stdout 2> external-validation/reference-controls.stderr
printf '%s\n' "$?" > external-validation/reference-controls.exit

python3 -m evals.kit.run --fixture evals/kit/public_constitution.json --mutants \
  --output external-validation/reference-public.json \
  > external-validation/reference-public.stdout 2> external-validation/reference-public.stderr
printf '%s\n' "$?" > external-validation/reference-public.exit

python3 -m evals.kit.run --adapter /absolute/path/to/my_adapter.py \
  --output external-validation/own-controls.json \
  > external-validation/own-controls.stdout 2> external-validation/own-controls.stderr
printf '%s\n' "$?" > external-validation/own-controls.exit

python3 -m evals.kit.run --adapter /absolute/path/to/my_adapter.py \
  --fixture evals/kit/public_constitution.json \
  --output external-validation/own-public.json \
  > external-validation/own-public.stdout 2> external-validation/own-public.stderr
printf '%s\n' "$?" > external-validation/own-public.exit
```

Run the commands in a shell that continues after nonzero exits; each `printf`
must immediately follow its runner. Exit 0 means the requested gates passed;
exit 1 means a gate failed. A missing report or process crash is a failed attempt,
not a clean zero-leak result. Keep original files and clearly mark any redacted
copies. The fixture reports contain public/synthetic text examples, IDs and
principal scopes; inspect logs and custom adapter output for credentials before
sharing. Local outputs are not automatically submitted or committed.

Reports record denominators, failing examples, Python version, fixture digest,
implementation digest and source commit. Also record retriever/library versions,
ranker and embedding configuration, backend/index settings, machine resources,
and a digest/version of external adapter dependencies: the kit's implementation
digest does not hash every external package or remote configuration. For ANN,
establish repeatability and investigate exact ID/order/score differences; the
current runner has no approximate tolerance. Preserve a second unchanged run
when diagnosing nondeterminism. Do not relabel it as a verified disclosure or
silently relax equality.

## Reviewer feedback form

Copy this form into a dated local record. The maintainer should publish only
consented attribution and inspectable evidence, with assistance and failures
retained beside successful results.

| Field | Reviewer response |
|---|---|
| Date, tested commit, role, prior project familiarity | |
| Retriever, adapter/dependency versions, backend/environment | |
| Disposable-index and test-identity mapping used | |
| Independent integration or supplied example only? | |
| Documentation used; human/AI assistance and when it occurred | |
| Time to first run and own-adapter run; setup blockers | |
| Report paths, exits, fixture/implementation digests | |
| Failures, unexpected denials, score differences, repeatability | |
| Revocation behavior and whether a restart was required | |
| Contract/documentation changes needed | |
| May attribution, adapter code and redacted reports be published? | |
| Verdict: successful unassisted / assisted / failed / incomplete | |

## Separate permissions and input realism review

A domain practitioner's review tests the underwriting scenario's plausibility;
it does not establish L4 or replace leak gates. Review roles, data classes,
workflow, and permission provenance separately from retrieval implementation.
Do not send private client records or infer real access rights from public text.

Ask the reviewer to identify plausible roles and document owners; who grants,
revokes and approves access; whether section/paragraph restrictions occur; how
exceptions, group changes and deletions should behave; and which synthetic
workflow steps or data classes are unrealistic. Record the source and authority
for every proposed permission rule, including conflicts and unresolved decisions.
Collect independently written realistic questions and expected readable sources,
including permitted, forbidden and no-answer cases. Keep them separate from
exact-content fixture questions and implementation-authored labels.

The [real-input requirements](REAL_INPUT_REQUIREMENTS.md) record the paused
industry-input work and primary-source access evidence. The current Constitution
fixture has externally authored public text but
fictional permissions. A broader input result requires documented rights to use
the input, independently reviewed policy provenance and question expectations,
plus a dated actual run. The historical 296-chunk legal corpus remains unrecovered.
Publish reviewer disagreement and unsupported claims rather than treating a
realism conversation as a successful security evaluation.
