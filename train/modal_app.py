"""Fine-tune SmolLM2-135M on the terllm corpus on Modal.

  uv run modal volume put terllm data/train/v4 /train/v4          # upload rendered train/val (once)
  uv run modal run train/modal_app.py::prep --version 4           # tokenize on CPU -> volume
  uv run modal run train/modal_app.py::fit --version 4 --max-steps 60 --run smoke   # throughput/loss smoke test
  uv run modal run --detach train/modal_app.py::fit --version 4 --run v4            # the real run
  uv run modal volume get terllm /out/v4 models/                  # download the checkpoint

Loss is on the completion only; the prompt is tokenized separately from the completion so the boundary matches inference
(the model sees the typed prefix as its own tokens and continues it).
"""
import modal

app = modal.App("terllm-train")
vol = modal.Volume.from_name("terllm", create_if_missing=True)
image = (modal.Image.debian_slim(python_version="3.12")
         .pip_install("torch==2.8.0", "transformers==4.57.3", "tokenizers", "zstandard", "numpy", "huggingface_hub"))
BASE = "HuggingFaceTB/SmolLM2-135M"
SPECIAL = ["<|cwd|>", "<|ls|>", "<|git|>", "<|env|>", "<|hist|>", "<|out|>", "<|err|>", "<|nl|>", "<|cmd|>", "<|end|>"]  # keep in sync with datagen/render.py
CLASSES = ["created_use", "chained", "error_fix", "state_action", "routine", "nav_noise", "t2c"]


def get_tokenizer():
    from tokenizers import Tokenizer
    t = Tokenizer.from_pretrained(BASE)
    t.add_special_tokens(SPECIAL)
    return t


def read_zst(path):
    import io
    import json
    import zstandard
    with open(path, "rb") as f, zstandard.ZstdDecompressor().stream_reader(f) as r:
        for line in io.TextIOWrapper(r, encoding="utf-8"):
            yield json.loads(line)


@app.function(image=image, cpu=8, memory=16384, volumes={"/data": vol}, timeout=3600)
def prep(version: int = 4):
    """jsonl.zst -> flat uint16 token array + (prompt_len, completion_len, class, kind) per example."""
    import glob
    import json
    import numpy as np
    tok = get_tokenizer()
    for split in ("train", "val"):
        files = sorted(glob.glob(f"/data/train/v{version}/{split}-*.jsonl.zst"))
        ids, lens, cls, kind, n = [], [], [], [], 0
        buf = []

        def flush():
            nonlocal n
            if not buf:
                return
            p = tok.encode_batch([b["prompt"] for b in buf])
            c = tok.encode_batch([b["completion"] for b in buf])
            for b, x, y in zip(buf, p, c):
                ids.append(np.array(x.ids + y.ids, dtype=np.uint16)); lens.append((len(x.ids), len(y.ids)))
                cls.append(CLASSES.index(b["class"])); kind.append(b["kind"] == "target")
            n += len(buf); buf.clear()

        for f in files:
            for ex in read_zst(f):
                buf.append(ex)
                if len(buf) >= 50000:
                    flush()
                    print(split, n, flush=True)
        flush()
        out = f"/data/prep/v{version}"
        import os
        os.makedirs(out, exist_ok=True)
        flat = np.concatenate(ids)
        flat.tofile(f"{out}/{split}.tokens.u16")
        np.save(f"{out}/{split}.lens.npy", np.array(lens, dtype=np.uint16))
        np.save(f"{out}/{split}.cls.npy", np.array(cls, dtype=np.uint8))
        np.save(f"{out}/{split}.kind.npy", np.array(kind, dtype=bool))
        json.dump({"examples": n, "tokens": int(flat.size), "special_ids": {s: tok.token_to_id(s) for s in SPECIAL}}, open(f"{out}/{split}.json", "w"))
        print(split, n, "examples", flat.size, "tokens", flush=True)
    vol.commit()


class Data:
    def __init__(self, d, split):
        import numpy as np
        self.tokens = np.memmap(f"{d}/{split}.tokens.u16", dtype=np.uint16, mode="r")
        self.lens = np.load(f"{d}/{split}.lens.npy").astype(np.int64)
        self.cls = np.load(f"{d}/{split}.cls.npy"); self.kind = np.load(f"{d}/{split}.kind.npy")
        self.total = self.lens.sum(1)
        self.start = np.concatenate([[0], np.cumsum(self.total)[:-1]])

    def batch(self, idx, pad_id, device):
        import numpy as np
        import torch
        T = int(self.total[idx].max())
        x = np.full((len(idx), T), pad_id, dtype=np.int64)
        tm = np.zeros((len(idx), T), dtype=bool)          # target (completion) positions
        am = np.zeros((len(idx), T), dtype=bool)
        for r, i in enumerate(idx):
            n = int(self.total[i]); p = int(self.lens[i, 0])
            x[r, :n] = self.tokens[self.start[i]:self.start[i] + n]
            tm[r, p:n] = True; am[r, :n] = True
        f = lambda a: torch.from_numpy(a).to(device, non_blocking=True)
        return f(x), f(am), f(tm)


def completion_loss(model, x, am, tm):
    """Summed CE over completion tokens; lm_head is applied only at those positions (cheap)."""
    import torch
    import torch.nn.functional as F
    h = model.model(input_ids=x, attention_mask=am).last_hidden_state
    sel = h[:, :-1][tm[:, 1:]]
    y = x[:, 1:][tm[:, 1:]]
    logits = model.lm_head(sel).float()
    return F.cross_entropy(logits, y, reduction="sum"), y.numel()


def batches(data, order, bs, rng, bucket=64):
    """Length-bucketed batches: sort inside chunks of bs*bucket examples, then shuffle the batches."""
    import numpy as np
    out = []
    for s in range(0, len(order), bs * bucket):
        ch = order[s:s + bs * bucket]
        ch = ch[np.argsort(data.total[ch], kind="stable")]
        out += [ch[i:i + bs] for i in range(0, len(ch), bs) if len(ch[i:i + bs]) == bs]
    rng.shuffle(out)
    return out


def history_baseline(prompt):
    """zsh-autosuggestions-like: most recent history command starting with the typed prefix (prefix must be non-empty)."""
    head, _, typed = prompt.rpartition("<|cmd|>")
    hist = head.split("<|hist|>")[-1]
    hist = hist.split("<|err|>")[0].split("<|out|>")[0].split("<|nl|>")[0]
    cmds = [l[2:].split("  # exit")[0] for l in hist.split("\n") if l.startswith("$ ")]
    if not typed:
        return ""
    for c in reversed(cmds):
        if c.startswith(typed) and c != typed:
            return c[len(typed):]
    return ""


@app.function(image=image, gpu="A100-40GB", volumes={"/data": vol}, timeout=3 * 3600, memory=32768)
def fit(version: int = 4, run: str = "v4", lr: float = 3e-4, batch: int = 1024, micro: int = 256, epochs: float = 1.0, max_steps: int = 0,
        max_minutes: int = 150, eval_every: int = 100, em_every: int = 300, warmup: int = 200, subset: int = 0):
    import json
    import math
    import time
    import numpy as np
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch.backends.cuda.matmul.allow_tf32 = True
    dev = "cuda"
    d = f"/data/prep/v{version}"
    tr, va = Data(d, "train"), Data(d, "val")
    htok = AutoTokenizer.from_pretrained(BASE)
    htok.add_special_tokens({"additional_special_tokens": SPECIAL})
    ttok = get_tokenizer()
    assert all(htok.convert_tokens_to_ids(s) == ttok.token_to_id(s) for s in SPECIAL), "tokenizer id mismatch"
    end_id, pad_id = ttok.token_to_id("<|end|>"), htok.eos_token_id
    model = AutoModelForCausalLM.from_pretrained(BASE, dtype=torch.float32, attn_implementation="sdpa")
    model.resize_token_embeddings(len(htok), pad_to_multiple_of=64)
    model.to(dev).train()
    N = len(tr.lens) if not subset else min(subset, len(tr.lens))
    rng = np.random.default_rng(0)
    steps_per_epoch = N // batch
    total = int(steps_per_epoch * epochs) if not max_steps else max_steps
    opt = torch.optim.AdamW(model.parameters(), lr=lr, betas=(0.9, 0.95), weight_decay=0.01, fused=True)
    sched = lambda s: lr * (s + 1) / warmup if s < warmup else lr * (0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * (s - warmup) / max(total - warmup, 1))))
    out = f"/data/out/{run}"
    import os
    os.makedirs(out, exist_ok=True)
    log = open(f"{out}/metrics.jsonl", "a")
    veval = rng.choice(len(va.lens), size=min(20000, len(va.lens)), replace=False)
    vtarget = veval[va.kind[veval]][:600]

    def val_loss():
        model.eval(); s = n = 0
        with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
            for i in range(0, len(veval), micro):
                x, am, tm = va.batch(veval[i:i + micro], pad_id, dev)
                l, k = completion_loss(model, x, am, tm); s += l.item(); n += k
        model.train()
        return s / n

    def exact_match(n_ex=600):
        """Greedy decode from the typed prefix; compares with the gold completion (target steps of unseen templates)."""
        model.eval(); htok.padding_side = "left"; ok = base_ok = 0; per = {}
        sel = vtarget[:n_ex]
        raw_prompts = []
        for i in sel:
            ids = np.asarray(va.tokens[va.start[i]:va.start[i] + va.lens[i, 0]], dtype=np.int64)
            gold = np.asarray(va.tokens[va.start[i] + va.lens[i, 0]:va.start[i] + va.total[i]], dtype=np.int64)
            raw_prompts.append((ids, gold, int(va.cls[i])))
        with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
            for b in range(0, len(raw_prompts), 128):
                chunk = raw_prompts[b:b + 128]
                T = max(len(p[0]) for p in chunk)
                x = torch.full((len(chunk), T), pad_id, dtype=torch.long); am = torch.zeros((len(chunk), T), dtype=torch.long)
                for r, (p, _, _) in enumerate(chunk):
                    x[r, T - len(p):] = torch.from_numpy(p); am[r, T - len(p):] = 1
                g = model.generate(input_ids=x.to(dev), attention_mask=am.to(dev), max_new_tokens=48, do_sample=False, eos_token_id=end_id, pad_token_id=pad_id)
                for r, (p, gold, c) in enumerate(chunk):
                    pred = g[r, T:].tolist()
                    pred = pred[:pred.index(end_id) + 1] if end_id in pred else pred
                    hit = pred == gold.tolist()
                    ok += hit; k = per.setdefault(CLASSES[c], [0, 0]); k[0] += hit; k[1] += 1
                    ptxt = htok.decode(p.tolist()); base = history_baseline(ptxt)
                    base_ok += (base + "<|end|>") == htok.decode(gold.tolist()) if base else 0
        model.train()
        return ok / len(raw_prompts), base_ok / len(raw_prompts), {c: round(v[0] / v[1], 3) for c, v in per.items()}

    t0, step, seen_tokens, order_epoch, best = time.time(), 0, 0, 0, float('inf')
    print(f"examples {N} steps {total} (bs {batch}) params {sum(p.numel() for p in model.parameters())/1e6:.1f}M", flush=True)
    while step < total:
        order = rng.permutation(N)
        for bidx in batches(tr, order, batch, rng, bucket=16):
            if step >= total or (time.time() - t0) / 60 > max_minutes:
                total = step
                break
            for g in opt.param_groups:
                g["lr"] = sched(step)
            tot_tokens = int(sum(tr.lens[bidx, 1]))
            loss_acc = 0.0
            for m in range(0, batch, micro):
                x, am, tm = tr.batch(bidx[m:m + micro], pad_id, dev)
                with torch.autocast("cuda", dtype=torch.bfloat16):
                    l, k = completion_loss(model, x, am, tm)
                (l / tot_tokens).backward(); loss_acc += l.item()
                seen_tokens += int(am.sum())
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step(); opt.zero_grad(set_to_none=True); step += 1
            if step % 50 == 0 or step == 1:
                el = time.time() - t0
                rec = {"step": step, "loss": loss_acc / tot_tokens, "lr": sched(step), "tok_s": int(seen_tokens / el), "min": round(el / 60, 1)}
                print(rec, flush=True); log.write(json.dumps(rec) + "\n"); log.flush()
            if step % 1000 == 0:                      # fallback checkpoint (weights only) in case the container dies
                model.save_pretrained(f"{out}/ckpt-latest", safe_serialization=True); vol.commit()
            if step % eval_every == 0:
                vl = val_loss()
                rec = {"step": step, "val_loss": vl}
                if step % em_every == 0:
                    em, bem, per = exact_match(300)
                    rec.update({"val_em": em, "history_baseline_em": bem, "val_em_by_class": per})
                print(rec, flush=True); log.write(json.dumps(rec) + "\n"); log.flush()
                if vl < best:                         # keep the best checkpoint by val loss (overfitting shows up as a rising val loss)
                    best = vl
                    model.save_pretrained(f"{out}/best", safe_serialization=True); htok.save_pretrained(f"{out}/best"); vol.commit()
                    print({"best_step": step, "best_val_loss": vl}, flush=True)
    vl = val_loss(); em, bem, per = exact_match(600)
    final = {"step": step, "final": True, "val_loss": vl, "val_em": em, "history_baseline_em": bem, "val_em_by_class": per, "train_tokens_seen": seen_tokens,
             "minutes": round((time.time() - t0) / 60, 1)}
    print(final, flush=True); log.write(json.dumps(final) + "\n"); log.close()
    model.to(torch.bfloat16).save_pretrained(out, safe_serialization=True)
    htok.save_pretrained(out)
    json.dump(final, open(f"{out}/final.json", "w"), indent=1)
    vol.commit()
    return final


@app.local_entrypoint()
def main():
    print("use ::prep and ::fit (see the module docstring)")


@app.function(image=image, cpu=8, memory=12288, volumes={"/data": vol}, timeout=1800)
def analyze(version: int = 4, run: str = "v4", ckpt: str = "ckpt-latest", n: int = 300):
    """Error analysis on CPU: where does the model miss on val targets (empty vs typed prefix, names vs logic)."""
    import numpy as np
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    vol.reload()
    d = f"/data/prep/v{version}"
    va = Data(d, "val")
    htok = AutoTokenizer.from_pretrained(BASE)
    htok.add_special_tokens({"additional_special_tokens": SPECIAL})
    end_id, pad_id = htok.convert_tokens_to_ids("<|end|>"), htok.eos_token_id
    model = AutoModelForCausalLM.from_pretrained(f"/data/out/{run}/{ckpt}", dtype=torch.float32).eval()
    rng = np.random.default_rng(0)
    veval = rng.choice(len(va.lens), size=min(20000, len(va.lens)), replace=False)
    sel = veval[va.kind[veval]][:n]
    rows = []
    for i in sel:
        p = np.asarray(va.tokens[va.start[i]:va.start[i] + va.lens[i, 0]], dtype=np.int64)
        gold = np.asarray(va.tokens[va.start[i] + va.lens[i, 0]:va.start[i] + va.total[i]], dtype=np.int64).tolist()
        with torch.no_grad():
            g = model.generate(input_ids=torch.from_numpy(p)[None], max_new_tokens=48, do_sample=False, eos_token_id=end_id, pad_token_id=pad_id)[0, len(p):].tolist()
        g = g[:g.index(end_id) + 1] if end_id in g else g
        ptxt = htok.decode(p.tolist()); typed = ptxt.rpartition("<|cmd|>")[2]
        rows.append((CLASSES[int(va.cls[i])], typed, htok.decode(gold).replace("<|end|>", ""), htok.decode(g).replace("<|end|>", ""), g == gold, ptxt))
    for name, f in (("empty prefix", lambda r: r[1] == ""), ("typed prefix", lambda r: r[1] != "")):
        sub = [r for r in rows if f(r)]
        print(f"{name}: {sum(r[4] for r in sub)}/{len(sub)} exact", flush=True)
    bad = [r for r in rows if not r[4]]
    print(f"--- {len(bad)} misses (showing 30): [class] typed | gold | pred", flush=True)
    for r in bad[:30]:
        hist = r[5].rpartition("<|hist|>")[2].rpartition("<|cmd|>")[0].strip().replace("\n", " ; ")[-110:]
        print(f"[{r[0]}] hist: …{hist}\n    typed={r[1]!r}\n    gold  ={r[2]!r}\n    pred  ={r[3]!r}", flush=True)
