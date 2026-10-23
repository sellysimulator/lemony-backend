"""No code calls the global ``random`` module: every draw comes from a seeded
``random.Random`` (or the game seed's ``random.SystemRandom``), so a day replays
identically. The scanner is tested on in-memory sources first, so the scan of
``app/`` cannot pass merely because it scans nothing.
"""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"
ALLOWED = {"Random", "SystemRandom"}


def global_random_uses(source: str) -> list[str]:
    """Each `random.<fn>(…)` call and `from random import <fn>` that is not a generator class."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "random"
            and node.func.attr not in ALLOWED
        ):
            found.append(f"random.{node.func.attr}()")
        elif isinstance(node, ast.ImportFrom) and node.module == "random":
            found.extend(
                f"from random import {a.name}" for a in node.names if a.name not in ALLOWED
            )
    return found


@pytest.mark.parametrize(
    "source, expected",
    [
        ("import random\nx = random.random()", ["random.random()"]),
        ("import random\nrandom.shuffle(xs)", ["random.shuffle()"]),
        ("from random import choice", ["from random import choice"]),
        ("import random\nrng = random.Random('1:2:customers')", []),
        ("import random\nseed = random.SystemRandom().randrange(10)", []),
        ("def f(rng):\n    return rng.random()", []),
    ],
)
def test_the_scanner(source, expected):
    assert global_random_uses(source) == expected


def test_app_never_calls_the_global_random_module():
    files = sorted(APP.rglob("*.py"))
    assert APP / "core" / "engine.py" in files
    offenders = {
        str(path.relative_to(APP)): uses
        for path in files
        if (uses := global_random_uses(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}
