"""Fail closed on real IP traffic; MockTransport and Unix IPC remain available.

Use PYTHONPATH=tests/offline_guard before launching Python (also inherited by
test subprocesses). Docker acceptance additionally uses --network none.
"""
import socket
import sys


def _offline(event, args):
    if event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
        raise RuntimeError("offline acceptance: DNS is forbidden")
    if event in {"socket.connect", "socket.bind", "socket.sendto"}:
        sock = args[0]
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            raise RuntimeError("offline acceptance: IP traffic is forbidden")


sys.addaudithook(_offline)
