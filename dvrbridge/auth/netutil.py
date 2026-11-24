"""Who is on the other end: the client address, honouring X-Forwarded-For only from trusted proxies."""
from __future__ import annotations

import ipaddress
from typing import Iterable

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(text: str) -> tuple[Network, ...]:
    return tuple(ipaddress.ip_network(p.strip(), strict=False) for p in (text or "").split(",") if p.strip())


def _addr(text: str):
    try:
        return ipaddress.ip_address(text.strip().strip("[]"))
    except ValueError:
        return None


def is_trusted(address: str, trusted: Iterable[Network]) -> bool:
    a = _addr(address or "")
    return a is not None and any(a in n for n in trusted)


def header(scope, name: str) -> str | None:
    key = name.lower().encode("latin-1")
    for k, v in scope.get("headers", ()):
        if k == key:
            return v.decode("latin-1")
    return None


def client_ip(scope, trusted: tuple[Network, ...]) -> str:
    """The peer address, or - when the peer is a trusted proxy - the nearest untrusted address in
    X-Forwarded-For (walking from the right, which is the part our own proxies wrote)."""
    peer = (scope.get("client") or ("", 0))[0] or ""
    if not trusted or not is_trusted(peer, trusted):
        return peer
    chain = [p.strip() for p in (header(scope, "x-forwarded-for") or "").split(",") if p.strip()]
    for hop in reversed(chain):
        if _addr(hop) is None:
            return peer  # garbage in the chain: do not guess
        if not is_trusted(hop, trusted):
            return hop
    return chain[0] if chain else peer
