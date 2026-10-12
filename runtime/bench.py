"""Exact-match + latency of a GGUF model served by llama-server on held-out val examples.

uv run runtime/bench.py --model models/gguf/terllm-v1-q8_0.gguf [--n 400] [--threads 4]
Latency is measured the way the plugin works: the context is sent once, then the typed prefix grows (prompt cache reuse).
"""
import argparse
import json
import pathlib
import random
import re
import statistics as st
import subprocess
import sys
import time
import urllib.request

import zstandard

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "vendor/llama.cpp/build/bin/llama-server"


def post(port, body):
    r = urllib.request.Request(f"http://127.0.0.1:{port}/completion", json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=30))


def examples(version, n, seed=0):
    out = []
    for f in sorted((ROOT / "data/train" / f"v{version}").glob("val-*.jsonl.zst")):
        with open(f, "rb") as fh, zstandard.ZstdDecompressor().stream_reader(fh) as r:
            for i, line in enumerate(r.read().decode().split("\n")):
                if line and i % 7 == 0:
                    out.append(json.loads(line))
    random.Random(seed).shuffle(out)
    return out[:n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--version", type=int, default=6)
    ap.add_argument("--n", type=int, default=400)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--strip", action="store_true", help="drop <|err|>/<|out|> blocks: what a plain zsh without output capture can provide")
    ap.add_argument("--nofs", action="store_true", help="drop the <|fs|> block (what a model without it, e.g. v2, can be fed)")
    a = ap.parse_args()
    port = 8097
    p = subprocess.Popen([str(BIN), "-m", a.model, "--host", "127.0.0.1", "--port", str(port), "-c", "4096", "-t", str(a.threads), "-np", "1", "--no-webui"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(200):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1); break
            except OSError:
                time.sleep(0.1)
        ex = examples(a.version, a.n)
        if a.strip:
            for e in ex:
                e["prompt"] = re.sub(r"<\|(err|out)\|>.*?(?=<\|nl\|>|<\|cmd\|>)", "", e["prompt"], flags=re.S)
        if a.nofs:
            for e in ex:
                e["prompt"] = re.sub(r"<\|fs\|>.*?(?=<\|cmd\|>)", "", e["prompt"], flags=re.S)
        hit, cold, warm, conf_hit, conf_miss = 0, [], [], [], []
        for e in ex:
            want = e["completion"].removesuffix("<|end|>")
            t = time.perf_counter()
            r = post(port, {"prompt": e["prompt"], "n_predict": 48, "temperature": 0, "cache_prompt": True, "n_probs": 1})
            cold.append(time.perf_counter() - t)
            got = r["content"].split("<|end|>")[0]
            probs = [x.get("prob", 2.718 ** x.get("logprob", 0)) for x in r.get("completion_probabilities", [])]
            (conf_hit if got == want else conf_miss).append(min(probs, default=1.0))
            hit += got == want
            # keystroke latency: the same context with one more typed char (prefix is cached)
            t = time.perf_counter()
            post(port, {"prompt": e["prompt"] + (want[:1] or " "), "n_predict": 48, "temperature": 0, "cache_prompt": True})
            warm.append(time.perf_counter() - t)
        q = lambda xs, p: sorted(xs)[int(len(xs) * p)] * 1000
        print(json.dumps({"model": pathlib.Path(a.model).name, "n": len(ex), "exact_match": round(hit / len(ex), 4),
                          "cold_ms_p50_p95": [round(q(cold, .5)), round(q(cold, .95))], "keystroke_ms_p50_p95": [round(q(warm, .5)), round(q(warm, .95))],
                          "min_prob_hit_median": round(st.median(conf_hit), 3), "min_prob_miss_median": round(st.median(conf_miss), 3)}))
    finally:
        p.terminate()


if __name__ == "__main__":
    main()
