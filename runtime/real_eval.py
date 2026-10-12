"""Model vs zsh-autosuggestions-style history match on a real history file, context-poor (cwd=~, no ls/git, previous commands as hist).
Prints aggregates only (no commands).  uv run runtime/real_eval.py --model models/gguf/terllm-v1-q4_k_m.gguf [--n 300]
"""
import argparse
import os
import random
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import bench  # noqa: E402
import policy  # noqa: E402
import hist_baseline  # noqa: E402

sys.path.insert(0, str(bench.ROOT / "datagen"))
import render  # noqa: E402
import terllmd  # noqa: E402


def ctx(prev):
    steps = [{"cwd": "~", "ls": [], "git": None, "env": {}, "cmd": c, "exit": 0, "stderr": "", "stdout": "", "target": True} for c in prev[-6:]]
    steps.append({**steps[0], "cmd": ""} if steps else {"cwd": "~", "ls": [], "git": None, "env": {}, "cmd": "", "exit": 0, "stderr": "", "stdout": "", "target": True})
    return render.context({"steps": steps}, len(steps) - 1, io=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--hist", default="~/.zsh_history")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--thresholds", default="0.5,0.8,0.95")
    ap.add_argument("--fs", action="store_true", help="add <|fs|> candidates computed from the real ~ and PATH (v3+ models); cwd is unknown in a history file, so relative paths resolve against ~")
    a = ap.parse_args()
    cmds = hist_baseline.load(a.hist)
    port = 8096
    p = subprocess.Popen([str(bench.BIN), "-m", a.model, "--host", "127.0.0.1", "--port", str(port), "-c", "4096", "-t", "4", "-np", "1", "--no-webui"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(200):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1); break
            except OSError:
                time.sleep(0.1)
        idx = sorted(random.Random(1).sample(range(len(cmds) // 5, len(cmds)), min(a.n, len(cmds) - len(cmds) // 5)))
        ths = [float(x) for x in a.thresholds.split(",")]
        res = {("hist", 0): [0, 0, 0]}                                     # [chars_saved, shown, wrong]
        for t in ths:
            res[("model", t)] = [0, 0, 0]; res[("hybrid", t)] = [0, 0, 0]
        total = 0
        for i in idx:
            c, prev = cmds[i], cmds[:i]
            base, total = ctx(prev), total + len(c)
            seen_h = prev
            mq = {}
            for k in range(0, len(c)):
                fs = render.fs_cands(c[:k], "~", terllmd.listdir, terllmd.BINS) if a.fs else []
                r = bench.post(port, {"prompt": render.prompt(base, c[:k], render.history_cands(seen_h, c[:k]), fs), "n_predict": 48, "temperature": 0,
                                      "cache_prompt": True, "n_probs": 1})
                mq[k] = policy.parse(r)
            for key in res:                                     # greedy user: accept a correct-prefix suggestion, otherwise type the next char
                k = 0
                while k < len(c):
                    h = next((x for x in reversed(seen_h) if k and x.startswith(c[:k])), "")[k:]
                    m = policy.confident_prefix(mq[k], key[1]) if key[0] != "hist" else ""
                    s = h if key[0] == "hist" else (m if key[0] == "model" else (m or h))
                    if s and c.startswith(c[:k] + s):
                        if len(s) > 1:
                            res[key][0] += len(s) - 1; res[key][1] += 1; k += len(s); continue
                    elif s:
                        res[key][2] += 1                         # visible wrong suggestion
                    k += 1
        for key, (sv, shown, wrong) in res.items():
            print(f"{'/'.join(map(str, key)):14s} chars_saved={sv/total:6.1%}  wrong_suggestion_events_per_cmd={wrong/len(idx):.2f}")
    finally:
        p.terminate()


if __name__ == "__main__":
    main()
