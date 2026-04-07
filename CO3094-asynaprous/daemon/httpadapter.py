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
daemon.httpadapter
~~~~~~~~~~~~~~~~~

This is the bridge between raw TCP sockets and our Request/Response objects.
Every incoming connection goes through HttpAdapter -- it reads the raw bytes,
turns them into a Request, finds the matching route handler, runs it,
then wraps the result in a proper HTTP Response and sends it back.

We have two versions of handle_client:
  - handle_client()           => synchronous, used by threading & callback
  - handle_client_coroutine() => async, used by the coroutine mode

Both do the same thing conceptually, just different I/O styles.
"""

from .request import Request
from .response import Response
from .dictionary import CaseInsensitiveDict

import asyncio
import inspect


def get_encoding_from_headers(headers):
    """For now we just assume UTF-8. Could parse charset from Content-Type later."""
    return 'utf-8'


class HttpAdapter:
    """Sits between the socket layer and our app logic.

    Think of it as the "glue" -- it doesn't know what the app does,
    it just knows how to read HTTP, call the right handler, and
    package the response back into valid HTTP bytes.
    """

    __attrs__ = [
        "ip", "port", "conn", "connaddr", "routes", "request", "response",
    ]

    def __init__(self, ip, port, conn, connaddr, routes):
        self.ip = ip
        self.port = port
        self.conn = conn
        self.connaddr = connaddr
        self.routes = routes
        self.request = Request()
        self.response = Response()

    def handle_client(self, conn, addr, routes):
        """Synchronous version -- used when mode is threading or callback.

        The flow is pretty straightforward:
        1. Read raw bytes from socket
        2. Parse into a Request object (method, path, headers, body)
        3. Check if any registered route matches
        4. If yes, call that handler and collect the result
        5. Package everything into an HTTP response and send it back
        """
        self.conn = conn
        self.connaddr = addr
        req = self.request
        resp = self.response

        raw_msg = conn.recv(4096).decode('utf-8', errors='ignore')
        if not raw_msg:
            conn.close()
            return

        req.prepare(raw_msg, routes)
        print("[HttpAdapter] Invoke handle_client connection {}".format(addr))

        response_data = None

        if req.hook:
            # The hook is the function registered via @app.route(...)
            result = req.hook(headers=req.headers, body=req.body)

            # Handlers can return a 3-tuple: (body, status_code, extra_headers)
            # This lets the app set redirects, cookies, auth challenges, etc.
            if isinstance(result, tuple) and len(result) == 3:
                app_body, app_status, app_headers = result
                resp._content = app_body if isinstance(app_body, bytes) else b""
                resp.status_code = app_status

                # Map status codes to their standard reason phrases
                reasons = {
                    200: "OK", 301: "Moved Permanently", 302: "Found",
                    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
                    404: "Not Found", 500: "Internal Server Error",
                }
                resp.reason = reasons.get(app_status, "OK")

                # Pull out special headers like Set-Cookie and Location
                if isinstance(app_headers, dict):
                    for k, v in app_headers.items():
                        if k.lower() == 'set-cookie':
                            resp.headers['Set-Cookie'] = v
                        elif k.lower() == 'location':
                            resp.headers['Location'] = v
                        else:
                            resp.headers[k] = v

                response_data = app_body

            # Simple return: just a dict, string, or bytes => 200 OK
            elif isinstance(result, (dict, str, bytes)):
                response_data = result
                resp.status_code = 200
                resp.reason = "OK"
            else:
                response_data = result

        # Let Response handle the final packaging (headers + body)
        response_bytes = resp.build_response(req, envelop_content=response_data)
        conn.sendall(response_bytes)
        conn.close()

    async def handle_client_coroutine(self, reader, writer):
        """Async version -- used when mode is coroutine (asyncio).

        Same logic as handle_client() above, but uses await for I/O
        so the event loop can serve other clients while we wait.
        Also supports calling async route handlers (like our /slow endpoint).
        """
        addr = writer.get_extra_info("peername")
        print("[HttpAdapter] New async connection from {}".format(addr))

        try:
            data = await reader.read(4096)
            if not data:
                return

            incoming_msg = data.decode('utf-8', errors='ignore')

            # Fresh Request/Response for each connection
            req = Request()
            req.prepare(incoming_msg, routes=self.routes)

            resp = Response()
            response_data = None

            if req.hook:
                # Need to check if the handler is async or sync
                # because we can't just `await` a regular function
                if inspect.iscoroutinefunction(req.hook):
                    result = await req.hook(headers=req.headers, body=req.body)
                else:
                    result = req.hook(headers=req.headers, body=req.body)

                # Same 3-tuple unpacking as the sync version
                if isinstance(result, tuple) and len(result) == 3:
                    response_data, status_code, extra = result
                    resp.status_code = status_code

                    reasons = {
                        200: "OK", 301: "Moved Permanently", 302: "Found",
                        400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
                        404: "Not Found", 500: "Internal Server Error",
                    }
                    resp.reason = reasons.get(status_code, "OK")

                    # Extra dict can contain cookies or special headers
                    if isinstance(extra, dict):
                        for k, v in extra.items():
                            if k.lower() == 'set-cookie':
                                resp.headers['Set-Cookie'] = v
                            elif k.lower() == 'location':
                                resp.headers['Location'] = v
                            else:
                                resp.cookies[k] = v
                else:
                    response_data = result
                    resp.status_code = 200
            else:
                # No route matched -- try serving as a static file
                mime_type = resp.get_mime_type(req.path)
                if 'text/html' in mime_type or 'text/css' in mime_type or \
                   'image/' in mime_type or 'javascript' in mime_type:
                    # Let build_response() handle file serving
                    response_data = None
                else:
                    response_data = {"error": "Not Found", "path": req.path}
                    resp.status_code = 404

            msg_to_send = resp.build_response(req, envelop_content=response_data)

            writer.write(msg_to_send)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        except Exception as e:
            print("[HttpAdapter] Coroutine Error: {}".format(e))
            import traceback
            traceback.print_exc()
            try:
                writer.close()
            except Exception:
                pass

    def extract_cookies(self, req):
        """Pulls cookies out of the request's Cookie header (RFC 6265).

        The browser sends something like "Cookie: session=abc; theme=dark"
        and we split that into {"session": "abc", "theme": "dark"}.
        """
        cookies = {}
        cookie_header = req.headers.get("cookie", "")
        if cookie_header:
            for pair in cookie_header.split(";"):
                if "=" in pair:
                    key, value = pair.strip().split("=", 1)
                    cookies[key] = value
        return cookies

    def build_response(self, req, resp_obj):
        """Populates a Response object with metadata from the request."""
        resp_obj.encoding = get_encoding_from_headers(resp_obj.headers)
        resp_obj.reason = "OK"

        if isinstance(req.url, bytes):
            resp_obj.url = req.url.decode("utf-8")
        else:
            resp_obj.url = req.url

        resp_obj.cookies = self.extract_cookies(req)
        resp_obj.request = req
        resp_obj.connection = self
        return resp_obj

    def build_json_response(self, req, resp_obj):
        """Same as build_response but for JSON payloads specifically."""
        resp_obj.request = req
        resp_obj.connection = self

        if isinstance(req.url, bytes):
            resp_obj.url = req.url.decode("utf-8")
        else:
            resp_obj.url = req.url

        return resp_obj

    def add_headers(self, request):
        """Hook for subclasses to inject custom headers. Does nothing by default."""
        pass

    def build_proxy_headers(self, proxy):
        """Builds authorization headers for requests going through a proxy."""
        headers = {}
        username, password = ("user1", "password")
        if username:
            headers["Proxy-Authorization"] = (username, password)
        return headers
