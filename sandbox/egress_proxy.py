"""Per-tenant egress proxy (HTTP CONNECT only), one instance per run.

It is the sandbox's only route out: the sandbox sits on an internal Docker network
with no gateway, and this proxy is the one container attached to both networks.
Each CONNECT is checked against the tenant's host allowlist, the name is resolved
once, private and link-local addresses are refused, and the proxy connects to the
exact address it checked. Every decision is logged as one JSON line on stdout.
It does not terminate TLS: it sees host names, not request contents.
"""
import ipaddress
import json
import select
import socket
import sys
import threading
import time

ALLOW = [h.strip().lower() for h in sys.argv[1].split(",") if h.strip()] if len(sys.argv) > 1 else []
PORT = 3128


def log(**kw):
    kw["ts"] = round(time.time(), 3)
    sys.stdout.write(json.dumps(kw) + "\n")
    sys.stdout.flush()


def host_allowed(host):
    host = host.lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in ALLOW)


def public(ip):
    a = ipaddress.ip_address(ip)
    return a.is_global and not a.is_multicast


def pipe(a, b):
    socks = [a, b]
    try:
        while True:
            r, _, x = select.select(socks, [], socks, 300)
            if x or not r:
                return
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    finally:
        a.close()
        b.close()


def handle(client, addr):
    try:
        client.settimeout(10)
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 8192:
            chunk = client.recv(4096)
            if not chunk:
                return
            head += chunk
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            log(decision="deny", reason="only CONNECT is supported", request=line[:200])
            client.sendall(b"HTTP/1.1 405 Only CONNECT\r\n\r\n")
            return
        target = parts[1]
        host, _, port = target.rpartition(":")
        host = host.strip("[]")
        port = int(port or 443)
        if port not in (443,):
            log(decision="deny", host=host, port=port, reason="port not allowed")
            client.sendall(b"HTTP/1.1 403 Port not allowed\r\n\r\n")
            return
        try:
            ipaddress.ip_address(host)
            log(decision="deny", host=host, port=port, reason="raw IP addresses are not allowed")
            client.sendall(b"HTTP/1.1 403 IP literal\r\n\r\n")
            return
        except ValueError:
            pass
        if not host_allowed(host):
            log(decision="deny", host=host, port=port, reason="host not in tenant allowlist")
            client.sendall(b"HTTP/1.1 403 Host not allowed\r\n\r\n")
            return
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ip = infos[0][4][0]
        if not public(ip):
            log(decision="deny", host=host, port=port, ip=ip, reason="resolves to a non-public address")
            client.sendall(b"HTTP/1.1 403 Non-public address\r\n\r\n")
            return
        upstream = socket.create_connection((ip, port), timeout=10)
        log(decision="allow", host=host, port=port, ip=ip)
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        client.settimeout(None)
        upstream.settimeout(None)
        pipe(client, upstream)
    except Exception as e:  # noqa: BLE001 - a proxy must never die on one bad client
        log(decision="error", error=str(e)[:200])
        try:
            client.close()
        except OSError:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(64)
    log(decision="ready", allow=ALLOW)
    while True:
        c, a = srv.accept()
        threading.Thread(target=handle, args=(c, a), daemon=True).start()


if __name__ == "__main__":
    main()
