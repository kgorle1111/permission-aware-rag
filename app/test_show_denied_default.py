"""T11: the hidden-chunk count is a corpus-size side channel, so it stays off unless a demo opts in."""

import os
import pathlib
import subprocess
import sys

APP = pathlib.Path(__file__).resolve().parent


def _flag(env_value: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "SHOW_DENIED"}
    if env_value is not None:
        env["SHOW_DENIED"] = env_value
    run = subprocess.run(
        [sys.executable, "-c", "import underwriter_server as s; print(s.SHOW_DENIED)"],
        cwd=APP,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    return run.stdout.strip().splitlines()[-1]


def test_denied_count_is_off_unless_opted_in() -> None:
    assert _flag(None) == "False"
    assert _flag("0") == "False"
    assert _flag("true") == "False"
    assert _flag("1") == "True"
