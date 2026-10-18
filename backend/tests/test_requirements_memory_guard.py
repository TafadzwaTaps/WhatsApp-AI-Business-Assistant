"""Regression guard: argostranslate must stay out of the always-installed
requirements (it OOM-killed the 512MB Render instance — see
requirements-translation.txt)."""
import os

_HERE = os.path.dirname(os.path.dirname(__file__))


def _lines(name):
    with open(os.path.join(_HERE, name), encoding="utf-8") as fh:
        return [l.strip().lower() for l in fh if l.strip() and not l.strip().startswith("#")]


def test_argostranslate_not_in_default_requirements():
    assert not any(l.startswith("argostranslate") for l in _lines("requirements.txt"))


def test_argostranslate_available_as_opt_in():
    assert any(l.startswith("argostranslate") for l in _lines("requirements-translation.txt"))
