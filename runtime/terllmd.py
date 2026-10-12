"""terllm daemon: keeps per-shell context, queries a local llama-server (GGUF) and answers inline-completion requests over a Unix socket.

Protocol: one tab-separated line per message.
  shell -> daemon   c <exit> <cwd> <venv> <lastcmd>   (precmd: previous command finished; lastcmd empty on the first prompt)
                    q <seq> <buf>                     (the user typed `buf`)
  daemon -> shell   s <seq> <buf> <completion>        (empty completion = abstain)

uv run runtime/terllmd.py [--model models/gguf/terllm-v1-q8_0.gguf] [--threshold 0.5]
"""
import argparse
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
import quality  # noqa: E402
import render  # noqa: E402  (the training prompt format is the single source of truth)

SOCK = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "terllm.sock"
HOME = os.path.expanduser("~")
LLAMA_SERVER = ROOT / "vendor/llama.cpp/build/bin/llama-server"
SHADOW = pathlib.Path(os.environ.get("XDG_DATA_HOME", HOME + "/.local/share")) / "terllm/shadow.jsonl"   # local only, never uploaded


def listing(cwd):
    try:
        names = os.listdir(cwd)
    except OSError:
        return []
    return sorted(names, key=lambda s: (s.lower().lstrip("."), s))[:40]   # same order as the simulator's ls -A


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

    def complete(self, prompt):
        body = json.dumps({"prompt": prompt, "n_predict": 48, "temperature": 0, "cache_prompt": True, "n_probs": 1}).encode()
        r = json.load(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{self.port}/completion", body, {"Content-Type": "application/json"}), timeout=5))
        probs = [p.get("prob", 2.718 ** p.get("logprob", 0)) for p in r.get("completion_probabilities", [])]
        return r["content"], min(probs, default=1.0)


class Session:
    """What the model sees: cwd/ls/git/env + the last HIST commands of this shell."""
    def __init__(self):
        self.steps, self.cwd, self.venv, self.events, self.accepted = [], HOME, "", [], False

    def log(self, last, code):
        """Shadow record: context, what the model said for every prefix the user typed, and what was finally run (offline precision/coverage)."""
        self.steps[-1]["cmd"] = ""
        ctx = render.context({"steps": self.steps}, len(self.steps) - 1)
        if quality.has_secret(ctx) or quality.has_secret(last):
            return
        SHADOW.parent.mkdir(parents=True, exist_ok=True)
        with open(SHADOW, "a") as f:
            os.chmod(SHADOW, 0o600)
            f.write(json.dumps({"t": int(time.time()), "ctx": ctx, "cmd": last, "exit": code, "events": self.events, "accepted": self.accepted}, ensure_ascii=False) + "\n")

    def update(self, code, cwd, venv, last):
        if last:
            self.log(last, code)
            self.steps[-1].update(cmd=last, exit=code)
            self.steps = self.steps[-16:]
        self.events, self.accepted = [], False
        self.cwd, self.venv = cwd, venv
        self.steps.append({"cwd": render_cwd(cwd), "ls": listing(cwd), "git": git_state(cwd), "env": {"VIRTUAL_ENV": os.path.basename(venv)} if venv else {},
                           "cmd": "", "exit": 0, "stderr": "", "stdout": "", "target": True, "nl": None})

    def prompt(self, buf):
        self.steps[-1]["cmd"] = buf
        s = {"steps": self.steps}
        return render.context(s, len(self.steps) - 1) + buf


def render_cwd(cwd):
    return "~" + cwd[len(HOME):] if cwd == HOME or cwd.startswith(HOME + "/") else cwd


def serve(backend):
    class H(socketserver.StreamRequestHandler):
        def handle(self):
            ses, latest, ev = Session(), [None], threading.Event()
            ses.update(0, HOME, "", "")

            def work():
                while True:
                    ev.wait(); ev.clear()
                    q, latest[0] = latest[0], None
                    if q is None:
                        continue
                    seq, buf = q
                    comp = ""
                    try:
                        text, conf = backend.complete(ses.prompt(buf))
                        text = text.split("<|end|>")[0]
                        comp = text if conf >= backend.threshold and "\n" not in text else ""
                        ses.events.append({"typed": buf, "raw": text, "conf": round(conf, 3), "shown": bool(comp)})
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
                    latest[0] = (f[1], "\t".join(f[2:])); ev.set()

    SOCK.unlink(missing_ok=True)
    with socketserver.ThreadingUnixStreamServer(str(SOCK), H) as srv:
        SOCK.chmod(0o600)
        srv.daemon_threads = True
        srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=str(ROOT / "models/gguf/terllm-v1-q8_0.gguf"))
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--threshold", type=float, default=0.5, help="abstain if the least confident token is below this probability")
    a = ap.parse_args()
    b = Backend(a.model, a.threads, a.threshold)
    try:
        serve(b)
    finally:
        b.proc.terminate()
