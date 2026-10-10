# Manual checks for decorated security functions

Run from the repository root with the platform development dependencies already
installed in the current Python environment:

```sh
python evals/verify_decorated_mutations.py --output /tmp/permrag-decorated-results
```

The output directory must be new or empty and outside `platform/`. The runner uses
`sys.executable` to run pytest, copies the platform into temporary snapshots, and
removes those snapshots (including generated test keys and databases) afterward.
It excludes local keys, environment files, caches, symlinks and mutation workspaces
from the copies. Repository source and tests are unchanged.

Five selected existing tests must pass on the clean snapshot before four separate
faults run: granting HR through `Principal.principals`, collapsing `scope_key`,
replacing the verified `/query` caller with HR, and removing the `/audit` security
guard. Exact patches and source/test hashes are pinned; drift stops the experiment
rather than silently adapting it. A caught fault requires pytest exit code 1 and
the designated security assertion, without setup errors, skipped cases or failed
positive controls. Other failures are reported separately.

The output includes the preregistered cases, exact patches, file hashes, pytest logs,
JUnit XML and `result.json`. Exit codes are 0 when all four faults are caught, 1 for
an undetected fault or invalid variant failure, and 2 for baseline/setup/drift errors.
`--platform` selects a source tree; `--timeout` limits each pytest invocation.

These four selected manual checks supplement the automated mutation run, whose
decorated-function omissions motivated the experiment. They are separate from its
denominator and do not establish exhaustive coverage. This verifier was authored
after the independent contract tests were frozen; its construction used source
inspection and existing tests. The original private experiment and this portable
runner retain the same four patches and test selections.
