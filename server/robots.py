"""Minimal RFC 9309 robots.txt evaluation.

The stdlib parser does not implement the `*` / `$` path wildcards that large
sites use, so a small conforming implementation lives here:
  * groups are selected by product token (case-insensitive), else `*`
  * the longest matching rule wins; on a tie, Allow wins
  * Crawl-delay (non-standard but widely used) is honoured as a minimum delay
  * Sitemap lines are collected regardless of group
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass
class RobotsRules:
    allow_all: bool = False
    disallow_all: bool = False
    rules: list = field(default_factory=list)  # (allow: bool, pattern: str, regex)
    crawl_delay: float | None = None
    sitemaps: list = field(default_factory=list)
    source_status: int | None = None
    note: str = ""

    def can_fetch(self, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        best_len, best_allow = -1, True
        for allow, pattern, rx in self.rules:
            if rx.match(path):
                plen = len(pattern)
                if plen > best_len or (plen == best_len and allow):
                    best_len, best_allow = plen, allow
        return best_allow


def _compile(pattern: str):
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(rx + ("$" if anchored else ""), re.S)


def parse_robots(text: str, token: str) -> RobotsRules:
    groups: list[tuple[list[str], list]] = []
    sitemaps: list[str] = []
    current_agents: list[str] = []
    current_rules: list = []
    last_was_agent = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if not last_was_agent:
                if current_agents:
                    groups.append((current_agents, current_rules))
                current_agents, current_rules = [], []
            current_agents.append(value.lower())
            last_was_agent = True
            continue
        last_was_agent = False
        if key == "sitemap":
            if value:
                sitemaps.append(value)
        elif key in ("allow", "disallow"):
            current_rules.append((key, value))
        elif key == "crawl-delay":
            current_rules.append((key, value))
    if current_agents:
        groups.append((current_agents, current_rules))

    tok = token.lower()
    selected = [rules for agents, rules in groups if tok in agents]
    if not selected:
        selected = [rules for agents, rules in groups if "*" in agents]
    out = RobotsRules(sitemaps=sitemaps)
    for rules in selected:
        for key, value in rules:
            if key == "crawl-delay":
                try:
                    d = float(value)
                    if d >= 0:
                        out.crawl_delay = max(out.crawl_delay or 0.0, d)
                except ValueError:
                    pass
            elif value:  # an empty Disallow means "allow everything"
                out.rules.append((key == "allow", value, _compile(value)))
    if not out.rules:
        out.allow_all = True
    return out


def rules_for_status(status: int | None, text: str | None, token: str) -> RobotsRules:
    """Apply RFC 9309 section 2.3.1 status handling, conservatively.

    2xx: parse. 404/410: no restrictions. Any other failure (401/403/429/5xx,
    network) is treated as "disallow everything" because the site has not
    shown that crawling is permitted.
    """
    if status is not None and 200 <= status < 300 and text is not None:
        rules = parse_robots(text, token)
        rules.source_status = status
        return rules
    if status in (404, 410):
        return RobotsRules(allow_all=True, source_status=status, note="robots.txt not found: no restrictions")
    return RobotsRules(disallow_all=True, source_status=status,
                       note=f"robots.txt unavailable (status {status}); treating site as disallowed")
