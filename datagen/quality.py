"""Shared quality checks: secrets, program extraction, flag validation, `bash -n` (run inside the sandbox only)."""
import json
import pathlib
import re
import shlex
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parents[1]
SANDBOX = str(ROOT / "datagen" / "sandbox.sh")

SECRET_RES = [re.compile(p, re.I if i >= 6 else 0) for i, p in enumerate([
    r"AKIA[0-9A-Z]{16}", r"gh[pousr]_[A-Za-z0-9]{30,}", r"github_pat_[A-Za-z0-9_]{20,}", r"sk-[A-Za-z0-9]{20,}", r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"://[^/\s:@]+:[^/\s@]+@",                                                  # credentials in URLs
    r"\b(password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[=:]\s*[^\s\"']{4,}",
    r"\bAuthorization:\s*(Bearer|Basic)\s+\S+", r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}\.",   # JWT
])]


def has_secret(text):
    return any(r.search(text) for r in SECRET_RES)


def session_texts(s):
    yield from s["setup"]
    if s.get("nl"):
        yield s["nl"]
    for st in s["steps"]:
        yield st["cmd"]; yield st["stderr"]; yield st["stdout"]; yield st.get("nl", "")


def session_has_secret(s):
    return any(has_secret(t) for t in session_texts(s) if t)


WRAPPERS = {"sudo", "time", "nohup", "env", "command", "exec", "xargs", "nice"}


def program_of(cmd):
    try:
        toks = shlex.split(cmd, posix=True)
    except ValueError:
        toks = cmd.split()
    while toks and (toks[0] in WRAPPERS or re.match(r"[A-Za-z_]\w*=", toks[0])):
        toks = toks[1:]
    return toks[0].split("/")[-1] if toks and toks[0] not in ("./",) else "?"


_flags = None


def flag_db():
    global _flags
    if _flags is None:
        p = ROOT / "data" / "harvest" / "flags.json"
        _flags = {k: set(v) for k, v in json.loads(p.read_text()).items()} if p.exists() else {}
    return _flags


def unknown_flags(cmd):
    """Flags in `cmd` that the harvested --help of its program doesn't list; None if the program isn't covered."""
    db = flag_db()
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return None
    while toks and (toks[0] in WRAPPERS or re.match(r"[A-Za-z_]\w*=", toks[0])):
        toks = toks[1:]
    if not toks:
        return None
    key = None
    if toks[0] in ("find", "ssh", "xargs", "tar"):
        return None                       # single-dash long options / old-style bundles: --help isn't a usable oracle
    if toks[0] in ("git", "npm", "cargo") and len(toks) > 1:
        sub = next((t for t in toks[1:] if not t.startswith("-")), None)
        key = f"{toks[0]} {sub}" if sub else None
    elif toks[0] == "pip" and "install" in toks:
        key = "pip install"
    elif toks[0] in db:
        key = toks[0]
    if key is None or key not in db or not db[key]:
        return None
    allowed, bad = db[key] | db.get(toks[0], set()) | (db.get("git (any)", set()) if toks[0] == "git" else set()) | {"-h", "--help", "-q", "--quiet", "-v", "--verbose", "-y", "-D", "-S", "-E", "-g", "-f", "-u", "-b"}, []
    for t in toks[1:]:
        if t == "--":
            break
        if t.startswith("--"):
            f = t.split("=")[0]
            if f not in allowed:
                bad.append(f)
        elif re.fullmatch(r"-[A-Za-z]+\d*", t) and len(t) > 1:
            for ch in t[1:]:
                if ch.isdigit():
                    break
                if f"-{ch}" not in allowed:
                    bad.append(f"-{ch}")
    return bad


def bash_n(cmds, jobs=4, chunk=3000):
    """Commands (single-line strings) that fail `bash -n`; checked inside the sandbox."""
    cmds = list(cmds)
    parts = [cmds[i:i + chunk] for i in range(0, len(cmds), chunk)]

    def run(part):
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as d:
            (pathlib.Path(d) / "cmds.txt").write_text("\n".join(part) + "\n")
            script = 'while IFS= read -r l; do bash -n -c "$l" >/dev/null 2>&1 || printf "%s\\n" "$l"; done </scratch/cmds.txt'
            p = subprocess.run([SANDBOX, "--scratch", d, "--timeout", "600", "--", "bash", "-c", script], capture_output=True, text=True, errors="replace")
            return [l for l in p.stdout.split("\n") if l]
    with ThreadPoolExecutor(jobs) as ex:
        return [c for r in ex.map(run, parts) for c in r]
