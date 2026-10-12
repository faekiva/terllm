"""Generate a corpus version: sessions -> dedup -> zstd JSONL shards. Gates/manifest: gates.py.

uv run datagen/build.py --version 1 --sessions 2000 [--workers 7] [--seed 1] [--cap 0.005]
"""
import argparse
import collections
import hashlib
import io
import json
import multiprocessing as mp
import pathlib
import random

import zstandard
from datasketch import MinHash, MinHashLSH

import quality
import render
import sessions as S
import t2c

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLASS_SHARE = {"created_use": 0.14, "chained": 0.13, "error_fix": 0.22, "state_action": 0.15, "routine": 0.13, "nav_noise": 0.07, "t2c": 0.16}
T2C_SPLIT = {"seed": 0.35, "tldr": 0.40, "nl2bash": 0.25}
POPULAR = set("""git git-commit git-push git-pull git-checkout git-branch git-log git-diff git-stash git-merge git-rebase git-clone git-add git-status git-reset
ls cd cp mv rm mkdir touch cat head tail less find grep tar zip unzip curl wget ssh scp rsync docker docker-compose kubectl npm pip python node cargo make cmake gcc
chmod chown sort uniq wc sed awk xargs ps kill top df du systemctl journalctl apt tmux vim nano echo export source ln date diff tee""".split())
_cache = {}


def lazy(name):
    if name not in _cache:
        if "valid" not in _cache:
            _cache["valid"] = t2c.valid_commands()
        ok_tldr, ok_nl = _cache["valid"]
        _cache[name] = {"seed": S.load_templates, "tldr": lambda: _group([e for e in t2c.tldr_examples() if e[2] in ok_tldr], lambda e: e[0]),
                        "nl2bash": lambda: _nl2bash_groups(ok_nl)}[name]()
    return _cache[name]


def _group(items, key):
    d = collections.defaultdict(list)
    for it in items:
        d[key(it)].append(it)
    return d


def _nl2bash_groups(ok):
    by_prog, out = collections.defaultdict(list), {}
    for p in t2c.nl2bash_pairs():
        if p[1] not in ok:
            continue
        w = p[1].split()
        by_prog[__import__("re").sub(r"\W", "_", w[0].lstrip("(")) if w else "x"].append(p)
    for prog, ps in by_prog.items():
        for k in range(0, len(ps), 25):
            out[f"nl2bash.{prog}.{k // 25}"] = [(p, k + j) for j, p in enumerate(ps[k:k + 25])]
    return out


def sig(s):
    toks = " ".join(render.canonical(s)).split()
    m = MinHash(num_perm=64)
    for i in range(max(len(toks) - 2, 1)):
        m.update(" ".join(toks[i:i + 3]).encode())
    return m


def work(task):
    kind, tid, quota, seed = task
    reg = lazy(kind)
    lsh, seen, out, attempts = MinHashLSH(threshold=0.88, num_perm=64), set(), [], 0
    while len(out) < quota and attempts < quota * 8:
        attempts += 1
        sd = seed * 100003 + attempts
        try:
            if kind == "seed":
                s, _ = S.make(reg[tid], sd, tries=2)
            elif kind == "tldr":
                ex = reg[tid][random.Random(sd).randrange(len(reg[tid]))]
                s = t2c.tldr_session(ex, sd)
            else:
                pair, j = reg[tid][random.Random(sd).randrange(len(reg[tid]))]
                s = t2c.nl2bash_session(pair, j, sd)
        except S.Discard:
            continue
        if s is None:
            continue
        canon = render.canonical(s)
        if not canon or quality.session_has_secret(s):
            continue
        if kind != "seed" and any(quality.unknown_flags(st["cmd"]) for st in s["steps"] if st["target"]):
            continue
        hs = [render.digest(c) for c in canon]
        if any(h in seen for h in hs):
            continue
        m = sig(s)
        if lsh.query(m):
            continue
        lsh.insert(f"{len(out)}", m)
        seen.update(hs)
        out.append(s)
    return tid, kind, quota, out


def profile(task):
    """(template_id, avg target steps per session, Counter program -> avg occurrences per session) from a few sample sessions."""
    kind, tid, _, seed = task
    if kind != "seed":
        prog = tid.split("-")[0] if kind == "tldr" else tid.split(".")[1]
        return tid, 1.0, collections.Counter({prog: 1.0})
    _, _, _, sess = work((kind, tid, 6, seed + 7))
    c, m = collections.Counter(), 0
    for s in sess:
        for st in s["steps"]:
            if st["target"] and st["exit"] == 0:
                c[quality.program_of(st["cmd"])] += 1; m += 1
    k = max(len(sess), 1)
    return tid, m / k, collections.Counter({p: v / k for p, v in c.items()})


def fit_quotas(tasks, cls_of, cap_n, target_cap, workers):
    """Scale down templates dominated by an over-represented program (git!) and give the freed quota to other templates of the class."""
    with mp.Pool(workers) as pool:
        prof = {tid: (m, c) for tid, m, c in pool.map(profile, tasks)}
    q = {t[1]: t[2] for t in tasks}
    for _ in range(60):
        total = sum(q[t] * max(prof[t][0], 1e-9) for t in q)
        shares = collections.Counter()
        for t in q:
            for p, v in prof[t][1].items():
                shares[p] += q[t] * v / total
        if not shares:
            break
        p, s = shares.most_common(1)[0]
        if s <= target_cap:
            break
        freed = collections.Counter()
        for t in q:
            frac = prof[t][1].get(p, 0) / max(prof[t][0], 1e-9)
            if frac >= s and q[t] > 1:
                new = max(1, int(q[t] * 0.85))
                freed[cls_of[t]] += q[t] - new
                q[t] = new
        for cls, f in freed.items():
            cands = sorted((t for t in q if cls_of[t] == cls and prof[t][1].get(p, 0) / max(prof[t][0], 1e-9) < 0.1 and q[t] < cap_n), key=lambda t: q[t])
            i = 0
            while f > 0 and cands:
                t = cands[i % len(cands)]
                if q[t] < cap_n:
                    q[t] += 1; f -= 1
                i += 1
                if i > 50 * len(cands):
                    break
    return [(k, t, q[t], sd) for k, t, _, sd in tasks], {p: round(v, 4) for p, v in shares.most_common(6)}


def plan(n, cap, seed, per_template=0):
    """-> list of tasks (kind, template_id, quota, seed) and the plan summary."""
    cap_n = per_template or max(1, int(n * cap))
    rng = random.Random(seed)
    seeds = S.load_templates()
    by_class = collections.defaultdict(list)
    for tid, spec in seeds.items():
        by_class[spec["class"]].append(tid)
    tasks, summary = [], {}
    for cls, share in CLASS_SHARE.items():
        total = round(n * share)
        if cls == "t2c":
            parts = {k: round(total * v) for k, v in T2C_SPLIT.items()}
        else:
            parts = {"seed": total}
        for src, tot in parts.items():
            if src == "seed":
                ids = sorted(by_class[cls])
                qs = {t: max(1, min(cap_n, tot // max(len(ids), 1))) for t in ids}
                extra = tot - sum(qs.values()) if len(ids) * cap_n > tot else 0
                for i, t in enumerate(ids):
                    if i < extra and qs[t] + 1 <= cap_n:
                        qs[t] += 1
            else:
                reg = lazy(src)
                ids = sorted(reg)
                reuse = 4 if src == "tldr" else 2      # sessions per underlying example/pair: more is just the same command again
                w = {t: (6 if (t.split("-")[0] in POPULAR or t in POPULAR) else 1) if src == "tldr" else 1 for t in ids}
                if sum(reuse * len(reg[t]) for t in ids) < tot * 1.0:
                    pass
                sw = sum(w.values())
                qs = {t: max(1, min(cap_n, reuse * len(reg[t]), round(tot * w[t] / sw))) for t in ids}
                if src == "nl2bash" and tot < sum(qs.values()):          # small runs: sample buckets
                    ids = rng.sample(ids, max(1, min(len(ids), tot // 3)))
                    qs = {t: max(1, min(cap_n, tot // len(ids))) for t in ids}
            for t in ids:
                tasks.append((src, t, qs[t], rng.randrange(10**8)))
            summary[f"{cls}/{src}"] = {"templates": len(ids), "quota": sum(qs[t] for t in ids)}
    return tasks, summary


class ShardWriter:
    def __init__(self, d, limit=100 * 2**20, prefix="shard"):
        self.prefix = prefix
        self.d, self.limit, self.n, self.f, self.w, self.paths = d, limit, 0, None, None, []
        d.mkdir(parents=True, exist_ok=True)

    def write(self, obj):
        if self.f is None or self.f.tell() >= self.limit:
            self.close()
            p = self.d / f"{self.prefix}-{self.n:05d}.jsonl.zst"
            self.n += 1
            self.paths.append(p.name)
            self.f = open(p, "wb")
            self.w = zstandard.ZstdCompressor(level=6).stream_writer(self.f)
        self.w.write((json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode())

    def close(self):
        if self.w:
            self.w.close(); self.f.close(); self.w = self.f = None


def llm_sessions(limit, per_template, seed):
    """Pre-built (datagen/llm_sessions.py) sessions: already executed in the simulator, so only filtered and capped here."""
    p = ROOT / "data" / "raw" / "llm" / "sessions.jsonl.zst"
    if not limit or not p.exists():
        return []
    by_t, rng, out = collections.defaultdict(list), random.Random(seed), []
    for s in render.read_shard(p):
        git = sum(st["cmd"].startswith("git ") for st in s["steps"]) / len(s["steps"])
        if git <= 0.34 and not quality.session_has_secret(s) and not any(quality.unknown_flags(st["cmd"]) for st in s["steps"]):   # LLMs love git: keep the program cap
            by_t[s["template_id"]].append(s)
    for ss in by_t.values():
        rng.shuffle(ss)
        out += ss[:per_template]
    rng.shuffle(out)
    return out[:limit]


def append_llm(out_dir, a):
    """Add LLM sessions to an existing version as shard-llm-*.jsonl.zst (exact-dedup against what is already there)."""
    gseen = set()
    for sh in sorted(out_dir.glob("shard-*.jsonl.zst")):
        if "llm" not in sh.name:
            gseen.update(render.digest(c) for s in render.read_shard(sh) for c in render.canonical(s))
    for old in out_dir.glob("shard-llm-*"):
        old.unlink()
    wr = ShardWriter(out_dir, prefix="shard-llm")
    kept = collections.Counter()
    for s in llm_sessions(a.append_llm, a.llm_per_template, a.seed):
        hs = [render.digest(c) for c in render.canonical(s)]
        if not hs or any(h in gseen for h in hs):
            continue
        gseen.update(hs)
        wr.write(s)
        kept[s["class"]] += 1
    wr.close()
    print(f"appended {sum(kept.values())} LLM sessions {dict(kept)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", type=int, required=True)
    ap.add_argument("--sessions", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=7)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--cap", type=float, default=0.005)
    ap.add_argument("--llm", type=int, default=0, help="extra sessions from data/raw/llm/sessions.jsonl.zst (LLM-proposed, simulator-executed)")
    ap.add_argument("--append-llm", type=int, default=0, help="only append up to N LLM sessions to an already built version")
    ap.add_argument("--llm-per-template", type=int, default=6)
    ap.add_argument("--per-template", type=int, default=0, help="fixed session quota for every seed template (overrides the class-share planner)")
    a = ap.parse_args()
    if a.append_llm:
        append_llm(ROOT / "data" / "sessions" / f"v{a.version}", a)
        return
    tasks, summary = plan(a.sessions, a.cap, a.seed, a.per_template)
    seeds = S.load_templates()
    cls_of = {t[1]: (seeds[t[1]]["class"] if t[0] == "seed" else "t2c") for t in tasks}
    tasks, top_shares = fit_quotas(tasks, cls_of, a.per_template or max(1, int(a.sessions * a.cap)), 0.12, a.workers)
    print("program shares after quota fitting (est.):", top_shares)
    out_dir = ROOT / "data" / "sessions" / f"v{a.version}"
    for old in out_dir.glob("shard-*"):
        old.unlink()
    wr, gseen, kept, short = ShardWriter(out_dir), set(), collections.Counter(), collections.Counter()
    with mp.Pool(a.workers) as pool:
        for tid, kind, quota, sess in pool.imap_unordered(work, tasks, chunksize=1):
            n = 0
            for s in sess:
                hs = [render.digest(c) for c in render.canonical(s)]
                if any(h in gseen for h in hs):
                    continue
                gseen.update(hs)
                wr.write(s); n += 1
                kept[s["class"]] += 1
            if n < quota:
                short[tid] = quota - n
    for s_ in llm_sessions(a.llm, a.llm_per_template, a.seed):
        hs = [render.digest(c) for c in render.canonical(s_)]
        if not hs or any(h in gseen for h in hs):
            continue
        gseen.update(hs)
        wr.write(s_)
        kept[s_["class"]] += 1
        kept["llm"] += 1
    wr.close()
    print(f"{sum(kept.values()) - kept['llm']} sessions in {len(wr.paths)} shard(s); per class {dict(kept)}")
    print(f"templates short of quota: {len(short)} (missing {sum(short.values())} sessions)")
    (out_dir / "plan.json").write_text(json.dumps({"plan": summary, "shards": wr.paths, "short": short}, indent=1))


if __name__ == "__main__":
    main()
