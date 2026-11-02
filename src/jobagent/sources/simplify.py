"""SimplifyJobs internships, read from the repo's structured listings.json."""
from __future__ import annotations

from .. import http
from ..models import Posting


def parse(listings: list[dict], categories: list[str], term: str) -> list[Posting]:
    out = []
    for x in listings:
        if not x.get("is_visible", True):
            continue
        if term not in (x.get("terms") or []):
            continue
        if x.get("category") not in categories:
            continue
        out.append(Posting(
            source="Simplify",
            company=(x.get("company_name") or "").strip(),
            title=(x.get("title") or "").strip(),
            url=x["url"],
            locations=x.get("locations") or [],
            category=x.get("category") or "",
            date_posted=int(x.get("date_posted") or 0),
            active=bool(x.get("active")),
            sponsorship=x.get("sponsorship") or "",
            degrees=x.get("degrees") or [],
            terms=x.get("terms") or [],
        ))
    return out


def fetch(cfg: dict, term: str) -> list[Posting]:
    return parse(http.get_json(cfg["url"]), cfg["categories"], term)
