#!/usr/bin/env python3
"""corral egress proxy.

Runs in its own container: attached to the cell's internal network (so the
sandbox can reach it as ``proxy:3128``) and to the default bridge (so it can
reach the internet). The sandbox has no other route out.

Rules:
- Only ``CONNECT host:443``. No plain HTTP, no other ports.
- ``host`` must be on the allowlist the gateway writes to
  /run/corral/allow.txt (exact names, or ``*.example.com`` for subdomains).
- The proxy resolves DNS itself and refuses any host that resolves to a
  non-public address (loopback, private, link-local, metadata endpoints).
  It then connects to the address it checked, not to the name again.

Every decision is logged as one JSON line on stdout; the gateway tails it.
Stdlib only, no third-party code in this container.
"""

import asyncio
import ipaddress
import json
import re
import socket
import sys
import time

ALLOW_FILE = "/run/corral/allow.txt"
IDLE_TIMEOUT = 300
HOST_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")


def log(**fields):
    fields["ts"] = round(time.time(), 3)
    sys.stdout.write(json.dumps(fields) + "\n")
    sys.stdout.flush()


def allowlist():
    try:
        with open(ALLOW_FILE) as f:
            return {line.strip().lower() for line in f if line.strip() and not line.startswith("#")}
    except FileNotFoundError:
        return set()


def host_allowed(host, entries):
    for entry in entries:
        if entry.startswith("*."):
            if host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False


async def resolve_public(host, port):
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise OSError("no addresses")
    chosen = None
    for family, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise PermissionError(f"resolves to non-public address {ip}")
        if chosen is None:
            chosen = (family, str(ip))
    return chosen


async def pipe(reader, writer, counter, key):
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), IDLE_TIMEOUT)
            if not data:
                break
            counter[key] += len(data)
            writer.write(data)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def reply(writer, status, text):
    writer.write(f"HTTP/1.1 {status} {text}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())


async def handle(reader, writer):
    host, port = "", 0
    try:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return
        parts = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
        if len(parts) != 3:
            reply(writer, 400, "Bad Request")
            return
        method, target, _ = parts
        if method != "CONNECT":
            log(decision="deny", host=target[:200], port=0, reason="only HTTPS (CONNECT) is allowed")
            reply(writer, 405, "Method Not Allowed")
            return
        host, _, port_s = target.rpartition(":")
        host = host.strip("[]").lower().rstrip(".")
        try:
            port = int(port_s)
        except ValueError:
            port = 0
        if not HOST_RE.fullmatch(host) or len(host) > 253:
            log(decision="deny", host=host[:200], port=port, reason="invalid host name")
            reply(writer, 400, "Bad Request")
            return
        if port != 443:
            log(decision="deny", host=host, port=port, reason="only port 443 is allowed")
            reply(writer, 403, "Forbidden")
            return
        if not host_allowed(host, allowlist()):
            log(decision="deny", host=host, port=port, reason="not on the allowlist")
            reply(writer, 403, "Forbidden")
            return
        try:
            family, ip = await asyncio.wait_for(resolve_public(host, port), 10)
        except PermissionError as e:
            log(decision="deny", host=host, port=port, reason=str(e))
            reply(writer, 403, "Forbidden")
            return
        except (OSError, asyncio.TimeoutError) as e:
            log(decision="deny", host=host, port=port, reason=f"dns: {e}")
            reply(writer, 502, "Bad Gateway")
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(ip, port, family=family), 10)
        except (OSError, asyncio.TimeoutError) as e:
            log(decision="error", host=host, port=port, reason=f"connect: {e}")
            reply(writer, 502, "Bad Gateway")
            return
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        counter = {"up": 0, "down": 0}
        started = time.time()
        await asyncio.gather(
            pipe(reader, up_writer, counter, "up"),
            pipe(up_reader, writer, counter, "down"),
        )
        log(decision="allow", host=host, port=port, ip=ip, bytes_up=counter["up"], bytes_down=counter["down"],
            seconds=round(time.time() - started, 2))
    except Exception as e:  # never let one connection take the proxy down
        log(decision="error", host=host[:200], port=port, reason=type(e).__name__)
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", 3128, limit=16384)
    log(decision="ready", host="", port=3128)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
