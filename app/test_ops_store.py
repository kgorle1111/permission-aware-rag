"""The spend cap is a reservation in a shared SQLite store (ROADMAP B13).

Threads and separate processes hammer one file with a tiny budget; the sum of settled spend must
never pass it. The workers are module-level so multiprocessing's spawn start method can import them.
"""

import datetime as dt
import multiprocessing as mp
import pathlib
import sqlite3
import threading
from collections.abc import Callable

import obs
import pytest

BUDGET = 10.0
PROJECTED = 1.0
ATTEMPTS = 40  # per worker; 4 workers x 40 attempts >> the 10 calls the budget allows


def _hammer(path: str, actual: float, attempts: int) -> int:
    ops = obs.Ops(BUDGET, db_path=path)
    won = 0
    for _ in range(attempts):
        token = ops.reserve(PROJECTED)
        if token is not None:
            ops.settle(token, actual)
            won += 1
    return won


def _run_processes(path: str, actual: float, workers: int = 4) -> int:
    with mp.get_context("spawn").Pool(workers) as pool:
        return sum(pool.starmap(_hammer, [(path, actual, ATTEMPTS)] * workers))


def _run_threads(path: str, actual: float, workers: int = 4) -> int:
    out: list[int] = []
    threads = [
        threading.Thread(target=lambda: out.append(_hammer(path, actual, ATTEMPTS))) for _ in range(workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sum(out)


@pytest.mark.parametrize("runner", [_run_threads, _run_processes])
def test_concurrent_reservations_never_pass_the_cap(
    tmp_path: pathlib.Path, runner: Callable[[str, float], int]
) -> None:
    path = str(tmp_path / "ops.sqlite")
    won = runner(path, PROJECTED)
    spent = obs.Ops(BUDGET, db_path=path).spent_today()
    assert won == 10 and spent == BUDGET  # exactly the budget: no overshoot, no lost capacity


@pytest.mark.parametrize("runner", [_run_threads, _run_processes])
def test_cheaper_settlement_frees_capacity_but_never_passes_the_cap(
    tmp_path: pathlib.Path, runner: Callable[[str, float], int]
) -> None:
    path = str(tmp_path / "ops.sqlite")
    won = runner(path, 0.4)
    spent = obs.Ops(BUDGET, db_path=path).spent_today()
    assert spent <= BUDGET and won * 0.4 == pytest.approx(spent)
    assert spent > BUDGET - PROJECTED  # stopped only when one more projection no longer fits


def test_release_returns_the_reservation() -> None:
    ops = obs.Ops(1.0)
    token = ops.reserve(1.0)
    assert token is not None and ops.reserve(0.01) is None  # held
    ops.release(token)
    assert ops.spent_today() == 0.0 and ops.reserve(1.0) is not None


def test_a_crashed_reservation_expires() -> None:
    clock = [dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.UTC)]
    ops = obs.Ops(1.0, clock=lambda: clock[0])
    assert ops.reserve(1.0) is not None  # the holder dies without settling or releasing
    assert ops.reserve(0.5) is None
    clock[0] += dt.timedelta(seconds=obs.RESERVATION_TTL_S + 1)
    assert ops.reserve(0.5) is not None


def test_settling_an_expired_reservation_still_books_the_spend() -> None:
    clock = [dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.UTC)]
    ops = obs.Ops(5.0, clock=lambda: clock[0])
    token = ops.reserve(1.0)
    clock[0] += dt.timedelta(seconds=obs.RESERVATION_TTL_S + 1)
    assert ops.reserve(0.5) is not None  # sweeps the stale hold
    assert token is not None
    ops.settle(token, 0.7)
    assert ops.spent_today() == 0.7  # the money was spent whether or not the hold survived


def test_spend_survives_a_new_instance_and_resets_at_utc_midnight(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "ops.sqlite"
    clock = [dt.datetime(2026, 10, 10, 23, 59, tzinfo=dt.UTC)]
    first = obs.Ops(5.0, clock=lambda: clock[0], db_path=path)
    token = first.reserve(1.0)
    assert token is not None
    first.settle(token, 0.25)
    assert obs.Ops(5.0, clock=lambda: clock[0], db_path=path).spent_today() == 0.25  # restart
    clock[0] = dt.datetime(2026, 10, 11, 0, 0, tzinfo=dt.UTC)
    assert first.spent_today() == 0.0


def test_store_is_wal_and_a_corrupt_file_fails_loudly(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "ops.sqlite"
    obs.Ops(1.0, db_path=path).spent_today()
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone() == ("wal",)
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        obs.Ops(1.0, db_path=bad).reserve(0.1)  # a cap that cannot be enforced must not silently allow spend


def test_db_path_is_configurable_with_a_gitignored_default() -> None:
    assert str(obs.ops_db_path({"OPS_DB_PATH": "/x/y.sqlite"})) == "/x/y.sqlite"
    assert obs.ops_db_path({}).name == "ops.sqlite"


def test_private_store_closes_its_connection_when_collected() -> None:
    import gc
    import warnings

    gc.collect()  # earlier tests' garbage must not be attributed to this store
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        store = obs.Ops(1.0)
        store.spent_today()
        del store
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]


def test_close_releases_the_private_store() -> None:
    store = obs.Ops(1.0)
    store.close()
    store.close()  # idempotent
    with pytest.raises(sqlite3.ProgrammingError):
        store.spent_today()
