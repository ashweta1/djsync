"""Web requests with a certificate bundle that works on any Mac Python.

Python from python.org can't verify HTTPS certificates until its "Install
Certificates" step has been run, so requests to Beatport and Deezer fail with
CERTIFICATE_VERIFY_FAILED. We ship certifi's bundle and use it instead.
"""

from __future__ import annotations

import ssl
import urllib.request

try:
    import certifi

    _CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:  # certifi missing: fall back to the system's certificates
    _CONTEXT = ssl.create_default_context()


def urlopen(req, timeout: float = 20):
    return urllib.request.urlopen(req, timeout=timeout, context=_CONTEXT)
