"""Which Redberry agent a trace belongs to (``@redberrylabs/otel`` SPEC.md §8, "Agent").

The hasher decides, and records the answer as the document's ``agent_id`` (and on every span). In order:
a valid ``redberry.agent_id`` on the root span (``with_redberry`` sets it on the invocation's root, from
its ``agent_id`` or the configuration below), then ``REDBERRY_AGENTS`` (route -> agent) against the root
span's routes (``http.route``, ``next.route``, then the URL path), then ``REDBERRY_AGENT_ID``. No other
span's attributes are read.
"""
from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

__all__ = ["AGENT_ID", "is_agent_id", "match_route", "resolve_agent", "route_candidates"]

AGENT_ID = re.compile(r"AGT-[0-9A-HJKMNP-TV-Z]{26}")


def is_agent_id(value: Any) -> bool:
    return isinstance(value, str) and AGENT_ID.fullmatch(value) is not None


def route_candidates(attrs: Mapping[str, Any]) -> list[str]:
    """The root span's candidate routes, in order: ``http.route``, ``next.route``, then the URL path
    (``url.path``, or ``http.target`` / ``http.url`` / ``url.full`` without the query string)."""
    out: list[str] = []

    def add(v: Any) -> None:
        if isinstance(v, str) and v and v not in out:
            out.append(v)

    add(attrs.get("http.route"))
    add(attrs.get("next.route"))
    add(attrs.get("url.path"))
    for k in ("http.target", "http.url", "url.full"):
        v = attrs.get(k)
        if isinstance(v, str) and v:
            add(urllib.parse.urlsplit(urllib.parse.urljoin("http://x", v)).path or v.split("?")[0])
    return out


def match_route(route: str, routes: Mapping[str, str]) -> str | None:
    """A key matches exactly, or, ending in ``*``, as a prefix; the longest prefix wins."""
    if route in routes:
        return routes[route]
    best: str | None = None
    for key in routes:
        if key.endswith("*") and route.startswith(key[:-1]) and (best is None or len(key) > len(best)):
            best = key
    return routes[best] if best is not None else None


def resolve_agent(root_attrs: Mapping[str, Any], agents_by_route: Mapping[str, str] | None,
                  agent_id: str | None) -> str | None:
    if agents_by_route:
        for route in route_candidates(root_attrs):
            hit = match_route(route, agents_by_route)
            if hit:
                return hit
    return agent_id or None
