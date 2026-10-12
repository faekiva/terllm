"""Shell simulator: tokenizer, list/pipeline execution, and the command set. Error text comes from the harvested corpus."""
import fnmatch
import json
import posixpath
import re
import sys

from . import git as G
from .errors import Unsupported, error
from .fs import FS, HOME, Node

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import fixtures as FX  # noqa: E402

PRESENT = set("""cd ls pwd mkdir touch rm rmdir mv cp cat echo printf head tail wc grep find chmod ln export unset source . deactivate
clear history true false sleep which type tar unzip zip gzip gunzip wget curl git python python3 pip pip3 node npm npx make cmake gcc g++ cc cargo
bash sh zsh less more file stat date whoami id uname env printenv ps df du free uptime hostname sudo apt apt-get systemctl service docker
kubectl ssh kill top tee sort uniq diff sed awk xargs journalctl scp docker-compose exit fx_remote fx_archive fx_project fx_port fx_bare pnpm yarn""".split())
# present-in-image tools that the simulator can't model => session dropped if used
MISSING = set()  # anything not in PRESENT is "command not found" (matches the sandbox image)
OPAQUE = {"docker-compose", "journalctl", "scp", "apt", "apt-get", "systemctl", "service", "docker", "kubectl", "ssh", "kill", "top", "ps", "df",
          "du", "free", "uptime", "uname", "date", "hostname", "id", "history", "env", "printenv", "sleep"}
APT_PROVIDES = {"ripgrep": "rg", "tree": "tree", "jq": "jq", "htop": "htop", "vim": "vim", "nano": "nano", "tmux": "tmux",
                "ncdu": "ncdu", "bat": "bat", "fd-find": "fdfind", "sqlite3": "sqlite3", "golang-go": "go", "default-jre": "java",
                "ffmpeg": "ffmpeg", "imagemagick": "convert", "net-tools": "netstat", "dnsutils": "dig", "lsof": "lsof",
                "strace": "strace", "rsync": "rsync", "screen": "screen", "redis-tools": "redis-cli", "postgresql-client": "psql",
                "neofetch": "neofetch", "httpie": "http", "shellcheck": "shellcheck", "silversearcher-ag": "ag"}
LONG_RUNNING = re.compile(r"^(npm (start|run dev|run serve)|python3? -m http\.server( \d+)?|ping .*|tail -f .*|top|htop|sleep \d{3,}|cargo run --release)$")


class R:
    def __init__(self, code=0, out="", err="", long=False, v=True):
        self.code, self.out, self.err, self.long, self.v = code, out, err, long, v


class ExitSignal(Exception):
    def __init__(self, code):
        self.code = code


# ---------------------------------------------------------------- tokenizer
def brace_expand(w):
    m = re.search(r"\{([^{}]*,[^{}]*)\}", w)
    if not m:
        m2 = re.search(r"\{(\d+)\.\.(\d+)\}", w)
        if not m2:
            return [w]
        a, b = int(m2.group(1)), int(m2.group(2))
        return [x for n in (range(a, b + 1) if a <= b else range(a, b - 1, -1)) for x in brace_expand(w[:m2.start()] + str(n) + w[m2.end():])]
    return [x for part in m.group(1).split(",") for x in brace_expand(w[:m.start()] + part + w[m.end():])]


def tokenize(line, env):
    toks, i, n = [], 0, len(line)
    cur, have, glob, anyq = [], False, False, False

    def flush():
        nonlocal cur, have, glob, anyq
        if have:
            w = "".join(cur)
            for x in (brace_expand(w) if not anyq and "{" in w else [w]):
                toks.append(("w", x, glob))
        cur, have, glob, anyq = [], False, False, False

    def var(j):  # returns (value, next index)
        if line.startswith("${", j):
            k = line.index("}", j)
            return env.get(line[j + 2:k], ""), k + 1
        m = re.match(r"[A-Za-z_]\w*", line[j + 1:])
        if m:
            return env.get(m.group(0), ""), j + 1 + m.end()
        if line[j + 1:j + 2] == "?":
            return env.get("?", "0"), j + 2
        return "$", j + 1

    while i < n:
        c = line[i]
        if c in " \t":
            flush(); i += 1
        elif c == "#" and not have:
            break
        elif c == "'":
            j = line.find("'", i + 1)
            if j < 0:
                raise Unsupported("unterminated quote")
            cur.append(line[i + 1:j]); have = anyq = True; i = j + 1
        elif c == '"':
            have = anyq = True; i += 1
            while i < n and line[i] != '"':
                if line[i] == "\\" and i + 1 < n and line[i + 1] in '"\\$`':
                    cur.append(line[i + 1]); i += 2
                elif line[i] == "$":
                    if line.startswith("$(", i):
                        raise Unsupported("command substitution")
                    v, i = var(i); cur.append(v)
                elif line[i] == "`":
                    raise Unsupported("backtick")
                else:
                    cur.append(line[i]); i += 1
            if i >= n:
                raise Unsupported("unterminated quote")
            i += 1
        elif c == "\\" and i + 1 < n:
            cur.append(line[i + 1]); have = anyq = True; i += 2
        elif c == "$":
            if line.startswith("$(", i):
                raise Unsupported("command substitution")
            v, i = var(i); cur.append(v); have = True; anyq = True
        elif c == "`":
            raise Unsupported("backtick")
        elif c == "~" and not have and (i + 1 == n or line[i + 1] in "/ \t"):
            cur.append(env.get("HOME", HOME)); have = True; anyq = True; i += 1
        elif c in "<>" or (c.isdigit() and not have and re.match(r"\d?[<>]", line[i:])) or (c == "&" and line[i + 1:i + 2] == ">"):
            m = re.match(r"(\d?|&)(>>|>&|>|<<|<)(\d?)", line[i:])
            if m.group(2) == "<<":
                raise Unsupported("heredoc")
            if have and not c.isdigit():
                flush()
            elif have:
                pass
            if not have or c in "<>&":
                flush()
                toks.append(("redir", m.group(1) or ("0" if m.group(2) == "<" else "1"), m.group(2), m.group(3)))
                i += m.end()
            else:
                cur.append(c); i += 1
        elif c in "&|;":
            flush()
            two = line[i:i + 2]
            if two in ("&&", "||"):
                toks.append(("op", two)); i += 2
            elif c == "&":
                raise Unsupported("background job")
            else:
                toks.append(("op", c)); i += 1
        else:
            if c in "*?[":
                glob = True
            if c in "()":
                raise Unsupported("subshell")
            cur.append(c); have = True; i += 1
    flush()
    return toks


def parse(toks):
    """-> list of (connector, [Cmd]) ; Cmd = dict(words, assigns, redirs)"""
    out, conn, pipe, cmd, i = [], None, [], {"words": [], "assigns": [], "redirs": []}, 0

    def end_cmd():
        nonlocal cmd
        if cmd["words"] or cmd["assigns"]:
            pipe.append(cmd)
        elif cmd["redirs"]:
            pipe.append(cmd)
        cmd = {"words": [], "assigns": [], "redirs": []}

    while i < len(toks):
        t = toks[i]
        if t[0] == "w":
            if not cmd["words"] and re.match(r"[A-Za-z_]\w*=", t[1]):
                k, v = t[1].split("=", 1)
                cmd["assigns"].append((k, v))
            else:
                cmd["words"].append((t[1], t[2]))
        elif t[0] == "redir":
            if t[2] == ">&":
                cmd["redirs"].append((t[1], ">&", t[3] or None))
            else:
                if i + 1 >= len(toks) or toks[i + 1][0] != "w":
                    raise Unsupported("bad redirect")
                cmd["redirs"].append((t[1], t[2], toks[i + 1][1])); i += 1
        else:
            end_cmd()
            if t[1] == "|":
                pass
            else:
                if pipe:
                    out.append((conn, pipe))
                pipe, conn = [], t[1]
        i += 1
    end_cmd()
    if pipe:
        out.append((conn, pipe))
    return out


# ---------------------------------------------------------------- simulator
class Sim:
    def __init__(self, shell="bash"):
        self.shell, self.fs, self.cwd, self.oldpwd = shell, FS(), HOME, HOME
        self.env = {"HOME": HOME, "USER": "dev", "SHELL": "/bin/" + shell}
        self.venv = None
        self.fake = set()           # commands installed by (opaque) apt
        self.ports, self.root = set(), False
        self.sys_pkgs = {"pip", "pkg_resources", "setuptools", "six", "wheel"}
        self.v = True
        self._shname = "bash"

    # -- helpers
    def abs(self, p):
        if p.startswith("~"):
            p = HOME + p[1:]
        return posixpath.normpath(p if p.startswith("/") else f"{self.cwd}/{p}")

    def e(self, key, **p):
        c, o, e = error(key, self.shell, cwd=self.cwd, home=HOME, **p)
        return R(c, o, e)

    def can_write_dir(self, d):
        n = self.fs.lookup(d)
        return n is not None and n.kind == "d" and (self.root or n.owner == "dev")

    def display_cwd(self):
        return "~" + self.cwd[len(HOME):] if self.cwd == HOME or self.cwd.startswith(HOME + "/") else self.cwd

    def snapshot(self):
        names = self.fs.listdir(self.cwd)
        root, repo = G.find_repo(self)
        git = None
        if repo:
            t = G.tracking(repo)
            git = {"branch": repo.head, "dirty": G.short_status(self, root, repo)[:12], "upstream": repo.upstream.get(repo.head)}
        env = {"VIRTUAL_ENV": posixpath.basename(self.venv)} if self.venv else {}
        return {"cwd": self.display_cwd(), "ls": names[:40], "git": git, "env": env}

    def has_cmd(self, c):
        if c in self.fake:
            return True
        if self.venv and (n := self.fs.lookup(f"{self.venv}/bin/{c}")) and n.kind != "d":
            return True
        return c in PRESENT

    # -- entry
    def run(self, line):
        self.v = True
        try:
            ast = parse(tokenize(line, {**self.env, "PWD": self.cwd}))
        except Unsupported:
            raise
        last, out, err = R(0), [], []
        long = False
        for conn, pipe in ast:
            if (conn == "&&" and last.code != 0) or (conn == "||" and last.code == 0):
                continue
            try:
                last = self.pipeline(pipe)
            except ExitSignal as x:
                last = R(x.code); break
            out.append(last.out); err.append(last.err); long = long or last.long
            self.env["?"] = str(last.code)
        res = R(last.code, "".join(out), "".join(err), long, self.v)
        if res.out and not res.out.endswith("\n"):
            res.out += "\n"
        return res

    def pipeline(self, pipe):
        r = self.command(pipe[0])
        for st in pipe[1:]:
            argv = [w for w, _ in st["words"]]
            if not argv:
                raise Unsupported("empty stage")
            if argv[0] == "sudo":
                argv, self.v = argv[1:], False
            if argv[0] == "grep":
                r = self.filter_grep(argv[1:], r)
            elif argv[0] in ("head", "tail"):
                n = 10
                if len(argv) > 2 and argv[1] == "-n":
                    n = int(argv[2])
                elif len(argv) > 1 and re.match(r"-\d+$", argv[1]):
                    n = int(argv[1][1:])
                lines = r.out.splitlines(True)
                r = R(r.code, "".join(lines[:n] if argv[0] == "head" else lines[-n:]), r.err)
            elif argv[0] in ("less", "more", "cat", "sort", "uniq"):
                r = R(0, r.out, r.err)
            elif argv[0] == "wc" and "-l" in argv:
                r = R(0, f"{len(r.out.splitlines())}\n", r.err)
            elif argv[0] == "tee":
                for f in [a for a in argv[1:] if not a.startswith("-")]:
                    self.write_file(self.abs(f), r.out, append="-a" in argv)
                r = R(0, r.out, r.err)
            else:
                raise Unsupported(f"pipe stage {argv[0]}")
        return r

    def filter_grep(self, args, r):
        flags = [a for a in args if a.startswith("-")]
        pat = [a for a in args if not a.startswith("-")]
        if len(pat) != 1:
            raise Unsupported("grep stage args")
        rx = re.compile(pat[0], re.I if "-i" in flags else 0)
        lines = [l for l in r.out.splitlines(True) if bool(rx.search(l)) != ("-v" in flags)]
        return R(0 if lines else 1, "".join(lines), r.err)

    # -- one simple command
    def command(self, cmd):
        words = []
        for w, g in cmd["words"]:
            if g:
                m = self.glob(w)
                if m is None:
                    if self.shell == "zsh":
                        return self.zsh_nomatch(w)
                    words.append(w)
                else:
                    words.extend(m)
            else:
                words.append(w)
        saved = dict(self.env)
        for k, v in cmd["assigns"]:
            self.env[k] = v
        if not words:
            return R(0)
        # redirections (checked before execution, as the shell does)
        redir_out = redir_err = None
        merge = False
        for fd, op, tgt in cmd["redirs"]:
            if op == "<":
                if not self.fs.lookup(self.abs(tgt)):
                    return self.e("sh.redir_nodir", path=tgt)
                continue
            if op == ">&":
                merge = fd in ("2", "1") and tgt == ("1" if fd == "2" else None) or merge
                continue
            if tgt == "/dev/null":
                tgt_abs = None
            else:
                tgt_abs = self.abs(tgt)
                d = posixpath.dirname(tgt_abs)
                if not self.fs.lookup(d):
                    return self.e("sh.redir_nodir", path=tgt)
                if not self.can_write_dir(d):
                    return self.e("sh.redir_denied", path=tgt)
            if fd in ("1", ""):
                redir_out = (tgt_abs, op)
            elif fd == "2":
                redir_err = (tgt_abs, op)
            elif fd == "&":
                redir_out = redir_err = (tgt_abs, op)
        try:
            r = self.dispatch(words)
        finally:
            if cmd["assigns"] and words:
                self.env = {**saved, **{k: v for k, v in self.env.items() if k not in dict(cmd["assigns"]) and k not in saved}}
        if cmd["assigns"] and not words:
            pass
        if merge:
            r = R(r.code, r.out + r.err, "", r.long, r.v)
        for stream, red in (("out", redir_out), ("err", redir_err)):
            if red:
                path, op = red
                if path:
                    self.write_file(path, getattr(r, stream), append=(op == ">>"))
                setattr(r, stream, "")
        return r

    def zsh_nomatch(self, w):
        return self.e("sh.nomatch", pattern=w)

    def glob(self, pat):
        d, base = posixpath.split(pat)
        dirs = [self.abs(d) if d else self.cwd]
        if any(c in d for c in "*?["):
            raise Unsupported("glob in dir part")
        n = self.fs.lookup(dirs[0])
        if not n or n.kind != "d":
            return None
        hits = [posixpath.join(d, name) if d else name for name in sorted(n.children)
                if fnmatch.fnmatchcase(name, base) and (base.startswith(".") or not name.startswith("."))]
        return hits or None

    def write_file(self, path, text, append=False):
        n = self.fs.lookup(path)
        if n and n.kind == "f":
            n.content = (n.content + text) if append else text
        else:
            self.fs.put(path, text)

    # -- dispatch
    def dispatch(self, argv):
        c, args = argv[0], argv[1:]
        if c == "sudo":
            return self.sudo(args)
        if c in ("deactivate",) and self.venv:
            self.venv = None; self.env.pop("VIRTUAL_ENV", None)
            return R(0)
        fn = getattr(self, "c_" + ("dot" if c == "." else re.sub(r"\W", "_", c)), None) if "/" not in c else None
        if "/" in c:
            return self.exec_path(c, args)
        if c in OPAQUE:
            self.v = False
            return self.opaque(c, args)
        if c in self.fake:
            self.v = False
            return R(0)
        if self.venv and (n := self.fs.lookup(f"{self.venv}/bin/{c}")) and n.kind != "d":
            return self.venv_tool(c, args, self.venv)
        if c in PRESENT and fn:
            return fn(args)
        if c in PRESENT:
            raise Unsupported(f"unmodelled command {c}")
        return self.e("sh.not_found", cmd=c)

    def sudo(self, args):
        if not args:
            raise Unsupported("bare sudo")
        old = self.root
        self.root = True
        try:
            r = self.dispatch(args) if args[0] not in ("apt", "apt-get") else self.apt(args[1:])
        finally:
            self.root = old
        self.v = False
        return r

    def apt(self, args):
        self.v = False
        if args and args[0] in ("install", "reinstall"):
            for p in [a for a in args[1:] if not a.startswith("-")]:
                if p not in APT_PROVIDES and p not in PRESENT:
                    return R(100, "", f"E: Unable to locate package {p}\n")
                if p in APT_PROVIDES:
                    self.fake.add(APT_PROVIDES[p])
        return R(0)

    def opaque(self, c, args):
        if c in ("apt", "apt-get"):
            if not self.root:
                self.v = True
                return self.e("apt.update_denied" if args and args[0] == "update" else "apt.install_denied")
            return self.apt(args)
        return R(0)

    def exec_path(self, c, args):
        p = self.abs(c)
        n = self.fs.lookup(p)
        m = re.search(r"(.*)/bin/(python[\d.]*|pip[\d.]*|pytest|black|ruff|flake8|mypy|uvicorn|gunicorn)$", p)
        if n and m and n.kind != "d":
            return self.venv_tool(m.group(2), args, m.group(1), argv0=c)
        if n is None:
            return self.e("sh.exec_nofile", path=c)
        if n.kind == "d":
            return self.e("sh.exec_isdir", path=c)
        if not (n.mode & 0o100):
            return self.e("sh.exec_denied", path=c)
        if n.content == "<elf>":
            return R(0, "hi\n")
        return self.run_script(n.content)

    def run_script(self, text):
        cwd = self.cwd
        r = R(0)
        try:
            for l in text.splitlines():
                if l.strip() and not l.startswith("#"):
                    r2 = self.run(l)
                    r = R(r2.code, r.out + r2.out, r.err + r2.err)
        finally:
            self.cwd = cwd
        return r

    # ------------------------------------------------------------ builtins & coreutils
    def c_cd(self, a):
        a = [x for x in a if x not in ("-P", "-L")]
        tgt = HOME if not a else (self.oldpwd if a[0] == "-" else a[0])
        p = self.abs(tgt)
        n = self.fs.lookup(p)
        if n is None:
            return self.e("cd.no_dir", path=tgt)
        if n.kind != "d":
            return self.e("cd.not_dir", path=tgt)
        self.oldpwd, self.cwd = self.cwd, p
        return R(0, (p + "\n") if a and a[0] == "-" else "")

    def c_pwd(self, a):
        return R(0, self.cwd + "\n")

    def c_clear(self, a):
        return R(0)

    c_true = c_clear

    def c_false(self, a):
        return R(1)

    def c_exit(self, a):
        raise ExitSignal(int(a[0]) if a else 0)

    def c_echo(self, a):
        nl = True
        if a and a[0] == "-n":
            nl, a = False, a[1:]
        if a and a[0] == "-e":
            a = [x.encode().decode("unicode_escape") for x in a[1:]]
        return R(0, " ".join(a) + ("\n" if nl else ""))

    def c_printf(self, a):
        if not a:
            return R(0)
        return R(0, a[0].replace("\\n", "\n") % tuple(a[1:]) if "%" in a[0] and len(a) > 1 else a[0].replace("\\n", "\n"))

    def c_export(self, a):
        for x in a:
            if "=" in x:
                k, v = x.split("=", 1)
                self.env[k] = v
        return R(0)

    def c_unset(self, a):
        for x in a:
            self.env.pop(x, None)
        return R(0)

    def c_whoami(self, a):
        return R(0, "dev\n")

    def c_which(self, a):
        miss = [x for x in a if not self.has_cmd(x)]
        return R(1 if miss else 0, "".join(f"/usr/bin/{x}\n" for x in a if self.has_cmd(x)))

    c_type = c_which

    def c_less(self, a):
        return self.c_cat(a) if a else R(0)

    c_more = c_less

    def c_ls(self, a):
        flags = "".join(x[1:] for x in a if x.startswith("-") and not x.startswith("--"))
        paths = [x for x in a if not x.startswith("-")] or ["."]
        out, err, code = [], [], 0
        for p in paths:
            n = self.fs.lookup(self.abs(p))
            if n is None:
                r = self.e("ls.no_file", path=p); err.append(r.err); code = r.code; continue
            names = self.fs.listdir(self.abs(p)) if n.kind == "d" else [p]
            if n.kind == "d" and "a" not in flags and "A" not in flags:
                names = [x for x in names if not x.startswith(".")]
            if "a" in flags:
                names = [".", ".."] + names
            if len(paths) > 1 and n.kind == "d":
                out.append(f"{p}:")
            out.append("\n".join(names) if "l" in flags or "1" in flags else "  ".join(names))
        return R(code, "\n".join(o for o in out if o) + ("\n" if any(out) else ""), "".join(err))

    def c_mkdir(self, a):
        parents = "-p" in a or "--parents" in a
        out = R(0)
        for p in [x for x in a if not x.startswith("-")]:
            ap = self.abs(p)
            par = posixpath.dirname(ap)
            if self.fs.lookup(ap):
                if parents and self.fs.lookup(ap).kind == "d":
                    continue
                out = self.e("mkdir.exists", path=p); continue
            if not parents and not self.fs.lookup(par):
                out = self.e("mkdir.no_parent", path=p); continue
            anc = ap
            while not self.fs.lookup(posixpath.dirname(anc)):
                anc = posixpath.dirname(anc)
            if not self.can_write_dir(posixpath.dirname(anc)):
                out = self.e("mkdir.denied", path=p); continue
            self.fs.mkdir(ap, owner="root" if self.root and not self.can_write_dir(par) else "dev")
        return out

    def c_touch(self, a):
        out = R(0)
        for p in [x for x in a if not x.startswith("-")]:
            ap = self.abs(p)
            par = posixpath.dirname(ap)
            if self.fs.lookup(ap):
                continue
            if not self.fs.lookup(par):
                out = self.e("touch.no_dir", path=p); continue
            if not self.can_write_dir(par):
                out = self.e("touch.denied", path=p); continue
            self.fs.put(ap, "")
        return out

    def c_rm(self, a):
        flags = "".join(x[1:] for x in a if x.startswith("-"))
        rec, force = "r" in flags or "R" in flags, "f" in flags
        out = R(0)
        for p in [x for x in a if not x.startswith("-")]:
            ap = self.abs(p)
            n = self.fs.lookup(ap, follow=False)
            if n is None:
                if not force:
                    out = self.e("rm.no_file", path=p)
                continue
            if n.kind == "d" and not rec:
                out = self.e("rm.is_dir", path=p); continue
            par, name = self.fs.parent(ap)
            if not self.can_write_dir(posixpath.dirname(ap)):
                out = self.e("rm.denied", path=p); continue
            del par.children[name]
            if ap == self.cwd or self.cwd.startswith(ap + "/"):
                pass
        return out

    def c_rmdir(self, a):
        out = R(0)
        for p in a:
            ap = self.abs(p)
            n = self.fs.lookup(ap)
            if n is None:
                out = self.e("rmdir.no_file", path=p)
            elif n.kind != "d":
                out = self.e("rmdir.not_dir", path=p)
            elif n.children:
                out = self.e("rmdir.not_empty", path=p)
            else:
                par, name = self.fs.parent(ap); del par.children[name]
        return out

    def _cp_mv(self, a, move):
        flags = "".join(x[1:] for x in a if x.startswith("-"))
        rec = "r" in flags or "R" in flags or "a" in flags
        paths = [x for x in a if not x.startswith("-")]
        if len(paths) < 2:
            raise Unsupported("cp/mv args")
        *srcs, dst = paths
        dabs = self.abs(dst)
        dn = self.fs.lookup(dabs)
        if dst.endswith("/") and not (dn and dn.kind == "d"):
            raise Unsupported("cp/mv to missing dir/")
        if len(srcs) > 1 and not (dn and dn.kind == "d"):
            raise Unsupported("multi-src to non-dir")
        key = "mv" if move else "cp"
        for s in srcs:
            sabs = self.abs(s)
            sn = self.fs.lookup(sabs, follow=False)
            if sn is None:
                return self.e(f"{key}.no_file", path=s)
            if sn.kind == "d" and not rec and not move:
                return self.e("cp.omit_dir", path=s)
            target = f"{dabs}/{posixpath.basename(sabs)}" if dn and dn.kind == "d" else dabs
            if not self.can_write_dir(posixpath.dirname(target)):
                return self.e(f"{key}.denied", target=target, src=s)
            if not self.fs.lookup(posixpath.dirname(target)):
                return self.e(f"{key}.no_dir", path=dst, src=s)
            if move and sabs == target:
                continue
            if sn.kind == "d" and (target + "/").startswith(sabs + "/"):
                raise Unsupported("copy into self")
            self.fs.mkdir(posixpath.dirname(target))
            self.fs.lookup(posixpath.dirname(target)).children[posixpath.basename(target)] = sn.clone()
            if move:
                par, name = self.fs.parent(sabs); del par.children[name]
        return R(0)

    def c_cp(self, a):
        return self._cp_mv(a, False)

    def c_mv(self, a):
        return self._cp_mv(a, True)

    def c_cat(self, a):
        out, err, code = [], [], 0
        for p in [x for x in a if not x.startswith("-")]:
            n = self.fs.lookup(self.abs(p))
            if n is None:
                r = self.e("cat.no_file", path=p); err.append(r.err); code = r.code
            elif n.kind == "d":
                r = self.e("cat.is_dir", path=p); err.append(r.err); code = r.code
            else:
                out.append(n.content)
        return R(code, "".join(out), "".join(err))

    def c_head(self, a):
        n = 10
        a2 = list(a)
        if "-n" in a2:
            i = a2.index("-n"); n = int(a2[i + 1]); del a2[i:i + 2]
        r = self.c_cat(a2)
        return R(r.code, "".join(r.out.splitlines(True)[:n]), r.err)

    def c_tail(self, a):
        if "-f" in a or "-F" in a:
            self.c_cat([x for x in a if not x.startswith("-")])
            return R(130, "", "", long=True)
        n = 10
        a2 = list(a)
        if "-n" in a2:
            i = a2.index("-n"); n = int(a2[i + 1]); del a2[i:i + 2]
        r = self.c_cat(a2)
        return R(r.code, "".join(r.out.splitlines(True)[-n:]), r.err)

    def c_wc(self, a):
        r = self.c_cat([x for x in a if not x.startswith("-")])
        return R(r.code, f"{len(r.out.splitlines())}\n" if "-l" in a else f"{len(r.out.split())}\n", r.err)

    def c_grep(self, a):
        flags = "".join(x[1:] for x in a if x.startswith("-"))
        rest = [x for x in a if not x.startswith("-")]
        if not rest:
            raise Unsupported("grep args")
        pat, files = rest[0], rest[1:]
        rx = re.compile(pat, re.I if "i" in flags else 0)
        hits, code = [], 1
        todo = []
        for f in files or ["."]:
            n = self.fs.lookup(self.abs(f))
            if n is None:
                return self.e("grep.no_file", path=f)
            if n.kind == "d":
                if "r" not in flags and "R" not in flags:
                    return R(2, "", f"grep: {f}: Is a directory\n")
                todo += [(p[len(self.abs(f)) + 1:] and posixpath.join(f, p[len(self.abs(f)) + 1:]), x) for p, x in self.fs.walk(self.abs(f)) if x.kind == "f" and "/.git/" not in p]
            else:
                todo.append((f, n))
        for name, n in todo:
            for l in n.content.splitlines():
                if bool(rx.search(l)) != ("v" in flags):
                    hits.append((f"{name}:" if len(todo) > 1 else "") + l + "\n"); code = 0
        return R(code, "".join(hits))

    def c_find(self, a):
        root = a[0] if a and not a[0].startswith("-") else "."
        name = typ = None
        maxd = 99
        i = 1 if a and not a[0].startswith("-") else 0
        while i < len(a):
            if a[i] == "-name": name = a[i + 1]; i += 2
            elif a[i] == "-type": typ = a[i + 1]; i += 2
            elif a[i] == "-maxdepth": maxd = int(a[i + 1]); i += 2
            else: raise Unsupported(f"find {a[i]}")
        base = self.abs(root)
        if not self.fs.lookup(base):
            return R(1, "", f"find: ‘{root}’: No such file or directory\n")
        out = []
        for p, n in sorted(self.fs.walk(base)):
            rel = p[len(base):]
            if rel.count("/") > maxd:
                continue
            if name and not fnmatch.fnmatchcase(posixpath.basename(p), name):
                continue
            if typ and ((typ == "f") != (n.kind == "f") if typ in "fd" and (typ == "f" or typ == "d") and (typ == "f") != (n.kind != "d") else False):
                continue
            out.append(root.rstrip("/") + rel if rel else root)
        return R(0, "\n".join(out) + ("\n" if out else ""))

    def c_chmod(self, a):
        args = [x for x in a if not x.startswith("-") or x[1:2] in "+-"]
        mode, files = args[0], args[1:]
        for f in files:
            n = self.fs.lookup(self.abs(f))
            if n is None:
                return self.e("chmod.no_file", path=f)
            if n.owner != "dev" and not self.root:
                return self.e("chmod.denied", path=f)
            if re.fullmatch(r"[0-7]{3,4}", mode):
                n.mode = int(mode, 8)
            elif m := re.fullmatch(r"([ugoa]*)([+-])([rwx]+)", mode):
                bits = (0o111 if "x" in m.group(3) else 0)
                n.mode = (n.mode | bits) if m.group(2) == "+" else (n.mode & ~bits)
            else:
                raise Unsupported("chmod mode")
        return R(0)

    def c_ln(self, a):
        paths = [x for x in a if not x.startswith("-")]
        if "-s" not in a and "-sf" not in a or len(paths) != 2:
            raise Unsupported("ln form")
        d = self.abs(paths[1])
        if self.fs.lookup(d, follow=False):
            return self.e("ln.exists", path=paths[1])
        self.fs.lookup(posixpath.dirname(d)).children[posixpath.basename(d)] = Node("l", target=paths[0])
        return R(0)

    def c_source(self, a):
        if not a:
            raise Unsupported("source args")
        n = self.fs.lookup(self.abs(a[0]))
        if n is None:
            return self.e("source.no_file", path=a[0])
        if n.kind != "f":
            raise Unsupported("source non-file")
        if n.content.startswith("# venv-activate:"):
            self.venv = n.content.split(":", 1)[1].strip()
            self.env["VIRTUAL_ENV"] = self.venv
            return R(0)
        cwd = self.cwd
        r = self.run_script(n.content)
        self.cwd = cwd if False else self.cwd
        return r

    c_dot = c_source

    def c_bash(self, a):
        files = [x for x in a if not x.startswith("-")]
        if not files:
            raise Unsupported("interactive shell")
        n = self.fs.lookup(self.abs(files[0]))
        if n is None:
            if a is None or self._shname != "bash":
                raise Unsupported("sh script missing")
            return self.e("sh.script_nofile", path=files[0])
        return self.run_script(n.content)

    def c_sh(self, a):
        self._shname = "sh"
        try:
            return self.c_bash(a)
        finally:
            self._shname = "bash"

    c_zsh = c_sh

    # ------------------------------------------------------------ archives & downloads
    def c_tar(self, a):
        if not a:
            raise Unsupported("tar args")
        first = a[0]
        flags = first.lstrip("-") if not first.startswith("--") else ""
        rest = a[1:] if flags else a
        cdir, fname, names = None, None, []
        i = 0
        # option letters may demand an argument: f -> archive, C -> dir (in letter order)
        need = [ch for ch in flags if ch in "fC"]
        for ch in need:
            if ch == "f":
                fname = rest[i]; i += 1
            else:
                cdir = rest[i]; i += 1
        names = rest[i:]
        j = 0
        while j < len(names):  # trailing -C dir
            if names[j] == "-C":
                cdir = names[j + 1]; del names[j:j + 2]
            else:
                j += 1
        if fname is None:
            raise Unsupported("tar without -f")
        base = self.abs(cdir) if cdir else self.cwd
        if "c" in flags:
            kind = "gz" if "z" in flags else "xz" if "J" in flags else "bz2" if "j" in flags else "tar"
            entries = {}
            for nm in names:
                src = self.fs.lookup(self.abs(nm) if cdir is None else f"{base}/{nm}")
                if src is None:
                    return self.e("tar.no_such", path=nm)
                entries[posixpath.basename(nm.rstrip("/"))] = src.clone()
            node = Node("f", "<archive>", meta={"kind": kind, "entries": entries})
            if not self.can_write_dir(posixpath.dirname(self.abs(fname))):
                raise Unsupported("tar create denied")
            self.fs.put(self.abs(fname))
            self.fs.lookup(self.abs(fname)).meta = node.meta
            return R(0)
        n = self.fs.lookup(self.abs(fname))
        if n is None:
            return self.e("tar.no_file", file=fname)
        meta = n.meta if isinstance(n.meta, dict) and "entries" in n.meta else None
        want = "gz" if "z" in flags else "xz" if "J" in flags else "bz2" if "j" in flags else None
        if meta is None or meta["kind"] == "zip" or (want and want != meta["kind"]):
            if want in ("xz", "bz2"):
                raise Unsupported("tar J/j mismatch")
            return self.e("tar.not_gzip", file=fname) if want == "gz" else self.e("tar.not_tar", file=fname)
        if "t" in flags:
            return R(0, "".join(f"{k}/\n" for k in meta["entries"]))
        if not self.fs.lookup(base):
            raise Unsupported("tar -C missing dir")
        for nm, node in meta["entries"].items():
            self.fs.lookup(base).children[nm] = node.clone()
        return R(0)

    def c_zip(self, a):
        rec = "-r" in a
        paths = [x for x in a if not x.startswith("-")]
        if len(paths) < 2:
            raise Unsupported("zip args")
        out, *srcs = paths
        out = out if out.endswith(".zip") else out + ".zip"
        entries = {}
        for s in srcs:
            n = self.fs.lookup(self.abs(s))
            if n is None:
                return self.e("zip.no_file", path=s, out=out)
            entries[posixpath.basename(s.rstrip("/"))] = n.clone()
        self.fs.put(self.abs(out))
        self.fs.lookup(self.abs(out)).meta = {"kind": "zip", "entries": entries}
        return R(0)

    def c_unzip(self, a):
        dest = None
        a2 = list(a)
        if "-d" in a2:
            i = a2.index("-d"); dest = a2[i + 1]; del a2[i:i + 2]
        files = [x for x in a2 if not x.startswith("-")]
        if len(files) != 1:
            raise Unsupported("unzip args")
        f = files[0]
        n = self.fs.lookup(self.abs(f)) or self.fs.lookup(self.abs(f + ".zip"))
        if n is None:
            return self.e("unzip.no_file", file=f)
        if not (isinstance(n.meta, dict) and n.meta.get("kind") == "zip"):
            return self.e("unzip.not_zip", file=f)
        base = self.abs(dest) if dest else self.cwd
        self.fs.mkdir(base)
        for nm, node in n.meta["entries"].items():
            self.fs.lookup(base).children[nm] = node.clone()
        return R(0)

    def _download(self, url, out):
        m = re.match(r"https?://([^/]+)/?(.*)$", url)
        if not m:
            raise Unsupported("url")
        base = posixpath.basename(m.group(2).split("?")[0])
        src = self.fs.lookup(f"/srv/dl/{base}") if base else None
        if src is None:
            return None, m.group(1)
        dst = self.abs(out or base)
        d = self.fs.lookup(dst)
        if d and d.kind == "d":
            dst = f"{dst}/{base}"
        self.fs.lookup(posixpath.dirname(dst)).children[posixpath.basename(dst)] = src.clone()
        return True, m.group(1)

    def c_wget(self, a):
        out, urls, i = None, [], 0
        while i < len(a):
            if a[i] in ("-O", "--output-document"):
                out = a[i + 1]; i += 1
            elif a[i] in ("-P", "--directory-prefix", "-t", "--tries", "-T"):
                i += 1
            elif not a[i].startswith("-"):
                urls.append(a[i])
            i += 1
        if len(urls) != 1:
            raise Unsupported("wget urls")
        ok, host = self._download(urls[0], out)
        return R(0) if ok else self.e("wget.dns", url=urls[0], host=host, ts="2026-01-01 00:00:00")

    def c_curl(self, a):
        out, urls, rname, i = None, [], False, 0
        while i < len(a):
            x = a[i]
            if x in ("-o", "--output"):
                out = a[i + 1]; i += 1
            elif x in ("-H", "-X", "-d", "--data", "-u", "-A", "--retry"):
                i += 1
            elif x == "--remote-name" or (x.startswith("-") and not x.startswith("--") and "O" in x):
                rname = True
            elif not x.startswith("-"):
                urls.append(x)
            i += 1
        if len(urls) != 1:
            raise Unsupported("curl urls")
        if out or rname:
            ok, host = self._download(urls[0], out)
            return R(0) if ok else self.curl_dns(a, host)
        m = re.match(r"https?://([^/]+)", urls[0])
        if not m:
            raise Unsupported("curl url")
        if self.fs.lookup(f"/srv/dl/{posixpath.basename(urls[0])}"):
            raise Unsupported("curl to stdout of fixture")
        return self.curl_dns(a, m.group(1))

    def curl_dns(self, a, host):
        r = self.e("curl.dns", host=host)
        if any(x.startswith("-") and not x.startswith("--") and "s" in x for x in a):
            r.err = r.err.splitlines()[-1].split("curl:")[-1].join(["curl:", "\n"]) if "curl:" in r.err else r.err
        return r

    # ------------------------------------------------------------ python / pip / venv
    def py_site(self, venv):
        return f"{venv}/lib/python3.12/site-packages" if venv else None

    def mod_installed(self, mod, venv):
        if mod in sys.stdlib_module_names:
            return True
        if venv:
            return self.fs.lookup(f"{self.py_site(venv)}/{mod}") is not None
        return mod in self.sys_pkgs

    def c_python(self, a):
        return self.python(a, self.venv, "python")

    def c_python3(self, a):
        return self.python(a, self.venv, "python3")

    def venv_tool(self, c, a, venv, argv0=None):
        if c.startswith("python"):
            return self.python(a, venv, argv0 or c)
        if c.startswith("pip"):
            return self.pip(a, venv)
        self.v = True
        return R(0, "ok\n")

    def python(self, a, venv, py):
        if a and a[0] in ("--version", "-V"):
            return R(0, "Python 3.12.3\n")
        if a[:1] == ["-m"]:
            mod, rest = a[1], a[2:]
            if mod == "venv":
                return self.make_venv(rest)
            if mod == "pip":
                return self.pip(rest, venv)
            if mod == "http.server":
                port = int(rest[0]) if rest and rest[0].isdigit() else 8000
                if port in self.ports:
                    return self.e("python.port_in_use", port=port, py=py)
                return R(130, f"Serving HTTP on 0.0.0.0 port {port} (http://0.0.0.0:{port}/) ...\n", "\nKeyboard interrupt received, exiting.\n", long=True)
            if self.mod_installed(mod.split(".")[0], venv):
                return R(0, "ok\n")
            return self.e("python.no_module_m", module=mod, py=py)
        if not a or a[0].startswith("-"):
            raise Unsupported("python args")
        f = a[0]
        n = self.fs.lookup(self.abs(f))
        if n is None or n.kind != "f":
            return self.e("python.no_file", path=self.abs(f), py=py) if n is None else R(2, "", f"{py}: can't find '__main__' module in '{self.abs(f)}'\n")
        for lineno, line in enumerate(n.content.splitlines(), 1):
            m = re.match(r"\s*(?:import|from)\s+([A-Za-z_]\w*)", line)
            if m:
                mod = m.group(1)
                local = self.fs.lookup(f"{posixpath.dirname(self.abs(f))}/{mod}.py") or self.fs.lookup(f"{posixpath.dirname(self.abs(f))}/{mod}")
                if not local and not self.mod_installed(mod, venv):
                    return self.e("python.no_module", file=self.abs(f), lineno=lineno, src=line.strip(), module=mod)
        return R(0, "ok\n" if "print" in n.content else "")

    def make_venv(self, rest):
        dirs = [x for x in rest if not x.startswith("-")]
        if len(dirs) != 1:
            raise Unsupported("venv args")
        d = self.abs(dirs[0])
        par = posixpath.dirname(d)
        if not self.fs.lookup(par) or not self.can_write_dir(par):
            raise Unsupported("venv parent")
        vn = self.fs.mkdir(d)
        if "--clear" in rest:
            vn.children.clear()
        for b in ("python", "python3", "python3.12"):
            vn.children.setdefault("bin", Node("d", mode=0o755)).children[b] = Node("l", target="/usr/bin/python3")
        b = vn.children["bin"]
        for f in ("Activate.ps1", "activate.csh", "activate.fish", "pip", "pip3", "pip3.12"):
            b.children[f] = Node("f", "", 0o755)
        b.children["activate"] = Node("f", f"# venv-activate:{d}\n", 0o644)
        vn.children.setdefault("include", Node("d", mode=0o755))
        vn.children["lib64"] = Node("l", target="lib")
        vn.children["pyvenv.cfg"] = Node("f", "home = /usr/bin\n")
        self.fs.mkdir(f"{d}/lib/python3.12/site-packages")
        sp = self.fs.lookup(f"{d}/lib/python3.12/site-packages")
        sp.children["pip"] = Node("d", mode=0o755)
        return R(0)

    def c_pip(self, a):
        return self.pip(a, self.venv)

    c_pip3 = c_pip

    def pip(self, a, venv):
        if not a:
            raise Unsupported("pip args")
        if a[0] in ("--version", "list", "freeze", "show", "check"):
            return R(0)
        if a[0] != "install":
            raise Unsupported(f"pip {a[0]}")
        flags = [x for x in a[1:] if x.startswith("-")]
        if "--break-system-packages" in flags and not venv:
            venv_sys = True
        elif not venv:
            return self.e("pip.external")
        else:
            venv_sys = False
        pkgs, i = [], 1
        while i < len(a):
            if a[i] == "-r":
                n = self.fs.lookup(self.abs(a[i + 1]))
                if n is None:
                    return self.e("pip.no_req", path=a[i + 1])
                pkgs += [re.split(r"[=<>~! ;\[]", l.strip())[0] for l in n.content.splitlines() if l.strip() and not l.startswith("#")]
                i += 1
            elif a[i] in ("-e", "--editable", "-c", "-i", "--index-url", "-f", "--find-links", "--target", "-t"):
                raise Unsupported("pip flag")
            elif not a[i].startswith("-"):
                pkgs.append(re.split(r"[=<>~! ;\[]", a[i])[0])
            i += 1
        if not pkgs and "-r" not in a:
            raise Unsupported("pip install nothing")
        for p in pkgs:
            norm = p.lower().replace("_", "-")
            if norm not in FX.PIP_PKGS:
                return self.e("pip.no_dist", pkg=p)
        for p in pkgs:
            norm = p.lower().replace("_", "-")
            mod = FX.PIP_PKGS[norm]
            if venv_sys:
                self.sys_pkgs.add(mod)
                continue
            sp = self.fs.lookup(self.py_site(venv))
            sp.children[mod] = Node("d", mode=0o755)
            sp.children[f"{norm.replace('-', '_')}-1.0.0.dist-info"] = Node("d", mode=0o755)
            if norm in FX.CONSOLE_SCRIPTS:
                self.fs.lookup(f"{venv}/bin").children[norm] = Node("f", "", 0o755)
        return R(0, "")

    # ------------------------------------------------------------ node / npm
    def c_node(self, a):
        f = [x for x in a if not x.startswith("-")]
        if not f:
            raise Unsupported("node repl")
        n = self.fs.lookup(self.abs(f[0]))
        if n is None:
            return self.e("node.no_module", path=self.abs(f[0]), file=f[0])
        for m in re.finditer(r"mkdirSync\('([^']+)'", n.content):
            self.fs.mkdir(self.abs(m.group(1)))
        for m in re.finditer(r"writeFileSync\('([^']+)'", n.content):
            self.write_file(self.abs(m.group(1)), "//")
        return R(0, "hello\n" if "hello" in n.content else "ok\n" if "ok" in n.content else "")

    def pkg_json(self):
        n = self.fs.lookup(f"{self.cwd}/package.json")
        return json.loads(n.content) if n and n.kind == "f" else None

    def c_npm(self, a):
        if not a:
            raise Unsupported("npm bare")
        sub, rest = a[0], a[1:]
        if sub == "init":
            if "-y" not in rest and "--yes" not in rest:
                raise Unsupported("npm init interactive")
            name = posixpath.basename(self.cwd).lower()
            pj = {"name": name, "version": "1.0.0", "description": "", "main": "index.js",
                  "scripts": {"test": 'echo "Error: no test specified" && exit 1'}, "keywords": [], "author": "", "license": "ISC"}
            self.write_file(f"{self.cwd}/package.json", json.dumps(pj, indent=2) + "\n")
            return R(0)
        if sub in ("install", "i", "add"):
            if any(x in ("-g", "--global") for x in rest):
                pk = [x for x in rest if not x.startswith("-")]
                raise Unsupported("npm -g")
            pj = self.pkg_json()
            if pj is None:
                if not self.fs.lookup(f"{self.cwd}/package-lock.json"):
                    self.write_file(f"{self.cwd}/package-lock.json", "{}")  # real npm leaves a stub lockfile behind
                return self.e("npm.no_package_json", ts="2026-01-01T00_00_00_000Z")
            pk = [x.split("@")[0] if not x.startswith("@") else x for x in rest if not x.startswith("-")]
            want = pk or list(pj.get("dependencies", {})) + list(pj.get("devDependencies", {}))
            for w in want:
                if w not in FX.NPM_PKGS:
                    return self.e("npm.network", pkg=w, ts="2026-01-01T00_00_00_000Z")
            for w in want:
                self.fs.mkdir(f"{self.cwd}/node_modules/{w}")
            dev = any(x in ("-D", "--save-dev") for x in rest)
            for w in pk:
                pj.setdefault("devDependencies" if dev else "dependencies", {})[w] = "^1.0.0"
            if pk:
                self.write_file(f"{self.cwd}/package.json", json.dumps(pj, indent=2) + "\n")
            self.write_file(f"{self.cwd}/package-lock.json", json.dumps({"name": pj.get("name", "x"), "lockfileVersion": 3}))
            self.write_file(f"{self.cwd}/node_modules/.package-lock.json", "{}")
            return R(0, f"\nadded {len(want)} packages, and audited {len(want) + 1} packages in 1s\n\nfound 0 vulnerabilities\n")
        if sub in ("run", "start", "test", "run-script", "t", "build"):
            script = rest[0] if sub in ("run", "run-script") and rest else ("test" if sub == "t" else sub)
            pj = self.pkg_json()
            if pj is None:
                return self.e("npm.run_no_package_json", ts="2026-01-01T00_00_00_000Z")
            cmd = pj.get("scripts", {}).get(script)
            if cmd is None:
                if script == "start" and self.fs.lookup(f"{self.cwd}/server.js"):
                    cmd = "node server.js"
                else:
                    return self.e("npm.missing_script", script=script, ts="2026-01-01T00_00_00_000Z")
            r = self.run(cmd)
            self.v = True
            if r.code and not r.long:
                r = R(r.code, r.out, r.err + f"npm ERR! Test failed.  See above for more details.\n" if script == "test" else r.err)
            return r
        raise Unsupported(f"npm {sub}")

    # ------------------------------------------------------------ build tools
    def c_gcc(self, a):
        out, srcs, i = None, [], 0
        while i < len(a):
            if a[i] == "-o":
                out = a[i + 1]; i += 1
            elif not a[i].startswith("-"):
                srcs.append(a[i])
            i += 1
        if not srcs:
            return self.e("gcc.no_input")
        for s in srcs:
            if not self.fs.lookup(self.abs(s)):
                return self.e("gcc.no_file", file=s)
        self.fs.put(self.abs(out or "a.out"), "<elf>", 0o755)
        return R(0)

    c_cc = c_gcc

    def c_g__(self, a):
        return self.c_gcc(a)

    def makefile_rules(self, text):
        rules, vars_, cur, phony = {}, {}, None, set()
        for l in text.splitlines():
            if l.startswith("\t") and cur:
                rules[cur][1].append(l[1:])
            elif m := re.match(r"(\w+)\s*=\s*(.*)", l):
                vars_[m.group(1)] = m.group(2)
            elif l.startswith(".PHONY:"):
                phony |= set(l.split(":", 1)[1].split())
            elif m := re.match(r"([\w./-]+)\s*:\s*(.*)", l):
                cur = m.group(1); rules[cur] = (m.group(2).split(), [])
        return rules, vars_, phony

    def c_make(self, a):
        tgts = [x for x in a if not x.startswith("-")]
        mk = self.fs.lookup(f"{self.cwd}/Makefile") or self.fs.lookup(f"{self.cwd}/makefile")
        if mk is None:
            return self.e("make.no_makefile") if not tgts else self.e("make.no_rule", target=tgts[0])
        if mk.content.startswith("# cmake"):
            self.fs.put(f"{self.cwd}/app", "<elf>", 0o755)
            return R(0, "[100%] Built target app\n")
        rules, vars_, phony = self.makefile_rules(mk.content)
        out = []
        errs = [None]

        def build(t, top):
            if t not in rules:
                if self.fs.lookup(self.abs(t)):
                    return False
                errs[0] = self.e("make.no_rule_needed", target=t, parent=top); return None
            deps, recipe = rules[t]
            did = False
            for d in deps:
                r = build(d, t)
                if r is None:
                    return None
                did = did or r
            exists = self.fs.lookup(self.abs(t)) is not None
            if t in phony or not exists:
                if not recipe and t in phony:
                    return did
                for line in recipe:
                    for k, v in vars_.items():
                        line = line.replace(f"$({k})", v)
                    out.append(line + "\n")
                    r = self.run(line.lstrip("@-"))
                    out.append(r.out)
                    if r.code:
                        errs[0] = R(2, "".join(out), r.err + f"make: *** [Makefile:1: {t}] Error {r.code}\n"); return None
                return True
            return did

        target = tgts[0] if tgts else next(iter(rules), None)
        if target is None:
            return self.e("make.no_makefile")
        res = build(target, None)
        if res is None:
            return errs[0]
        if res is False:
            msg = f"make: '{target}' is up to date.\n" if target not in phony else f"make: Nothing to be done for '{target}'.\n"
            out.append(msg)
        return R(0, "".join(out))

    def c_cmake(self, a):
        if "--build" in a:
            d = a[a.index("--build") + 1]
            n = self.fs.lookup(self.abs(d))
            if n is None or n.kind != "d":
                return self.e("cmake.build_nodir", dir=self.abs(d))
            mk = n.children.get("Makefile")
            if mk is None:
                return self.e("cmake.build_nocache", dir=self.abs(d))
            self.fs.put(f"{self.abs(d)}/app", "<elf>", 0o755)
            return R(0, "[100%] Built target app\n")
        src, build, i = None, None, 0
        while i < len(a):
            if a[i] == "-S":
                src = a[i + 1]; i += 1
            elif a[i] == "-B":
                build = a[i + 1]; i += 1
            elif a[i].startswith("-D") or a[i] in ("-G",):
                i += 1 if a[i] == "-G" else 0
            elif not a[i].startswith("-"):
                src = a[i]
            i += 1
        s = self.abs(src or ".")
        if build is None:
            build = "."
        elif src is None:
            src = "."
        if not self.fs.lookup(f"{s}/CMakeLists.txt"):
            if src is not None:
                return self.e("cmake.no_source", src=s)
            if a and a[-1] in (".", ".."):
                return self.e("cmake.no_source_dot" if a[-1] == "." else "cmake.no_source_up", src=s)
            raise Unsupported("cmake no source form")
        b = self.abs(build)
        self.fs.mkdir(b)
        for nm, node in (("CMakeCache.txt", Node("f", "")), ("CMakeFiles", Node("d", mode=0o755)), ("Makefile", Node("f", f"# cmake\n# src={s}\n")), ("cmake_install.cmake", Node("f", ""))):
            self.fs.lookup(b).children[nm] = node
        return R(0, "-- Configuring done\n-- Generating done\n")

    def c_cargo(self, a):
        if not a:
            raise Unsupported("cargo bare")
        sub, rest = a[0], [x for x in a[1:] if not x.startswith("-")]
        if sub in ("new", "init"):
            name = rest[0] if rest else posixpath.basename(self.cwd)
            d = self.abs(rest[0]) if sub == "new" else self.cwd
            if sub == "new" and self.fs.lookup(d):
                return self.e("cargo.exists", path=d, name=name)
            self.fs.mkdir(d)
            files = FX.project_files("rust", name)
            for p in ("Cargo.toml", "src/main.rs"):
                self.fs.put(f"{d}/{p}", files[p])
            if not G.find_repo(self, d)[1]:
                G.init_repo(self, d, branch="master")
                self.fs.put(f"{d}/.gitignore", "/target\n")
            return R(0, "", f"     Created binary (application) `{name}` package\n")
        if sub in ("build", "run", "test", "check", "clean"):
            p = self.cwd
            while p != "/" and not self.fs.lookup(f"{p}/Cargo.toml"):
                p = posixpath.dirname(p)
            if p == "/":
                return self.e("cargo.no_manifest")
            if sub == "clean":
                par, _ = self.fs.parent(f"{p}/target")
                par.children.pop("target", None)
                return R(0)
            self.fs.mkdir(f"{p}/target/debug")
            self.fs.lookup(f"{p}/target").children["CACHEDIR.TAG"] = Node("f", "")
            self.fs.put(f"{p}/Cargo.lock", "# lock\n")
            return R(0, "hi\n" if sub == "run" else "")
        raise Unsupported(f"cargo {sub}")

    # ------------------------------------------------------------ setup helpers (mirror docker/bin/fx_*)
    def c_fx_remote(self, a):
        G.fx_remote(self, a[0], a[1] if len(a) > 1 else "docs", a[2:])
        return R(0)

    def c_fx_archive(self, a):
        fn, lang = a[0], (a[1] if len(a) > 1 else "docs")
        top, kind = fn, "tar"
        for ext, k in ((".tar.gz", "gz"), (".tgz", "gz"), (".tar.xz", "xz"), (".tar.bz2", "bz2"), (".zip", "zip")):
            if fn.endswith(ext):
                top, kind = fn[: -len(ext)], k
        root = Node("d", mode=0o755)
        for p, c in FX.project_files(lang, top).items():
            d = root
            parts = p.split("/")
            for q in parts[:-1]:
                d = d.children.setdefault(q, Node("d", mode=0o755))
            d.children[parts[-1]] = Node("f", c)
        self.fs.put(f"/srv/dl/{fn}")
        self.fs.lookup(f"/srv/dl/{fn}").meta = {"kind": kind, "entries": {top: root}}
        return R(0)

    def c_fx_project(self, a):
        d, lang = self.abs(a[0]), (a[1] if len(a) > 1 else "docs")
        for p, c in FX.project_files(lang, posixpath.basename(d)).items():
            self.fs.put(f"{d}/{p}", c)
        if "git" in a[2:]:
            repo = G.init_repo(self, d)
            G.commit_all_files(self, d, repo, "Initial commit")
        return R(0)

    def c_fx_bare(self, a):
        G.init_repo(self, f"/srv/git/{a[0]}.git", bare=True)
        return R(0)

    def c_pnpm(self, a):
        self.v = False
        return self.c_npm(npm_equiv(a))

    c_yarn = c_pnpm

    def c_fx_port(self, a):
        self.ports.add(int(a[0]))
        return R(0)

    # ------------------------------------------------------------ git
    def c_git(self, a):
        from .gitcmd import git
        return git(self, a)


def npm_equiv(a):
    """pnpm/yarn -> the npm command with the same effect (sandbox has neither; steps are marked unvalidated)."""
    if not a or a[0] in ("install", "i"):
        return ["install"] + a[1:]
    if a[0] in ("add",):
        return ["install"] + a[1:]
    if a[0] in ("run",):
        return a
    if a[0] in ("start", "test", "build", "dev"):
        return ["run"] + a if a[0] in ("build", "dev") else a
    raise Unsupported("pnpm/yarn form")


def new_session(shell="bash"):
    return Sim(shell)
