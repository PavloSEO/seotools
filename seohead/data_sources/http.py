"""Fixed-provider HTTP helpers that never forward credentials across redirects."""

from __future__ import annotations

import urllib.request


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects())
_DEFAULT_URLOPEN = urllib.request.urlopen


def open_no_redirect(request: urllib.request.Request, *, timeout: float):
    """Open one fixed HTTPS request while turning every redirect into an HTTP error."""
    # An explicitly injected stdlib transport is the offline test boundary. Production uses the
    # private no-redirect opener; a monkeypatched urlopen never reaches a real network endpoint.
    if urllib.request.urlopen is not _DEFAULT_URLOPEN:
        return urllib.request.urlopen(request, timeout=timeout)
    return _OPENER.open(request, timeout=timeout)
