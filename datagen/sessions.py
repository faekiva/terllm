"""Scenario YAML -> concrete sessions, executed in the simulator (the simulator supplies exit codes, stderr and state)."""
import hashlib
import json
import pathlib
import random
import re

import yaml

from fixtures import NAMED
from sim.errors import Unsupported
from sim.shell import Sim
from wordlists import base_dir, draw, pick

ROOT = pathlib.Path(__file__).resolve().parent
SCEN = ROOT / "scenarios"
CLASSES = ["created_use", "chained", "error_fix", "state_action", "routine", "nav_noise", "t2c", "paths"]
MAX_OUT, MAX_ERR = 1000, 1500


class Discard(Exception):
    pass


def load_templates(path=SCEN):
    """-> {template_id: spec} with variants flattened."""
    out = {}
    for f in sorted(pathlib.Path(path).rglob("*.yaml")):
        docs = yaml.safe_load(f.read_text())
        for t in docs if isinstance(docs, list) else [docs]:
            variants = {None: {}, **(t.pop("variants", None) or {})}
            for vname, ov in variants.items():
                spec = {**t, **{k: v for k, v in ov.items() if k != "vars"}}
                spec["vars"] = {**t.get("vars", {}), **ov.get("vars", {})}
                tid = t["id"] + (f"/{vname}" if vname else "")
                if "foreach" in spec:          # one template per item: the held-out split then tests unseen tools/modules/errors
                    (name, items), = spec.pop("foreach").items()
                    items = NAMED[items[1:]] if isinstance(items, str) else items
                    for it in items:
                        key = re.sub(r"[^\w.+-]", "_", str(it[0] if isinstance(it, (list, tuple)) else it))
                        sp = {**spec, "vars": {name: {"choice": [list(it) if isinstance(it, tuple) else it]}, **spec["vars"]}, "template_id": f"{tid}@{key}"}
                        out[sp["template_id"]] = sp
                    continue
                spec["template_id"] = tid
                out[tid] = spec
    return out


def typo(r, w):
    """A plausible keyboard slip of w (drop / swap / double a character), never equal to w."""
    for _ in range(20):
        i = r.randrange(1, max(len(w) - 1, 2))
        op = r.randrange(3)
        t = w[:i] + w[i + 1:] if op == 0 else (w[:i] + w[i + 1] + w[i] + w[i + 2:] if i + 1 < len(w) else w) if op == 1 else w[:i] + w[i] + w[i:]
        if t != w and t:
            return t
    return w + "x"


def fmt(s, v):
    if not isinstance(s, str):
        return s
    out = s.format(**v)
    # foreach items carry their own {placeholders}: resolve a second time, only for known variable names
    return re.sub(r"\{(\w+)\}", lambda m: str(v[m.group(1)]) if m.group(1) in v else m.group(0), out)


def noise_pool(sim):
    names = [n for n in sim.fs.listdir(sim.cwd)]
    pool = ["ls", "ls", "ls -la", "ls -a", "pwd", "clear"]
    root, repo = __import__("sim.git", fromlist=["x"]).find_repo(sim)
    if repo:
        pool += ["git status", "git status", "git branch"]
        if repo.head_id():
            pool.append("git log --oneline -3")
    pool += [f"cat {n}" for n in names if n in ("README.md", "requirements.txt", "Makefile", "package.json", "main.c", "app.py") or n.endswith((".txt", ".md"))][:2]
    return pool


def run_step(sim, cmd, target, expect=None):
    pre = {**sim.snapshot(), "fs": sim.fs_for(cmd)}
    try:
        res = sim.run(cmd)
    except Unsupported as e:
        raise Discard(f"unsupported:{e}")
    if res.long:
        res.code = 130
    if expect == "any":
        pass
    elif expect == "fail" and res.code == 0:
        raise Discard(f"expected failure: {cmd}")
    elif expect in (None, "ok") and res.code != 0:
        raise Discard(f"unexpected failure({res.code}): {cmd}")
    elif expect == "fail" and res.code == 130:
        raise Discard(f"interrupted, not failed: {cmd}")
    rec = {**pre, "cmd": cmd, "exit": res.code, "stderr": res.err[:MAX_ERR], "stdout": res.out[:MAX_OUT], "target": target, "v": res.v}
    if res.long:
        rec["long"] = True
    return rec


def build_session(spec, seed):
    r = random.Random(seed)
    v = {"base": base_dir(r)}
    for name, vs in spec.get("vars", {}).items():
        if "fmt" in vs:
            v[name] = fmt(vs["fmt"], v)
        elif "typo" in vs:
            v[name] = typo(r, str(v[vs["typo"]]))
        elif "add" in vs:
            v[name] = v[vs["add"][0]] + vs["add"][1]
        else:
            v[name] = draw(r, vs)
    shell = pick(r, spec.get("shell", ["bash", "zsh"]))
    sim = Sim(shell)
    setup = [f"mkdir -p {v['base']} && cd {v['base']}"] + [fmt(s, v) for s in spec.get("setup", [])]
    for line in setup:
        try:
            res = sim.run(line)
        except Unsupported as e:
            raise Discard(f"setup unsupported:{e}")
        if res.code != 0:
            raise Discard(f"setup failed: {line}: {res.err[:80]}")
    steps, nl = [], None
    noise = spec.get("noise", {"before": [0, 2], "between": 0.12})
    if noise:
        for _ in range(r.randint(*noise.get("before", [0, 2]))):
            steps.append(run_step(sim, pick(r, noise_pool(sim)), False, expect="any"))
    for i, st in enumerate(spec["steps"]):
        cmd = fmt(st["cmd"], v)
        tgt = bool(st.get("target"))
        rec = run_step(sim, cmd, tgt, st.get("expect"))
        if "nl" in st:
            rec["nl"] = fmt(pick(r, st["nl"]), v)
            nl = nl or rec["nl"]
        steps.append(rec)
        if noise and i < len(spec["steps"]) - 1 and rec["exit"] == 0 and r.random() < noise.get("between", 0.12) and not st.get("no_noise_after"):
            steps.append(run_step(sim, pick(r, noise_pool(sim)), False, expect="any"))
    if not any(s["target"] for s in steps):
        raise Discard("no target step")
    steps = [{k: s[k] for k in s} for s in steps]
    sid = hashlib.blake2b(json.dumps([shell, setup, [(s["cmd"], s.get("nl", "")) for s in steps]]).encode(), digest_size=8).hexdigest()
    out = {"id": f"s-{sid}", "class": spec["class"], "source": "seed", "template_id": spec["template_id"], "shell": shell, "setup": setup, "steps": steps}
    if nl:
        out["nl"] = nl
    return out


def make(spec, seed, tries=8):
    last = None
    for k in range(tries):
        try:
            return build_session(spec, seed * 1000 + k), None
        except Discard as e:
            last = str(e)
    return None, last
