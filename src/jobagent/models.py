from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Posting:
    """One job listing as read from a source list."""

    source: str                 # "Simplify" | "SpeedyApply"
    company: str
    title: str
    url: str
    locations: list[str] = field(default_factory=list)
    category: str = ""          # Simplify category; "Software" for SpeedyApply
    date_posted: int = 0        # epoch seconds
    active: bool = True
    sponsorship: str = ""       # Simplify only
    degrees: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
