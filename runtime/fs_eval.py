"""Does the model use the <|fs|> candidates sensibly? Greedy exact match on val examples that carry an <|fs|> block, with and without the block.

uv run runtime/fs_eval.py --model models/gguf/terllm-v7b-q4_k_m.gguf [--version 7] [--n 1500]
Categories (by the gold completion): stop = command already complete (<|end|>), copy = the word being typed ends up as a candidate,
new = a name that is not a candidate. false_copy = in `new`, the model answered with a candidate anyway.
"""
import argparse
import collections
import json
import re
import subprocess
import time
import urllib.request

from bench import BIN, examples, post

FS = re.compile(r"<\|fs\|>.*?(?=<\|cmd\|>)", re.S)


def category(e):
    p = e["prompt"]
    cands = p.split("<|fs|>")[1].split("<|cmd|>")[0].split()
    want = e["completion"].removesuffix("<|end|>")
    if not want:
        return "stop", cands
    typed = p.rpartition("<|cmd|>")[2]
    word = typed.rpartition(" ")[2] + want.split(" ")[0]
    return ("copy" if word in cands else "new"), cands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--version", type=int, default=7)
    ap.add_argument("--n", type=int, default=1500)
    ap.add_argument("--only", default="", help="restrict to a class, e.g. paths")
    ap.add_argument("--jsonl", default="", help="evaluate these rendered examples instead of the val split (see datagen/paths_probe.py)")
    a = ap.parse_args()
    port = 8098
    srv = subprocess.Popen([str(BIN), "-m", a.model, "--host", "127.0.0.1", "--port", str(port), "-c", "4096", "-t", "4", "-np", "1", "--no-webui"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(200):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1); break
            except OSError:
                time.sleep(0.1)
        ex = [e for e in (map(json.loads, open(a.jsonl)) if a.jsonl else examples(a.version, 10**9)) if "<|fs|>" in e["prompt"] and (not a.only or e["class"] == a.only)][:a.n]
        hit, tot, false_copy = collections.Counter(), collections.Counter(), collections.Counter()
        for e in ex:
            cat, cands = category(e)
            want = e["completion"].removesuffix("<|end|>")
            for mode, prompt in (("fs", e["prompt"]), ("nofs", FS.sub("", e["prompt"]))):
                got = post(port, {"prompt": prompt, "n_predict": 48, "temperature": 0, "cache_prompt": True})["content"].split("<|end|>")[0]
                tot[mode, cat] += 1; hit[mode, cat] += got == want
                if cat == "new" and (e["prompt"].rpartition("<|cmd|>")[2].rpartition(" ")[2] + got.split(" ")[0]) in cands:
                    false_copy[mode] += 1
        for mode in ("fs", "nofs"):
            row = {c: f"{hit[mode, c] / max(tot[mode, c], 1):.3f} (n={tot[mode, c]})" for c in ("copy", "new", "stop")}
            n = sum(tot[mode, c] for c in ("copy", "new", "stop"))
            print(mode, {"all": round(sum(hit[mode, c] for c in ("copy", "new", "stop")) / max(n, 1), 3), **row,
                         "false_copy": round(false_copy[mode] / max(tot[mode, "new"], 1), 3)})
    finally:
        srv.terminate()


if __name__ == "__main__":
    main()
