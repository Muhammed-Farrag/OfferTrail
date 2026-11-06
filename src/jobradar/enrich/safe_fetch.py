"""Bounded, SSRF-guarded HTTP(S) fetcher for job pages.

* http/https only; every hop (including redirects) is re-validated;
* hosts resolving to private, loopback, link-local, reserved or multicast addresses are blocked;
* response size and total time are capped; content is returned as text data only.

There is no login, cookie reuse or CAPTCHA handling: a page that needs those is reported as
blocked, and the user can paste the description instead.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx


class BlockedURL(Exception):
    pass


@dataclass
class FetchResult:
    status: str  # ok | blocked | error:<reason>
    url: str
    final_url: str | None = None
    http_status: int | None = None
    text: str | None = None


def _check_host(host: str, resolver=socket.getaddrinfo) -> None:
    if not host:
        raise BlockedURL("missing host")
    try:
        infos = resolver(host, None)
    except socket.gaierror as e:
        raise BlockedURL(f"DNS resolution failed for {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise BlockedURL(f"{host} resolves to non-public address {ip}")


def validate_url(url: str, resolver=socket.getaddrinfo) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL(f"scheme {parts.scheme!r} not allowed")
    if parts.username or parts.password:
        raise BlockedURL("credentials in URL not allowed")
    _check_host(parts.hostname or "", resolver)


def fetch(url: str, *, timeout_s: float = 10.0, max_bytes: int = 2_000_000,
          max_redirects: int = 5, transport: httpx.BaseTransport | None = None,
          resolver=socket.getaddrinfo) -> FetchResult:
    current = url
    headers = {"User-Agent": "AI-Job-Radar/0.1 (personal job-search assistant)",
               "Accept": "text/html,text/plain;q=0.9"}
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=False, transport=transport,
                          headers=headers) as client:
            for _ in range(max_redirects + 1):
                validate_url(current, resolver)
                with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        loc = resp.headers.get("location")
                        if not loc:
                            return FetchResult("error:redirect_without_location", url, current,
                                               resp.status_code)
                        current = urljoin(current, loc)
                        continue
                    if resp.status_code in (401, 403, 429, 999):
                        return FetchResult("blocked", url, current, resp.status_code)
                    if resp.status_code >= 400:
                        return FetchResult(f"error:http_{resp.status_code}", url, current,
                                           resp.status_code)
                    ctype = resp.headers.get("content-type", "")
                    if ctype and not ("text/" in ctype or "html" in ctype):
                        return FetchResult("error:unsupported_content_type", url, current,
                                           resp.status_code)
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) > max_bytes:
                            return FetchResult("error:too_large", url, current, resp.status_code)
                    text = body.decode(resp.encoding or "utf-8", errors="replace")
                    if _looks_like_login_wall(text):
                        return FetchResult("blocked", url, current, resp.status_code)
                    return FetchResult("ok", url, current, resp.status_code, text)
            return FetchResult("error:too_many_redirects", url, current)
    except BlockedURL as e:
        return FetchResult(f"error:blocked_url:{e}", url, current)
    except httpx.TimeoutException:
        return FetchResult("error:timeout", url, current)
    except httpx.HTTPError as e:
        return FetchResult(f"error:{type(e).__name__}", url, current)


def _looks_like_login_wall(text: str) -> bool:
    low = text[:20000].lower()
    return ("captcha" in low or "authwall" in low or "sign in to view" in low
            or "join now to see" in low and "job description" not in low)
