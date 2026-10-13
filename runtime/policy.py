"""When to show a suggestion: the longest prefix of the completion whose tokens are all confident, cut at a word boundary."""


def parse(r):
    """llama-server /completion response -> [(token text, probability)] without the end marker."""
    out = []
    for x in r.get("completion_probabilities", []):
        if x["token"] in ("<|end|>", ""):
            break
        out.append((x["token"], 2.718281828 ** x["logprob"]))
    return out


def confident_prefix(toks, thr):
    text = ""
    for i, (t, p) in enumerate(toks):
        if p < thr:
            if text and not text.endswith(" ") and not t.startswith(" "):
                text = text[:text.rfind(" ") + 1]                  # the doubtful token continues a word: drop the unfinished word
            return text
        text += t
    return text


def parse_healed(r, forced):
    """Like parse, for a token-healed call whose output was forced to start with `forced`.

    Tokens wholly inside `forced` are dropped (their probability only says how the typed
    word is split into tokens). The token that crosses the end of `forced` keeps its
    probability renormalised over the top-k tokens the grammar allowed there; later
    tokens keep their raw probability.
    """
    out, rest = [], forced
    for x in r.get("completion_probabilities", []):
        tok = x["token"]
        if tok in ("<|end|>", ""):
            break
        if not rest:
            out.append((tok, 2.718281828 ** x["logprob"]))
            continue
        if rest.startswith(tok):
            rest = rest[len(tok):]
            continue
        top = x.get("top_logprobs") or [x]
        ok = sum(2.718281828 ** t["logprob"] for t in top
                 if t["token"] and (t["token"].startswith(rest) or rest.startswith(t["token"])))
        p = 2.718281828 ** x["logprob"]
        out.append((tok[len(rest):], p / ok if ok >= p else 1.0))
        rest = ""
    return out
