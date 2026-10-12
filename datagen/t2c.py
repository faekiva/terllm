"""Text-to-command sessions from public data: tldr-pages (CC BY 4.0) and NL2Bash (MIT data). See data/SOURCES.md.

The commands are NOT executed by the simulator (arbitrary tools); each session gets a small real context (files that the
command refers to exist in the simulated cwd) and the target step is marked v=False (unvalidated, syntax-checked in gates).
"""
import hashlib
import pathlib
import random
import re

from sessions import Discard, run_step
from sim.shell import Sim
from wordlists import GENERATORS, base_dir, filename, noun, pick, project, user

ROOT = pathlib.Path(__file__).resolve().parents[1]
TLDR = ROOT / "data" / "raw" / "tldr-src" / "pages"
NL2BASH = ROOT / "data" / "raw" / "nl2bash"
TLDR_SKIP = {"windows", "osx"}


def tldr_examples():
    out = []
    for plat in ("common", "linux"):
        for f in sorted((TLDR / plat).glob("*.md")):
            page, desc = f.stem, None
            for line in f.read_text().splitlines():
                if line.startswith("- "):
                    desc = line[2:].rstrip(":").strip()
                elif line.startswith("`") and line.endswith("`") and desc:
                    out.append((page, desc, line.strip("`")))
                    desc = None
    return out


def clean_desc(d):
    d = re.sub(r"\[(\w)\](\w)", r"\1\2", d)           # "[c]reate" -> "create"
    d = re.sub(r"\[(\w)\]", r"\1", d)
    return re.sub(r"\s+", " ", d.replace("`", "")).strip()


class Fill:
    """Resolve {{placeholders}} to names that also exist in the simulated cwd."""

    def __init__(self, r):
        self.r, self.files, self.dirs = r, [], []

    def file(self, ext=None):
        n = filename(self.r, ext)
        self.files.append(n)
        return n

    def dir(self):
        n = pick(self.r, [noun(self.r), project(self.r)])
        self.dirs.append(n)
        return n

    def one(self, ph):
        r = self.r
        m = re.fullmatch(r"\[(-\w)\|(--[\w-]+)\]", ph)
        if m:
            return m.group(1) if r.random() < 0.6 else m.group(2)
        low = ph.lower()
        ext = re.search(r"\.(\w{1,5})$", ph)
        if ext and ext.group(1) in ("txt", "md", "py", "js", "c", "sh", "json", "yaml", "csv"):
            return self.file(ext.group(1))
        if any(k in low for k in ("tar.gz", "tgz")):
            return pick(r, [noun(r), project(r)]) + ".tar.gz"
        if "zip" in low:
            return noun(r) + ".zip"
        if "director" in low or "folder" in low or low.endswith("/dir") or "dir" == low:
            return self.dir()
        if "file" in low or "path" in low:
            return self.file()
        if any(k in low for k in ("number", "count", "seconds", "size", "lines", "depth", "pid", "n")) and not any(c.isalpha() for c in low.replace("number", "").replace("count", "").replace("seconds", "").replace("size", "").replace("lines", "").replace("depth", "").replace("pid", "")):
            return str(r.randrange(1, 100))
        if "port" in low:
            return str(GENERATORS["port"](r))
        if "url" in low or "http" in low:
            return f"https://example.com/{noun(r)}"
        if "user" in low:
            return user(r)
        if "host" in low or "domain" in low or "ip" in low:
            return pick(r, ["example.com", "192.168.1.10", "localhost", "10.0.0.5"])
        if "branch" in low:
            return GENERATORS["branch"](r)
        if "package" in low or "module" in low:
            return pick(r, GENERATORS["pip"].__globals__["PIP"] + GENERATORS["npm"].__globals__["NPM"])
        if "message" in low or "text" in low or "string" in low or "pattern" in low or "regex" in low or "query" in low or "keyword" in low:
            return noun(r)
        if "version" in low:
            return GENERATORS["version"](r)
        return noun(r)

    def command(self, tpl):
        def sub(m):
            inner = m.group(1)
            if " " in inner and "path/to/file" in inner:       # "{{path/to/file1 path/to/file2 ...}}"
                return " ".join(self.file() for _ in range(self.r.randrange(2, 4)))
            return self.one(inner)
        return re.sub(r"\{\{(.*?)\}\}", sub, tpl)


def context_session(r, setup_files, setup_dirs, final_cmd, nl, tid, source, meta=None):
    base = base_dir(r)
    sim = Sim(pick(r, ["bash", "zsh"]))
    setup = [f"mkdir -p {base} && cd {base}"]
    setup += [f"mkdir -p {d}" for d in dict.fromkeys(setup_dirs)] + [f"echo {noun(r)} > {f}" for f in dict.fromkeys(setup_files)]
    setup += [f"echo {noun(r)} > {pick(r, ['README.md', 'notes.txt', 'todo.md'])}"] * (r.random() < 0.5)
    for line in setup:
        if sim.run(line).code != 0:
            raise Discard(f"t2c setup failed: {line}")
    steps = []
    for _ in range(r.randint(0, 2)):
        steps.append(run_step(sim, pick(r, ["ls", "ls -la", "pwd", "ls -a"]), False, "any"))
    pre = {**sim.snapshot(), "fs": sim.fs_for(final_cmd)}
    steps.append({**pre, "cmd": final_cmd, "exit": 0, "stderr": "", "stdout": "", "target": True, "v": False, "nl": nl})
    sid = hashlib.blake2b(repr((setup, [x["cmd"] for x in steps], final_cmd, nl, sim.shell)).encode(), digest_size=8).hexdigest()
    return {"id": f"s-{sid}", "class": "t2c", "source": source, "template_id": tid, "shell": sim.shell, "setup": setup, "steps": steps, "nl": nl,
            **({"meta": meta} if meta else {})}


def nl_phrase(r, desc):
    d = clean_desc(desc)
    low = d[0].lower() + d[1:] if d[:2].upper() != d[:2] else d       # keep acronyms ("SSH ...") intact
    if d.lower().startswith(("how", "what")):
        return d
    return pick(r, [d if r.random() < 0.3 else low, low, f"how do I {low}", f"command to {low}", f"{low} in the terminal"])


def tldr_session(ex, seed):
    r = random.Random(seed)
    page, desc, tpl = ex
    f = Fill(r)
    cmd = f.command(tpl)
    if "{{" in cmd or "..." in cmd or len(cmd) > 180 or "\n" in cmd:
        raise Discard("tldr: unfilled/long")
    return context_session(r, f.files, f.dirs, cmd, nl_phrase(r, desc), f"tldr.{page}", "tldr", {"page": page})


def nl2bash_pairs(limit=None):
    nl = (NL2BASH / "all.nl").read_text().splitlines()
    cm = (NL2BASH / "all.cm").read_text().splitlines()
    seen, out = set(), []
    for a, b in zip(nl, cm):
        a = re.sub(r"^\((?:GNU|BSD|OSX|Linux|Mac)[^)]*\)\s*", "", a.strip())
        if not a or not b or len(b) > 200 or "\\" in b or (a, b) in seen:
            continue
        seen.add((a, b))
        out.append((a, b.strip()))
    return out


def nl2bash_session(pair, idx, seed):
    r = random.Random(seed)
    nl, cmd = pair
    prog = cmd.split()[0].lstrip("(") if cmd.split() else "x"
    prog = re.sub(r"\W", "_", prog) or "x"
    files = [t for t in re.findall(r"(?<![\w/.-])([\w.-]+\.(?:txt|md|py|sh|c|log|csv|json|conf))\b", cmd)][:3]
    return context_session(r, files, [], cmd, nl[0].lower() + nl[1:] if r.random() < 0.5 else nl, f"nl2bash.{prog}.{idx // 25}", "nl2bash")


CACHE = ROOT / "data" / "raw" / "bash_n_cache.json"


def valid_commands():
    """(set of tldr templates, set of nl2bash commands) that pass `bash -n` (checked once in the sandbox, cached)."""
    import json
    import quality
    tl = {tpl for _, _, tpl in tldr_examples()}
    nl = {c for _, c in nl2bash_pairs()}
    key = lambda c: re.sub(r"\{\{.*?\}\}", "x", c)
    cands = sorted({key(c) for c in tl | nl})
    bad = set(json.loads(CACHE.read_text())) if CACHE.exists() else None
    if bad is None:
        bad = set(quality.bash_n(cands, jobs=6))
        CACHE.write_text(json.dumps(sorted(bad)))
    return {c for c in tl if key(c) not in bad}, {c for c in nl if c not in bad}
