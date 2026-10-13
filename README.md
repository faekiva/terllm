# terllm

Local AI command autocomplete for **zsh** and **bash**. A 135M-parameter model suggests the rest of the command you are typing as grey ghost text, like
zsh-autosuggestions, but the suggestion comes from a model that sees your directory, git state and recent commands instead of only matching your history.

- Runs entirely on your CPU through [llama.cpp](https://github.com/ggml-org/llama.cpp). Nothing leaves your machine.
- Never executes anything. It only draws text; accept it with <kbd>→</kbd> / <kbd>End</kbd> (bash also <kbd>Ctrl-E</kbd>) at the end of the line.
- Also completes files, directories and commands (fish style): the model is shown what tab completion would offer and decides whether to use it.

## Install (Debian / Ubuntu, amd64)

Download the `.deb` from the [latest release](https://github.com/partum55/terllm/releases/latest) and install it:

```sh
sudo apt install ./terllm_0.3.0_amd64.deb
```

The package enables terllm in `~/.zshrc` and `~/.bashrc` of every normal user (a marked block; symlinked rc files are left alone and only reported) and
`apt remove terllm` takes it out again. Open a new terminal. The first one starts the daemon (a few seconds to load the model); it exits after 15
minutes without a connected shell and the next terminal starts it again.

Requirements: `python3` >= 3.10, an x86-64 CPU with AVX2, zsh, or bash >= 5.1.

### Manual setup

```sh
source /usr/lib/terllm/runtime/terllm.zsh     # zsh (disables zsh-autosuggestions: both draw in POSTDISPLAY)
source /usr/lib/terllm/runtime/terllm.bash    # bash >= 5.1
```

From a clone of this repo you also need a `llama-server` build in `vendor/llama.cpp/build/bin/` and the model in `models/gguf/`
(`packaging/build-deb.sh` fetches both and shows how they are built).

## How it works

```
shell plugin ──unix socket──▶ terllmd.py ──HTTP──▶ llama-server (GGUF model)
 (zsh / bash)                  per-shell context        greedy, 48 tokens
```

1. The plugin reports each finished command (exit code, cwd, venv) and every buffer you type.
2. The daemon keeps the last commands of each shell and builds the prompt: cwd, `ls`, git status, venv, recent history, the older history commands that
   extend what you typed, and the tab-completion candidates for the word under the cursor.
3. The model continues the line. A suggestion is shown only up to the point where every token has probability >= 0.8 (`--threshold`), cut at a word boundary.
4. Stale answers are dropped, so the ghost text always belongs to what is on the line now.

The prompt format lives in one place, [`datagen/render.py`](datagen/render.py), and is shared by training and runtime.

## The model

[SmolLM2-135M](https://huggingface.co/HuggingFaceTB/SmolLM2-135M) fine-tuned (loss on the completion only) on a synthetic corpus of shell sessions:

- ~200k sessions in 8 classes (error recovery, routine git/tools, state-dependent actions, chained commands, navigation, paths, text-to-command, created-then-used files).
- Sessions are scripted from seed scenarios (`datagen/scenarios/`), executed by a shell/git/filesystem simulator (`datagen/sim/`) and validated against a real
  Ubuntu container. Text-to-command pairs come from [tldr-pages](https://github.com/tldr-pages/tldr) and [NL2Bash](https://github.com/TellinaTool/nl2bash)
  (see [`data/SOURCES.md`](data/SOURCES.md) for licenses).
- Trained on [Modal](https://modal.com) (`train/modal_app.py`), quantized to GGUF `q4_k_m`. Weights: [huggingface.co/partum/terllm](https://huggingface.co/partum/terllm).

### How good is it

Honest numbers, all measured on synthetic data:

| Check | Result |
|---|---|
| Exact match of the whole completion, held-out synthetic sessions | ~61% |
| Path arguments with fresh names (`runtime/fs_eval.py`) | 73.5% (v2 without the candidate list: 33%) |
| Path arguments in unseen command / directory shapes | ~15% (n=61) |

The model generalises across names but not yet across command and tree shapes, and synthetic scores overstate real use. The number that matters is
on your own commands, see below.

## Measure it on your own shell

The daemon appends a local log of what it suggested for every prefix you typed and what you finally ran to `~/.local/share/terllm/shadow.jsonl`
(mode 0600, never uploaded, records that look like they contain secrets are skipped). Then:

```sh
uv run runtime/shadow_report.py            # precision and characters saved per confidence threshold
uv run runtime/hist_baseline.py            # what zsh-autosuggestions (history prefix match) achieves on your ~/.zsh_history
```

## Known limits

- Directories with more than 20 matches give the model no candidates, so it guesses names there.
- Words with quotes, globs, `$`, `=` and similar are not completed.
- x86-64 with AVX2 only for the packaged build; other platforms need a local llama.cpp build.
- English commands; the model is small and will be wrong often. Check the suggestion before pressing <kbd>→</kbd> then <kbd>Enter</kbd>.

## Repository layout

| Path | What |
|---|---|
| `runtime/` | daemon (`terllmd.py`), zsh and bash plugins, confidence policy, evaluation scripts |
| `datagen/` | scenarios, simulator, session builder, prompt renderer, quality gates |
| `train/` | Modal fine-tuning job |
| `packaging/` | `.deb` build, maintainer scripts; CI builds it on every push and publishes it on `v*` tags |

Debugging: the daemon logs to `/tmp/terllmd.log`. Build and test: `packaging/build-deb.sh <version>`, `uv run datagen/test_sim.py`.

## License

MIT, see [LICENSE](LICENSE). The model derives from SmolLM2 (Apache-2.0); training-data sources are listed in [`data/SOURCES.md`](data/SOURCES.md).
