"""Fresh `paths` sessions (seeds never used by build.py) rendered with every <|fs|> block kept: a held-out-by-name probe for runtime/fs_eval.py.

uv run datagen/paths_probe.py out.jsonl [--per-template 6]
"""
import argparse
import json
import random

import render
import sessions as S

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("--per-template", type=int, default=6)
a = ap.parse_args()
rng, n = random.Random(12345), 0
with open(a.out, "w") as f:
    for tid, spec in sorted(S.load_templates().items()):
        if spec["class"] != "paths":
            continue
        for k in range(a.per_template):
            s, _ = S.make(spec, 10**7 + k)
            for st in render.render_session(s, rng, cuts=2, p_other=0, p_fs=1.0) if s else []:
                f.write(json.dumps(st) + "\n"); n += 1
print(n, "examples")
