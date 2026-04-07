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
daemon.backend
~~~~~~~~~~~~~~~~~

This is the core server module that handles all incoming TCP connections.
We implemented three different non-blocking strategies here so we can
compare how each one behaves under load:

  1. Threading   -- spawn a new thread per client (simplest approach)
  2. Callback    -- use selectors to get notified when data is ready
  3. Coroutine   -- use asyncio so one thread can juggle many clients

The mode is picked by changing the `mode_async` variable below.
We default to "coroutine" since it scales best for our chat app,
but switching to "threading" or "callback" is just one line change.

Usage:
    >>> create_backend("127.0.0.1", 9000, routes={})
"""

import socket
import threading
import argparse
import asyncio
import inspect
import selectors

from .response import *
from .httpadapter import HttpAdapter
from .dictionary import CaseInsensitiveDict

# We use a global selector for the callback/event-driven mode.
# It watches file descriptors and tells us when they're ready for I/O.
sel = selectors.DefaultSelector()

# Change this to "threading" or "callback" to switch non-blocking strategy.
# "coroutine" gives us the best concurrency for the chat app.
mode_async = "coroutine"


def handle_client(ip, port, conn, addr, routes):
    """Called inside a dedicated thread -- each client gets their own.

    This is the simplest non-blocking approach: the main loop keeps
    accepting new connections while each client is served in parallel.
    """
    print("[Backend] Accepted connection from {}".format(addr))
    daemon = HttpAdapter(ip, port, conn, addr, routes)
    daemon.handle_client(conn, addr, routes)


def handle_client_callback(conn, mask, ip, port, routes):
    """Triggered by the selector when a client socket has data to read.

    Unlike threading, we don't create a new thread here -- the selector
    just tells us "hey, this socket is ready" and we handle it inline.
    After we're done, we unregister it so it doesn't fire again.
    """
    addr = conn.getpeername()
    print("[Backend] Callback readiness for {}".format(addr))
    daemon = HttpAdapter(ip, port, conn, addr, routes)
    daemon.handle_client(conn, addr, routes)
    # Done with this client, stop watching its socket
    sel.unregister(conn)


def accept_wrapper(sock, mask, ip, port, routes):
    """Fires when the *server* socket has a new incoming connection.

    We accept the connection, set it to non-blocking, then register it
    with the selector so we get notified when data arrives on it.
    """
    conn, addr = sock.accept()
    print("[Backend] Accepted connection (Callback) from {}".format(addr))
    # Non-blocking so recv() won't stall the entire event loop
    conn.setblocking(False)
    # Now watch this client socket for incoming data
    sel.register(conn, selectors.EVENT_READ, data=(handle_client_callback, ip, port, routes))


async def handle_client_coroutine(reader, writer, routes):
    """Handles one client using async/await -- no threads needed.

    The event loop can pause this coroutine while we wait for data
    and go serve other clients in the meantime. That's why asyncio
    can handle thousands of connections on a single thread.
    """
    addr = writer.get_extra_info("peername")
    print("[Backend] Accepted connection (Coroutine) from {}".format(addr))

    daemon = HttpAdapter(None, None, None, addr, routes)
    try:
        await daemon.handle_client_coroutine(reader, writer)
    except Exception as e:
        print("[Backend] Coroutine Error: {}".format(e))
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def async_server(ip="0.0.0.0", port=7000, routes={}):
    """Boots up the asyncio TCP server.

    We pass a lambda so that each new connection gets the shared `routes`
    dict -- asyncio.start_server only gives us (reader, writer) by default,
    but our handler also needs routes to know which function to call.
    """
    print("[Backend] **ASYNC** listening on {}:{}".format(ip, port))

    if routes != {}:
        print("[Backend] Route settings:")
        for key, value in routes.items():
            is_async = "**ASYNC** " if inspect.iscoroutinefunction(value) else ""
            print("   + ('{}', '{}'): {}{}".format(key[0], key[1], is_async, str(value)))

    server = await asyncio.start_server(
        lambda r, w: handle_client_coroutine(r, w, routes),
        ip, port
    )

    async with server:
        await server.serve_forever()


def run_backend(ip, port, routes):
    """The main dispatcher -- picks the right non-blocking strategy and runs it.

    We read the global `mode_async` to decide:
      - "coroutine" => hand off to asyncio (best for our chat app)
      - "callback"  => event loop with selectors (more manual but educational)
      - anything else => classic multi-threading (one thread per client)
    """
    global mode_async

    print("[Backend] Running in mode: {}".format(mode_async))

    # --- Coroutine path: let asyncio handle everything ---
    if mode_async == "coroutine":
        try:
            asyncio.run(async_server(ip, port, routes))
        except KeyboardInterrupt:
            print("\n[Backend] Server stopped by user (Ctrl+C)")
        return

    # --- For threading and callback, we need a raw TCP socket ---
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # SO_REUSEADDR lets us restart quickly without "port already in use"
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server.bind((ip, port))
        server.listen(50)

        print("[Backend] Listening on {}:{}".format(ip, port))
        if routes != {}:
            print("[Backend] Route settings:")
            for key, value in routes.items():
                is_async = "**ASYNC** " if inspect.iscoroutinefunction(value) else ""
                print("   + ('{}', '{}'): {}{}".format(key[0], key[1], is_async, str(value)))

        # --- Callback path: register with selector and loop on events ---
        if mode_async == "callback":
            server.setblocking(False)
            sel.register(server, selectors.EVENT_READ, data=(accept_wrapper, ip, port, routes))
            print("[Backend] Callback mode: event loop started")
            while True:
                # Block until at least one socket is ready
                events = sel.select(timeout=None)
                for key, mask in events:
                    # Unpack the handler + args we stored during register()
                    callback, *args = key.data
                    callback(key.fileobj, mask, *args)

        # --- Threading path: spawn a thread for each accepted connection ---
        else:
            print("[Backend] Threading mode: accepting connections")
            while True:
                conn, addr = server.accept()
                # daemon=True so threads die when the main process exits
                client_thread = threading.Thread(
                    target=handle_client,
                    args=(ip, port, conn, addr, routes),
                    daemon=True
                )
                client_thread.start()

    except socket.error as e:
        print("Socket error: {}".format(e))
    finally:
        server.close()


def create_backend(ip, port, routes={}):
    """Simple wrapper so other modules can just call create_backend()."""
    run_backend(ip, port, routes)
