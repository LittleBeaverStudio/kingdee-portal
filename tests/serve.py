"""Isolated UI test server; keeps all runtime state outside the repository."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["PORTAL_NO_WARMUP"] = "1"
os.environ["PORTAL_STATE_DIR"] = tempfile.mkdtemp(prefix="kingdee-ui-")
import app

app.CFG["enable_write_ops"] = False
app.app.run(host="127.0.0.1", port=int(os.environ.get("PORTAL_TEST_PORT", "8875")), threaded=True)
