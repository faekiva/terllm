"""Virtual filesystem: tree of Nodes, owner-based write permission, exec bit."""
import copy
import posixpath

HOME = "/home/dev"


class Node:
    __slots__ = ("kind", "content", "mode", "owner", "children", "target", "meta")

    def __init__(self, kind, content="", mode=0o644, owner="dev", target=None, meta=None):
        self.kind, self.content, self.mode, self.owner, self.target, self.meta = kind, content, mode, owner, target, meta
        self.children = {} if kind == "d" else None

    def clone(self):
        return copy.deepcopy(self)


def dnode(owner="dev", mode=0o755):
    return Node("d", mode=mode, owner=owner)


class FS:
    def __init__(self):
        self.root = dnode("root")
        for p, o in [("/etc", "root"), ("/usr", "root"), ("/usr/local", "root"), ("/usr/local/bin", "root"),
                     ("/opt", "root"), ("/var", "root"), ("/home", "root"), (HOME, "dev"), ("/tmp", "dev"), ("/srv", "dev")]:
            self.mkdir(p, owner=o)
        self.root.children["etc"].children["hosts"] = Node("f", "127.0.0.1 localhost\n", owner="root")

    def lookup(self, path, follow=True):
        """Node at absolute path or None. Intermediate symlinks are always followed; the last one iff follow."""
        parts = [p for p in path.split("/") if p]
        node, cur, i, hops = self.root, [], 0, 0
        while i < len(parts):
            nxt = node.children.get(parts[i]) if node.kind == "d" else None
            if nxt is None:
                return None
            cur.append(parts[i])
            i += 1
            if nxt.kind == "l" and (follow or i < len(parts)):
                hops += 1
                if hops > 20:
                    return None
                tgt = posixpath.normpath(nxt.target if nxt.target.startswith("/") else posixpath.join("/" + "/".join(cur[:-1]), nxt.target))
                parts = [p for p in tgt.split("/") if p] + parts[i:]
                node, cur, i = self.root, [], 0
                continue
            node = nxt
        return node

    def parent(self, path):
        d, name = posixpath.split(path.rstrip("/") or "/")
        return self.lookup(d), name

    def mkdir(self, path, owner="dev"):
        cur = self.root
        for part in [p for p in path.split("/") if p]:
            cur = cur.children.setdefault(part, dnode(owner))
        return cur

    def put(self, path, content="", mode=0o644, owner="dev"):
        self.mkdir(posixpath.dirname(path))
        self.lookup(posixpath.dirname(path)).children[posixpath.basename(path)] = Node("f", content, mode, owner)

    def walk(self, path):
        """Yield (abs_path, node) for every descendant (not following symlinks)."""
        node = self.lookup(path)
        stack = [(path.rstrip("/"), node)] if node else []
        while stack:
            p, n = stack.pop()
            if n.kind == "d":
                for name, c in n.children.items():
                    stack.append((f"{p}/{name}", c))
            yield p, n

    def listdir(self, path):
        n = self.lookup(path)
        return sorted(n.children, key=lambda s: (s.lower().lstrip("."), s)) if n and n.kind == "d" else []

    def writable_dir(self, path):
        n = self.lookup(path)
        return n is not None and n.kind == "d" and n.owner == "dev"
