# faekiva fork of terllm

[terllm](https://github.com/partum55/terllm) (fast local ghost text, 135M model) plus the
big-model features of [paiksca/zsh-ai-autocomplete](https://github.com/paiksca/zsh-ai-autocomplete),
vendored under `assist/`. Remotes: `upstream` (terllm), `paiksca`. Branch `paiksca-fixes` is
paiksca's plugin with this fork's fixes, the source of `assist/backend.py`.

## What it does

| Piece | Model | Latency |
|---|---|---|
| Ghost text as you type (`runtime/`) | terllm v3, llama-server, ~340 MB | <30 ms |
| `ai <what you want>` / `ask` → pre-filled command, fzf alternatives, danger gate | qwen2.5-coder:7b via Ollama | 1–5 s |
| Auto-fix: a failed command's fix appears as ghost text | same | ~1 s warm |
| Ctrl-O: complete this line with the big model; `git commit -m "` gets the staged diff | same | ~1 s warm |

The 7b unloads after `AIZSH_OLLAMA_KEEPALIVE` (2m), so the first fix/Ctrl-O after idle
waits for a model load. Accept any ghost text with Right arrow / End, or Shift+Enter
(`TERLLM_ACCEPT_KEY`, default `^J`: iTerm2's Claude Code setup sends `\n` for Shift+Enter).
With nothing to accept, Shift+Enter runs the line like Enter.

## Changes from upstream terllm

- `runtime/lineparse.py`: candidates and rules per *current command* of the line, from
  zsh's own word split (`${(z)BUFFER}`, sent by the plugin). Directories made earlier on the
  line (`mkdir x && cd `) are candidates. Rule 1: a word that lands on a real candidate is
  shown even below the confidence threshold. Rule 2: for `cd`/`cat`/… a guess that isn't a
  candidate is dropped.
- `runtime/targets.py`: make / just / task / npm-style targets as candidates, in *strict*
  mode (the list is complete): a unique match is completed outright, non-targets are dropped.
- `policy.parse_healed` + `Backend.complete(heal=…)`: token healing — the half-typed word is
  cut from the prompt and the output forced to restart with it (llama-server grammar).
  `TERLLM_HEAL=0` turns it off.
- `runtime/terllm.zsh`: sends the `(z)` split; `_terllm_pin` lets `assist/` keep its
  suggestion until the user diverges; Shift+Enter (`^J`) accepts.

## Changes from paiksca

- Only the backend is used; its ghost/stats layers are off (`AIZSH_STATS=0`).
- `prompt` is renamed `ai` (the original clobbers zsh's `promptinit` `prompt`).
- Staged diff in the context for `git commit` completions; cancelled requests really abort
  the Ollama generation (socket `shutdown` before `close`).

## Run it (macOS)

With nix: set `TERLLM_LLAMA_SERVER` (a llama-server binary) and `TERLLM_MODEL` (the gguf) and
skip the `vendor/` / `models/` setup below — nixfiles does this. Without nix:

```sh
# llama-server: the daemon expects vendor/llama.cpp/build/bin/llama-server. Use a wrapper,
# not a symlink — llama.cpp loads its backends from next to the path it was started as.
mkdir -p vendor/llama.cpp/build/bin models/gguf
printf '#!/bin/sh\nexec %s "$@"\n' "$(nix build --no-link --print-out-paths nixpkgs#llama-cpp)/bin/llama-server" \
  > vendor/llama.cpp/build/bin/llama-server && chmod +x vendor/llama.cpp/build/bin/llama-server
curl -L -o models/gguf/terllm-v3-q4_k_m.gguf \
  https://huggingface.co/partum/terllm/resolve/main/v3/terllm-v3-q4_k_m.gguf
ollama pull qwen2.5-coder:7b
```

```zsh
# ~/.zshrc — instead of zsh-autosuggestions (both draw ghost text)
source ~/code/github.com/faekiva/terllm/runtime/terllm.zsh
source ~/code/github.com/faekiva/terllm/assist/assist.zsh
```

`assist status|doctor|restart|autofix on|off` manages the backend.

## Tests

```sh
cd runtime && for t in test_lineparse test_targets test_policy; do python3 $t.py; done
```

## Known gaps

- The 135M model is sensitive to recent history; one-off A/B runs vary. Measure with
  upstream's `runtime/shadow_report.py` over real use.
- Ctrl-O output is only as good as the 7b's context (no pod names etc. → junk like
  `kubectl logs -1000…`).
- The `vendor/` wrapper pins a `/nix/store` path; a GC can break it.
