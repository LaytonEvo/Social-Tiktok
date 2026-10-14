import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _names(specs):
    return {re.split(r"[<>=\[ ]", s.strip(), maxsplit=1)[0].lower() for s in specs if s.strip() and not s.startswith("#")}


def test_railway_installs_every_runtime_dependency():
    """Railway installs requirements.txt, not pyproject.toml: a package missing there crashes the job live."""
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    installed = (ROOT / "requirements.txt").read_text().splitlines()
    assert _names(declared) == _names(installed)
