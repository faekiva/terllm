"""Segment-aware candidates and candidate-backed suggestions (fork additions).

The zsh plugin sends zsh's own word split of the line (${(z)BUFFER}) with every query, so
the daemon knows which command the word under the cursor belongs to. Pure functions only:
no shell, no model, no filesystem.
"""
import posixpath
from typing import NamedTuple

import policy

SEPARATORS = {"&&", "||", "|", "|&", ";", "&", "&!", "&|", "(", ")", "{", "}", "!"}
PRECOMMANDS = {"sudo", "command", "builtin", "exec", "nocorrect", "noglob", "time",
               "nohup", "env"}
# argument must already exist on disk (rule 2 drops guesses that aren't candidates)
MUST_EXIST = {"cd", "pushd", "cat", "less", "more", "bat", "source", ".", "head", "tail"}
UNQUOTABLE = set("\"'$*?`=|&;<>()\\{}[]!# \t")   # same as render.NOCOMP
LISTING_BLIND = (".", "~", "/", "-")   # paths a cwd listing never offers: .., hidden, ~, /


class Line(NamedTuple):
    segment: str     # text of the current command, in the form render.fs_cands expects
    cmd: str         # its command name; '' while the command name itself is being typed
    cur: str         # word under the cursor; '' right after a space
    simple: bool     # the line is a single command (no &&, |, ;, subshell, …)
    created: list    # directories made earlier on the line, as candidates ('beep/')
    words: list      # the current command's finished words ('npm', 'run')


def parse(buf, words=None):
    """Split `buf` using zsh's words (falls back to whitespace) into a Line."""
    words = [w for w in (words if words is not None else buf.split()) if w]
    after_space = not buf or buf[-1].isspace()
    done = words if after_space else words[:-1]
    cur = "" if after_space else words[-1]
    cut = max((i for i, w in enumerate(done) if w in SEPARATORS), default=-1)
    seg = done[cut + 1:]
    head = [w for w in seg if w not in PRECOMMANDS and not _is_assignment(w)]
    segment = " ".join(seg + [cur]) if seg else cur
    return Line(segment=segment, cmd=head[0] if head else "", cur=cur,
                simple=not any(w in SEPARATORS for w in words),
                created=_created_dirs(done[:cut + 1]), words=head)


def candidates(line, fs):
    """fs_cands' list plus directories created earlier on the line that fit `cur`."""
    extra = [d for d in line.created
             if d.startswith(line.cur) and d.rstrip("/") != line.cur and d not in fs]
    return (list(fs) + extra)[:8]


def decide(toks, thr, line, cands, strict=False):
    """What to show: the confident prefix, widened by rule 1 and vetoed by rule 2.

    strict: `cands` is the complete list of valid words here (task-runner targets), so
    rule 2 applies even in chains and for any command.

    Returns (suggestion, rule) where rule is '', 'backed' or 'vetoed' (for the log).
    """
    shown = policy.confident_prefix(toks, thr)
    rule = ""
    if strict and len(cands) == 1:   # the only valid word: complete it like fish would
        return cands[0][len(line.cur):], "unique"
    backed = _backed_word(toks, line.cur, cands)
    if backed and not shown.startswith(backed):
        shown, rule = backed, "backed"
    must_exist = strict or (line.simple and line.cmd in MUST_EXIST)
    if shown and strict and not cands:   # no valid word starts with what's typed
        return "", "vetoed"
    if shown and must_exist and cands \
            and not _fits_candidates(line.cur + _first_word(shown), line.cur, cands):
        shown, rule = "", "vetoed"
    return shown, rule


def _backed_word(toks, cur, cands):
    """Rule 1: the model's whole word if it lands on a candidate, or up to a candidate
    directory it goes into (`pr` -> `proj/mk`, unsure past `proj/` -> show `oj/`)."""
    word = _first_word("".join(t for t, _ in toks))
    target = cur + word
    if word and any(target.rstrip("/") == c.rstrip("/") for c in cands):
        return word
    into = [c for c in cands if c.endswith("/") and target.startswith(c) and len(c) > len(cur)]
    return max(into, key=len)[len(cur):] if word and into else ""


def _fits_candidates(target, cur, cands):
    """Rule 2 check: `target` is (a prefix of / inside) a candidate, or out of view."""
    if target == cur:
        return True
    rest = target[len(cur[:cur.rfind("/") + 1]):]
    if rest.startswith(LISTING_BLIND):
        return True
    return any(c.startswith(target) or target.rstrip("/") == c.rstrip("/")
               or (c.endswith("/") and target.startswith(c)) for c in cands)


def _first_word(text):
    for i, ch in enumerate(text):
        if ch.isspace():
            return text[:i]
    return text


def _is_assignment(w):
    return "=" in w and not w.startswith("=")


def _created_dirs(words):
    """Directories that `mkdir` / `git clone` earlier on the line will create."""
    out, seg = [], []
    for w in words + [";"]:
        if w not in SEPARATORS:
            seg.append(w)
            continue
        args = [a for a in seg[1:] if not a.startswith("-")]
        if seg[:1] == ["mkdir"]:
            out += args
        elif seg[:2] == ["git", "clone"] and len(args) >= 2:
            out.append(args[2] if len(args) > 2 else
                       posixpath.basename(args[1].rstrip("/")).removesuffix(".git"))
        seg = []
    return [d.rstrip("/") + "/" for d in out if d and not UNQUOTABLE & set(d)]
