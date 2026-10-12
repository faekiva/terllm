"""Sessions -> training examples (prompt, completion). The prompt format lives only here; sessions stay structured.

Format (special tokens in <| |>; loss on `completion` only):
  <|cwd|>~/p/foo<|ls|>README.md app.py<|git|>main -> origin/main | ?? x.py<|env|>VIRTUAL_ENV=.venv
  <|hist|>$ git clone ...\n$ cd foo  # exit 1<|err|>stderr of the last command<|nl|>query<|cand|>git checkout main\ngit cherry-pick x<|fs|>src/ scripts/<|cmd|>git ch
  -> completion: "eckout main<|end|>".  <|cand|> = up to 3 older history commands starting with the typed prefix (most recent first).
  <|fs|> = what tab completion offers for the word being typed (<=8 files/dirs/commands, fish-style); the model decides whether to copy one, write a new name or stop.

uv run datagen/render.py --sample 25 --shard data/sessions/v1/shard-00000.jsonl.zst
"""
import argparse
import hashlib
import json
import pathlib
import posixpath
import random
import re

SPECIAL = ["<|cwd|>", "<|ls|>", "<|git|>", "<|env|>", "<|hist|>", "<|out|>", "<|err|>", "<|nl|>", "<|cmd|>", "<|end|>", "<|cand|>", "<|fs|>"]  # append only: ids stay stable
MAX_CANDS, CAND_CHARS = 3, 160
HIST, LS_MAX, GIT_MAX = 6, 30, 8
FS_SHOW, FS_MATCHES = 8, 20              # candidates shown / more matches than this = the list says nothing, show none
NOCOMP = set("\"'$*?`=|&;<>()\\{}[]!# \t")   # a word with one of these is never completed (quoting, globs, expansions)
CMD_AFTER = {"|", "&&", "||", ";", "sudo", "time", "nohup", "env", "xargs"}   # a command name is expected after these
DIRS_ONLY = {"cd", "pushd", "rmdir"}
CORE_BINS = set("""cd ls pwd mkdir touch rm rmdir mv cp cat echo printf head tail wc grep find chmod ln export source clear history which tar unzip zip gzip wget curl git
python python3 pip pip3 node npm npx make cmake gcc g++ cargo bash sh zsh less file stat date whoami env ps df du sudo apt docker docker-compose kubectl ssh kill top tee sort
uniq diff sed awk xargs journalctl scp pnpm yarn vim nano pytest black ruff mypy""".split())


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


def prompt(ctx, typed, cands=(), fs=()):
    return ctx + ("<|cand|>" + "\n".join(cands) if cands else "") + ("<|fs|>" + " ".join(fs) if fs else "") + "<|cmd|>" + typed


def history_cands(history, typed):
    """The zsh-autosuggestions part: most recent distinct commands (history is oldest -> newest) that extend the typed prefix."""
    out = []
    for h in reversed(history):
        if typed and h.startswith(typed) and h != typed and len(h) <= CAND_CHARS and h not in out:
            out.append(h)
            if len(out) == MAX_CANDS:
                break
    return out

def fs_key(cwd, d):
    """Display path (~/x or /x) of directory `d` as typed in a shell whose cwd is `cwd`."""
    if d.startswith(("~", "/")):
        return posixpath.normpath(d)
    return posixpath.normpath(cwd + "/" + d)


def fs_cands(typed, cwd, listdir, bins):
    """What tab completion offers for the last word of `typed`: full word values, <= FS_SHOW, [] when there is nothing to say.
    listdir(key) -> [name | name/ | name*] (dir / executable) or None; bins: sorted command names. Shared by datagen and runtime."""
    words = typed.split(" ")
    w = words[-1]
    if w.startswith("-") or NOCOMP & set(w):
        return []
    prev = words[-2] if len(words) > 1 else None
    cmdpos = prev is None or prev in CMD_AFTER
    base = w.rpartition("/")[2]
    dirpart = w[:len(w) - len(base)]
    if cmdpos and not dirpart:
        out = sorted((b for b in bins if w and b.startswith(w) and b != w), key=lambda b: (b not in CORE_BINS, b))   # common tools first
    else:
        names = listdir(fs_key(cwd, dirpart.rstrip("/") or ("/" if dirpart else "."))) or []
        only_dirs = bool(prev is not None and words[0] in DIRS_ONLY)
        out = []
        for n in names:
            raw = n.rstrip("*")
            if not raw.startswith(base) or (raw.startswith(".") and not base.startswith(".")) or NOCOMP & set(raw.rstrip("/")):
                continue
            if (only_dirs and not n.endswith("/")) or (cmdpos and not (n.endswith(("/", "*")))):
                continue
            if dirpart + raw != w:
                out.append(dirpart + raw)
    return out[:FS_SHOW] if len(out) <= FS_MATCHES else []


_BINS = []


def load_bins():
    if not _BINS:
        _BINS.extend(pathlib.Path(__file__).with_name("bins.txt").read_text().split())
    return _BINS


def sample_bins(rng, venv=False):
    """A plausible PATH for one session: the core tools always, the rest of bins.txt with probability 0.6 (real machines differ)."""
    xs = [b for b in load_bins() if b in CORE_BINS or rng.random() < 0.6]
    if venv:
        xs += ["pytest", "black", "ruff", "flake8", "mypy", "uvicorn", "gunicorn", "pip", "python"]
    return sorted(set(xs))


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


def arg_cut(rng, cmd):
    """One decision point inside the arguments: right after a space or a slash (is a path/argument needed? which entry?), or inside a path word."""
    pts = [i for i in range(1, len(cmd)) if cmd[i - 1] in " /"]
    pts += [i for i in range(1, len(cmd)) if "/" in cmd[:i].rpartition(" ")[2] + cmd[i:].partition(" ")[0] and cmd[i - 1] not in " /"][::2]
    return rng.choice(pts) if pts else None


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


def render_step(s, i, rng, cuts=2, bank=None, p_io=0.5, bins=(), p_fs=0.85, p_end=0.15):
    st = s["steps"][i]
    io = rng.random() < p_io
    ctx = context(s, i, io)
    pts = cut_points(rng, st["cmd"], cuts)
    if " " in st["cmd"] and rng.random() < 0.5:
        a = arg_cut(rng, st["cmd"])
        pts = sorted(set(pts + [a])) if a else pts
    if rng.random() < p_end:
        pts.append(len(st["cmd"]))                      # the command is complete: the right answer is <|end|>, not an invented argument
    out = []
    for c in pts:
        typed = st["cmd"][:c]
        cands = history_cands(synth_history(rng, s, i, typed, bank), typed)
        fs = fs_cands(typed, st["cwd"], st["fs"].get, bins) if st.get("fs") and rng.random() < p_fs else []
        out.append({"prompt": prompt(ctx, typed, cands, fs), "completion": st["cmd"][c:] + "<|end|>", "class": s["class"], "template_id": s["template_id"],
                    "source": s["source"], "kind": "target" if st["target"] else "other", "cut": c})
    return out


def render_session(s, rng, cuts=2, p_other=0.25, bank=None, p_io=0.5, p_fs=0.85):
    out = []
    bins = sample_bins(rng, venv=any(st["env"] for st in s["steps"]))
    for i, st in enumerate(s["steps"]):
        if st["exit"] != 0:
            continue                      # never train on commands that failed
        if st["target"] or rng.random() < p_other:
            out += render_step(s, i, rng, cuts, bank, p_io, bins, p_fs)
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
