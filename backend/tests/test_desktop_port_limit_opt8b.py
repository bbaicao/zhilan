"""OPT8B 合法端口上界：65535 占用时可见失败，不能探测越界端口。"""
import errno

import pytest

from tests import test_s8_desktop_startup as s8


def test_port_scan_stops_at_last_valid_port_and_closes_socket(monkeypatch):
    attempted = []
    sockets = []

    class OccupiedSocket:
        closed = False

        def __init__(self, *args):
            sockets.append(self)

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            attempted.append(address[1])
            assert address[1] == 65535, "不能探测超出65535的端口"
            raise OSError(errno.EADDRINUSE, "occupied")

        def close(self):
            self.closed = True

    monkeypatch.setattr(s8.app.socket, "socket", OccupiedSocket)
    with pytest.raises(RuntimeError, match="65535"):
        s8.app.reserve_port("127.0.0.1", 65535)
    assert attempted == [65535]
    assert all(sock.closed for sock in sockets)
