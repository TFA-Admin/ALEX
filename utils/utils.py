# utils.py
import socket
import psutil


def get_lan_ip():
    """
    Reads local network interfaces only — never opens a connection
    or sends a packet anywhere, including to determine routing.
    """
    try:
        for addrs in psutil.net_if_addrs().values():
            for addr in addrs:
                if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                    return addr.address
    except Exception:
        pass

    return "127.0.0.1"