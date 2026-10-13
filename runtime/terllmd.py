"""terllm daemon: keeps per-shell context, queries a local llama-server (GGUF) and answers inline-completion requests over a Unix socket.

Protocol: one tab-separated line per message.
  shell -> daemon   c <exit> <cwd> <venv> <lastcmd>   (precmd: previous command finished; lastcmd empty on the first prompt)
                    q <seq> <buf>                     (the user typed `buf`)
  daemon -> shell   s <seq> <buf> <completion>        (empty completion = abstain)

uv run runtime/terllmd.py [--model models/gguf/terllm-v3-q4_k_m.gguf] [--threshold 0.8] [--idle 15] [--no-fs]   (--no-fs: no file/dir/command candidates in the prompt; required for v1/v2 models)
"""
import argparse
import itertools
import json
import os
import pathlib
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "datagen"))
import hist_baseline  # noqa: E402
import lineparse  # noqa: E402
import targets  # noqa: E402
import policy  # noqa: E402
import quality  # noqa: E402
import render  # noqa: E402  (the training prompt format is the single source of truth)

HISTORY = hist_baseline.load(os.environ.get("HISTFILE", "~/.zsh_history")) if os.path.exists(os.path.expanduser(os.environ.get("HISTFILE", "~/.zsh_history"))) else []   # shared by all shells, oldest -> newest
SOCK = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "terllm.sock"
HOME = os.path.expanduser("~")
LLAMA_SERVER = ROOT / "vendor/llama.cpp/build/bin/llama-server"
FS = False
HEAL = os.environ.get("TERLLM_HEAL", "1") != "0"   # token healing of the half-typed word
SHADOW = pathlib.Path(os.environ.get("XDG_DATA_HOME", HOME + "/.local/share")) / "terllm/shadow.jsonl"   # local only, never uploaded


def listing(cwd):
    try:
        names = os.listdir(cwd)
    except OSError:
        return []
    return sorted(names, key=lambda s: (s.lower().lstrip("."), s))[:40]   # same order as the simulator's ls -A


def listdir(key):
    """render.fs_cands' view of a directory (display path, ~ allowed): [name | name/ | name*] like datagen/sim/shell.py fs_for. None if unreadable."""
    try:
        with os.scandir(os.path.expanduser(key)) as it:
            es = list(itertools.islice(it, 3000))
    except OSError:
        return None
    out = [(e.name, "/" if e.is_dir() else "*" if os.access(e.path, os.X_OK) and e.is_file() else "") for e in es]
    return [n + k for n, k in sorted(out, key=lambda x: (x[0].lower().lstrip("."), x[0]))]


def path_bins(dirs):
    """Names of the executables on PATH (or in the given dirs), sorted."""
    names = set()
    for d in dirs:
        try:
            names.update(e.name for e in os.scandir(d) if e.is_file() and os.access(e.path, os.X_OK))
        except OSError:
            pass
    return sorted(names)


BINS = path_bins(os.environ.get("PATH", "").split(":"))


def git_state(cwd):
    try:
        out = subprocess.run(["git", "-C", cwd, "status", "--porcelain=v1", "--branch"], capture_output=True, text=True, timeout=1.5,
                             env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}).stdout.split("\n")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not out or not out[0].startswith("## "):
        return None
    head = out[0][3:].split(" [")[0]
    branch, _, up = head.partition("...")
    branch = branch.removeprefix("No commits yet on ")
    return {"branch": branch, "upstream": up or None, "dirty": [l for l in out[1:] if l][:12]}


class Backend:
    def __init__(self, model, threads, threshold):
        self.threshold = threshold
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = s.getsockname()[1]; s.close()
        self.proc = subprocess.Popen([str(LLAMA_SERVER), "-m", model, "--host", "127.0.0.1", "--port", str(self.port), "-c", "4096", "-t", str(threads),
                                      "-np", "1", "--no-webui", "--log-disable"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(200):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=1); return
            except OSError:
                time.sleep(0.1)
        raise SystemExit("llama-server did not start")

    def complete(self, prompt, heal=""):
        """heal: the half-typed word at the end of `prompt`; it is cut off and the output forced to restart with it."""
        req = {"prompt": prompt, "n_predict": 48, "temperature": 0, "cache_prompt": True, "n_probs": 1}
        if heal:
            lit = heal.replace("\\", "\\\\").replace('"', '\\"')
            req.update(prompt=prompt[:-len(heal)], n_probs=20, grammar=f'root ::= "{lit}" [^\\n]*')
        body = json.dumps(req).encode()
        r = json.load(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{self.port}/completion", body, {"Content-Type": "application/json"}), timeout=5))
        return policy.parse_healed(r, heal) if heal else policy.parse(r)


class Session:
    """What the model sees: cwd/ls/git/env + the last HIST commands of this shell."""
    def __init__(self):
        self.steps, self.cwd, self.venv, self.events, self.accepted = [], HOME, "", [], False

    def log(self, last, code):
        """Shadow record: context, what the model said for every prefix the user typed, and what was finally run (offline precision/coverage)."""
        self.steps[-1]["cmd"] = ""
        ctx = render.context({"steps": self.steps}, len(self.steps) - 1, io=False)
        if quality.has_secret(ctx) or quality.has_secret(last):
            return
        SHADOW.parent.mkdir(parents=True, exist_ok=True)
        with open(SHADOW, "a") as f:
            os.chmod(SHADOW, 0o600)
            f.write(json.dumps({"t": int(time.time()), "ctx": ctx, "cmd": last, "exit": code, "events": self.events, "accepted": self.accepted}, ensure_ascii=False) + "\n")

    def update(self, code, cwd, venv, last):
        if last:
            self.log(last, code)
            HISTORY.append(last)
            self.steps[-1].update(cmd=last, exit=code)
            self.steps = self.steps[-16:]
        self.events, self.accepted = [], False
        self.cwd, self.venv = cwd, venv
        self.steps.append({"cwd": render_cwd(cwd), "ls": listing(cwd), "git": git_state(cwd), "env": {"VIRTUAL_ENV": os.path.basename(venv)} if venv else {},
                           "cmd": "", "exit": 0, "stderr": "", "stdout": "", "target": True, "nl": None})

    def prompt(self, buf, words=None):
        self.steps[-1]["cmd"] = buf
        cands = render.history_cands(HISTORY, buf)
        line = lineparse.parse(buf, words)   # candidates for the current command, not the line's first
        tg, strict = target_cands(line, self.cwd)
        fs = tg if strict else lineparse.candidates(line, render.fs_cands(line.segment, self.steps[-1]["cwd"], listdir, sorted(set(BINS) | set(path_bins([self.venv + "/bin"] if self.venv else []))))) if FS else []
        return render.prompt(render.context({"steps": self.steps}, len(self.steps) - 1, io=False), buf, cands, fs[:render.FS_SHOW]), cands, fs, line, strict


_TARGETS = {}   # (runner, cwd) -> (time, names): `task --list-all` is too slow per keystroke


def target_cands(line, cwd, ttl=5.0):
    """(matching targets, strict): strict when the runner has a target list at all."""
    runner = targets.runner_for(line.words)
    if not runner or line.cur.startswith("-"):
        return [], False
    hit = _TARGETS.get((runner, cwd))
    if not hit or time.time() - hit[0] > ttl:
        hit = _TARGETS[(runner, cwd)] = (time.time(), targets.targets(runner, cwd))
    return [t for t in hit[1] if t.startswith(line.cur) and t != line.cur], bool(hit[1])


def render_cwd(cwd):
    return "~" + cwd[len(HOME):] if cwd == HOME or cwd.startswith(HOME + "/") else cwd


def serve(backend, idle=0.0):
    live = [0, time.time()]                                  # connected shells, when the last one left

    class H(socketserver.StreamRequestHandler):
        def setup(self):
            super().setup(); live[0] += 1

        def finish(self):
            super().finish(); live[0] -= 1; live[1] = time.time()

        def handle(self):
            ses, latest, ev = Session(), [None], threading.Event()
            ses.update(0, HOME, "", "")

            def work():
                while True:
                    ev.wait(); ev.clear()
                    q, latest[0] = latest[0], None
                    if q is None:
                        continue
                    seq, buf, words = q
                    comp = ""
                    try:
                        prompt, cands, fs, line, strict = ses.prompt(buf, words)
                        heal = line.cur if HEAL and prompt.endswith(line.cur) else ""
                        toks = backend.complete(prompt, heal)
                        comp, rule = lineparse.decide(toks, backend.threshold, line, fs, strict)
                        comp = "" if "\n" in comp else comp
                        ses.events.append({"typed": buf, "toks": [[t, round(p, 3)] for t, p in toks], "shown": comp, "rule": rule, "cands": cands, "fs": fs})
                    except Exception as e:                     # never break the shell
                        print("query failed:", e, file=sys.stderr)
                    try:
                        self.wfile.write(f"s\t{seq}\t{buf}\t{comp}\n".encode()); self.wfile.flush()
                    except OSError:
                        return

            threading.Thread(target=work, daemon=True).start()
            for raw in self.rfile:
                f = raw.decode(errors="replace").rstrip("\n").split("\t")
                if f[0] == "c" and len(f) >= 5:
                    ses.update(int(f[1] or 0), f[2], f[3], "\t".join(f[4:]))
                elif f[0] == "a":
                    ses.accepted = True
                elif f[0] == "q" and len(f) >= 3:
                    buf, sep, zwords = "\t".join(f[2:]).partition("\x1f")   # plugin appends zsh's ${(z)} split
                    latest[0] = (f[1], buf, zwords.split("\x1e") if sep else None); ev.set()

    SOCK.unlink(missing_ok=True)
    with socketserver.ThreadingUnixStreamServer(str(SOCK), H) as srv:
        SOCK.chmod(0o600)
        srv.daemon_threads = True
        if idle:                                             # nobody connected for `idle` minutes: exit (the next terminal starts us again)
            def watch():
                while True:
                    time.sleep(min(30, idle * 20))
                    if live[0] == 0 and time.time() - live[1] > idle * 60:
                        srv.shutdown(); return
            threading.Thread(target=watch, daemon=True).start()
        srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=str(ROOT / "models/gguf/terllm-v3-q4_k_m.gguf"))
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--threshold", type=float, default=0.8, help="show the longest prefix of the completion whose tokens all have at least this probability")
    ap.add_argument("--idle", type=float, default=15, help="exit after this many minutes without a connected shell (0 = never)")
    ap.add_argument("--no-fs", action="store_true", help="leave file/dir/command candidates out of the prompt (v1/v2 models were not trained with <|fs|>)")
    a = ap.parse_args()
    FS = not a.no_fs
    b = Backend(a.model, a.threads, a.threshold)
    try:
        serve(b, a.idle)
    finally:
        b.proc.terminate()
