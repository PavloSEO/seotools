"""Fail-closed target policy for an authenticated, queued crawl job.

The service owns this policy. A request may select a saved project and bounded
settings, but it may not widen the project's private-host allowlist or budgets.
The policy is bound to clients created while ``active()`` is in effect; each
client's transport retains it across worker threads and browser route callbacks.
"""

from __future__ import annotations

import contextlib
import contextvars
import ipaddress
import math
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


class RemoteTargetError(ValueError):
    """A safe, URL-free rejection suitable for an API response or event."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_active: contextvars.ContextVar[RemoteEgressPolicy | None] = contextvars.ContextVar(
    "seohead_remote_egress_policy", default=None
)
_STAGING_RANGES = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def current_remote_policy() -> RemoteEgressPolicy | None:
    """Return the policy for a client being constructed in this execution context."""
    return _active.get()


@dataclass
class RemoteEgressPolicy:
    """Project-bound limits and exact private staging hosts for one queued job.

    ``allowed_private_hosts`` comes from trusted service configuration, never a
    submitted job. The existing local ``SEOHEAD_ALLOW_PRIVATE_*`` environment
    variables are ignored when this policy is active.
    """

    project_id: str
    allowed_private_hosts: frozenset[str] = frozenset()
    max_requests_per_origin: int = 1_000
    max_total_requests: int = 20_000
    max_concurrency: int = 4
    min_delay_seconds: float = 0.5
    _requests: dict[tuple[str, str, int], int] = field(default_factory=dict, init=False, repr=False)
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.project_id or not isinstance(self.project_id, str):
            raise ValueError("project_id must be a nonempty string")
        if any(
            type(value) is not int or value < 1
            for value in (
                self.max_requests_per_origin,
                self.max_total_requests,
                self.max_concurrency,
            )
        ):
            raise ValueError("remote request and concurrency limits must be positive integers")
        if (
            type(self.min_delay_seconds) not in (int, float)
            or not math.isfinite(self.min_delay_seconds)
            or self.min_delay_seconds < 0
        ):
            raise ValueError("remote delay floor must be finite and nonnegative")
        normalized: set[str] = set()
        for value in self.allowed_private_hosts:
            if not isinstance(value, str):
                raise ValueError("private host allowlist requires DNS hostname strings")
            host = value.rstrip(".").lower()
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError("private host allowlist requires DNS hostnames, not IP literals")
            try:
                socket.inet_aton(host)
            except OSError:
                pass
            else:
                raise ValueError("private host allowlist requires DNS hostnames, not IP aliases")
            if (
                not host
                or host != host.strip()
                or host == "localhost"
                or host.endswith(".localhost")
                or "://" in host
                or ":" in host
                or "/" in host
                or "@" in host
                or "*" in host
            ):
                raise ValueError("private host allowlist must contain exact hostnames")
            normalized.add(host)
        self.allowed_private_hosts = frozenset(normalized)

    def allows_private_host(self, host: str) -> bool:
        return host.rstrip(".").lower() in self.allowed_private_hosts

    def allows_private_address(self, host: str, address: str) -> bool:
        """Allow an exact staging hostname only into RFC1918 or IPv6 ULA."""
        if not self.allows_private_host(host):
            return False
        try:
            parsed = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return False
        return any(parsed in network for network in _STAGING_RANGES)

    def authorize_submission(
        self, project_id: str, target_url: str, effective_config: dict[str, Any]
    ) -> None:
        """Reject an unsafe submitted target or an unbounded crawl before queueing.

        The worker must also call ``active()`` around the actual core run: a
        submit-time DNS check alone cannot defend against rebinding or redirects.
        """
        if project_id != self.project_id:
            raise RemoteTargetError("foreign_project", "target policy belongs to another project")
        if not isinstance(target_url, str):
            raise RemoteTargetError("unsafe_target", "target must be an HTTP or HTTPS URL")
        try:
            parts = urlsplit(target_url)
            if parts.fragment or parts.username is not None or parts.password is not None:
                raise ValueError("fragment or URL credentials")
            from seohead.recon.net import validate_url

            validate_url(target_url, policy=self)
        except ValueError:
            raise RemoteTargetError(
                "unsafe_target", "target is not a permitted public or approved staging URL"
            ) from None
        if not isinstance(effective_config, dict):
            raise RemoteTargetError("invalid_config", "effective crawl settings are required")
        try:
            from seohead.crawl.settings import validate

            validate(effective_config)
            limits = effective_config["limits"]
            speed = effective_config["speed"]
            rendering = effective_config["rendering"]
            http = effective_config["http"]
            if (
                not 1 <= limits["max_requests"] <= self.max_total_requests
                or not 1 <= limits["max_urls"] <= self.max_total_requests
                or not 1 <= speed["concurrency"] <= self.max_concurrency
                or not math.isfinite(speed["min_delay_seconds"])
                or speed["min_delay_seconds"] < self.min_delay_seconds
            ):
                raise RemoteTargetError(
                    "budget_exceeded",
                    "remote crawl request, URL, concurrency or rate budget exceeded",
                )
            if http.get("proxy"):
                raise RemoteTargetError(
                    "unsupported_proxy", "remote proxy routing is not enabled for queued jobs"
                )
            browser = rendering["browser"]
            if (
                browser.get("persistent_profile")
                or browser.get("transport", "local") != "local"
                or browser.get("backend", "local") != "local"
            ):
                raise RemoteTargetError(
                    "unsupported_browser", "remote browser backend or profile is unsupported"
                )
        except RemoteTargetError:
            raise
        except (KeyError, TypeError, ValueError):
            raise RemoteTargetError(
                "invalid_config", "effective crawl settings are invalid"
            ) from None

    def reserve_request(self, url: str, *, original_host: str | None = None) -> None:
        """Spend one physical HTTP attempt, including redirects and browser resources."""
        try:
            parts = urlsplit(url)
            host = (original_host or parts.hostname or "").rstrip(".").lower()
            port = parts.port or (443 if parts.scheme == "https" else 80)
            key = (parts.scheme, host, port)
            if parts.scheme not in {"http", "https"} or not host:
                raise ValueError("invalid origin")
        except ValueError:
            raise RemoteTargetError("unsafe_target", "request has an invalid origin") from None
        with self._lock:
            if sum(self._requests.values()) >= self.max_total_requests:
                raise RemoteTargetError("request_budget", "remote total request budget exhausted")
            if self._requests.get(key, 0) >= self.max_requests_per_origin:
                raise RemoteTargetError(
                    "origin_budget", "remote per-origin request budget exhausted"
                )
            self._requests[key] = self._requests.get(key, 0) + 1

    @contextlib.contextmanager
    def active(self) -> Iterator[RemoteEgressPolicy]:
        """Bind this policy while the worker constructs all network clients."""
        token = _active.set(self)
        try:
            yield self
        finally:
            _active.reset(token)

    @contextlib.contextmanager
    def checked_job(
        self, project_id: str, target_url: str, effective_config: dict[str, Any]
    ) -> Iterator[RemoteEgressPolicy]:
        """Revalidate at dispatch and bind egress for the complete core run."""
        self.authorize_submission(project_id, target_url, effective_config)
        with self.active():
            yield self
