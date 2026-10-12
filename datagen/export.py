"""Render a session corpus into fine-tuning files (split by template_id) and count exact SmolLM2 tokens.

uv run datagen/export.py --version 4 [--cuts 2] [--p-other 0.25]
-> data/train/v4/{train,val}-XXXXX.jsonl.zst  with {"prompt", "completion", "class", "template_id", "source", "kind"}
   loss goes on `completion` only; the prompt already ends with the typed prefix after <|cmd|>.
"""
import argparse
import json
import pathlib
import random

import zstandard

import gates
import render

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Out:
    def __init__(self, d, name, limit=200 * 2**20):
        self.d, self.name, self.limit, self.n, self.f, self.w, self.files = d, name, limit, 0, None, None, []

    def write(self, line):
        if self.f is None or self.f.tell() >= self.limit:
            self.close()
            p = self.d / f"{self.name}-{self.n:05d}.jsonl.zst"
            self.n += 1
            self.files.append(p.name)
            self.f = open(p, "wb")
            self.w = zstandard.ZstdCompressor(level=6).stream_writer(self.f)
        self.w.write(line.encode())

    def close(self):
        if self.w:
            self.w.close(); self.f.close(); self.w = self.f = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", type=int, required=True)
    ap.add_argument("--cuts", type=int, default=2)
    ap.add_argument("--p-other", type=float, default=0.25)
    ap.add_argument("--p-io", type=float, default=0.5, help="share of examples that keep the previous command's stdout/stderr in the prompt")
    a = ap.parse_args()
    src = ROOT / "data" / "sessions" / f"v{a.version}"
    dst = ROOT / "data" / "train" / f"v{a.version}"
    dst.mkdir(parents=True, exist_ok=True)
    tok = render.tokenizer()
    bank = render.Bank(st["cmd"] for sh in sorted(src.glob("shard-*.jsonl.zst")) for s in render.read_shard(sh) for st in s["steps"] if st["exit"] == 0)
    outs = {"train": Out(dst, "train"), "val": Out(dst, "val")}
    stat = {sp: {"examples": 0, "tokens": 0, "completion_tokens": 0} for sp in outs}
    by_class = {}
    buf = {"train": [], "val": []}

    def flush(sp):
        if not buf[sp]:
            return
        enc = tok.encode_batch([e["prompt"] + e["completion"] for e in buf[sp]])
        cmp = tok.encode_batch([e["completion"] for e in buf[sp]])
        for e, x, c in zip(buf[sp], enc, cmp):
            n = len(x.ids)
            stat[sp]["examples"] += 1; stat[sp]["tokens"] += n; stat[sp]["completion_tokens"] += len(c.ids)
            k = by_class.setdefault(e["class"], [0, 0]); k[0] += 1; k[1] += n
            outs[sp].write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
        buf[sp] = []

    for sh in sorted(src.glob("shard-*.jsonl.zst")):
        for s in render.read_shard(sh):
            sp = gates.split_of(s["template_id"], s["class"])
            rng = random.Random(s["id"])
            buf[sp].extend(render.render_session(s, rng, cuts=a.cuts, p_other=a.p_other, bank=bank, p_io=a.p_io))
            if len(buf[sp]) >= 20000:
                flush(sp)
    for sp in outs:
        flush(sp); outs[sp].close()
    rep = {"version": a.version, "cuts": a.cuts, "p_other": a.p_other, "p_io": a.p_io, **{sp: stat[sp] for sp in stat},
           "total_tokens": sum(stat[s]["tokens"] for s in stat), "total_examples": sum(stat[s]["examples"] for s in stat),
           "per_class": {c: {"examples": v[0], "tokens": v[1]} for c, v in by_class.items()}, "files": {sp: outs[sp].files for sp in outs}}
    (dst / "export.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps({k: v for k, v in rep.items() if k != "files"}, indent=1))


if __name__ == "__main__":
    main()
