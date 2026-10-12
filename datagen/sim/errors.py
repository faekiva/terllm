"""Harvested error corpus: templates recorded from real command runs in the sandbox (see harvest.py)."""
import json
import pathlib

CORPUS_PATH = pathlib.Path(__file__).resolve().parents[2] / "data" / "harvest" / "errors.json"
_corpus = None


class Unsupported(Exception):
    """Session uses something the simulator can't model faithfully -> drop the session."""


def corpus():
    global _corpus
    if _corpus is None:
        _corpus = json.loads(CORPUS_PATH.read_text())
    return _corpus


def error(key, shell, **params):
    """(exit, stdout, stderr) for harvested probe `key`, falling back from shell-specific to `any`."""
    c = corpus()
    e = c.get(f"{key}|{shell}") or c.get(f"{key}|any")
    if e is None:
        raise Unsupported(f"no harvested error for {key}|{shell}")
    return e["exit"], e["stdout"].format(**params), e["stderr"].format(**params)
