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

from urllib.parse import urlparse, unquote


def get_auth_from_url(url):
    """Extracts username and password embedded in a URL.

    Some URLs contain credentials like:
        http://admin:secret@example.com/path

    We use Python's urlparse to pull out the username ("admin")
    and password ("secret"), and unquote handles percent-encoding
    (e.g., %40 becomes @).
    """
    if not url:
        return ("", "")

    parsed = urlparse(url)

    try:
        username = unquote(parsed.username) if parsed.username else ""
        password = unquote(parsed.password) if parsed.password else ""
        auth = (username, password)
    except (AttributeError, TypeError):
        auth = ("", "")

    return auth
