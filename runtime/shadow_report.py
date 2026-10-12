"""Offline precision / chars saved from the plugin's shadow log (what the user typed, what the model said at each prefix).

uv run runtime/shadow_report.py [~/.local/share/terllm/shadow.jsonl] [--thresholds 0.5,0.7,0.8,0.9,0.95]
A suggestion is correct iff typed + suggestion is a prefix of the command that was finally run. chars saved = best single acceptance
per command (len(suggestion) - 1 for the accept key). Prints aggregates only.
"""
import argparse
import json
import os

import policy

ap = argparse.ArgumentParser()
ap.add_argument("path", nargs="?", default="~/.local/share/terllm/shadow.jsonl")
ap.add_argument("--thresholds", default="0.5,0.7,0.8,0.9,0.95")
a = ap.parse_args()
recs = [json.loads(l) for l in open(os.path.expanduser(a.path)) if l.strip()]
recs = [r for r in recs if r["exit"] == 0 and r["cmd"].strip()]
total = sum(len(r["cmd"]) for r in recs)
print(f"{len(recs)} commands, {total} chars; accepted in-shell: {sum(r['accepted'] for r in recs)}")


def evaluate(pick):
    saved = shown = right = wrong_cmds = 0
    for r in recs:
        best, bad = 0, False
        for e in r["events"]:
            s = pick(e)
            if not s:
                continue
            shown += 1
            if r["cmd"].startswith(e["typed"] + s):
                right += 1
                best = max(best, len(s) - 1)
            else:
                bad = True
        saved += best
        wrong_cmds += bad and not best
    return f"saved={saved / total:6.1%} shown={shown:5d} precision={right / max(shown, 1):6.1%} cmds_with_only_wrong_hints={wrong_cmds / len(recs):5.1%}"


hist = lambda e: e["cands"][0][len(e["typed"]):] if e.get("cands") else ""
print("history ", evaluate(hist))
for t in map(float, a.thresholds.split(",")):
    m = lambda e: policy.confident_prefix([tuple(x) for x in e["toks"]], t)
    print(f"model {t:<4}", evaluate(m))
    print(f"hybrid{t:<4}", evaluate(lambda e: m(e) or hist(e)))
