"""Batch text generation with Qwen2.5-Coder-7B-Instruct (Apache-2.0) via vLLM on Modal; used to expand scenario ideas, never as ground truth
(every session built from its output is re-executed in the simulator, see datagen/llm_sessions.py).

  uv run modal volume put terllm data/raw/llm/tasks.prompts.jsonl /llm/tasks.prompts.jsonl
  uv run modal run train/llm_gen.py --name tasks --n 1 --temperature 1.0 --max-tokens 700
  uv run modal volume get terllm /llm/tasks.out.jsonl data/raw/llm/tasks.out.jsonl
Input lines: {"pid", "prompt", ...extra}; output lines: the same fields + {"k": sample index, "text"}.
"""
import modal

app = modal.App("terllm-llmgen")
vol = modal.Volume.from_name("terllm", create_if_missing=True)
image = (modal.Image.debian_slim(python_version="3.12").pip_install("vllm==0.11.0", "transformers==4.57.3", "huggingface_hub")
         .env({"HF_HOME": "/data/hf", "VLLM_LOGGING_LEVEL": "WARNING"}))
MODEL = "Qwen/Qwen2.5-Coder-7B-Instruct"


@app.function(image=image, gpu="A100-40GB", volumes={"/data": vol}, timeout=2 * 3600, memory=32768)
def gen(name: str, n: int = 1, temperature: float = 0.8, max_tokens: int = 400, limit: int = 0):
    import json
    import time
    from vllm import LLM, SamplingParams
    rows = [json.loads(l) for l in open(f"/data/llm/{name}.prompts.jsonl")]
    if limit:
        rows = rows[:limit]
    llm = LLM(MODEL, dtype="bfloat16", max_model_len=4096, gpu_memory_utilization=0.92, enable_prefix_caching=True)
    tok = llm.get_tokenizer()
    texts = [tok.apply_chat_template([{"role": "user", "content": r["prompt"]}], tokenize=False, add_generation_prompt=True) for r in rows]
    t0 = time.time()
    outs = llm.generate(texts, SamplingParams(n=n, temperature=temperature, top_p=0.95, max_tokens=max_tokens, seed=0))
    with open(f"/data/llm/{name}.out.jsonl", "w") as f:
        for r, o in zip(rows, outs):
            for k, c in enumerate(o.outputs):
                f.write(json.dumps({**{x: v for x, v in r.items() if x != "prompt"}, "k": k, "text": c.text}, ensure_ascii=False) + "\n")
    vol.commit()
    print(f"{len(rows)} prompts x {n} in {time.time() - t0:.0f}s")


@app.local_entrypoint()
def main(name: str, n: int = 1, temperature: float = 0.8, max_tokens: int = 400, limit: int = 0):
    gen.remote(name, n, temperature, max_tokens, limit)
