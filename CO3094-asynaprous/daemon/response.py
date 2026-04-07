#
# Copyright (C) 2026 pdnguyen of HCMC University of Technology VNU-HCM.
# All rights reserved.
# This file is part of the CO3093/CO3094 course.
#
# AsynApRous release
#
# The authors hereby grant to Licensee personal permission to use
# and modify the Licensed Source Code for the sole purpose of studying
# while attending the course
#

"""
daemon.response
~~~~~~~~~~~~~~~~~

Constructs valid HTTP/1.1 responses to send back to the client.

This module handles three kinds of responses:
  1. JSON from app handlers  (e.g., {"status": "ok"} from /send-peer)
  2. HTML from app handlers  (e.g., the login page served as a string)
  3. Static files            (e.g., /css/messenger.css read from disk)

It also takes care of the important HTTP headers:
  - Set-Cookie for session management       (RFC 6265)
  - WWW-Authenticate for Basic Auth         (RFC 7235)
  - Location for redirects                  (RFC 7231)
  - Content-Type, Content-Length, Date, etc.
"""
import datetime
import os
import json
import mimetypes
from .dictionary import CaseInsensitiveDict

# This gets prepended to file paths when serving static files.
# Empty string means "look relative to the working directory".
BASE_DIR = ""


class Response():
    """Holds everything we need to build an HTTP response.

    After the app handler runs, we populate status_code, headers,
    cookies, and _content, then build_response() assembles them
    into the final byte string that goes over the wire.
    """

    __attrs__ = [
        "_content", "_header", "status_code", "method", "headers",
        "url", "history", "encoding", "reason", "cookies",
        "elapsed", "request", "body",
    ]

    def __init__(self, request=None):
        self._content = b""
        self._header = b""
        self._content_consumed = False
        self._next = None
        self.status_code = 200
        self.reason = "OK"
        self.headers = {}          # Custom headers set by the app
        self.url = None
        self.encoding = "utf-8"
        self.history = []
        self.cookies = CaseInsensitiveDict()  # Cookies to send via Set-Cookie
        self.elapsed = datetime.timedelta(0)
        self.request = request
        self.body = None

    def get_mime_type(self, path):
        """Guesses the MIME type from the file extension.

        e.g., ".html" -> "text/html", ".png" -> "image/png"
        Falls back to "application/octet-stream" for unknown types.
        """
        try:
            mime_type, _ = mimetypes.guess_type(path)
        except Exception:
            return 'application/octet-stream'
        return mime_type or 'application/octet-stream'

    def prepare_content_type(self, mime_type='text/html'):
        """Figures out which directory to look in based on the content type.

        Our project structure separates files like this:
          - www/       -> HTML pages
          - static/    -> CSS, JS, images, fonts, etc.

        So a request for something.html looks in www/,
        while something.css or something.png looks in static/.
        """
        main_type, sub_type = mime_type.split('/', 1)
        base_dir = ""

        if main_type == 'text':
            self.headers['Content-Type'] = 'text/{}; charset={}'.format(sub_type, self.encoding)
            if sub_type in ['plain', 'css', 'javascript']:
                base_dir = os.path.join(BASE_DIR, "static/")
            elif sub_type == 'html':
                base_dir = os.path.join(BASE_DIR, "www/")
            else:
                base_dir = os.path.join(BASE_DIR, "static/")
        elif main_type == 'image':
            self.headers['Content-Type'] = 'image/{}'.format(sub_type)
            base_dir = os.path.join(BASE_DIR, "static/")
        elif main_type == 'application':
            if sub_type == 'javascript':
                self.headers['Content-Type'] = 'application/javascript; charset={}'.format(self.encoding)
                base_dir = os.path.join(BASE_DIR, "static/")
            else:
                self.headers['Content-Type'] = mime_type
                base_dir = os.path.join(BASE_DIR, "static/")
        else:
            self.headers['Content-Type'] = mime_type
            base_dir = os.path.join(BASE_DIR, "static/")

        return base_dir

    def build_content(self, path, base_dir):
        """Reads a file from disk and returns its content as bytes.

        Returns (length, content) on success, or (-1, b"") if the file
        doesn't exist -- the caller uses -1 to know it should send a 404.
        """
        filename = path.lstrip('/')
        if not filename or filename == 'index.html':
            filename = 'index.html'

        filepath = os.path.join(base_dir, filename)
        print("[Response] Serving the object at location {}".format(filepath))

        try:
            with open(filepath, "rb") as f:
                content = f.read()
            return len(content), content
        except Exception as e:
            print("[Response] build_content exception: {}".format(e))
            return -1, b""

    def build_response_header(self, request):
        """Assembles the HTTP response header string.

        This is where all the RFC magic happens:
          - RFC 7235: if we're sending 401, include WWW-Authenticate
                      so the browser shows its login popup
          - RFC 6265: append Set-Cookie for each cookie we want to set
          - RFC 7231: Location header is already set by the app for redirects
        """
        status_line = "HTTP/1.1 {} {}".format(self.status_code, self.reason)

        # Start with standard headers every response should have
        full_headers = {
            "Date": datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT"),
            "Server": "AsynapRous/1.0",
            "Content-Length": str(len(self._content)),
            "Connection": "close",
        }

        # Merge in whatever the app set (Content-Type, Location, etc.)
        full_headers.update(self.headers)

        # If the app returned 401 but forgot to set WWW-Authenticate,
        # we add it automatically so the browser knows to show the login dialog
        if self.status_code == 401 and 'WWW-Authenticate' not in full_headers:
            full_headers["WWW-Authenticate"] = 'Basic realm="HCMUT Secure Area"'

        # Build the header lines, but skip Set-Cookie here (handled separately)
        header_lines = [status_line]
        for key, value in full_headers.items():
            if key.lower() != 'set-cookie':
                header_lines.append("{}: {}".format(key, value))

        # Cookies from self.cookies dict (set by the route handler)
        if self.cookies:
            for key, value in self.cookies.items():
                header_lines.append("Set-Cookie: {}={}; Path=/".format(key, value))

        # Also check if there's a Set-Cookie directly in self.headers
        # (used when the app returns it as part of the extra headers dict)
        if 'Set-Cookie' in self.headers:
            header_lines.append("Set-Cookie: {}".format(self.headers['Set-Cookie']))

        # End headers with a blank line before the body
        fmt_header = "\r\n".join(header_lines) + "\r\n\r\n"
        return fmt_header.encode('utf-8')

    def build_notfound(self):
        """Quick 404 response for when we can't find the requested file."""
        body = b"404 Not Found"
        header = (
            "HTTP/1.1 404 Not Found\r\n"
            "Content-Type: text/plain\r\n"
            "Content-Length: {}\r\n"
            "Connection: close\r\n\r\n"
        ).format(len(body)).encode('utf-8')
        return header + body

    def build_response(self, request, envelop_content=None):
        """The main method -- builds a complete HTTP response (header + body).

        If envelop_content is provided, that's the app's response data:
          - dict  => serialize to JSON
          - str   => treat as HTML
          - bytes => send raw
          - None  => fall back to serving a static file from disk
        """
        self.request = request
        path = request.path if request else "/"

        if envelop_content is not None:
            # App gave us a dict -- serialize it as JSON
            if isinstance(envelop_content, dict):
                json_str = json.dumps(envelop_content, ensure_ascii=False)
                self._content = json_str.encode(self.encoding)
                if 'Content-Type' not in self.headers:
                    self.headers['Content-Type'] = 'application/json; charset=utf-8'

            # App gave us a string -- probably HTML content
            elif isinstance(envelop_content, str):
                self._content = envelop_content.encode(self.encoding)
                if 'Content-Type' not in self.headers:
                    self.headers['Content-Type'] = 'text/html; charset=utf-8'

            # App gave us raw bytes -- send as-is
            elif isinstance(envelop_content, bytes):
                self._content = envelop_content
                if 'Content-Type' not in self.headers:
                    self.headers['Content-Type'] = 'application/octet-stream'

            else:
                self._content = str(envelop_content).encode(self.encoding)
        else:
            # No app content -- try to serve the path as a static file
            mime_type = self.get_mime_type(path)
            base_dir = self.prepare_content_type(mime_type)
            length, content = self.build_content(path, base_dir)

            if length >= 0:
                self._content = content
            else:
                return self.build_notfound()

        self._header = self.build_response_header(request)
        return self._header + self._content
