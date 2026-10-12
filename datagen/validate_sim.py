"""Replay sessions in the Docker sandbox and compare cwd / ls / exit code / git state per step against the simulator.

uv run datagen/validate_sim.py --n 500 [--seed 7] [--jobs 6] [--templates PATTERN] [--out data/validate/report.json]
"""
import argparse
import base64
import collections
import json
import pathlib
import random
import re
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor

import sessions as S

ROOT = pathlib.Path(__file__).resolve().parent
SANDBOX = str(ROOT / "sandbox.sh")
HOME = "/home/dev"
OPAQUE_FIRST = {"docker-compose", "journalctl", "scp", "apt", "apt-get", "systemctl", "service", "docker", "kubectl", "ssh", "kill", "top", "ps", "df", "du", "free", "uptime", "uname",
                "date", "hostname", "id", "history", "env", "printenv", "sleep"}
COMPARE_OUT = {"git status", "git status -s", "git status -sb", "git branch", "git status --short"}
SNAP_FN = r"""
SNAP() {
  if git rev-parse --git-dir >/dev/null 2>&1; then
    br=$(git symbolic-ref -q --short HEAD || echo DETACHED); st=$(git status --porcelain 2>/dev/null | tr '\n' '\034')
    up=$(git for-each-ref --format='%(upstream:short)' "$(git symbolic-ref -q HEAD)" 2>/dev/null); [ -z "$up" ] && up=-
  else br=-; st=; up=-; fi
  printf '@@SNAP\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$PWD" "$(LC_ALL=C ls -A | tr '\n' '\034')" "$br" "$st" "$up" "${VIRTUAL_ENV:--}"
}
"""


def docker_cmd(step):
    """Command the sandbox really runs for a simulated step (opaque tools are skipped, sudo is stripped, pnpm/yarn -> npm)."""
    c = step["cmd"]
    w = c.split()
    if w and w[0] in ("pnpm", "yarn"):
        from sim.shell import npm_equiv
        return " ".join(["npm"] + npm_equiv(w[1:]))
    if step.get("v", True):
        return c
    m = re.match(r"sudo\s+(.*)$", c)
    inner = m.group(1) if m else c
    first = inner.split()[0] if inner.split() else ""
    return "true" if first in OPAQUE_FIRST or first in ("sudo",) else inner


def script_for(session):
    L = [SNAP_FN, "set +e"]
    for line in session["setup"]:
        L.append(f"eval {shlex.quote(line)} >/dev/null 2>&1")
    L.append('SNAP -1 0')
    for i, st in enumerate(session["steps"]):
        c = docker_cmd(st)
        if st.get("long"):
            L.append(f"timeout -s INT 2 bash -c {shlex.quote(c)} </dev/null >/tmp/o 2>/tmp/e; rc=$?; [ $rc -eq 124 ] && rc=130")
        else:
            L.append(f"eval {shlex.quote(c)} </dev/null >/tmp/o 2>/tmp/e; rc=$?")
        L.append(f'SNAP {i} $rc')
        if st["cmd"] in COMPARE_OUT:
            L.append(f"printf '@@OUT\\t{i}\\t%s\\n' \"$(base64 -w0 /tmp/o)\"")
    return "\n".join(L) + "\n"


def run_docker(session):
    shell = session["shell"]
    p = subprocess.run([SANDBOX, "--timeout", "90", "--", shell, "-c", script_for(session)], capture_output=True, text=True, errors="replace")
    snaps, outs = {}, {}
    for line in p.stdout.split("\n"):
        f = line.split("\t")
        if f[0] == "@@SNAP" and len(f) == 9:
            snaps[int(f[1])] = {"exit": int(f[2]), "cwd": f[3], "ls": sorted(x for x in f[4].split("\x1c") if x), "branch": f[5],
                                "dirty": sorted(x for x in f[6].split("\x1c") if x), "upstream": f[7], "venv": f[8]}
        elif f[0] == "@@OUT" and len(f) == 3:
            outs[int(f[1])] = base64.b64decode(f[2]).decode(errors="replace")
    return snaps, outs, p.stderr[-300:]


def sim_replay(session):
    """Re-run the session in a fresh simulator, capturing post-state per step (the stored steps hold pre-state)."""
    from sim.shell import Sim
    sim = Sim(session["shell"])
    for line in session["setup"]:
        sim.run(line)
    post = []
    for st in session["steps"]:
        r = sim.run(st["cmd"])
        root, repo = __import__("sim.git", fromlist=["x"]).find_repo(sim)
        post.append({"exit": 130 if r.long else r.code, "cwd": sim.cwd, "ls": sorted(sim.fs.listdir(sim.cwd)),
                     "branch": repo.head if repo else "-", "dirty": sorted(__import__("sim.git", fromlist=["x"]).short_status(sim, root, repo)) if repo else [],
                     "upstream": (repo.upstream.get(repo.head) or "-") if repo else "-",
                     "venv": sim.venv or "-", "out": r.out})
    return post


def compare(session, post, snaps, outs):
    rows = []
    for i, st in enumerate(session["steps"]):
        got, exp = snaps.get(i), post[i]
        if got is None:
            rows.append((i, st, ["missing"])); continue
        diffs = [k for k in ("exit", "cwd", "ls", "branch", "dirty", "upstream", "venv") if got[k] != exp[k]]
        if i in outs and outs[i].strip() != exp["out"].strip():
            diffs.append("stdout")
        rows.append((i, st, diffs, {k: (exp[k], got[k]) for k in diffs if k != "stdout"}, (exp["out"], outs.get(i)) if "stdout" in diffs else None))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--templates", default="")
    ap.add_argument("--out", default=str(ROOT.parent / "data" / "validate" / "report.json"))
    a = ap.parse_args()
    T = {k: v for k, v in S.load_templates().items() if a.templates in k}
    r = random.Random(a.seed)
    tids = sorted(T)
    sessions = []
    for i in range(a.n):
        tid = tids[i % len(tids)] if a.n >= len(tids) else r.choice(tids)
        s, err = S.make(T[tid], r.randrange(10**9))
        if s:
            sessions.append(s)
    print(f"{len(sessions)} sessions from {len(tids)} templates")
    with ThreadPoolExecutor(a.jobs) as ex:
        results = list(ex.map(run_docker, sessions))
    n_steps = n_ok = n_sess_ok = n_excl = 0
    fields, bad_cmds, first_div, examples = collections.Counter(), collections.Counter(), collections.Counter(), []
    per_tpl = collections.defaultdict(lambda: [0, 0])
    for s, (snaps, outs, errtail) in zip(sessions, results):
        post = sim_replay(s)
        rows = compare(s, post, snaps, outs)
        sess_ok, first = True, None
        for row in rows:
            i, st = row[0], row[1]
            if not st.get("v", True):
                n_excl += 1; continue
            n_steps += 1
            per_tpl[s["template_id"]][0] += 1
            if row[2]:
                sess_ok = False
                per_tpl[s["template_id"]][1] += 1
                for d in row[2]:
                    fields[d] += 1
                bad_cmds[" ".join(st["cmd"].split()[:2])] += 1
                if first is None:
                    first = row
                    first_div[" ".join(st["cmd"].split()[:2])] += 1
                    examples.append({"template": s["template_id"], "shell": s["shell"], "step": i, "cmd": st["cmd"], "diffs": row[3] if len(row) > 3 else row[2],
                                     "stdout": row[4] if len(row) > 4 else None})
            else:
                n_ok += 1
        n_sess_ok += sess_ok
    rep = {"sessions": len(sessions), "compared_steps": n_steps, "step_agreement": n_ok / max(n_steps, 1), "excluded_opaque_steps": n_excl,
           "session_agreement": n_sess_ok / max(len(sessions), 1), "field_mismatches": fields.most_common(), "first_divergence_by_cmd": first_div.most_common(25),
           "worst_templates": sorted(((k, v[1] / v[0]) for k, v in per_tpl.items() if v[1]), key=lambda x: -x[1])[:25], "examples": examples[:60]}
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=1, default=str))
    print(f"step agreement {rep['step_agreement']:.1%} ({n_ok}/{n_steps}), session agreement {rep['session_agreement']:.1%}, opaque steps excluded {n_excl}")
    print("field mismatches:", fields.most_common())
    print("first divergence by cmd:", first_div.most_common(12))
    for e in examples[:12]:
        print(" ", e["template"], e["shell"], "|", e["cmd"], "|", json.dumps(e["diffs"], default=str)[:300])


if __name__ == "__main__":
    main()
