"""Minimal git model: commits are full-tree snapshots {path: (content, exec)}; enough for status/branch/push flows."""
import fnmatch
import hashlib
import posixpath

from .fs import Node, dnode


class Repo:
    def __init__(self, bare=False):
        self.bare, self.head = bare, "main"
        self.branches, self.commits = {}, {}      # name -> id ; id -> (parents, tree, msg)
        self.index = {}                            # staged tree
        self.remotes, self.remote_refs, self.upstream = {}, {}, {}   # name->abs path ; "origin/main"->id ; branch->"origin/main"
        self.tags, self.stash, self.n = {}, [], 0
        self.merging, self.conflicts = None, set()  # merge target branch / paths with markers

    def tree(self, cid):
        return self.commits[cid][1] if cid else {}

    def head_id(self):
        return self.branches.get(self.head)

    def new_commit(self, parents, tree, msg):
        self.n += 1
        cid = hashlib.sha1(f"{self.n}{parents}{msg}{sorted(tree)}".encode()).hexdigest()
        self.commits[cid] = (tuple(parents), dict(tree), msg)
        return cid

    def ancestors(self, cid):
        seen, stack = set(), [cid]
        while stack:
            c = stack.pop()
            if c and c not in seen:
                seen.add(c)
                stack.extend(self.commits[c][0])
        return seen

    def import_from(self, other, ids):
        for i in other.ancestors_of_all(ids):
            self.commits.setdefault(i, other.commits[i])

    def ancestors_of_all(self, ids):
        out = set()
        for i in ids:
            out |= self.ancestors(i)
        return out


def find_repo(sim, path=None):
    """(root_abs, Repo) of the worktree containing path/cwd, or (None, None)."""
    p = path or sim.cwd
    while True:
        n = sim.fs.lookup(p)
        g = n.children.get(".git") if n and n.kind == "d" else None
        if g is not None and isinstance(g.meta, Repo):
            return p, g.meta
        if p == "/":
            return None, None
        p = posixpath.dirname(p) or "/"


def worktree(sim, root):
    out = {}
    for p, n in sim.fs.walk(root):
        rel = p[len(root) + 1:]
        if not rel or rel == ".git" or rel.startswith(".git/") or n.kind == "d":
            continue
        out[rel] = (n.content if n.kind == "f" else "->" + n.target, bool(n.mode & 0o100))
    return out


def ignored(patterns, rel):
    parts = rel.split("/")
    for pat in patterns:
        anchored, dir_only = pat.startswith("/"), pat.endswith("/")
        pat = pat.strip("/")
        if anchored or "/" in pat:
            if fnmatch.fnmatch(rel, pat) or rel.startswith(pat + "/"):
                return True
            continue
        last = len(parts) - 1
        for i, part in enumerate(parts):
            if fnmatch.fnmatch(part, pat) and (not dir_only or i < last):
                return True
    return False


def gitignore(sim, root):
    n = sim.fs.lookup(root + "/.gitignore")
    if not n or n.kind != "f":
        return []
    return [l.strip() for l in n.content.splitlines() if l.strip() and not l.startswith("#")]


def status_entries(sim, root, repo):
    """list of (XY, path or 'old -> new') in git porcelain order."""
    head = repo.tree(repo.head_id())
    wt = worktree(sim, root)
    pats = gitignore(sim, root)
    idx = repo.index
    rows, untracked = [], []
    paths = sorted(set(head) | set(idx) | {p for p in wt if p in idx or not ignored(pats, p)})
    renames = {}
    added = [p for p in paths if p in idx and p not in head and idx[p][0]]
    deleted = [p for p in paths if p in head and p not in idx and head[p][0]]
    for a in added:
        for d in deleted:
            if head[d] == idx[a] and d not in renames.values():
                renames[a] = d
                break
    for p in paths:
        if p in repo.conflicts:
            rows.append(("UU", p)); continue
        if p not in idx and p not in head:
            untracked.append(p); continue
        x = " "
        if p in idx and p not in head:
            x = "A"
        elif p in head and p not in idx:
            x = "D"
        elif idx[p] != head[p]:
            x = "M"
        if p in renames:
            rows.append(("R ", f"{renames[p]} -> {p}")); continue
        if p in renames.values():
            continue
        y = " "
        if p in idx:
            if p not in wt:
                y = "D"
            elif wt[p] != idx[p]:
                y = "M"
        if x != " " or y != " ":
            rows.append((x + y, p))
    # collapse fully-untracked directories like git does
    dirs, flat = {}, []
    for u in untracked:
        top = u.split("/")[0]
        if "/" in u and not any(q.startswith(top + "/") for q in list(idx) + list(head)):
            dirs[top + "/"] = 1
        else:
            flat.append(u)
    return sorted(rows, key=lambda r: r[1].split(" -> ")[-1]) + [("??", u) for u in sorted(flat + list(dirs))]


def short_status(sim, root, repo):
    return [f"{xy} {p}" for xy, p in status_entries(sim, root, repo)]


def tracking(repo):
    up = repo.upstream.get(repo.head)
    if not up or up not in repo.remote_refs:
        return None
    mine, theirs = repo.head_id(), repo.remote_refs[up]
    a = repo.ancestors(mine) if mine else set()
    ahead = len(a - repo.ancestors(theirs))
    behind = len(repo.ancestors(theirs) - a)
    return up, ahead, behind


def long_status(sim, root, repo):
    out = [f"On branch {repo.head}"]
    t = tracking(repo)
    if t:
        up, ahead, behind = t
        pl = lambda n: f"{n} commit" + ("" if n == 1 else "s")
        if ahead and behind:
            out += [f"Your branch and '{up}' have diverged,", f"and have {ahead} and {behind} different commits each, respectively.", '  (use "git pull" if you want to integrate the remote branch with yours)']
        elif ahead:
            out += [f"Your branch is ahead of '{up}' by {pl(ahead)}.", '  (use "git push" to publish your local commits)']
        elif behind:
            out += [f"Your branch is behind '{up}' by {pl(behind)}, and can be fast-forwarded.", '  (use "git pull" to update your local branch)']
        else:
            out += [f"Your branch is up to date with '{up}'."]
    if repo.head_id() is None:
        out += ["", "No commits yet"]
    rows = status_entries(sim, root, repo)
    word = {"M": "modified:   ", "A": "new file:   ", "D": "deleted:    ", "R": "renamed:    "}
    staged = [(xy[0], p) for xy, p in rows if xy[0] in "MADR" and xy != "??"]
    unst = [(xy[1], p) for xy, p in rows if xy[1] in "MD" and xy != "UU"]
    unmerged = [p for xy, p in rows if xy == "UU"]
    untr = [p for xy, p in rows if xy == "??"]
    if repo.merging and unmerged:
        out += ["", "You have unmerged paths.", '  (fix conflicts and run "git commit")', '  (use "git merge --abort" to abort the merge)']
    if staged:
        hint = '  (use "git restore --staged <file>..." to unstage)' if repo.head_id() else '  (use "git rm --cached <file>..." to unstage)'
        out += ["", "Changes to be committed:", hint] + [f"\t{word[x]}{p}" for x, p in staged]
    if unmerged:
        out += ["", "Unmerged paths:", '  (use "git add <file>..." to mark resolution)'] + [f"\tboth modified:   {p}" for p in unmerged]
    if unst:
        dele = any(x == "D" for x, _ in unst)
        out += ["", "Changes not staged for commit:", f'  (use "git add{"/rm" if dele else ""} <file>..." to update what will be committed)',
                '  (use "git restore <file>..." to discard changes in working directory)'] + [f"\t{word[x]}{p}" for x, p in unst]
    if untr:
        out += ["", "Untracked files:", '  (use "git add <file>..." to include in what will be committed)'] + [f"\t{p}" for p in untr]
    if not staged and not unmerged:
        out += [""]
        if unst:
            out += ['no changes added to commit (use "git add" and/or "git commit -a")']
        elif untr:
            out += ['nothing added to commit but untracked files present (use "git add" to track)' if repo.head_id() else 'nothing added to commit but untracked files present (use "git add" to track)']
        else:
            out += ["nothing to commit, working tree clean" if repo.head_id() else 'nothing to commit (create/copy files and use "git add" to track)']
    return "\n".join(out) + "\n"


def write_tree(sim, root, tree, old_tree):
    """Make worktree match `tree` for paths that differ between old_tree and tree."""
    for p in sorted(set(old_tree) | set(tree)):
        if old_tree.get(p) == tree.get(p):
            continue
        full = f"{root}/{p}"
        if p in tree:
            sim.fs.put(full, tree[p][0], 0o755 if tree[p][1] else 0o644)
        else:
            parent, name = sim.fs.parent(full)
            if parent and name in parent.children:
                del parent.children[name]
            # prune now-empty parent dirs inside the worktree
            d = posixpath.dirname(full)
            while d != root and (n := sim.fs.lookup(d)) and n.kind == "d" and not n.children:
                pp, nm = sim.fs.parent(d)
                del pp.children[nm]
                d = posixpath.dirname(d)


def init_repo(sim, path, branch="main", bare=False):
    r = Repo(bare)
    r.head = branch
    d = sim.fs.mkdir(path)
    if bare:
        d.meta = r
    else:
        d.children[".git"] = Node("d", mode=0o755, meta=r)
        d.children[".git"].children = {"HEAD": Node("f", f"ref: refs/heads/{branch}\n")}
    return r


def commit_all_files(sim, root, repo, msg):
    wt = worktree(sim, root)
    repo.index = dict(wt)
    cid = repo.new_commit([repo.head_id()] if repo.head_id() else [], wt, msg)
    repo.branches[repo.head] = cid
    return cid


def fx_remote(sim, slug, lang="docs", branches=()):
    from fixtures import project_files
    name = slug.split("/")[-1]
    dest = f"/srv/git/{slug}.git"
    r = init_repo(sim, dest, bare=True)
    tree = {p: (c, False) for p, c in project_files(lang, name).items()}
    cid = r.new_commit([], tree, "Initial commit")
    r.branches["main"] = cid
    for b in branches:
        t = dict(tree)
        t[b.replace("/", "_") + ".txt"] = (b + "\n", False)
        r.branches[b] = r.new_commit([cid], t, f"Add {b}")
    return r


def clone(sim, url, dest):
    slug = url
    for pre in ("https://github.com/", "git@github.com:", "https://gitlab.com/", "git@gitlab.com:"):
        if slug.startswith(pre):
            slug = slug[len(pre):]
            break
    else:
        slug = slug if slug.startswith("/") else None
    if slug is None:
        return None
    src = slug if slug.startswith("/") else f"/srv/git/{slug.rstrip('/')}"
    for cand in (src, src + ".git", src[:-4] if src.endswith(".git") else None):
        n = sim.fs.lookup(cand) if cand else None
        if n and isinstance(n.meta, Repo):
            break
        if n and isinstance(n.children.get(".git") and n.children[".git"].meta, Repo):
            n = n.children[".git"]
            break
    else:
        return None
    remote = n.meta
    r = init_repo(sim, dest, branch=remote.head)
    r.commits = dict(remote.commits)
    r.remotes["origin"] = cand if n.kind == "d" and n.meta is remote and cand.endswith(".git") else cand
    r.remotes["origin"] = cand
    for b, c in remote.branches.items():
        r.remote_refs[f"origin/{b}"] = c
    r.branches[remote.head] = remote.branches[remote.head]
    r.upstream[remote.head] = f"origin/{remote.head}"
    r.index = dict(remote.tree(r.head_id()))
    write_tree(sim, dest, r.index, {})
    return r
