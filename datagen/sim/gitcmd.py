"""git subcommands on top of the snapshot model in git.py."""
import posixpath
import re

from . import git as G
from .errors import Unsupported
from .fs import Node

URL_PREFIXES = ("https://github.com/", "git@github.com:", "https://gitlab.com/", "git@gitlab.com:")


def R(*a, **k):
    from .shell import R as _R
    return _R(*a, **k)


def short(cid):
    return cid[:7]


def rel(sim, root, arg):
    p = sim.abs(arg)
    if p != root and not p.startswith(root + "/"):
        raise Unsupported("path outside repo")
    return "" if p == root else p[len(root) + 1:]


def under(path, pref):
    return pref == "" or path == pref or path.startswith(pref + "/")


def remote_repo(sim, repo, name):
    p = repo.remotes.get(name)
    n = sim.fs.lookup(p) if p else None
    return n.meta if n is not None and isinstance(n.meta, G.Repo) else None


def url_of(repo, name):
    return getattr(repo, "urls", {}).get(name) or repo.remotes[name]


def fetch(sim, repo, name):
    rr = remote_repo(sim, repo, name)
    if rr is None:
        return False
    repo.commits.update(rr.commits)
    for k in [k for k in repo.remote_refs if k.startswith(name + "/")]:
        if k[len(name) + 1:] not in rr.branches:
            del repo.remote_refs[k]
    for b, c in rr.branches.items():
        repo.remote_refs[f"{name}/{b}"] = c
    return True


def resolve_commit(sim, repo, spec):
    base, _, n = spec.partition("~")
    cid = repo.head_id() if base in ("HEAD", "") else repo.branches.get(base) or repo.remote_refs.get(base) or (base if base in repo.commits else None)
    for _ in range(int(n or 0) if n.isdigit() or not n else 0):
        cid = repo.commits[cid][0][0] if cid and repo.commits[cid][0] else None
    return cid


def changed_between(a, b):
    return {p for p in set(a) | set(b) if a.get(p) != b.get(p)}


def dirty_paths(sim, root, repo):
    return {p for xy, p in G.status_entries(sim, root, repo) if xy != "??" and " -> " not in p}


def git(sim, a):
    a = list(a)
    saved_cwd = sim.cwd
    try:
        while a and a[0] in ("-C", "-c", "--no-pager", "-P"):
            if a[0] == "-C":
                sim.cwd = sim.abs(a[1]); del a[:2]
            elif a[0] == "-c":
                del a[:2]
            else:
                del a[:1]
        if not a:
            raise Unsupported("bare git")
        sub, args = a[0], a[1:]
        if sub in ("--version", "version"):
            return R(0, "git version 2.43.0\n")
        fn = SUBS.get(sub)
        if fn is None:
            return sim.e("git.typo." + sub, cmd=sub)
        quiet = sub in QUIET_OK and any(x in ("-q", "--quiet") for x in args)
        if quiet:
            args = [x for x in args if x not in ("-q", "--quiet")]
        if sub in ("init", "clone", "config"):
            res = fn(sim, args)
        else:
            root, repo = G.find_repo(sim)
            if repo is None:
                return sim.e("git.not_repo")
            res = fn(sim, args, root, repo)
        if quiet and res.code == 0:
            res.out = res.err = ""
        return res
    finally:
        sim.cwd = saved_cwd


# ---------------------------------------------------------------- init / clone / config
def c_init(sim, args):
    branch, d, bare = "main", ".", False
    i = 0
    while i < len(args):
        if args[i] in ("-b", "--initial-branch"):
            branch = args[i + 1]; i += 1
        elif args[i] == "--bare":
            bare = True
        elif not args[i].startswith("-"):
            d = args[i]
        i += 1
    p = sim.abs(d)
    again = sim.fs.lookup(p) is not None and ".git" in (sim.fs.lookup(p).children or {})
    if sim.fs.lookup(p) is None and not sim.can_write_dir(posixpath.dirname(p)):
        raise Unsupported("init denied")
    if again:
        return R(0, f"Reinitialized existing Git repository in {p}/.git/\n")
    G.init_repo(sim, p, branch, bare)
    return R(0, f"Initialized empty Git repository in {p}/.git/\n")


def c_clone(sim, args):
    branch, pos, i = None, [], 0
    while i < len(args):
        if args[i] in ("-b", "--branch"):
            branch = args[i + 1]; i += 1
        elif args[i] in ("--depth", "--origin", "-o"):
            i += 1
        elif not args[i].startswith("-"):
            pos.append(args[i])
        i += 1
    if not pos:
        raise Unsupported("clone args")
    url = pos[0]
    name = pos[1] if len(pos) > 1 else re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1].split(":")[-1])
    dest = sim.abs(name)
    n = sim.fs.lookup(dest)
    if n is not None and (n.kind != "d" or n.children):
        return sim.e("git.clone.exists", dir=name)
    if not sim.can_write_dir(posixpath.dirname(dest)):
        raise Unsupported("clone denied")
    repo = G.clone(sim, url, dest, branch)
    if repo is None:
        raise Unsupported("clone source unknown")
    repo.urls = {"origin": url}
    if branch and repo.head != branch:
        raise Unsupported("clone -b missing")
    return R(0, "", f"Cloning into '{name}'...\n")


def c_config(sim, args):
    a = [x for x in args if not x.startswith("--")]
    if len(a) == 2:
        return R(0)
    raise Unsupported("git config read")


# ---------------------------------------------------------------- status / add / commit
def c_status(sim, args, root, repo):
    flags = "".join(x[1:] for x in args if x.startswith("-") and not x.startswith("--")) + " ".join(x for x in args if x.startswith("--"))
    if "porcelain" in flags or "s" in flags.split(" ")[0] or "short" in flags or "b" in flags.split(" ")[0]:
        lines = G.short_status(sim, root, repo)
        if "b" in flags.split(" ")[0] or "branch" in flags:
            t = G.tracking(repo)
            h = f"## {repo.head}" + (f"...{t[0]}" if t else "")
            if t and (t[1] or t[2]):
                h += " [" + ", ".join(x for x in (f"ahead {t[1]}" if t[1] else "", f"behind {t[2]}" if t[2] else "") if x) + "]"
            lines.insert(0, h)
        return R(0, "".join(l + "\n" for l in lines))
    return R(0, G.long_status(sim, root, repo))


def stage(sim, root, repo, rel_path, scope=None):
    wt = G.worktree(sim, root)
    pats = G.gitignore(sim, root)
    for p in [p for p in wt if under(p, rel_path) and not G.ignored(pats, p)]:
        repo.index[p] = wt[p]
        repo.conflicts.discard(p)
    for p in [p for p in repo.index if under(p, rel_path) and p not in wt]:
        del repo.index[p]
        repo.conflicts.discard(p)


def c_add(sim, args, root, repo):
    flags = [x for x in args if x.startswith("-")]
    paths = [x for x in args if not x.startswith("-")]
    if any(f not in ("-A", "--all", "-u", "--update", "-f", "-v", "--verbose") for f in flags):
        raise Unsupported("add flag")
    if "-f" in flags:
        raise Unsupported("add -f")
    wt = G.worktree(sim, root)
    if not paths:
        if "-A" in flags or "--all" in flags:
            paths = [root]
        else:
            raise Unsupported("add nothing")
    for p in paths:
        r = rel(sim, root, p)
        if "-u" in flags or "--update" in flags:
            for q in [q for q in list(repo.index) if under(q, r)]:
                if q in wt:
                    repo.index[q] = wt[q]
                else:
                    del repo.index[q]
            continue
        exists = any(under(q, r) for q in wt) or any(under(q, r) for q in repo.index)
        if not exists:
            return sim.e("git.add.nomatch", path=p)
        if r and r in wt and G.ignored(G.gitignore(sim, root), r) and r not in repo.index:
            raise Unsupported("add ignored file")
        stage(sim, root, repo, r)
    return R(0)


def c_commit(sim, args, root, repo):
    msgs, amend, allow_empty, auto, noedit, i = [], False, False, False, False, 0
    while i < len(args):
        x = args[i]
        if x in ("-m", "--message"):
            msgs.append(args[i + 1]); i += 1
        elif x.startswith("-m") and len(x) > 2 and not x.startswith("--"):
            msgs.append(x[2:])
        elif x == "--amend":
            amend = True
        elif x == "--allow-empty":
            allow_empty = True
        elif x == "--no-edit":
            noedit = True
        elif x.startswith("-") and not x.startswith("--") and set(x[1:]) <= {"a", "m", "q", "v"}:
            if "a" in x[1:]:
                auto = True
            if "m" in x[1:]:
                msgs.append(args[i + 1]); i += 1
        elif x in ("-q", "--quiet", "-a", "--all"):
            auto = auto or x in ("-a", "--all")
        else:
            raise Unsupported(f"commit flag {x}")
        i += 1
    if auto:
        wt = G.worktree(sim, root)
        for p in list(repo.index):
            if p in wt:
                repo.index[p] = wt[p]
            else:
                del repo.index[p]
    if repo.conflicts:
        return sim.e("git.commit.unmerged", files="\n".join(f"U\t{p}" for p in sorted(repo.conflicts)))
    if not msgs and not (repo.merging and noedit) and not (amend and noedit):
        raise Unsupported("commit needs editor")
    head = repo.head_id()
    if not amend and not repo.merging and not allow_empty and repo.index == repo.tree(head):
        return R(1, G.long_status(sim, root, repo))
    msg = "\n\n".join(msgs) or (repo.commits[head][2] if amend else f"Merge branch '{repo.merging}'")
    parents = list(repo.commits[head][0]) if amend else [head] if head else []
    if repo.merging:
        parents.append(repo.branches.get(repo.merging) or repo.remote_refs.get(repo.merging))
    cid = repo.new_commit(parents, repo.index, msg)
    repo.branches[repo.head] = cid
    repo.merging = None
    changed = len(changed_between(repo.index, repo.tree(parents[0]) if parents else {}))
    return R(0, f"[{repo.head}{' (root-commit)' if not parents else ''} {short(cid)}] {msg.splitlines()[0]}\n {changed} file{'s' if changed != 1 else ''} changed\n")


# ---------------------------------------------------------------- branches
def switch_to(sim, root, repo, name, create_from=None, track=None):
    target = create_from if create_from is not None else repo.branches[name]
    old, new = repo.tree(repo.head_id()), repo.tree(target)
    clash = sorted(changed_between(old, new) & dirty_paths(sim, root, repo))
    if clash:
        return sim.e("git.checkout.dirty", files="\n\t".join(clash))
    G.write_tree(sim, root, new, old)
    repo.index = dict(new)
    repo.head = name
    return None


def start_branch(sim, root, repo, name, start=None, track=None):
    if name in repo.branches:
        return sim.e("git.branch.exists", name=name)
    cid = resolve_commit(sim, repo, start) if start else repo.head_id()
    if cid is None and not start:
        pass
    repo.branches[name] = cid
    if track:
        repo.upstream[name] = track
    return None


def c_checkout(sim, args, root, repo):
    args = list(args)
    if args and args[0] in ("-b", "-B"):
        name = args[1]
        start = args[2] if len(args) > 2 else None
        err = None
        if name in repo.branches and args[0] == "-b":
            return sim.e("git.branch.exists", name=name)
        cid = resolve_commit(sim, repo, start) if start else repo.head_id()
        track = start if start and start in repo.remote_refs else None
        err = switch_to(sim, root, repo, name, create_from=cid)
        if err:
            return err
        repo.branches[name] = cid
        if track:
            repo.upstream[name] = track
        return R(0, "", f"Switched to a new branch '{name}'\n")
    if args and args[0] == "--":
        args = args[1:]
        return restore_paths(sim, root, repo, args, staged=False)
    if len(args) == 1 and args[0] == "-":
        raise Unsupported("checkout -")
    if len(args) == 1:
        name = args[0]
        if name in repo.branches:
            if name == repo.head:
                return R(0, "", f"Already on '{name}'\n")
            err = switch_to(sim, root, repo, name)
            return err or R(0, "", f"Switched to branch '{name}'\n")
        if f"origin/{name}" in repo.remote_refs:
            err = switch_to(sim, root, repo, name, create_from=repo.remote_refs[f"origin/{name}"])
            if err:
                return err
            repo.branches[name] = repo.remote_refs[f"origin/{name}"]
            repo.upstream[name] = f"origin/{name}"
            return R(0, f"branch '{name}' set up to track 'origin/{name}'.\n", f"Switched to a new branch '{name}'\n")
        wt = G.worktree(sim, root)
        r = rel(sim, root, name)
        if any(under(p, r) for p in list(wt) + list(repo.index)):
            return restore_paths(sim, root, repo, [name], staged=False)
        return sim.e("git.checkout.nomatch", name=name)
    return restore_paths(sim, root, repo, args, staged=False)


def c_switch(sim, args, root, repo):
    if args and args[0] in ("-c", "-C"):
        name = args[1]
        if name in repo.branches and args[0] == "-c":
            return sim.e("git.branch.exists", name=name)
        cid = repo.head_id()
        err = switch_to(sim, root, repo, name, create_from=cid)
        if err:
            return err
        repo.branches[name] = cid
        return R(0, "", f"Switched to a new branch '{name}'\n")
    if len(args) != 1 or args[0].startswith("-"):
        raise Unsupported("switch form")
    name = args[0]
    if name in repo.branches:
        if name == repo.head:
            return R(0, "", f"Already on '{name}'\n")
        err = switch_to(sim, root, repo, name)
        return err or R(0, "", f"Switched to branch '{name}'\n")
    if f"origin/{name}" in repo.remote_refs:
        err = switch_to(sim, root, repo, name, create_from=repo.remote_refs[f"origin/{name}"])
        if err:
            return err
        repo.branches[name] = repo.remote_refs[f"origin/{name}"]
        repo.upstream[name] = f"origin/{name}"
        return R(0, f"branch '{name}' set up to track 'origin/{name}'.\n", f"Switched to a new branch '{name}'\n")
    return sim.e("git.switch.invalid", name=name)


def c_branch(sim, args, root, repo):
    flags = [x for x in args if x.startswith("-")]
    pos = [x for x in args if not x.startswith("-")]
    if "--show-current" in flags:
        return R(0, repo.head + "\n")
    for f in flags:
        if f.startswith("--set-upstream-to=") or f in ("-u",):
            up = f.split("=", 1)[1] if "=" in f else pos.pop(0)
            if up not in repo.remote_refs:
                return sim.e("git.branch.badupstream", name=up)
            repo.upstream[pos[0] if pos else repo.head] = up
            return R(0, f"branch '{pos[0] if pos else repo.head}' set up to track '{up}'.\n")
    if "-d" in flags or "-D" in flags:
        for b in pos:
            if b not in repo.branches:
                return sim.e("git.branch.notfound", name=b)
            if b == repo.head:
                return sim.e("git.branch.delete_current", name=b, path=root)
            del repo.branches[b]
            repo.upstream.pop(b, None)
        return R(0, "".join(f"Deleted branch {b} (was abc1234).\n" for b in pos))
    if "-m" in flags or "-M" in flags:
        old, new = (pos[0], pos[1]) if len(pos) == 2 else (repo.head, pos[0])
        if old not in repo.branches:
            return sim.e("git.branch.notfound", name=old)
        repo.branches[new] = repo.branches.pop(old)
        if old in repo.upstream:
            repo.upstream[new] = repo.upstream.pop(old)
        if repo.head == old:
            repo.head = new
        return R(0)
    if pos:
        if not repo.head_id():
            return sim.e("git.branch.nocommit", head=repo.head)
        return start_branch(sim, root, repo, pos[0], pos[1] if len(pos) > 1 else None) or R(0)
    names = sorted(repo.branches)
    out = [("* " if b == repo.head else "  ") + b for b in names]
    if "-a" in flags or "-r" in flags:
        rem = [f"  remotes/{k}" for k in sorted(repo.remote_refs)]
        out = rem if "-r" in flags else out + rem
    if not names and not ("-r" in flags):
        out = []
    return R(0, "".join(l + "\n" for l in out))


# ---------------------------------------------------------------- remotes
def c_remote(sim, args, root, repo):
    if not args or args[0] == "-v":
        names = sorted(repo.remotes)
        if args:
            return R(0, "".join(f"{n}\t{url_of(repo, n)} (fetch)\n{n}\t{url_of(repo, n)} (push)\n" for n in names))
        return R(0, "".join(n + "\n" for n in names))
    if args[0] == "add":
        name, url = args[1], args[2]
        if name in repo.remotes:
            return sim.e("git.remote.exists", name=name)
        path = url
        for pre in URL_PREFIXES:
            if url.startswith(pre):
                path = "/srv/git/" + url[len(pre):]
        repo.remotes[name] = path[:-4] + ".git" if False else path
        if not hasattr(repo, "urls"):
            repo.urls = {}
        repo.urls[name] = url
        # normalise to the bare-repo path used in /srv/git
        if not sim.fs.lookup(path) and sim.fs.lookup(path + ".git"):
            repo.remotes[name] = path + ".git"
        return R(0)
    if args[0] in ("remove", "rm"):
        if args[1] not in repo.remotes:
            return sim.e("git.remote.nosuch", name=args[1])
        del repo.remotes[args[1]]
        for k in [k for k in repo.remote_refs if k.startswith(args[1] + "/")]:
            del repo.remote_refs[k]
        for b in [b for b, u in repo.upstream.items() if u.startswith(args[1] + "/")]:
            del repo.upstream[b]
        return R(0)
    if args[0] == "set-url":
        if args[1] not in repo.remotes:
            raise Unsupported("set-url missing")
        return c_remote(sim, ["add", args[1] + "__tmp", args[2]], root, repo) and (_swap(repo, args[1]) or R(0))
    if args[0] == "get-url":
        if args[1] not in repo.remotes:
            raise Unsupported("get-url missing")
        return R(0, url_of(repo, args[1]) + "\n")
    raise Unsupported(f"remote {args[0]}")


def _swap(repo, name):
    repo.remotes[name] = repo.remotes.pop(name + "__tmp")
    repo.urls[name] = repo.urls.pop(name + "__tmp")


def c_fetch(sim, args, root, repo):
    names = [x for x in args if not x.startswith("-")] or (["origin"] if "origin" in repo.remotes else list(repo.remotes))
    if not repo.remotes:
        return R(0)
    for n in names:
        if n not in repo.remotes:
            return sim.e("git.remote.missing", remote=n)
        if not fetch(sim, repo, n):
            raise Unsupported("fetch from missing remote")
    return R(0)


def c_push(sim, args, root, repo):
    setup = any(x in ("-u", "--set-upstream") for x in args)
    force = any(x in ("-f", "--force", "--force-with-lease") for x in args)
    pos = [x for x in args if not x.startswith("-")]
    if any(x.startswith("--") and x not in ("--set-upstream", "--force", "--force-with-lease") for x in args):
        raise Unsupported("push flag")
    up = repo.upstream.get(repo.head)
    if not pos:
        if not repo.remotes:
            return sim.e("git.push.no_remote")
        if not up:
            return sim.e("git.push.no_upstream", branch=repo.head)
        remote, branch = up.split("/", 1)[0], repo.head
    else:
        remote = pos[0]
        branch = pos[1] if len(pos) > 1 else repo.head
        if ":" in branch:
            raise Unsupported("refspec")
    if remote not in repo.remotes:
        return sim.e("git.remote.missing", remote=remote)
    rr = remote_repo(sim, repo, remote)
    if rr is None:
        raise Unsupported("push to missing remote")
    cid = repo.branches.get(branch)
    if cid is None:
        return sim.e("git.push.nosrc", branch=branch, url=url_of(repo, remote))
    cur = rr.branches.get(branch)
    if cur == cid:
        if setup:
            repo.upstream[branch] = f"{remote}/{branch}"
        return R(0, "", "Everything up-to-date\n")
    if cur and cur not in repo.ancestors(cid) and not force:
        return sim.e("git.push.rejected", branch=branch, url=url_of(repo, remote))
    rr.commits.update({k: v for k, v in repo.commits.items() if k in repo.ancestors(cid)})
    rr.branches[branch] = cid
    repo.remote_refs[f"{remote}/{branch}"] = cid
    if setup:
        repo.upstream[branch] = f"{remote}/{branch}"
    err = f"To {url_of(repo, remote)}\n" + (f" * [new branch]      {branch} -> {branch}\n" if cur is None else f"   {short(cur)}..{short(cid)}  {branch} -> {branch}\n")
    if setup:
        err += f"branch '{branch}' set up to track '{remote}/{branch}'.\n"
    return R(0, "", err)


def merge_in(sim, root, repo, target_id, label, no_ff=False):
    head = repo.head_id()
    if target_id in repo.ancestors(head):
        return R(0, "Already up to date.\n")
    if head in repo.ancestors(target_id) and not no_ff:
        old, new = repo.tree(head), repo.tree(target_id)
        clash = sorted(changed_between(old, new) & dirty_paths(sim, root, repo))
        if clash:
            return sim.e("git.merge.dirty", files="\n\t".join(clash))
        G.write_tree(sim, root, new, old)
        repo.index = dict(new)
        repo.branches[repo.head] = target_id
        return R(0, f"Updating {short(head)}..{short(target_id)}\nFast-forward\n")
    # three-way, file-level
    anc_h, anc_t = repo.ancestors(head), repo.ancestors(target_id)
    common = anc_h & anc_t
    base_id = next((c for c in _topo(repo, head) if c in common), None)
    base, ours, theirs = repo.tree(base_id), repo.tree(head), repo.tree(target_id)
    merged, conflicts = {}, []
    for p in set(base) | set(ours) | set(theirs):
        b, o, t = base.get(p), ours.get(p), theirs.get(p)
        if o == t:
            res = o
        elif b == o:
            res = t
        elif b == t:
            res = o
        else:
            conflicts.append(p); res = o
        if res is not None:
            merged[p] = res
    if conflicts:
        if len(conflicts) > 1:
            raise Unsupported("multi-file conflict")
        p = conflicts[0]
        o, t = ours.get(p), theirs.get(p)
        if o is None or t is None:
            raise Unsupported("modify/delete conflict")
        old = repo.tree(head)
        merged_wt = {q: v for q, v in merged.items() if q != p}
        G.write_tree(sim, root, merged_wt, old)
        sim.fs.put(f"{root}/{p}", f"<<<<<<< HEAD\n{o[0]}=======\n{t[0]}>>>>>>> {label}\n")
        repo.index = {q: v for q, v in merged_wt.items()}
        repo.index[p] = o
        repo.merging, repo.conflicts = label, {p}
        return sim.e("git.merge.conflict", file=p)
    cid = repo.new_commit([head, target_id], merged, f"Merge branch '{label}'")
    old = repo.tree(head)
    G.write_tree(sim, root, merged, old)
    repo.index = dict(merged)
    repo.branches[repo.head] = cid
    return R(0, "Merge made by the 'ort' strategy.\n")


def _topo(repo, cid):
    out, seen, queue = [], set(), [cid]
    while queue:
        c = queue.pop(0)
        if c in seen:
            continue
        seen.add(c); out.append(c)
        queue.extend(repo.commits[c][0])
    return out


def c_merge(sim, args, root, repo):
    if "--abort" in args:
        if not repo.merging:
            return sim.e("git.merge.noabort")
        old, new = {}, repo.tree(repo.head_id())
        G.write_tree(sim, root, new, {p: ("", False) for p in list(new)})
        for p, v in new.items():
            sim.fs.put(f"{root}/{p}", v[0], 0o755 if v[1] else 0o644)
        repo.index, repo.merging, repo.conflicts = dict(new), None, set()
        return R(0)
    pos = [x for x in args if not x.startswith("-")]
    if any(x not in ("--no-edit", "--no-ff", "-m") and x.startswith("-") for x in args) or len(pos) != 1:
        raise Unsupported("merge form")
    name = pos[0]
    tid = repo.branches.get(name) or repo.remote_refs.get(name)
    if tid is None:
        return sim.e("git.merge.unknown", name=name)
    return merge_in(sim, root, repo, tid, name, no_ff="--no-ff" in args)


def c_pull(sim, args, root, repo):
    pos = [x for x in args if not x.startswith("-")]
    if any(x.startswith("--") and x not in ("--no-edit", "--ff-only", "--no-rebase") for x in args) or any(x.startswith("-") and not x.startswith("--") for x in args):
        raise Unsupported("pull flag")
    up = repo.upstream.get(repo.head)
    if not pos:
        if not repo.remotes:
            return sim.e("git.pull.no_remote", branch=repo.head)
        if not up:
            return sim.e("git.pull.no_tracking", branch=repo.head)
        remote, branch = up.split("/", 1)
    else:
        remote, branch = pos[0], (pos[1] if len(pos) > 1 else repo.head)
        if remote not in repo.remotes:
            return sim.e("git.remote.missing", remote=remote)
    if not fetch(sim, repo, remote):
        raise Unsupported("pull missing remote")
    ref = f"{remote}/{branch}"
    if ref not in repo.remote_refs:
        return sim.e("git.pull.noref", branch=branch, remote=remote)
    r = merge_in(sim, root, repo, repo.remote_refs[ref], ref)
    return r


# ---------------------------------------------------------------- misc
def c_stash(sim, args, root, repo):
    sub = args[0] if args and not args[0].startswith("-") else "push"
    untracked = any(x in ("-u", "--include-untracked") for x in args)
    if sub in ("push", "save"):
        head = repo.head_id()
        dirt = dirty_paths(sim, root, repo)
        unt = [p for xy, p in G.status_entries(sim, root, repo) if xy == "??"] if untracked else []
        if not dirt and not unt:
            return R(0, "No local changes to save\n")
        wt = G.worktree(sim, root)
        repo.stash.append(({p: wt.get(p) for p in dirt}, {p: wt[p] for p in unt if p in wt}, head))
        base = repo.tree(head)
        G.write_tree(sim, root, base, {p: v for p, v in wt.items() if p in dirt or p in base} | {p: ("", False) for p in dirt if p not in base})
        for p in dirt:
            if p in base:
                sim.fs.put(f"{root}/{p}", base[p][0], 0o755 if base[p][1] else 0o644)
        for p in unt:
            parent, name = sim.fs.parent(f"{root}/{p}")
            parent.children.pop(name, None)
        repo.index = dict(base)
        msg = repo.commits[head][2] if head else ""
        return R(0, f"Saved working directory and index state WIP on {repo.head}: {short(head)} {msg}\n")
    if sub in ("pop", "apply"):
        if not repo.stash:
            return sim.e("git.stash.empty")
        mods, unt, shead = repo.stash[-1] if sub == "apply" else repo.stash.pop()
        base, cur = repo.tree(shead), repo.tree(repo.head_id())
        if any(base.get(p) != cur.get(p) for p in mods):
            raise Unsupported("stash pop conflict")
        for p, v in {**mods, **unt}.items():
            if v is None:
                parent, name = sim.fs.parent(f"{root}/{p}")
                if parent:
                    parent.children.pop(name, None)
            else:
                sim.fs.put(f"{root}/{p}", v[0], 0o755 if v[1] else 0o644)
        return R(0, G.long_status(sim, root, repo))
    if sub == "list":
        return R(0, "".join(f"stash@{{{len(repo.stash) - 1 - i}}}: WIP on {repo.head}: {short(h)} x\n" for i, (_, _, h) in enumerate(repo.stash)))
    raise Unsupported(f"stash {sub}")


def c_log(sim, args, root, repo):
    head = repo.head_id()
    if head is None:
        return sim.e("git.log.nocommits", branch=repo.head)
    n = None
    for i, x in enumerate(args):
        if x == "-n":
            n = int(args[i + 1])
        elif re.fullmatch(r"-\d+", x):
            n = int(x[1:])
    chain = [c for c in _topo(repo, head)][:n]
    one = "--oneline" in args
    return R(0, "".join((f"{short(c)} {repo.commits[c][2].splitlines()[0]}\n" if one else f"commit {c}\nAuthor: Dev User <dev@example.com>\n\n    {repo.commits[c][2]}\n\n") for c in chain))


def c_diff(sim, args, root, repo):
    return R(0, "")


c_show = c_diff


def restore_paths(sim, root, repo, paths, staged, source_head=False):
    head = repo.tree(repo.head_id())
    wt = G.worktree(sim, root)
    for p in paths:
        r = rel(sim, root, p)
        known = [q for q in set(wt) | set(repo.index) | set(head) if under(q, r)]
        if not known:
            return sim.e("git.checkout.nomatch", name=p)
        for q in known:
            if staged:
                if q in head:
                    repo.index[q] = head[q]
                else:
                    repo.index.pop(q, None)
            else:
                if q in repo.index:
                    sim.fs.put(f"{root}/{q}", repo.index[q][0], 0o755 if repo.index[q][1] else 0o644)
    return R(0)


def c_restore(sim, args, root, repo):
    staged = "--staged" in args or "-S" in args
    paths = [x for x in args if not x.startswith("-")]
    if not paths or any(x.startswith("--source") for x in args):
        raise Unsupported("restore form")
    return restore_paths(sim, root, repo, paths, staged)


def c_reset(sim, args, root, repo):
    mode = next((x[2:] for x in args if x in ("--hard", "--soft", "--mixed")), "mixed")
    pos = [x for x in args if not x.startswith("-")]
    head = repo.head_id()
    if pos and (pos[0].startswith("HEAD") or pos[0] in repo.commits):
        cid = resolve_commit(sim, repo, pos[0])
        if cid is None:
            return sim.e("git.reset.badrev", name=pos[0])
        old = repo.tree(head)
        repo.branches[repo.head] = cid
        if mode in ("mixed", "hard"):
            repo.index = dict(repo.tree(cid))
        if mode == "hard":
            tracked_old = {p: v for p, v in G.worktree(sim, root).items() if p in old or p in repo.tree(cid)}
            G.write_tree(sim, root, repo.tree(cid), tracked_old)
            for p, v in repo.tree(cid).items():
                sim.fs.put(f"{root}/{p}", v[0], 0o755 if v[1] else 0o644)
            return R(0, f"HEAD is now at {short(cid)} {repo.commits[cid][2].splitlines()[0]}\n")
        return R(0, "" if mode == "soft" else "Unstaged changes after reset:\n" if False else "")
    if pos:
        return restore_paths(sim, root, repo, pos, staged=True)
    base = repo.tree(head)
    if mode == "hard":
        old = G.worktree(sim, root)
        for p in list(old):
            if p in repo.index or p in base:
                pass
        G.write_tree(sim, root, base, {p: v for p, v in old.items() if p in base or p in repo.index})
        for p, v in base.items():
            sim.fs.put(f"{root}/{p}", v[0], 0o755 if v[1] else 0o644)
        repo.index, repo.merging, repo.conflicts = dict(base), None, set()
        return R(0, f"HEAD is now at {short(head)} {repo.commits[head][2].splitlines()[0]}\n")
    repo.index = dict(base)
    return R(0)


def c_rm(sim, args, root, repo):
    cached = "--cached" in args
    paths = [x for x in args if not x.startswith("-")]
    for p in paths:
        r = rel(sim, root, p)
        hits = [q for q in repo.index if under(q, r)]
        if not hits:
            return sim.e("git.rm.nomatch", path=p)
        if len(hits) > 1 and not any(x in ("-r",) for x in args):
            raise Unsupported("rm dir without -r")
        wt = G.worktree(sim, root)
        head = repo.tree(repo.head_id())
        for q in hits:
            if "-f" not in args and q in wt and wt[q] != repo.index[q] and not cached:
                return sim.e("git.rm.modified", path=q)
            if "-f" not in args and repo.index[q] != head.get(q) and not cached:
                raise Unsupported("git rm staged changes")
        for q in hits:
            del repo.index[q]
            if not cached:
                parent, name = sim.fs.parent(f"{root}/{q}")
                if parent:
                    parent.children.pop(name, None)
    return R(0, "".join(f"rm '{rel(sim, root, p)}'\n" for p in paths))


def c_mv(sim, args, root, repo):
    paths = [x for x in args if not x.startswith("-")]
    if len(paths) != 2:
        raise Unsupported("git mv args")
    s, d = rel(sim, root, paths[0]), rel(sim, root, paths[1])
    if s not in repo.index:
        return sim.e("git.mv.untracked", path=paths[0], dest=paths[1])
    r = sim.c_mv(paths)
    if r.code:
        return r
    dn = sim.fs.lookup(sim.abs(paths[1]))
    if dn is not None and dn.kind == "d":
        raise Unsupported("git mv into dir")
    repo.index[d] = repo.index.pop(s)
    return R(0)


def c_tag(sim, args, root, repo):
    pos = [x for x in args if not x.startswith("-")]
    if "-d" in args:
        for t in pos:
            if t not in repo.tags:
                return sim.e("git.tag.notfound", name=t)
            del repo.tags[t]
        return R(0)
    if not pos:
        return R(0, "".join(t + "\n" for t in sorted(repo.tags)))
    if pos[0] in repo.tags:
        return sim.e("git.tag.exists", name=pos[0])
    if not repo.head_id():
        raise Unsupported("tag without commits")
    repo.tags[pos[0]] = repo.head_id()
    return R(0)


QUIET_OK = {"init", "clone", "checkout", "switch", "push", "pull", "fetch", "merge", "stash", "reset", "add", "restore"}
SUBS = {"init": c_init, "clone": c_clone, "config": c_config, "status": c_status, "add": c_add, "commit": c_commit,
        "checkout": c_checkout, "switch": c_switch, "branch": c_branch, "remote": c_remote, "fetch": c_fetch,
        "push": c_push, "pull": c_pull, "merge": c_merge, "stash": c_stash, "log": c_log, "diff": c_diff, "show": c_show,
        "restore": c_restore, "reset": c_reset, "rm": c_rm, "mv": c_mv, "tag": c_tag}
