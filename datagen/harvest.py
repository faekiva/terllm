"""Harvest real behaviour inside the sandbox.

uv run datagen/harvest.py errors   -> data/harvest/errors.json   (error/output templates the simulator replays)
uv run datagen/harvest.py flags    -> data/harvest/flags.json    (valid flags per program/subcommand from --help)
"""
import json
import pathlib
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import yaml

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT.parent / "data" / "harvest"
SANDBOX = str(ROOT / "sandbox.sh")
MARK = {"out": "@@OUT@@", "err": "@@ERR@@", "rc": "@@RC@@"}


def sandbox(script, timeout=120):
    p = subprocess.run([SANDBOX, "--timeout", str(timeout), "--", "bash", "-c", script], capture_output=True, text=True, errors="replace")
    return p.stdout


def probe_script(pr, shell):
    cmd, setup, cwd = pr["cmd"], pr.get("setup", ""), pr.get("cwd", "")
    run = {"any": 'eval "$CMD"', "bash": 'bash -ic "$CMD"', "zsh": 'zsh -ic "$CMD"'}[shell]
    return f"""set +e
touch ~/.zshrc; cd ~
{{ :; {setup}
}} >/dev/null 2>&1
cd ~; {'cd ' + shlex.quote(cwd) if cwd else ':'}
CMD={shlex.quote(cmd)}
{run} >/tmp/o 2>/tmp/e </dev/null; echo $? >/tmp/rc
printf '\\n{MARK['out']}\\n'; cat /tmp/o; printf '\\n{MARK['err']}\\n'; cat /tmp/e; printf '\\n{MARK['rc']}\\n'; cat /tmp/rc
"""


def parse(raw):
    m = re.search(rf"{MARK['out']}\n(.*)\n{MARK['err']}\n(.*)\n{MARK['rc']}\n(\d+)", raw, re.S)
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


def templatize(text, params, cwd, shell):
    text = text.replace("\r", "")
    text = re.sub(r"(?m)^Looking in links: .*\n", "", text)  # sandbox artefact (PIP_FIND_LINKS)
    text = text.replace("\nWARNING: apt does not have a stable CLI interface. Use with caution in scripts.\n\n", "")  # only printed without a tty
    if shell == "bash":
        text = "\n".join(l for l in text.split("\n") if "cannot set terminal process group" not in l and "no job control" not in l)
    if shell == "zsh":
        text = re.sub(r"(?m)^zsh:(\w+):\d+: ", r"\1: ", text)
        text = re.sub(r"(?m)^zsh:\d+: ", "zsh: ", text)
    text = re.sub(r"\d{4}-\d\d-\d\dT\d\d_\d\d_\d\d_\d+Z", "{ts}", text.replace("{", "{{").replace("}", "}}"))
    text = re.sub(r"--\d{4}-\d\d-\d\d \d\d:\d\d:\d\d--", "--{ts}--", text)
    text = re.sub(r"(?m)^(Resolving .*|wget: .*)$", lambda m: m.group(0), text)
    pairs = [(str(v), k) for k, v in params.items()] + [(cwd, "cwd")]
    if cwd != "/home/dev":
        pairs.append(("/home/dev", "home"))
    for v, k in sorted(pairs, key=lambda kv: -len(kv[0])):
        text = re.sub(rf"(?<![\w./-]){re.escape(v)}(?![\w-])" if re.match(r"\w", v) else re.escape(v), "{" + k + "}", text)
    return text


def run_probe(args):
    pr, shell = args
    cwd = "/home/dev" + ("/" + pr["cwd"] if pr.get("cwd") else "")
    got = parse(sandbox(probe_script(pr, shell)))
    if got is None:
        return pr["key"], shell, None
    out, err, rc = got
    params = pr.get("params", {})
    return pr["key"], shell, {"exit": rc, "stdout": templatize(out, params, cwd, shell), "stderr": templatize(err, params, cwd, shell),
                              "cmd": pr["cmd"], "raw_stderr": err}


def harvest_errors():
    probes = yaml.safe_load((ROOT / "probes.yaml").read_text())
    jobs = []
    for pr in probes:
        sh = pr.get("shells", "any")
        for s in (["bash", "zsh"] if sh == "both" else ["any"] if sh == "any" else sh):
            jobs.append((pr, s))
    corpus, bad = {}, []
    with ThreadPoolExecutor(6) as ex:
        for key, shell, res in ex.map(run_probe, jobs):
            if res is None:
                bad.append(f"{key}|{shell}"); continue
            corpus[f"{key}|{shell}"] = res
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "errors.json").write_text(json.dumps({k: {x: v[x] for x in ("exit", "stdout", "stderr")} for k, v in corpus.items()}, indent=1))
    (OUT / "errors_raw.json").write_text(json.dumps(corpus, indent=1))
    print(f"{len(corpus)} templates, {len(bad)} failed {bad}")
    ok0 = [k for k, v in corpus.items() if v["exit"] == 0 and not k.startswith("git.stash")]
    print("exit==0 (probe may not have failed):", ok0)


FLAG_PROGS = ["ls", "cp", "mv", "rm", "mkdir", "rmdir", "cat", "head", "tail", "grep", "find", "chmod", "ln", "touch", "wc", "tar", "zip",
              "unzip", "curl", "wget", "python3", "pip", "node", "cargo", "make", "cmake", "gcc", "git", "ssh", "less", "sort", "file"]
GIT_SUBS = ["init", "clone", "status", "add", "commit", "checkout", "switch", "branch", "push", "pull", "fetch", "merge", "stash", "remote", "log",
            "diff", "show", "restore", "reset", "rm", "mv", "tag", "config", "rebase", "cherry-pick", "revert", "clean", "blame", "bisect", "describe"]
NPM_SUBS = ["install", "run", "start", "test", "init", "uninstall", "update", "ls", "publish", "ci"]
CARGO_SUBS = ["new", "init", "build", "run", "test", "check", "clean", "add", "fmt", "clippy"]


def harvest_flags():
    jobs = [(p, [p, "--help"]) for p in FLAG_PROGS] + [(f"git {s}", ["git", s, "-h"]) for s in GIT_SUBS] \
        + [(f"npm {s}", ["npm", s, "--help"]) for s in NPM_SUBS] + [(f"cargo {s}", ["cargo", s, "--help"]) for s in CARGO_SUBS] \
        + [("pip install", ["pip", "install", "--help"]), ("python3 -m venv", ["python3", "-m", "venv", "--help"])]
    script = "set +e\n" + "".join(f"printf '\\n@@PROG {name}\\n'; timeout 20 {' '.join(map(shlex.quote, argv))} 2>&1 </dev/null | head -400\n" for name, argv in jobs)
    raw = sandbox(script, timeout=600)
    flags = {}
    for chunk in raw.split("\n@@PROG ")[1:]:
        name, _, body = chunk.partition("\n")
        found = set(re.findall(r"(?<![\w-])(--[A-Za-z][\w-]*|-[A-Za-z0-9])(?![\w])", body))
        flags[name.strip()] = sorted(found)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "flags.json").write_text(json.dumps(flags, indent=1))
    empty = [k for k, v in flags.items() if not v]
    print(f"{len(flags)} programs, no flags parsed for: {empty}")


if __name__ == "__main__":
    {"errors": harvest_errors, "flags": harvest_flags}[sys.argv[1] if len(sys.argv) > 1 else "errors"]()
