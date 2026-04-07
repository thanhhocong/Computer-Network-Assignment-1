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
daemon.asynaprous
~~~~~~~~~~~~~~~~~

This is the mini-framework that makes building RESTful apps easy.
Instead of manually parsing URLs and methods, you just do:

    app = AsynapRous()

    @app.route('/login', methods=['POST'])
    def login(headers, body):
        ...

    app.prepare_address('0.0.0.0', 8000)
    app.run()

Under the hood it builds a routes dict mapping (METHOD, path) to handler
functions, then passes that to create_backend() which does the actual
TCP serving. So AsynapRous is really just a nice decorator layer on top
of our backend server.
"""

from .backend import create_backend
import asyncio
import inspect


class AsynapRous:
    """Lightweight web framework for the assignment.

    Provides Flask-style route decorators but runs on our own
    non-blocking backend (no external libraries needed).
    """

    def __init__(self):
        # Maps (METHOD, path) tuples to handler functions
        # e.g., {("POST", "/login"): <function login>}
        self.routes = {}
        self.ip = None
        self.port = None

    def prepare_address(self, ip, port):
        """Tell the server which IP and port to bind to."""
        self.ip = ip
        self.port = port

    def route(self, path, methods=['GET']):
        """Decorator to register a URL route.

        Usage:
            @app.route('/hello', methods=['GET', 'POST'])
            def hello(headers, body):
                return {"message": "hi"}

        The handler function always receives (headers, body) and can
        return either:
          - a dict/string/bytes  (treated as 200 OK)
          - a 3-tuple: (body, status_code, extra_headers_dict)
        """
        def decorator(func):
            # Register this handler for each HTTP method
            for method in methods:
                self.routes[(method.upper(), path)] = func

            # Store route info on the function itself (useful for debugging)
            func._route_path = path
            func._route_methods = methods

            # Wrap the function to add logging -- helps us see what's
            # happening in the terminal when requests come in
            def sync_wrapper(*args, **kwargs):
                print("[AsynapRous] Running sync function... [{}] {}".format(methods, path))
                result = func(*args, **kwargs)
                return result

            async def async_wrapper(*args, **kwargs):
                print("[AsynapRous] Running async function... [{}] {}".format(methods, path))
                result = await func(*args, **kwargs)
                return result

            # Pick the right wrapper based on whether the handler is async
            if inspect.iscoroutinefunction(func):
                return async_wrapper
            else:
                return sync_wrapper

        return decorator

    def run(self):
        """Starts the server. Call this after registering all your routes."""
        if not self.ip or not self.port:
            print("AsynapRous app needs to prepare address "
                  "by calling app.prepare_address(ip, port)")
            return

        # Hand off to the backend which handles all the TCP stuff
        create_backend(self.ip, self.port, self.routes)
