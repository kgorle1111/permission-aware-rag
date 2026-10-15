"""The AWS demo image copies an explicit file list; a module added to the server must be added there too."""

import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "deploy" / "aws" / "Dockerfile"


def copied_files() -> list[pathlib.Path]:
    copy = re.search(r"^COPY\s+(?:--\S+\s+)*(.+?)\s+\./\s*$", DOCKERFILE.read_text(), re.M)
    assert copy, "demo Dockerfile has no COPY ... ./ line"
    return [pathlib.Path(p) for p in copy.group(1).split()]


def test_demo_image_file_list_is_enough_to_import_the_server(tmp_path: pathlib.Path) -> None:
    for rel in copied_files():
        shutil.copy(ROOT / rel, tmp_path / rel.name)
    run = subprocess.run(
        [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, '.'); import underwriter_server"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, f"demo image would crash at startup:\n{run.stderr[-2000:]}"
