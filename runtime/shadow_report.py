"""Offline precision / coverage / chars saved from the plugin's shadow log (what the user typed, what the model said at each prefix).

uv run runtime/shadow_report.py [~/.local/share/terllm/shadow.jsonl] [--thresholds 0.5,0.7,0.8,0.9,0.95]
A suggestion at prefix `typed` is correct iff typed + suggestion == the command that was finally run. Prints aggregates only.
"""
import argparse
import json
import os

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
        done = bad = False
        for e in r["events"]:
            s = pick(e)
            if not s:
                continue
            shown += 1
            if e["typed"] + s == r["cmd"]:
                right += 1
                if not done:
                    saved += len(r["cmd"]) - len(e["typed"]) - 1
                    done = True
            elif r["cmd"].startswith(e["typed"]):
                bad = True
        wrong_cmds += bad and not done
    return f"saved={saved / total:6.1%} shown={shown:5d} precision={right / max(shown, 1):6.1%} cmds_with_only_wrong_hints={wrong_cmds / len(recs):5.1%}"


print("history ", evaluate(lambda e: e["cands"][0][len(e["typed"]):] if e.get("cands") else ""))
for t in map(float, a.thresholds.split(",")):
    print(f"model {t:<4}", evaluate(lambda e: e["raw"] if e["conf"] >= t and "\n" not in e["raw"] else ""))
    print(f"hybrid{t:<4}", evaluate(lambda e: e["raw"] if e["conf"] >= t and "\n" not in e["raw"] else (e["cands"][0][len(e["typed"]):] if e.get("cands") else "")))
