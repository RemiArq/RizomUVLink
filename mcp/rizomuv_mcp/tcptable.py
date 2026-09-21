"""Who else is connected to a RizomUVLink port (Windows TCP table).

The link serves one client per port and has no in-band way to tell that another client
is there: a probe request sent during another client's command would silently receive
that command's answer. The OS knows, though: every client has an ESTABLISHED IPv4
connection whose remote end is 127.0.0.1:<port>. RizomUV binds 127.0.0.1 only, so IPv4
is the whole picture.
"""
import logging
import os
import socket
import struct
import sys

from . import discovery

log = logging.getLogger(__name__)

_AF_INET = 2
_TCP_TABLE_OWNER_PID_ALL = 5
_MIB_TCP_STATE_LISTEN = 2
_MIB_TCP_STATE_ESTAB = 5
_ERROR_INSUFFICIENT_BUFFER = 122


def _rows():
    """(state, local addr, local port, remote addr, remote port, pid) of every IPv4 row."""
    import ctypes
    import ctypes.wintypes as wt

    class Row(ctypes.Structure):   # MIB_TCPROW_OWNER_PID
        _fields_ = [("state", wt.DWORD), ("local_addr", wt.DWORD), ("local_port", wt.DWORD),
                    ("remote_addr", wt.DWORD), ("remote_port", wt.DWORD), ("pid", wt.DWORD)]

    fn = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    fn.restype = wt.DWORD
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD), wt.BOOL, wt.ULONG, ctypes.c_int, wt.ULONG]

    size = wt.DWORD(0)
    buf = None
    for _ in range(8):   # the table can grow between the sizing call and the real one
        rc = fn(buf, ctypes.byref(size), False, _AF_INET, _TCP_TABLE_OWNER_PID_ALL, 0)
        if rc == 0:
            break
        if rc != _ERROR_INSUFFICIENT_BUFFER:
            raise OSError(rc, "GetExtendedTcpTable failed")
        buf = ctypes.create_string_buffer(size.value + 32 * ctypes.sizeof(Row))
        size = wt.DWORD(len(buf))
    else:
        raise OSError(_ERROR_INSUFFICIENT_BUFFER, "GetExtendedTcpTable kept growing")
    if buf is None:
        return []
    count = wt.DWORD.from_buffer(buf).value            # MIB_TCPTABLE_OWNER_PID.dwNumEntries
    rows = (Row * count).from_buffer(buf, ctypes.sizeof(wt.DWORD))
    # addresses and ports are in network byte order in the low bytes of their DWORD
    return [(r.state, socket.inet_ntoa(struct.pack("<I", r.local_addr)), socket.ntohs(r.local_port & 0xFFFF),
             socket.inet_ntoa(struct.pack("<I", r.remote_addr)), socket.ntohs(r.remote_port & 0xFFFF), r.pid)
            for r in rows]


def _image_name(pid):
    image = discovery.process_image(pid)
    return os.path.basename(image) if image else "?"


def foreign_clients(port, exclude_pids=frozenset()):
    """[(pid, image basename)] of the processes connected to 127.0.0.1:<port>, other than
    exclude_pids (this server, its link worker, the instance itself). [] off Windows, or
    when the table cannot be read -- this check is a safety net, never a reason to fail."""
    if sys.platform != "win32":
        return []
    try:
        rows = _rows()
    except Exception as e:   # noqa: BLE001 -- ctypes failures come in many shapes
        log.debug("TCP table unavailable: %r", e)
        return []
    exclude = {int(p) for p in exclude_pids}
    pids = sorted({pid for state, _, _, raddr, rport, pid in rows
                   if state == _MIB_TCP_STATE_ESTAB and raddr == "127.0.0.1" and rport == port
                   and pid not in exclude})
    return [(pid, _image_name(pid)) for pid in pids]


def _listeners():
    """{port: {pids}} of the IPv4 listening sockets, any local address. None when unknown
    (off Windows, or the table cannot be read), so a caller can tell "nobody" from "no idea"."""
    if sys.platform != "win32":
        return None
    try:
        rows = _rows()
    except Exception as e:   # noqa: BLE001
        log.debug("TCP table unavailable: %r", e)
        return None
    found = {}
    for state, _, lport, _, _, pid in rows:
        if state == _MIB_TCP_STATE_LISTEN:
            found.setdefault(lport, set()).add(pid)
    return found


def listening_pids(port):
    """Pids listening on TCP <port>, or None when unknown."""
    found = _listeners()
    return None if found is None else found.get(port, set())


def listening_ports():
    """Every TCP port something listens on, or None when unknown."""
    found = _listeners()
    return None if found is None else set(found)
