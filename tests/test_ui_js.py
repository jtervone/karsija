"""The browser UI's pure logic (keys, filters, moving, decision state), tested with Node's own test runner.

Skipped when Node is not installed: the UI itself needs no Node, only these tests do.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

JS_TESTS = Path(__file__).parent / "js"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_ui_logic() -> None:
    files = sorted(str(p) for p in JS_TESTS.glob("*.test.mjs"))
    assert files
    result = subprocess.run(["node", "--test", *files], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
