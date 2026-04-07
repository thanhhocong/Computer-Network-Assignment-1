#
# Copyright (C) 2026 pdnguyen of HCMC University of Technology VNU-HCM.
# All rights reserved.
# This file is part of the CO3093/CO3094 course.
#
# AsynapRous release
#
# The authors hereby grant to Licensee personal permission to use
# and modify the Licensed Source Code for the sole purpose of studying
# while attending the course
#

"""
daemon.proxy
~~~~~~~~~~~~~~~~~

A reverse proxy that sits between clients and backend servers.

The idea is simple: client sends a request to us, we look at the Host
header to figure out which backend should handle it, forward the request
there, get the response, and pipe it back to the client.

For non-blocking behavior, we use one thread per client connection.
This way the main accept() loop never gets stuck waiting for a slow
backend -- each client is handled independently in its own thread.

The routing config comes from proxy.conf and supports round-robin
load balancing when multiple backends are listed for the same host.
"""

import socket
import threading
from .response import Response
from .httpadapter import HttpAdapter
from .dictionary import CaseInsensitiveDict

# Default routing table (overridden by proxy.conf at runtime)
PROXY_PASS = {
    "192.168.56.103:8080": ('192.168.56.103', 9000),
    "app1.local": ('192.168.56.103', 9001),
    "app2.local": ('192.168.56.103', 9002),
}

# Tracks the current index for round-robin per hostname.
# e.g., {"app2.local": 1} means the next request goes to backend #1
routing_counters = {}


def forward_request(host, port, request):
    """Opens a TCP connection to the backend and relays the request.

    We send the raw HTTP request as-is (no modification), then read
    the response in chunks until the backend closes the connection.
    If the backend is down, we return a 502 Bad Gateway.
    """
    backend = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        backend.connect((host, port))
        backend.sendall(request.encode())

        response = b""
        while True:
            chunk = backend.recv(4096)
            if not chunk:
                break
            response += chunk
        return response
    except socket.error as e:
        print("[Proxy] Backend connection error ({}:{}): {}".format(host, port, e))
        return (
            "HTTP/1.1 502 Bad Gateway\r\n"
            "Content-Type: text/plain\r\n"
            "Content-Length: 15\r\n"
            "Connection: close\r\n\r\n"
            "502 Bad Gateway"
        ).encode('utf-8')
    finally:
        backend.close()


def resolve_routing_policy(hostname, routes):
    """Given a hostname, figures out which backend to send the request to.

    If there are multiple backends for one hostname (load balancing),
    we rotate through them using round-robin: first request goes to
    backend 0, next to backend 1, then back to 0, etc.
    """
    target_info = routes.get(hostname)

    # Unknown host -- fall back to localhost:9000
    if not target_info:
        return '127.0.0.1', '9000'

    proxy_map, policy = target_info

    if isinstance(proxy_map, list):
        if len(proxy_map) == 0:
            return '127.0.0.1', '9000'

        # Pick the next backend in the round-robin rotation
        idx = routing_counters.get(hostname, 0)
        target = proxy_map[idx]
        # Advance the counter for next time, wrapping around
        routing_counters[hostname] = (idx + 1) % len(proxy_map)
        proxy_host, proxy_port = target.split(":", 1)
    else:
        # Only one backend -- no load balancing needed
        proxy_host, proxy_port = proxy_map.split(":", 1)

    return proxy_host, proxy_port


def handle_client(ip, port, conn, addr, routes):
    """Runs in its own thread -- handles one client from start to finish.

    We read the request, pull out the Host header to figure out where
    to forward it, then relay the backend's response back to the client.
    """
    try:
        data = conn.recv(4096).decode()
        if not data:
            return

        # The Host header tells us which backend this request is for
        hostname = ""
        for line in data.splitlines():
            if line.lower().startswith('host:'):
                hostname = line.split(':', 1)[1].strip()
                break

        print("[Proxy] Request from {} to Host: {}".format(addr, hostname))

        resolved_host, resolved_port = resolve_routing_policy(hostname, routes)

        try:
            resolved_port = int(resolved_port)
        except ValueError:
            print("[Proxy] Invalid port number")
            resolved_port = 9000

        print("[Proxy] Forwarding to {}:{}".format(resolved_host, resolved_port))
        response = forward_request(resolved_host, resolved_port, data)

        conn.sendall(response)
    except Exception as e:
        print("[Proxy] Error handling client {}: {}".format(addr, e))
    finally:
        conn.close()


def run_proxy(ip, port, routes):
    """Main proxy loop -- accepts connections and spawns a thread for each.

    This is our non-blocking mechanism for the proxy: the main thread
    only does accept(), and each client gets its own thread. So even
    if one backend is slow, other clients aren't blocked.
    """
    proxy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Lets us restart without getting "address already in use"
    proxy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        proxy.bind((ip, port))
        proxy.listen(50)
        print("[Proxy] Listening on {}:{}".format(ip, port))

        while True:
            conn, addr = proxy.accept()
            # Each client gets its own thread so the proxy stays responsive
            client_thread = threading.Thread(
                target=handle_client,
                args=(ip, port, conn, addr, routes),
                daemon=True   # Auto-cleanup when main process exits
            )
            client_thread.start()

    except socket.error as e:
        print("[Proxy] Socket error: {}".format(e))
    finally:
        proxy.close()


def create_proxy(ip, port, routes):
    """Entry point -- called from start_proxy.py."""
    run_proxy(ip, port, routes)
