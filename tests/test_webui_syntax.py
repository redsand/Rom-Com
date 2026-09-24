"""The browser's parser is stricter than `node --check`."""
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "romcom" / "webui"


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
@pytest.mark.parametrize("name", ["app.js"])
def test_the_ui_parses_as_a_classic_script(name):
    """`node --check` treats a file as CommonJS, where a top-level `return` is legal. The
    browser loads app.js as a classic script, where it is a SyntaxError that kills the whole
    file — the entire UI goes dead, not just the broken handler. That is exactly what shipped
    once: a click handler spliced two lines above its listener instead of inside it, passed
    `node --check`, and took the page down with
    `Uncaught SyntaxError: Illegal return statement`.

    `new Function(src)` applies the same rules as a script tag without executing the body.
    """
    src = (UI / name).read_text(encoding="utf-8")
    checker = "const fs=require('fs');new Function(fs.readFileSync(process.argv[1],'utf8'));"
    r = subprocess.run(["node", "-e", checker, str(UI / name)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"{name} does not parse as a classic script:\n{r.stderr.strip()}"
    assert src.strip(), f"{name} is empty"
