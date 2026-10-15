"""The suite is hermetic about credentials: nothing in it reads `.env`."""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _reads_dotenv(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.replace("\\", "/")
            if value == ".env" or value.endswith("/.env") or value == "HANDLEHQ_TOKEN":
                return True
    return False


def test_nothing_in_the_suite_reads_dotenv():
    """A test that read the operator's `.env` (or the dogfood token it holds)
    would pass on one machine, fail on another, and touch a credential."""
    here = Path(__file__).resolve()
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted((REPO_ROOT / "tests").rglob("*.py"))
        if path.resolve() != here
        and "__pycache__" not in path.parts
        and _reads_dotenv(path.read_text(encoding="utf-8-sig"))
    ]
    assert not offenders, f"these test files reference .env or its token: {offenders}"
