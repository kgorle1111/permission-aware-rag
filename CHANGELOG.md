# Changelog

## Unreleased

- Preserve section restrictions during permission updates and persist reconciliation barriers across failures.
- Serialize retrieval, generation, and audit commits against permission mutations; prevent superseded work from clearing a newer barrier.
- Fail closed on malformed ACLs, invalid identities, missing indexed chunks, and audit failures.
- Scope bounded caches by permission revision, identity, exact query, and requested result count.
- Reconcile permissions at startup and through polling, administrative triggers, and authenticated Drive notifications.
- Persist Drive checkpoints transactionally with permission changes and paginate complete permission snapshots.
- Add isolated leakage, latency, and revocation measurements, plus security regression tests.
- Render demo answers and audit data safely as text and discard stale responses after identity changes.
- Enforce at least 96% combined application and command-script branch coverage in CI.
- Add a nonroot persistent Docker image and functional container checks for authentication, access filtering, restart persistence, and public-key-only verification.
- Add Linux mutation testing and validate complete mutation reports before accepting results.
