"""The hosted dashboard's gate (deploy/cloudflare/live/src/gate.ts), tested under Node with the rest of the suite."""

import shutil
import subprocess
from pathlib import Path

import pytest

CLOUDFLARE = Path(__file__).parents[1] / "deploy" / "cloudflare"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_the_workers_gate():
    result = subprocess.run(["node", "--test", "--experimental-strip-types", "live/test/gate.test.ts"], cwd=CLOUDFLARE,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "# fail 0" in result.stdout and "# pass 0" not in result.stdout
