"""SpeedyApply 2027 SWE list: the USA internship tables live in README.md."""
from __future__ import annotations

import html
import re
import time

from .. import http
from ..models import Posting

_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r'href="([^"]+)"')


def _plain(cell: str) -> str:
    return html.unescape(_TAG.sub("", cell)).replace("**", "").strip()


def _age_days(cell: str) -> int:
    m = re.match(r"(\d+)\s*d", cell.strip())
    return int(m.group(1)) if m else 0


def parse(md: str, section: str, now: float | None = None) -> list[Posting]:
    now = time.time() if now is None else now
    start = md.find(section)
    if start < 0:
        raise ValueError(f"SpeedyApply README 中找不到 {section!r}，页面结构可能变了")
    nxt = md.find("\n## ", start + len(section))
    body = md[start: nxt if nxt > 0 else len(md)]

    out, header = [], None
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            if line.startswith("#"):
                header = None
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0] == "Company":
            header = cells
            continue
        if not header or len(cells) != len(header) or set(line) <= set("|-: "):
            continue
        row = dict(zip(header, cells))
        href = _HREF.search(row.get("Posting", ""))
        if not href:
            continue
        location = _plain(row.get("Location", ""))
        out.append(Posting(
            source="SpeedyApply",
            company=_plain(row["Company"]),
            title=_plain(row["Position"]),
            url=html.unescape(href.group(1)),
            locations=[location] if location else [],
            category="Software",
            date_posted=int(now - _age_days(row.get("Age", "")) * 86400),
        ))
    return out


def fetch(cfg: dict, term: str) -> list[Posting]:
    return parse(http.get_text(cfg["url"]), cfg["section"])
