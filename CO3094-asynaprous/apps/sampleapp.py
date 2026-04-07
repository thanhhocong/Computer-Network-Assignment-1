#
# Copyright (C) 2026 pdnguyen of HCMC University of Technology VNU-HCM.
# All rights reserved.
# This file is part of the CO3093/CO3094 course.
#
# AsynapRous release
#

"""
apps.sampleapp
~~~~~~~~~~~~~~~~~

A small demo app to test the AsynapRous framework.
Has a few endpoints that show different features:
  - /admin   => Basic Auth (browser popup)
  - /login   => Form-based login with cookies
  - /echo    => Echoes back whatever JSON you send
  - /hello   => Async handler returning user data
  - /slow    => Sleeps 10 seconds to prove non-blocking works

The /slow endpoint is the best way to verify async behavior:
open two browser tabs, hit /slow in one and /hello in the other --
you'll see /hello responds instantly even though /slow is still waiting.
"""

import json
import base64
import asyncio

from daemon import AsynapRous

app = AsynapRous()

# Simple user database for testing
USER_DB = {
    "admin": "admin123",
    "alice": "alice123",
    "bob": "bob123",
}


def get_basic_auth_creds(auth_header):
    """Pulls username:password out of the Base64-encoded auth header."""
    if not auth_header or not auth_header.startswith('Basic '):
        return None, None
    try:
        encoded = auth_header.split(' ')[1]
        decoded = base64.b64decode(encoded).decode('utf-8')
        return decoded.split(':', 1)
    except Exception:
        return None, None


def parse_form_body(body):
    """Splits "username=admin&password=123" into a dict."""
    params = {}
    if isinstance(body, bytes):
        body = body.decode('utf-8')
    if isinstance(body, str):
        for pair in body.split('&'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                params[k] = v
    return params


@app.route('/admin', methods=['GET'])
def admin_route(headers, body):
    """If you have the right credentials, you get redirected to form.html.
    Otherwise the browser shows its built-in login popup (RFC 7235).
    """
    auth_header = headers.get('authorization', '')
    user, pw = get_basic_auth_creds(auth_header)

    if user and USER_DB.get(user) == pw:
        return "", 302, {"Location": "/form.html"}

    return "Unauthorized", 401, {
        "WWW-Authenticate": 'Basic realm="Admin Area"',
    }


@app.route('/login', methods=['POST'])
def login(headers="guest", body="anonymous"):
    """Cookie-based login: check credentials, set a session cookie on success."""
    params = parse_form_body(body) if isinstance(body, str) else body

    username = params.get('username', '')
    password = params.get('password', '')

    if USER_DB.get(username) == password:
        return "Login successful", 302, {
            "Location": "/index.html",
            "Set-Cookie": "auth_token=session_{}; Path=/; HttpOnly".format(username),
        }

    return {"error": "Invalid credentials"}, 401, {}


@app.route("/echo", methods=["POST"])
def echo(headers="guest", body="anonymous"):
    """Whatever JSON you POST, we send it right back. Useful for testing."""
    try:
        message = json.loads(body) if isinstance(body, str) else body
        return {"received": message}, 200, {}
    except (json.JSONDecodeError, TypeError):
        return {"error": "Invalid JSON"}, 400, {}


@app.route('/hello', methods=['PUT'])
async def hello(headers, body):
    """An async handler -- shows that our framework supports coroutines."""
    data = {"id": 1, "name": "Alice", "email": "alice@example.com"}
    return data, 200, {}


@app.route('/slow', methods=['GET'])
async def slow_request(headers, body):
    """Sleeps 10 seconds to prove the server doesn't block.

    While this request is "sleeping", other requests (like /hello)
    are still served normally. That's the whole point of non-blocking I/O.
    Try opening two tabs: one with /slow, one with /hello -- /hello
    will respond instantly even though /slow is still waiting.
    """
    print("[SampleApp] Processing slow request (10 seconds)...")
    await asyncio.sleep(10)
    print("[SampleApp] Slow request completed!")
    return {
        "status": "completed",
        "message": "10-second task completed without blocking the server!",
    }, 200, {}


def create_sampleapp(ip, port):
    app.prepare_address(ip, port)
    app.run()
