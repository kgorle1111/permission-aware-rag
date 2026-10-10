# Actual promptfoo CLI integration — 2026-10-08

The official **promptfoo 0.124.1 CLI** executed the existing local CommonJS
provider and `output.passed === true` assertion. Node was 26.3.0 and npm 11.16.0.
The existing shim/config required no changes. Each CLI evaluation contains one
assertion over a complete deterministic kit report; kit probe counts are distinct
from promptfoo test counts.

| Case | CLI exit | promptfoo success / assertion failure / provider error | Observed kit behavior |
|---|---:|---|---|
| Exact supplied `evals/kit/promptfooconfig.yaml` | 0 | 1 / 0 / 0 | 192 probes; zero visibility/isolation failures; 18 required hits; 12 invalid-ACL controls; warmed revocation passed |
| Public fixture, reference adapter | 0 | 1 / 0 / 0 | 2,088 probes; zero visibility/isolation failures; 197 required hits; warmed revocation passed |
| Public fixture, standalone example adapter | 0 | 1 / 0 / 0 | 2,088 probes; zero visibility/isolation failures; 197 required hits; warmed revocation passed |
| Deliberately faulty `PostFilterTopK` adapter | 100 | 0 / 1 / 0 | 85 visibility failures and 192 isolation failures; `passed: false` rejected by assertion |
| Missing fixture subprocess | 100 | 0 / 0 / 1 | Provider error `invalid kit JSON: Unexpected end of JSON input`; no clean kit report and no passing assertion |

The missing-fixture process fails before producing JSON. The shim accepts exit 1
for a valid failed kit report, then rejects the empty stdout here during parsing.
This proves failure propagation; it does not preserve the Python traceback as the
reported diagnostic. The faulty adapter is an existing planted project fault,
not a third-party competitor. Its named behavior also ignores child ACLs, so these
counts do not isolate only post-filter ranking effects.

[Machine report](report.json) records exact versions, source and export hashes,
resolved npm integrity, npx lock hash, each CLI exit, assertion/provider distinction
and environment controls. Full CLI exports are retained:
[supplied](supplied-controls.json), [public reference](public-reference.json),
[public example](public-example.json), [fault](faulty-retriever.json),
[subprocess error](subprocess-failure.json). Only machine-specific repository and
tooling path prefixes were replaced by `<REPO_ROOT>` and `<TOOLING_DIR>` in those
published copies; original and published hashes are recorded separately. The
source commit is the kit report's actual run revision, not the future commit that
will contain this evidence. Its local base `495d854` has the same Git tree as
public revision [07b12e6](https://github.com/kgorle1111/permission-aware-rag/tree/07b12e63c36fbc92ea713558ce1010f797d75da3),
recorded separately in the report.

## Reproduce locally

Use Python 3.12+ and Node supported by your chosen promptfoo release. Run from a
fresh checkout containing this evidence. Installing the CLI needs network access;
the provider performs only local Python subprocess calls over synthetic or public
fixture text. No model keys or model calls are needed. Installation here used a
temporary npm cache and added no app dependency. The first evaluations started
while the final optional SDK package was downloading; installation then completed
successfully, and the portable cases were rechecked after completion.

```sh
mkdir -p /tmp/permrag-promptfoo
PROMPTFOO_DISABLE_TELEMETRY=1 PROMPTFOO_DISABLE_UPDATE=1 \
  npm --cache /tmp/permrag-promptfoo/npm-cache exec --yes \
  --package=promptfoo@0.124.1 -- promptfoo --version
```

Locate the installed entrypoint without fetching a different version:

```sh
python3 - <<'PY'
import json
from pathlib import Path
for package in Path('/tmp/permrag-promptfoo/npm-cache/_npx').glob('*/node_modules/promptfoo/package.json'):
    metadata = json.loads(package.read_text())
    if metadata['version'] == '0.124.1':
        print(package.parent / metadata['bin']['promptfoo'])
PY
```

Pass the printed absolute entrypoint to the checked-in stdlib verifier:

```sh
python3 evals/results/2026-10-08-promptfoo/verify_cli.py \
  --cli /absolute/path/to/promptfoo/dist/src/entrypoint.js \
  --output-dir /tmp/permrag-promptfoo/recheck
```

The verifier runs all five CLI commands serially, including the **exact existing
config**, [public reference](public-reference.yaml), [example](public-example.yaml),
[fault](faulty-retriever.yaml) with [minimal fault adapter](fault-adapter.py), and
[missing fixture](subprocess-failure.yaml). Leave `deliberately-missing-fixture.json`
absent. It captures JSON/stdout/stderr locally and exits 0 only when the three
positive passes and both expected negative outcomes are observed. This verifier's
exit 0 does not mean the two deliberately negative promptfoo evaluations passed.
A missing export/crash fails verification. Do not submit the output folder to a
cloud dashboard or describe a runner error as a clean zero-leak result.

The verifier uses a stripped environment, temporary state/log/cache directories,
no-cache/no-write/no-share flags, disabled telemetry/update/remote generation and
sharing, and self-hosted mode. No `.env` file was loaded in its temporary working
directory. The controls follow official [CLI documentation](https://www.promptfoo.dev/docs/usage/command-line/),
[custom-provider contract](https://www.promptfoo.dev/docs/providers/custom-api/),
[object assertion behavior](https://www.promptfoo.dev/docs/configuration/expected-outputs/javascript/),
and [telemetry controls](https://www.promptfoo.dev/docs/configuration/telemetry/).
These settings are not a network firewall; evaluations used the local restricted
sandbox without network escalation.

## Limits

This establishes compatibility of the actual CLI with this local provider and
its positive/negative reports. It does not establish unassisted external
integration (L4), superiority over promptfoo's own assertions or red-team suites,
production identity/sync safety, semantic recall, real embeddings, approximate
ranking isolation, or model/answer security. Public text still has fictional
permissions and exact-content questions. No external engineer, model provider,
cloud run or public posting participated.

The recorded npx lock digest identifies this installation. A fresh installation
of the same top-level version may resolve different transitive dependencies;
this artifact does not vendor the full dependency lock or installed toolchain.
