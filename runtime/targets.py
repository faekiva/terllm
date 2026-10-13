"""Task-runner targets as completion candidates (fork addition).

`make de` -> the Makefile's targets that start with `de`. Readers are injectable:
`read(path) -> str | None` and `run(argv, cwd) -> str` (stdout, '' on failure).
"""
import json
import os
import re
import subprocess

# command words before the target position -> runner
RUNNERS = {("make",): "make", ("just",): "just", ("task",): "task",
           ("npm", "run"): "npm", ("pnpm", "run"): "npm", ("yarn", "run"): "npm",
           ("bun", "run"): "npm", ("pnpm",): "npm", ("yarn",): "npm"}
MAKE_RE = re.compile(r"^([A-Za-z0-9][\w.\-/]*)\s*::?(?!=)", re.M)
JUST_RE = re.compile(r"^@?([A-Za-z_][\w\-]*)(?:\s+[^:\n]*)?:(?!=)", re.M)   # params may have defaults


def runner_for(words):
    """The runner whose target is being typed, given the segment's finished words."""
    args = [w for w in words if not w.startswith("-")]
    for n in (2, 1):
        if len(args) == n and tuple(args) in RUNNERS:
            return RUNNERS[tuple(args)]
    return ""


def targets(runner, cwd, read=None, run=None):
    """Target names for `runner` in `cwd`, in file order, deduplicated."""
    read = read or _read
    run = run or _run
    if runner == "make":
        text = next(filter(None, (read(os.path.join(cwd, f)) for f in
                                  ("GNUmakefile", "makefile", "Makefile"))), "")
        names = [n for n in MAKE_RE.findall(text) if not n.startswith(".")]
    elif runner == "just":
        text = next(filter(None, (read(os.path.join(cwd, f)) for f in
                                  ("justfile", ".justfile", "Justfile"))), "")
        names = JUST_RE.findall(text)
    elif runner == "npm":
        try:
            names = list(json.loads(read(os.path.join(cwd, "package.json")) or "{}")
                         .get("scripts", {}))
        except (ValueError, AttributeError):
            names = []
    elif runner == "task":
        try:
            out = json.loads(run(["task", "--list-all", "--json"], cwd) or "{}")
            names = [t["name"] for t in out.get("tasks", [])]
        except (ValueError, KeyError, TypeError):
            names = []
    else:
        names = []
    return list(dict.fromkeys(names))


def candidates(words, cur, cwd, read=None, run=None):
    """Targets matching `cur` when `words` (the segment so far) puts it in target position."""
    runner = runner_for(words)
    if not runner or cur.startswith("-"):
        return []
    return [t for t in targets(runner, cwd, read, run) if t.startswith(cur) and t != cur]


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(200_000)
    except OSError:
        return None


def _run(argv, cwd):
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=1.0).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
