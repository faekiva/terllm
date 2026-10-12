"""Stage 2 of LLM scenario expansion: a concrete simulated context + task -> LLM writes the commands -> the simulator re-executes them.

uv run datagen/llm_sessions.py prompts --per-task 4   -> data/raw/llm/sessions.prompts.jsonl  (upload, run train/llm_gen.py --name sessions)
uv run datagen/llm_sessions.py build <out.jsonl>      -> data/raw/llm/sessions.jsonl.zst      (consumed by build.py as source "llm")
The LLM only proposes command sequences. Exit codes, stderr and state come from the simulator; a session stops at the first command the
simulator cannot model (it is truncated there, never guessed), and sessions that stay shorter than 3 steps are dropped.
"""
import argparse
import collections
import hashlib
import json
import pathlib
import random
import re

import zstandard

from sessions import Discard, run_step
from sim.shell import Sim
from wordlists import base_dir, branch, noun, project, user, filename

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "raw" / "llm"
LANGS = ["python", "python", "node", "node", "c", "cmake", "rust", "docs"]
BLOCK = re.compile(r"rm\s+-\w*r\w*f?\s+(/|~|\$HOME)(\s|$)|mkfs|dd\s+.*of=/dev/|:\(\)\s*\{|chmod\s+-R\s+777\s+/|shutdown|reboot|poweroff|\|\s*(sudo\s+)?(ba)?sh\b|>\s*/dev/sd", re.I)


def recipe(r):
    """-> (setup commands, shell). A random but realistic starting state, built with the same helpers the Docker harvest uses."""
    shell = r.choice(["zsh", "zsh", "bash"])
    d, lang, base = project(r), r.choice(LANGS), base_dir(r)
    s = [f"mkdir -p {base} && cd {base}"]
    k = r.randrange(12)
    if k <= 3:
        s += [f"fx_project {d} {lang} git", f"cd {d}"]
        if k == 1:
            s += [f"echo '# todo' >> README.md"]
        if k == 2:
            s += [f"touch {filename(r)}", f"echo x >> README.md"]
        if k == 3:
            s += [f"git switch -c {branch(r)}"]
    elif k <= 5:
        u = user(r)
        s += [f"fx_remote {u}/{d} {lang}", f"git clone -q https://github.com/{u}/{d}", f"cd {d}"]
    elif k == 6:
        s += [f"fx_project {d} python git", f"cd {d}", "python3 -m venv .venv", "source .venv/bin/activate"]
    elif k == 7:
        s += [f"fx_project {d} node git", f"cd {d}", "npm install"]
    elif k == 8:
        s += [f"mkdir {d}", f"cd {d}"]
    elif k == 9:
        s += ["mkdir -p Downloads", "cd Downloads"] + [f"touch {filename(r, e)}" for e in r.sample(["txt", "md", "py", "json", "csv", "sh"], 3)]
    elif k == 10:
        s += [f"mkdir -p {d}/logs", f"cd {d}", "echo ERROR started > logs/app.log", "echo INFO done > logs/worker.log", f"touch {filename(r, 'sh')}"]
    else:
        s += [f"fx_project {d} {lang}", f"cd {d}"]
    return s, shell


def describe(sim):
    st = sim.snapshot()
    g = st["git"]
    git = "not a git repository" if not g else f"branch {g['branch']}" + (f", upstream {g['upstream']}" if g["upstream"] else "") + (", modified: " + "; ".join(g["dirty"][:5]) if g["dirty"] else ", clean")
    env = (" Python virtualenv is active: " + st["env"]["VIRTUAL_ENV"] + ".") if st["env"] else ""
    return f"Shell: {sim.shell}. Directory: {st['cwd']}. Files here: {' '.join(st['ls'][:25]) or '(empty)'}. Git: {git}.{env}"


def make_prompt(task, ctx, mistake):
    return (f"Write the terminal session of a developer working on this task: {task}\n{ctx}\n"
            "Output only the commands they type, one per line, exactly as typed, 3 to 9 commands, in the order they run them. "
            "Use the real file and branch names above when relevant; pick sensible arguments for anything else. "
            "No explanations, no comments, no markdown, no prompt symbols, no heredocs, no multi-line commands." +
            (" Include one realistic mistake (a typo, a wrong flag or a missing step) that fails, immediately followed by the corrected command." if mistake else ""))


def prompts(per_task):
    tasks = json.load(open(OUT / "tasks.json"))
    n = 0
    with open(OUT / "sessions.prompts.jsonl", "w") as f:
        for t in tasks:
            for j in range(per_task):
                r = random.Random(f"{t['id']}/{j}")
                for _ in range(5):                                # a recipe that fails in the simulator is simply redrawn
                    setup, shell = recipe(r)
                    sim = Sim(shell)
                    try:
                        for line in setup:
                            if sim.run(line).code != 0:
                                raise Discard(line)
                        break
                    except Exception:
                        sim = None
                if sim is None:
                    continue
                mistake = t["class"] == "error_fix" or r.random() < 0.2
                f.write(json.dumps({"pid": f"{t['id']}/{j}", "task": t["id"], "class": t["class"], "category": t["category"], "setup": setup, "shell": shell,
                                    "prompt": make_prompt(t["task"], describe(sim), mistake)}) + "\n")
                n += 1
    print(n, "prompts")


def clean(text):
    cmds = []
    for line in text.replace("\r", "").split("\n"):
        line = re.sub(r"^\s*(\$|%|>|#\s)\s*", "", line).strip()
        if not line or line.startswith(("```", "#")) or line.endswith("\\"):
            continue
        cmds.append(line)
    return cmds[:12]


def build_one(o):
    sim = Sim(o["shell"])
    for line in o["setup"]:
        if sim.run(line).code != 0:
            return None
    steps = []
    for cmd in clean(o["text"]):
        if BLOCK.search(cmd) or len(cmd) > 200:
            break
        try:
            steps.append(run_step(sim, cmd, True, "any"))
        except Discard:
            break                                                 # unsupported by the simulator: keep the realistic prefix only
    if len(steps) < 3:
        return None
    sid = hashlib.blake2b(json.dumps([o["shell"], o["setup"], [s["cmd"] for s in steps]]).encode(), digest_size=8).hexdigest()
    return {"id": f"s-{sid}", "class": o["class"], "source": "llm", "template_id": f"llm.{o['task']}", "shell": o["shell"], "setup": o["setup"], "steps": steps}


def build(path):
    kept, drop, by_class = 0, collections.Counter(), collections.Counter()
    with open(OUT / "sessions.jsonl.zst", "wb") as f, zstandard.ZstdCompressor(level=6).stream_writer(f) as w:
        for line in open(path):
            o = json.loads(line)
            try:
                s = build_one(o)
            except Exception as e:
                drop[type(e).__name__] += 1
                continue
            if s is None:
                drop["short"] += 1
                continue
            w.write((json.dumps(s, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
            kept += 1
            by_class[s["class"]] += 1
    print(f"kept {kept}; dropped {dict(drop)}; per class {dict(by_class)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prompts", "build"])
    ap.add_argument("path", nargs="?")
    ap.add_argument("--per-task", type=int, default=4)
    a = ap.parse_args()
    prompts(a.per_task) if a.cmd == "prompts" else build(a.path)
