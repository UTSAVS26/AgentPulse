"""Validation of user-supplied model endpoint URLs.

* Only ``http``/``https``, a host, no credentials in the URL; query and fragment are dropped.
* In Docker, ``localhost``/``127.0.0.1`` mean the container itself, so they are rewritten to
  ``LOCALHOST_ALIAS`` (compose sets ``host.docker.internal``) and the caller is told.
* When private endpoints are not allowed (the default on the public demo), the host is resolved and every
  address must be public: no private, loopback, link-local (cloud metadata), reserved or multicast ranges,
  and no ``*.internal`` names. This stops visitors from making the server probe its own network (SSRF).
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from app.config import Settings

LOCAL_NAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}
BLOCKED_SUFFIXES = (".internal", ".local", ".localhost")


class UnsafeURL(ValueError):
    """The URL is malformed or not allowed here."""


@dataclass(frozen=True)
class CheckedURL:
    url: str
    hint: str | None = None


def private_allowed(settings: Settings) -> bool:
    if settings.allow_private_endpoints is not None:
        return settings.allow_private_endpoints
    from app.mode import is_public_deployment  # lazy: app.mode imports the clients package

    return not is_public_deployment(settings)


def normalize(url: str, localhost_alias: str = "") -> CheckedURL:
    raw = (url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("the base URL must start with http:// or https://")
    if not parts.hostname:
        raise UnsafeURL("the base URL has no host")
    if parts.username or parts.password:
        raise UnsafeURL("don't put credentials in the URL: use the API key field")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeURL("the base URL has an invalid port") from exc
    host = parts.hostname
    hint = None
    if localhost_alias and host.lower() in LOCAL_NAMES:
        hint = (
            f"'{host}' inside Docker is the container itself: "
            f"using '{localhost_alias}' (your machine) instead"
        )
        host = localhost_alias
    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    path = parts.path.rstrip("/")
    return CheckedURL(urlunsplit((parts.scheme, netloc, path, "", "")), hint)


def _public_only(url: str) -> None:
    host = (urlsplit(url).hostname or "").lower()
    if host in LOCAL_NAMES or host == "host.docker.internal" or host.endswith(BLOCKED_SUFFIXES):
        raise UnsafeURL(f"'{host}' is a local/internal address, which isn't allowed on this deployment")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURL(f"can't resolve '{host}'") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise UnsafeURL(
                f"'{host}' resolves to a private network address ({ip}), "
                "which isn't allowed on this deployment"
            )


def check(url: str, settings: Settings) -> CheckedURL:
    """Normalise ``url`` and enforce the network policy. Raises :class:`UnsafeURL`."""
    checked = normalize(url, settings.localhost_alias)
    if not private_allowed(settings):
        _public_only(checked.url)
    return checked
