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
daemon.request
~~~~~~~~~~~~~~~~~

Parses raw HTTP request strings into a structured Request object.

An HTTP request looks something like this:

    POST /login HTTP/1.1
    Host: 127.0.0.1:8000
    Content-Type: application/json
    Cookie: session_token=abc123

    {"username": "alice", "password": "alice123"}

Our job here is to split that into usable parts: method, path, headers,
body, cookies, auth credentials, and figure out which route handler
(if any) should process this request.
"""

import json
import base64
from .dictionary import CaseInsensitiveDict


class Request():
    """Represents one parsed HTTP request.

    After calling prepare(), all the fields below will be populated
    and the matching route handler (if any) will be in self.hook.

    Usage::
      >>> req = Request()
      >>> req.prepare(raw_http_string, routes=my_routes)
      >>> print(req.method, req.path)
      POST /login
    """

    __attrs__ = [
        "method", "url", "headers", "body", "_raw_headers",
        "_raw_body", "reason", "cookies", "routes", "hook",
    ]

    def __init__(self):
        self.method = None
        self.url = None
        self.path = None
        self.version = None
        # Using CaseInsensitiveDict so "Content-Type" and "content-type" both work
        self.headers = CaseInsensitiveDict()
        self.cookies = {}
        self.auth = None          # Will hold (username, password) if Basic Auth is present
        self.body = None
        self._raw_headers = ""
        self._raw_body = ""
        self.routes = {}
        self.hook = None          # The matched route handler function
        self.reason = None

    def fetch_headers_body(self, incoming_msg):
        """HTTP uses a blank line (\\r\\n\\r\\n) to separate headers from body."""
        if "\r\n\r\n" in incoming_msg:
            parts = incoming_msg.split("\r\n\r\n", 1)
            return parts[0], parts[1]
        return incoming_msg, ""

    def extract_request_line(self, header_section):
        """The first line of any HTTP request is "METHOD /path HTTP/1.1".
        We extract those three pieces here.
        """
        try:
            lines = header_section.splitlines()
            if not lines:
                return None, None, None

            first_line = lines[0]
            parts = first_line.split()

            method = parts[0].upper()
            path = parts[1]
            version = parts[2] if len(parts) > 2 else "HTTP/1.1"

            # Serve index.html by default when someone hits the root
            if path == '/':
                path = '/index.html'

            return method, path, version
        except Exception:
            return "GET", "/", "HTTP/1.1"

    def prepare_headers(self, header_section):
        """Turns the header lines into a dictionary.

        Each header looks like "Key: Value", so we split on ": "
        and skip the first line (that's the request line, not a header).
        """
        lines = header_section.split('\r\n')
        headers = CaseInsensitiveDict()
        for line in lines[1:]:
            if ': ' in line:
                key, val = line.split(': ', 1)
                headers[key] = val
        return headers

    def prepare_cookies(self, cookie_header):
        """Parses the Cookie header string into a dict (RFC 6265).

        Browsers send cookies as: "Cookie: name1=val1; name2=val2"
        We split on ";" and then on "=" to get individual key-value pairs.
        """
        cookies = {}
        if cookie_header:
            pairs = cookie_header.split(';')
            for pair in pairs:
                if '=' in pair:
                    key, val = pair.strip().split('=', 1)
                    cookies[key] = val
        self.cookies = cookies

    def prepare_auth(self, auth_header):
        """Handles HTTP Basic Authentication (RFC 2617 / RFC 7235).

        The browser sends: "Authorization: Basic dXNlcjpwYXNz"
        where the gibberish after "Basic " is base64("user:pass").
        We decode that to get the actual username and password.
        """
        if auth_header and auth_header.startswith('Basic '):
            try:
                encoded_str = auth_header.split(' ', 1)[1]
                decoded_str = base64.b64decode(encoded_str).decode('utf-8')
                if ':' in decoded_str:
                    user, password = decoded_str.split(':', 1)
                    self.auth = (user, password)
            except Exception:
                self.auth = None

    def prepare_body(self, raw_body):
        """Processes the request body.

        If Content-Type says JSON, we try to parse it into a dict.
        Otherwise we just keep it as a raw string -- works fine for
        form-urlencoded data which we parse later in the app layer.
        """
        self._raw_body = raw_body
        content_type = self.headers.get('content-type', '')

        if 'application/json' in content_type:
            try:
                self.body = json.loads(raw_body)
            except Exception:
                self.body = raw_body
        else:
            self.body = raw_body

        self.headers["Content-Length"] = str(len(raw_body))

    def prepare_content_length(self, body):
        self.headers["Content-Length"] = str(len(body)) if body else "0"

    def prepare(self, incoming_msg, routes=None):
        """Main entry point -- takes a raw HTTP string and populates all fields.

        The order matters here:
        1. Split headers from body
        2. Parse the request line (GET /path HTTP/1.1)
        3. Parse headers into a dict (need this before cookies/auth)
        4. Extract cookies and auth from headers
        5. Parse the body (needs Content-Type from headers)
        6. Look up the matching route handler
        """
        if not incoming_msg:
            return self

        print("[Request] Processing incoming message...")

        # Step 1-2: split and parse the first line
        self._raw_headers, self._raw_body = self.fetch_headers_body(incoming_msg)
        self.method, self.path, self.version = self.extract_request_line(self._raw_headers)
        self.url = self.path

        # Step 3: headers need to be ready before we can look up cookies/auth
        self.headers = self.prepare_headers(self._raw_headers)

        # Step 4: cookies for session tracking, auth for Basic Auth
        self.prepare_cookies(self.headers.get('cookie', ''))
        self.prepare_auth(self.headers.get('authorization', ''))

        # Step 5: body parsing (JSON or raw)
        self.prepare_body(self._raw_body)

        # Step 6: find which route handler matches (METHOD, path)
        if routes:
            self.routes = routes
            # Try exact match first: ("POST", "/login")
            self.hook = routes.get((self.method, self.path))
            # Fallback: maybe the route was registered without a method
            if not self.hook:
                self.hook = routes.get(self.path)

        print("[Request] Completed: {} {}".format(self.method, self.path))
        return self

    def __repr__(self):
        return "<Request [{}]>".format(self.method or "INVALID")
