"""stdin <-> unix socket relay: bash cannot open a unix socket, so terllm.bash runs this as a coproc.

python3 runtime/terllm_relay.py /run/user/1000/terllm.sock
"""
import os
import select
import socket
import sys

s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
while True:
    ready = select.select([0, s], [], [])[0]
    if 0 in ready:
        d = os.read(0, 65536)
        if not d:
            break
        s.sendall(d)
    if s in ready:
        d = s.recv(65536)
        if not d:
            break
        os.write(1, d)
