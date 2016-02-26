# -*- coding: utf-8 -*-

import errno
import sys
from threading import Timer

import pytest

from pyxs.client import RVar, Router, Client
from pyxs.connection import UnixSocketConnection, XenBusConnection
from pyxs.exceptions import InvalidPath, InvalidPermission, \
    UnexpectedPacket, PyXSError
from pyxs._internal import NUL, Op, Packet


def setup_function(f):
    try:
        with Client() as c:
            c.delete(b"/foo")
    except PyXSError:
        pass


def test_init():
    # a) UnixSocketConnection
    c = Client()
    assert c.tx_id == 0
    assert isinstance(c.router.connection, UnixSocketConnection)
    assert not c.router_thread.is_alive()

    c = Client(unix_socket_path="/var/run/xenstored/socket")
    assert isinstance(c.router.connection, UnixSocketConnection)
    assert not c.router_thread.is_alive()

    # b) XenBusConnection
    c = Client(xen_bus_path="/dev/xen/xenbus")
    assert isinstance(c.router.connection, XenBusConnection)
    assert not c.router_thread.is_alive()


virtualized = pytest.mark.skipif(
    "not os.path.exists('/dev/xen') or not Client.SU")


@virtualized
def test_context_manager():
    # a) no transaction is running
    c = Client()
    assert not c.router_thread.is_alive()

    with c:
        assert c.router_thread.is_alive()

    assert not c.router_thread.is_alive()


@virtualized
def test_execute_command_invalid_characters():
    with Client() as c:
        c.execute_command(Op.WRITE, b"/foo/bar" + NUL, b"baz")

        with pytest.raises(ValueError):
            c.execute_command(Op.DEBUG, b"\x07foo" + NUL)


@virtualized
def test_execute_command_error():
    with Client() as c:
        with pytest.raises(PyXSError):
            c.execute_command(Op.READ, b"/unexisting/path" + NUL)

        with pytest.raises(PyXSError):
            c.execute_command(-42, b"/unexisting/path" + NUL)


def monkeypatch_router(client, response_packet):
    class FakeRouter:
        def send(self, packet):
            rvar = RVar()
            rvar.set(response_packet)
            return rvar

        def terminate(self):
            pass

    client.close()
    client.router = FakeRouter()


@virtualized
def test_execute_command_invalid_op():
    with Client() as c:
        monkeypatch_router(c, Packet(Op.DEBUG, b"/local" + NUL))

        with pytest.raises(UnexpectedPacket):
            c.execute_command(Op.READ, b"/local" + NUL)


@virtualized
def test_execute_command_invalid_tx_id():
    with Client() as c:
        monkeypatch_router(c, Packet(Op.READ, b"/local" + NUL, tx_id=42))

        with pytest.raises(UnexpectedPacket):
            c.execute_command(Op.READ, b"/local" + NUL)


@pytest.mark.parametrize("op", [
    "read", "mkdir", "delete", "list", "exists", "get_perms"
])
def test_check_path(op):
    with pytest.raises(InvalidPath):
        getattr(Client(), op)(b"INVALID%PATH!")


@pytest.yield_fixture(params=[UnixSocketConnection, XenBusConnection])
def client(request):
    c = Client(router=Router(request.param()))
    try:
        yield c.__enter__()
    finally:
        c.__exit__(sys.exc_info())


@virtualized
def test_read(client):
    # a) non-existant path.
    try:
        client.read(b"/foo/bar")
    except PyXSError as e:
        assert e.args[0] == errno.ENOENT

    # b) using a default.
    client.read(b"/foo/bar", b"baz") == b"baz"

    # c) OK-case (`/local` is allways in place).
    assert client.read("/local") == b""
    assert client["/local"] == b""

    # d) No read perms (should be ran in DomU)?


@virtualized
def test_write(client):
    client.write(b"/foo/bar", b"baz")
    assert client.read(b"/foo/bar") == b"baz"

    client[b"/foo/bar"] = b"boo"
    assert client[b"/foo/bar"] == b"boo"

    # b) No write perms (should be ran in DomU)?


def test_write_invalid():
    with pytest.raises(InvalidPath):
        Client().write(b"INVALID%PATH!", b"baz")


@virtualized
def test_mkdir(client):
    client.mkdir(b"/foo/bar")
    assert client.list(b"/foo") == [b"bar"]
    assert client.read(b"/foo/bar") == b""


@virtualized
def test_delete(client):
    client.mkdir(b"/foo/bar")
    client.delete(b"/foo/bar")

    try:
        client.read(b"/foo/bar")
    except PyXSError as e:
        assert e.args[0] == errno.ENOENT

    assert client.read(b"/foo") == b""


@virtualized
def test_list(client):
    client.mkdir(b"/foo/bar")

    # a) OK-case.
    assert client.list(b"/foo") == [b"bar"]
    assert client.list(b"/foo/bar") == []

    # b) directory doesn't exist.
    try:
        client.list(b"/path/to/something")
    except PyXSError as e:
        assert e.args[0] == errno.ENOENT

    # c) No list perms (should be ran in DomU)?


@virtualized
def test_exists(client):
    # a) Path exists.
    client.mkdir(b"/foo/bar")
    assert client.exists(b"/foo/bar")

    # b) Path does not exist.
    client.delete(b"/foo/bar")
    assert not client.exists(b"/foo/bar")

    # c) No list perms (should be ran in DomU)?


@virtualized
def test_perms(client):
    client.delete(b"/foo")
    client.mkdir(b"/foo/bar")

    # a) checking default perms -- full access.
    assert client.get_perms(b"/foo/bar") == [b"n0"]

    # b) setting new perms, and making sure it worked.
    client.set_perms(b"/foo/bar", [b"b0"])
    assert client.get_perms(b"/foo/bar") == [b"b0"]

    # c) conflicting perms -- XenStore doesn't care.
    client.set_perms(b"/foo/bar", [b"b0", b"n0", b"r0"])
    assert client.get_perms(b"/foo/bar") == [b"b0", b"n0", b"r0"]

    # d) invalid permission format.
    with pytest.raises(InvalidPermission):
        client.set_perms(b"/foo/bar", [b"x0"])


def test_set_perms_invalid():
    with pytest.raises(InvalidPath):
        Client().set_perms(b"INVALID%PATH!", [])

    with pytest.raises(InvalidPermission):
        Client().set_perms(b"/foo/bar", [b"z"])


@virtualized
def test_get_domain_path(client):
    # Note, that XenStore doesn't care if a domain exists, but
    # according to the spec we shouldn't really count on a *valid*
    # reply in that case.
    assert client.get_domain_path(0) == b"/local/domain/0"
    assert client.get_domain_path(999) == b"/local/domain/999"


@virtualized
def test_is_domain_introduced(client):
    for domid in map(int, client.list("/local/domain")):
        assert client.is_domain_introduced(domid)

    assert not client.is_domain_introduced(999)


@virtualized
def test_monitor(client):
    if isinstance(client.router.connection, XenBusConnection):
        # http://lists.xen.org/archives/html/xen-users/2016-02/msg00159.html
        pytest.xfail("unsupported connection")

    client.write(b"/foo/bar", b"baz")
    m = client.monitor()
    m.watch(b"/foo/bar", b"boo")

    waiter = m.wait()
    # a) we receive the first event immediately, so `next` doesn't
    #    block.
    assert next(waiter) == (b"/foo/bar", b"boo")

    # b) before the second call we have to make sure someone
    #    will change the path being watched.
    Timer(.1, lambda: client.write(b"/foo/bar", b"baz")).run()
    assert next(waiter) == (b"/foo/bar", b"boo")

    # c) changing a children of the watched path triggers watch
    #    event as well.
    Timer(.1, lambda: client.write(b"/foo/bar/baz", b"???")).run()
    assert next(waiter) == (b"/foo/bar/baz", b"boo")


@pytest.mark.parametrize("op", ["watch", "unwatch"])
def test_check_watch_path(op):
    with pytest.raises(InvalidPath):
        getattr(Client().monitor(), op)(b"INVALID%PATH", b"token")

    with pytest.raises(InvalidPath):
        getattr(Client().monitor(), op)(b"@arbitraryPath", b"token")


@virtualized
def test_transaction(client):
    assert client.tx_id == 0
    client.transaction()
    assert client.tx_id != 0


@virtualized
def test_nested_transaction(client):
    client.transaction()

    with pytest.raises(PyXSError):
        client.transaction()


@virtualized
def test_transaction_rollback(client):
    assert not client.exists(b"/foo/bar")
    client.transaction()
    client[b"/foo/bar"] = b"boo"
    client.rollback()
    assert client.tx_id == 0
    assert not client.exists(b"/foo/bar")


@virtualized
def test_transaction_commit_ok(client):
    assert not client.exists(b"/foo/bar")
    client.transaction()
    client[b"/foo/bar"] = b"boo"
    assert client.commit()
    assert client.tx_id == 0
    assert client[b"/foo/bar"] == b"boo"


@virtualized
def test_transaction_commit_retry(client):
    def writer():
        with Client() as other:
            other[b"/foo/bar"] = b"unexpected write"

    assert not client.exists(b"/foo/bar")
    client.transaction()
    writer()
    client[b"/foo/bar"] = b"boo"
    assert not client.commit()


@virtualized
def test_transaction_exception():
    try:
        with Client() as c:
            assert not c.exists(b"/foo/bar")
            c.transaction()
            c[b"/foo/bar"] = b"boo"
            raise ValueError
    except ValueError:
        pass

    with Client() as c:
        assert not c.exists(b"/foo/bar")


@virtualized
def test_uncommitted_transaction():
    with pytest.raises(PyXSError):
        with Client() as c:
            c.transaction()


@virtualized
def test_monitor_leftover_events(client):
    if isinstance(client.router.connection, XenBusConnection):
        # http://lists.xen.org/archives/html/xen-devel/2016-02/msg03816.html
        pytest.xfail("unsupported connection")

    with client.monitor() as m:
        m.watch(b"/foo/bar", b"boo")

        def writer():
            for i in range(128):
                client[b"/foo/bar"] = str(i).encode()

        Timer(.1, writer).run()
        m.unwatch(b"/foo/bar", b"boo")
        assert not m.events.empty()


@virtualized
def test_header_decode_error(client):
    # The following packet's header cannot be decoded to UTF-8, but
    # we still need to handle it somehow.
    p = Packet(Op.WRITE, b"/foo", rq_id=0, tx_id=128)
    client.router.send(p)
