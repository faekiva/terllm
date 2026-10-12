"""Quality gates over a corpus version (streams shards) + manifest.json.

uv run datagen/gates.py --version 1 [--tokenize 4000] [--no-bash-n]
"""
import argparse
import collections
import hashlib
import json
import pathlib
import random
import subprocess

import quality
import render

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE_CAP, PROGRAM_CAP = 0.005, 0.15


def split_of(template_id, cls):
    """Deterministic train/val split by template_id (~5% of templates -> val)."""
    h = int.from_bytes(hashlib.blake2b(template_id.encode(), digest_size=4).digest(), "big")
    return "val" if h % 20 == 0 else "train"


def ngram_stats(cmds):
    out = {}
    for n in (1, 2, 3):
        tot, seen = 0, set()
        for c in cmds:
            w = c.split()
            for i in range(len(w) - n + 1):
                seen.add(tuple(w[i:i + n])); tot += 1
        out[f"distinct_{n}gram"] = {"distinct": len(seen), "total": tot, "ratio": round(len(seen) / max(tot, 1), 4)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", type=int, required=True)
    ap.add_argument("--tokenize", type=int, default=4000)
    ap.add_argument("--no-bash-n", action="store_true")
    a = ap.parse_args()
    d = ROOT / "data" / "sessions" / f"v{a.version}"
    shards = sorted(d.glob("shard-*.jsonl.zst"))
    n = 0
    cls, src, tpl, prog = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
    cls_targets, cls_templates, ids, exact, secrets = collections.Counter(), collections.defaultdict(set), set(), set(), 0
    targets, dup_ids, dup_exact, steps_total, unverified = [], 0, 0, 0, 0
    split_sessions, split_templates = collections.Counter(), collections.defaultdict(set)
    flag_checked = flag_bad = 0
    flag_examples = collections.Counter()
    sample, rng = [], random.Random(0)
    rendered = 0
    for sh in shards:
        for s in render.read_shard(sh):
            n += 1
            cls[s["class"]] += 1; src[s["source"]] += 1; tpl[s["template_id"]] += 1; cls_templates[s["class"]].add(s["template_id"])
            if s["id"] in ids:
                dup_ids += 1
            ids.add(s["id"])
            if quality.session_has_secret(s):
                secrets += 1
            h = tuple(render.digest(c) for c in render.canonical(s))
            if h in exact:
                dup_exact += 1
            exact.add(h)
            sp = split_of(s["template_id"], s["class"])
            split_sessions[sp] += 1; split_templates[sp].add(s["template_id"])
            for st in s["steps"]:
                steps_total += 1
                unverified += not st["v"]
                if st["target"] and st["exit"] == 0:
                    cls_targets[s["class"]] += 1
                    targets.append(st["cmd"])
                    prog[quality.program_of(st["cmd"])] += 1
                    bad = quality.unknown_flags(st["cmd"])
                    if bad is not None:
                        flag_checked += 1
                        if bad:
                            flag_bad += 1
                            flag_examples.update(f"{quality.program_of(st['cmd'])} {b}" for b in bad)
            ex = render.render_session(s, rng)
            rendered += len(ex)
            sample.extend(ex if len(sample) < 20000 else [])
    unique_targets = set(targets)
    bad_syntax = [] if a.no_bash_n else quality.bash_n(sorted(unique_targets))
    top_tpl = tpl.most_common(5)
    tpl_share = top_tpl[0][1] / n if n else 0
    top_prog = prog.most_common(30)
    prog_share = top_prog[0][1] / max(len(targets), 1) if top_prog else 0
    # token estimate from a sample of rendered examples
    tok_info = {}
    if a.tokenize and sample:
        tok = render.tokenizer()
        pick = rng.sample(sample, min(a.tokenize, len(sample)))
        lens = [len(tok.encode(e["prompt"] + e["completion"]).ids) for e in pick]
        avg = sum(lens) / len(lens)
        tok_info = {"sample": len(lens), "avg_tokens_per_example": round(avg, 1), "max_in_sample": max(lens), "examples_rendered": rendered,
                    "estimated_total_tokens": int(avg * rendered)}
    gates = {
        "no_duplicate_ids": dup_ids == 0,
        "no_exact_duplicates": dup_exact == 0,
        f"template_cap<={TEMPLATE_CAP:.1%}": tpl_share <= TEMPLATE_CAP + 1e-9,
        f"program_cap<={PROGRAM_CAP:.0%}": prog_share <= PROGRAM_CAP,
        "no_secrets": secrets == 0,
        "bash_n_clean": len(bad_syntax) == 0 if not a.no_bash_n else None,
        "val_split_by_template": not (split_templates["train"] & split_templates["val"]),
    }
    val_rep = ROOT / "data" / "validate" / "report.json"
    sim = json.loads(val_rep.read_text()) if val_rep.exists() else {}
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    manifest = {
        "version": a.version, "generator_commit": commit, "sessions": n, "shards": [p.name for p in shards],
        "per_class": {c: {"sessions": cls[c], "share": round(cls[c] / n, 4), "target_steps": cls_targets[c], "templates": len(cls_templates[c])} for c in cls},
        "per_source": dict(src), "templates": len(tpl), "top_templates": [(t, c, round(c / n, 4)) for t, c in top_tpl],
        "target_commands": {"total": len(targets), "unique": len(unique_targets), "unique_ratio": round(len(unique_targets) / max(len(targets), 1), 4), **ngram_stats(targets)},
        "top30_programs": [(p, c, round(c / max(len(targets), 1), 4)) for p, c in top_prog], "distinct_programs": len(prog),
        "unverified_steps_share": round(unverified / max(steps_total, 1), 4),
        "flags": {"checked_targets": flag_checked, "with_unknown_flag": flag_bad, "rate": round(flag_bad / max(flag_checked, 1), 4), "top_unknown": flag_examples.most_common(15)},
        "bash_n_failures": bad_syntax[:20], "secret_sessions": secrets, "exact_dup_sessions": dup_exact,
        "split": {"train_sessions": split_sessions["train"], "val_sessions": split_sessions["val"], "train_templates": len(split_templates["train"]),
                  "val_templates": len(split_templates["val"])},
        "tokens": tok_info,
        "simulator_agreement": {"step": sim.get("step_agreement"), "session": sim.get("session_agreement"), "sessions_replayed": sim.get("sessions"),
                                "opaque_steps_excluded": sim.get("excluded_opaque_steps")},
        "gates": gates, "gates_passed": all(v for v in gates.values() if v is not None),
    }
    (d / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    print(json.dumps({k: manifest[k] for k in ("sessions", "per_class", "target_commands", "unverified_steps_share", "tokens", "gates", "gates_passed")}, indent=1))
    print("top templates:", manifest["top_templates"])
    print("top-30 programs:", [(p, round(s * 100, 1)) for p, _, s in manifest["top30_programs"]])
    print("flags:", manifest["flags"])
    print("bash -n failures:", bad_syntax[:10])


if __name__ == "__main__":
    main()
