"""Fetch job-description text (and, where exposed, application questions) without a browser.

Each ATS gets the public JSON endpoint that its own job pages use; anything else falls back to
JSON-LD or the visible text of the server-rendered page.
"""
from __future__ import annotations

import html as htmllib
import json
import re
import threading
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlsplit

import requests
from bs4 import BeautifulSoup

from . import http

MIN_TEXT = 400  # shorter than this means we only got a JS shell


@dataclass
class JobDetail:
    text: str
    via: str                              # which fetcher produced it
    questions: list[dict] | None = None   # Greenhouse application questions
    location: str = ""


def html_to_text(fragment: str) -> str:
    soup = BeautifulSoup(fragment or "", "html.parser")
    text = soup.get_text("\n")
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _is_404(e: Exception) -> bool:
    return isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code == 404


def _is_refused(e: Exception) -> bool:
    """Not found, or the site declined the request: don't retry, the browser phase reads it instead."""
    return isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code in (401, 403, 404, 410)


# ------------------------------------------------------------------ Greenhouse

_gh_board_by_host: dict[str, str] = {}
_gh_lock = threading.Lock()


def _gh_board_guesses(host: str, company: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", company.lower())
    labels = [x for x in host.lower().split(".") if x not in {"www", "careers", "jobs", "job", "com", "io", "co", "net", "org"}]
    guesses = ["".join(words), *(words[:1]), *labels]
    seen, out = set(), []
    for g in guesses:
        if g and g not in seen:
            seen.add(g)
            out.append(g)
    return out


def _greenhouse(url: str, company: str) -> JobDetail | None:
    p = urlsplit(url)
    qs = dict(parse_qsl(p.query))
    m = re.search(r"greenhouse\.io/([\w-]+)/jobs/(\d+)", url)
    if m and m.group(1) != "embed":
        boards, jid = [m.group(1)], m.group(2)
    else:
        jid = qs.get("gh_jid") or qs.get("token")
        if not jid:
            return None
        with _gh_lock:
            known = _gh_board_by_host.get(p.netloc)
        boards = [qs["for"]] if "for" in qs else ([known] if known else _gh_board_guesses(p.netloc, company))
    for board in boards:
        try:
            data = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}?questions=true")
        except requests.HTTPError as e:
            if _is_404(e):
                continue
            raise
        with _gh_lock:
            _gh_board_by_host[p.netloc] = board
        return JobDetail(
            text=html_to_text(htmllib.unescape(data.get("content", ""))),
            via=f"greenhouse:{board}",
            questions=data.get("questions") or [],
            location=(data.get("location") or {}).get("name", ""),
        )
    return None


# ------------------------------------------------------------------ Lever / Ashby

def _lever(url: str, company: str) -> JobDetail | None:
    m = re.search(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]{36})", url)
    if not m:
        return None
    d = http.get_json(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}")
    parts = [d.get("descriptionPlain", "")]
    for lst in d.get("lists", []):
        parts.append(lst.get("text", ""))
        parts.append(html_to_text(lst.get("content", "")))
    parts.append(d.get("additionalPlain", ""))
    return JobDetail(text="\n".join(x for x in parts if x), via="lever",
                     location=(d.get("categories") or {}).get("location", ""))


_ashby_boards: dict[str, list[dict]] = {}
_ashby_lock = threading.Lock()


def _ashby(url: str, company: str) -> JobDetail | None:
    m = re.search(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url)
    if not m:
        return None
    org, jid = m.group(1), m.group(2)
    with _ashby_lock:
        jobs = _ashby_boards.get(org)
    if jobs is None:
        jobs = http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{quote(org)}").get("jobs", [])
        with _ashby_lock:
            _ashby_boards[org] = jobs
    job = next((j for j in jobs if j.get("id") == jid), None)
    if not job:
        return None
    return JobDetail(text=job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml", "")),
                     via="ashby", location=job.get("location", ""))


# ------------------------------------------------------------------ Workday

def _workday(url: str, company: str) -> JobDetail | None:
    p = urlsplit(url)
    segs = [s for s in p.path.split("/") if s]
    if segs and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segs[0]):
        segs = segs[1:]
    if "myworkdaysite.com" in p.netloc and segs[:1] == ["recruiting"]:
        tenant, segs = segs[1], segs[2:]
    else:
        tenant = p.netloc.split(".")[0]
    if "job" not in segs or segs.index("job") == 0:
        return None
    i = segs.index("job")
    site, rest = segs[i - 1], [s for s in segs[i + 1:] if s != "apply"]
    d = http.get_json(f"https://{p.netloc}/wday/cxs/{tenant}/{site}/job/{'/'.join(rest)}")
    info = d.get("jobPostingInfo") or {}
    return JobDetail(text=html_to_text(info.get("jobDescription", "")), via="workday",
                     location=info.get("location", ""))


# ------------------------------------------------------------------ SmartRecruiters / Oracle

def _smartrecruiters(url: str, company: str) -> JobDetail | None:
    m = re.search(r"smartrecruiters\.com/([^/]+)/(\d+)", url)
    if not m:
        return None
    d = http.get_json(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}")
    sections = (d.get("jobAd") or {}).get("sections") or {}
    text = "\n".join(html_to_text(s.get("text", "")) for s in sections.values() if isinstance(s, dict))
    loc = d.get("location") or {}
    return JobDetail(text=text, via="smartrecruiters",
                     location=", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x))


def _oracle(url: str, company: str) -> JobDetail | None:
    m = re.search(r"https://([^/]+)/hcmUI/CandidateExperience/[^/]+/sites/([^/]+)/job/(\d+)", url)
    if not m:
        return None
    host, site, jid = m.groups()
    api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
           f"?expand=all&onlyData=true&finder=ById;Id=%22{jid}%22,siteNumber={site}")
    items = http.get_json(api).get("items") or []
    if not items:
        return None
    it = items[0]
    keys = ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")
    return JobDetail(text="\n".join(html_to_text(it.get(k) or "") for k in keys), via="oracle",
                     location=it.get("PrimaryLocation", ""))


# ------------------------------------------------------------------ generic page

def _generic(url: str, company: str) -> JobDetail | None:
    page = http.get_text(url, allow_redirects=True)
    soup = BeautifulSoup(page, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if isinstance(it, dict) and it.get("@type") == "JobPosting" and it.get("description"):
                return JobDetail(text=html_to_text(it["description"]), via="json-ld")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()
    text = html_to_text(str(soup))
    return JobDetail(text=text, via="page") if len(text) >= MIN_TEXT else None


_FETCHERS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "workday": _workday,
    "smartrecruiters": _smartrecruiters,
    "oracle": _oracle,
}


def fetch_detail(url: str, ats: str, company: str) -> JobDetail | None:
    """Return the JD, or None if it can't be read without a browser. Network errors propagate."""
    fetcher = _FETCHERS.get(ats)
    detail = None
    if fetcher:
        try:
            detail = fetcher(url, company)
        except requests.HTTPError as e:
            if not _is_refused(e):
                raise
    if detail is None or len(detail.text) < MIN_TEXT:
        try:
            detail = _generic(url, company) or detail
        except requests.HTTPError as e:
            if not _is_refused(e):
                raise
    if detail is None or len(detail.text) < MIN_TEXT:
        return None
    return detail
