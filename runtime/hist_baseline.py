"""What zsh-autosuggestions (most recent history match by prefix) achieves on a zsh history file. Prints aggregates only.

uv run runtime/hist_baseline.py [~/.zsh_history]
"""
import os
import re
import sys


def load(path="~/.zsh_history"):
    """Single-line commands only: zsh stores a multi-line command as lines ending with a backslash."""
    out, cur = [], ""
    for line in open(os.path.expanduser(path), errors="replace").read().split("\n"):
        line = line if cur else re.sub(r"^: \d+:\d+;", "", line)
        cur = cur + "\n" + line if cur else line
        if cur.endswith("\\"):
            continue
        if cur.strip() and "\n" not in cur and len(cur) < 200:
            out.append(cur)
        cur = ""
    return out


def main():
    cmds = load(sys.argv[1] if len(sys.argv) > 1 else "~/.zsh_history")
    seen, hit, saved, total = [], 0, 0, 0
    for c in cmds:
        total += len(c)
        for k in range(1, len(c)):                  # earliest prefix length at which the suggestion is exactly the command
            if next((h for h in reversed(seen) if h.startswith(c[:k])), None) == c:
                saved += len(c) - k - 1             # chars not typed, minus the accept key
                hit += 1
                break
        seen.append(c)
    print(f"commands={len(cmds)} predicted_somewhere={hit/len(cmds):.1%} chars_saved={saved/total:.1%}")


if __name__ == "__main__":
    main()
