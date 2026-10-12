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
