"""Sessions -> training examples (prompt, completion). The prompt format lives only here; sessions stay structured.

Format (special tokens in <| |>; loss on `completion` only):
  <|cwd|>~/p/foo<|ls|>README.md app.py<|git|>main -> origin/main | ?? x.py<|env|>VIRTUAL_ENV=.venv
  <|hist|>$ git clone ...\n$ cd foo  # exit 1<|err|>stderr of the last command<|nl|>query<|cand|>git checkout main\ngit cherry-pick x<|cmd|>git ch
  -> completion: "eckout main<|end|>".  <|cand|> = up to 3 older history commands starting with the typed prefix (most recent first).

uv run datagen/render.py --sample 25 --shard data/sessions/v1/shard-00000.jsonl.zst
"""
import argparse
import hashlib
import json
import random
import re

SPECIAL = ["<|cwd|>", "<|ls|>", "<|git|>", "<|env|>", "<|hist|>", "<|out|>", "<|err|>", "<|nl|>", "<|cmd|>", "<|end|>", "<|cand|>"]  # append only: ids stay stable
MAX_CANDS, CAND_CHARS = 3, 160
HIST, LS_MAX, GIT_MAX = 6, 30, 8


def clip(s, lines, chars):
    s = "\n".join(s.strip("\n").splitlines()[:lines])
    return s[:chars]


def context(s, i, io=True):
    """Everything before <|cand|>/<|cmd|>. io=False drops the previous command's stdout/stderr (a plain shell cannot supply them)."""
    st = s["steps"][i]
    ls = " ".join(st["ls"][:LS_MAX]) + (" …" if len(st["ls"]) > LS_MAX else "")
    git = ""
    if st["git"]:
        g = st["git"]
        git = f"{g['branch']} -> {g['upstream'] or '-'} | " + "; ".join(g["dirty"][:GIT_MAX])
    env = " ".join(f"{k}={v}" for k, v in st["env"].items())
    hist = []
    for h in s["steps"][max(0, i - HIST):i]:
        hist.append(f"$ {h['cmd']}" + (f"  # exit {h['exit']}" if h["exit"] not in (0,) else ""))
    prev = s["steps"][i - 1] if i else None
    tail = ""
    if not io:
        pass
    elif prev and prev["exit"] not in (0, 130) and prev["stderr"].strip():
        tail = "<|err|>" + clip(prev["stderr"], 6, 320)
    elif prev and prev["stdout"].strip() and len(prev["stdout"]) < 400:
        tail = "<|out|>" + clip(prev["stdout"], 4, 200)
    nl = f"<|nl|>{st['nl']}" if st.get("nl") else ""
    return f"<|cwd|>{st['cwd']}<|ls|>{ls}<|git|>{git}<|env|>{env}\n<|hist|>" + "\n".join(hist) + tail + nl


def prompt(ctx, typed, cands=()):
    return ctx + ("<|cand|>" + "\n".join(cands) if cands else "") + "<|cmd|>" + typed


def history_cands(history, typed):
    """The zsh-autosuggestions part: most recent distinct commands (history is oldest -> newest) that extend the typed prefix."""
    out = []
    for h in reversed(history):
        if typed and h.startswith(typed) and h != typed and len(h) <= CAND_CHARS and h not in out:
            out.append(h)
            if len(out) == MAX_CANDS:
                break
    return out

def cut_points(rng, cmd, n):
    pts, spaces = [], [i for i in range(1, len(cmd)) if cmd[i - 1] == " "]
    for k in range(n):
        if k == 0 and rng.random() < 0.5:
            pts.append(0)
        elif spaces and rng.random() < 0.55:
            pts.append(rng.choice(spaces))
        else:
            pts.append(rng.randrange(0, max(len(cmd), 1)))
    return sorted(set(p for p in pts if p < len(cmd)))


class Bank:
    """Sorted unique commands of the corpus: source of plausible wrong-but-similar history entries (distractors)."""
    def __init__(self, cmds):
        self.xs = sorted(set(c for c in cmds if len(c) <= CAND_CHARS))

    def like(self, rng, typed, k):
        import bisect
        i = bisect.bisect_left(self.xs, typed)
        j = i
        while j < len(self.xs) and j - i < 40 and self.xs[j].startswith(typed):
            j += 1
        pool = [c for c in self.xs[i:j] if c != typed]
        return rng.sample(pool, min(k, len(pool)))


def synth_history(rng, s, i, typed, bank, p_true=0.3, p_near=0.3):
    """Older history for step i: earlier commands of this session beyond the 6-command window, plus a replayed target and/or look-alikes."""
    st = s["steps"][i]
    h = [x["cmd"] for x in s["steps"][:max(0, i - HIST)]]
    extra = []
    if typed and bank is not None:
        if rng.random() < p_near:
            extra += bank.like(rng, typed, rng.choice([1, 1, 2]))
        if rng.random() < p_true and st["cmd"] != typed:
            extra.append(st["cmd"])
        rng.shuffle(extra)
    return h + extra


def render_step(s, i, rng, cuts=2, bank=None, p_io=0.5):
    st = s["steps"][i]
    io = rng.random() < p_io
    ctx = context(s, i, io)
    out = []
    for c in cut_points(rng, st["cmd"], cuts):
        cands = history_cands(synth_history(rng, s, i, st["cmd"][:c], bank), st["cmd"][:c])
        out.append({"prompt": prompt(ctx, st["cmd"][:c], cands), "completion": st["cmd"][c:] + "<|end|>", "class": s["class"], "template_id": s["template_id"],
                    "source": s["source"], "kind": "target" if st["target"] else "other", "cut": c})
    return out


def render_session(s, rng, cuts=2, p_other=0.25, bank=None, p_io=0.5):
    out = []
    for i, st in enumerate(s["steps"]):
        if st["exit"] != 0:
            continue                      # never train on commands that failed
        if st["target"] or rng.random() < p_other:
            out += render_step(s, i, rng, cuts, bank, p_io)
    return out


def canonical(s):
    """Texts used for dedup: context + target at cut 0, one per target step."""
    return [context(s, i) + "<|cmd|>" + st["cmd"] for i, st in enumerate(s["steps"]) if st["target"] and st["exit"] == 0]


def digest(text):
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big")


def tokenizer():
    from tokenizers import Tokenizer
    t = Tokenizer.from_pretrained("HuggingFaceTB/SmolLM2-135M")
    t.add_special_tokens(SPECIAL)
    return t


def read_shard(path):
    import io
    import zstandard
    with open(path, "rb") as f, zstandard.ZstdDecompressor().stream_reader(f) as r:
        for line in io.TextIOWrapper(r, encoding="utf-8"):
            yield json.loads(line)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", required=True)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    exs = [e for s in read_shard(a.shard) for e in render_session(s, rng)]
    tok = tokenizer()
    for e in rng.sample(exs, min(a.sample, len(exs))):
        n = len(tok.encode(e["prompt"] + e["completion"]).ids)
        print(f"----- [{e['class']}] {e['template_id']} ({e['kind']}, {n} tokens)\n{e['prompt']}│{e['completion']}")
