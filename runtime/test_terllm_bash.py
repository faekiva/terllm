"""Drives runtime/terllm.bash in a real bash on a pty with a fake daemon and checks what the terminal shows (pyte).

uv run --no-project --with pyte python runtime/test_terllm_bash.py
"""
import fcntl
import os
import pathlib
import pty
import select
import socketserver
import struct
import tempfile
import termios
import threading
import time

import pyte

HERE = pathlib.Path(__file__).resolve().parent
FULL = ["clear", "echo hello"]                       # the fake model "knows" these commands
COLS, ROWS = 60, 8


class Fake(socketserver.StreamRequestHandler):
    def handle(self):
        for raw in self.rfile:
            f = raw.decode().rstrip("\n").split("\t")
            if f[0] == "q":
                buf = f[2]
                comp = next((c[len(buf):] for c in FULL if c.startswith(buf) and c != buf), "")
                self.wfile.write(f"s\t{f[1]}\t{buf}\t{comp}\n".encode()); self.wfile.flush()


class Term:
    def __init__(self, rc, env):
        self.screen = pyte.Screen(COLS, ROWS); self.stream = pyte.ByteStream(self.screen)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvpe("bash", ["bash", "--noprofile", "--rcfile", rc, "-i"], {**os.environ, **env, "TERM": "xterm", "COLUMNS": str(COLS), "LINES": str(ROWS)})
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        self.pump(1.5)

    def pump(self, t):
        end = time.time() + t
        while time.time() < end:
            if select.select([self.fd], [], [], 0.05)[0]:
                try:
                    self.stream.feed(os.read(self.fd, 65536))
                except OSError:
                    return

    def send(self, keys, t=0.35):
        os.write(self.fd, keys.encode()); self.pump(t)

    def line(self):
        """(text, grey_text, cursor_x) of the cursor row after the '$ ' prompt."""
        y = self.screen.cursor.y; row = self.screen.buffer[y]
        txt = "".join(row[x].data for x in range(COLS)).rstrip()
        grey = "".join(row[x].data for x in range(COLS) if row[x].fg == "brightblack").rstrip()
        return txt[2:], grey, self.screen.cursor.x - 2


def main():
    d = tempfile.mkdtemp()
    sock = pathlib.Path(d) / "terllm.sock"
    srv = socketserver.ThreadingUnixStreamServer(str(sock), Fake); srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rc = pathlib.Path(d) / "rc"
    rc.write_text(f"PS1='$ '\nHISTFILE={d}/h\nsource {HERE}/terllm.bash\n")
    t = Term(str(rc), {"XDG_RUNTIME_DIR": d, "HOME": d})
    t.send("\r", 0.8)                                                      # first prompt -> connect (socket exists)
    t.send("c"); t.send("l")
    assert t.line() == ("clear", "ear", 2), t.line()                       # grey suggestion after the cursor
    t.send("e")
    assert t.line() == ("clear", "ar", 3), t.line()                        # typing along: never 'cleear'
    t.send("\x7f"); t.send("\x7f")
    assert t.line() == ("clear", "ear", 2) or t.line()[0] == "clear", t.line()
    t.send("\x1b[C")                                                       # Right arrow accepts
    assert t.line() == ("clear", "", 5), t.line()
    t.send("\x15")                                                         # Ctrl-U
    assert t.line()[0] == "", t.line()
    for ch in "echo hel":
        t.send(ch)
    assert t.line()[:2] == ("echo hello", "lo"), t.line()
    t.send("\r", 0.8)                                                      # Enter executes only what was typed
    rows = [r.rstrip() for r in t.screen.display]
    assert "$ echo hel" in rows and "hel" in rows and not any("hello" in r for r in rows), rows
    for ch in "ec":
        t.send(ch)
    t.send("\x1b[D")                                                       # Left arrow hides the suggestion
    assert t.line()[1] == "" and t.line()[0] == "ec", t.line()
    print("terllm.bash ok")
    os.kill(t.pid, 9)


if __name__ == "__main__":
    main()
