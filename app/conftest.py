"""Test session setup: keep the server module's ops store (spend cap, stats) out of app/ops.sqlite."""

import atexit
import os
import shutil
import tempfile

# Runs before any test module imports underwriter_server, which reads OPS_DB_PATH at import time.
_ops_dir = tempfile.mkdtemp(prefix="permrag-ops-")
atexit.register(shutil.rmtree, _ops_dir, ignore_errors=True)
os.environ["OPS_DB_PATH"] = os.path.join(_ops_dir, "ops.sqlite")
