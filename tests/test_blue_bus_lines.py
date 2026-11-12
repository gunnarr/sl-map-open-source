"""Blue bus colouring: a letter variant of a blue line (676X, 176X) is blue too.

The helper lives inline in map.html and lgtv.html, so it is cut out of each page and run in node.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_NODE = shutil.which("node")

# lgtv.html is not part of the open-source export.
PAGES = ["map.html"] + [p for p in ["lgtv.html"] if (PROJECT_ROOT / p).is_file()]

CASES = {
    "676": True,
    "676X": True,
    "176X": True,
    "1": True,
    "179": True,
    "873": True,
    "60": False,
    "155X": False,
    "43X": False,
    "6X1": False,
    "676XX": False,
    "X676": False,
    "?": False,
}


def _helper_source(page):
    html = (PROJECT_ROOT / page).read_text(encoding="utf-8")
    m = re.search(r"(?:const|var) BLUE_BUS_LINES = .*?\n  function isBlueBusLine\(desig\) \{\n.*?\n  \}\n", html, re.S)
    assert m, f"BLUE_BUS_LINES + isBlueBusLine not found in {page}"
    return m.group(0)


@pytest.mark.skipif(_NODE is None, reason="node not installed")
@pytest.mark.parametrize("page", PAGES)
def test_letter_variants_of_blue_lines_are_blue(page):
    script = (
        _helper_source(page)
        + f"const cases={json.dumps(list(CASES))};"
        + "process.stdout.write(JSON.stringify(Object.fromEntries(cases.map(c => [c, isBlueBusLine(c)]))));"
    )
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=20, check=True)
    assert json.loads(out.stdout) == CASES


@pytest.mark.parametrize("page", PAGES)
def test_bus_colour_uses_the_helper(page):
    html = (PROJECT_ROOT / page).read_text(encoding="utf-8")
    assert "isBlueBusLine(desig) ? BLUE_BUS_COLOR : BUS_COLOR" in html
