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

# Python 3.10+ moved MutableMapping to collections.abc
# but older versions still have it in collections directly
try:
    from collections.abc import MutableMapping
except ImportError:
    from collections import MutableMapping


class CaseInsensitiveDict(MutableMapping):
    """A dictionary where keys are always lowercased.

    HTTP headers are case-insensitive by spec, meaning "Content-Type"
    and "content-type" should be treated the same. This dict handles
    that automatically so we never have to worry about casing.

    Usage::
      >>> d = CaseInsensitiveDict()
      >>> d['Content-Type'] = 'text/html'
      >>> d['content-type']
      'text/html'
    """

    def __init__(self, *args, **kwargs):
        self.store = {}
        data = dict(*args, **kwargs)
        for key, value in data.items():
            self.store[key.lower()] = value

    def __getitem__(self, key):
        return self.store[key.lower()]

    def __setitem__(self, key, value):
        self.store[key.lower()] = value

    def __delitem__(self, key):
        del self.store[key.lower()]

    def __iter__(self):
        return iter(self.store)

    def __len__(self):
        return len(self.store)

    def __repr__(self):
        return str(self.store)

    def __copy__(self):
        return CaseInsensitiveDict(self.store)
