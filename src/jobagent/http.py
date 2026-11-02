"""Shared HTTP session with retries and a browser-like User-Agent."""
from __future__ import annotations

from functools import lru_cache

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
TIMEOUT = 25


@lru_cache
def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    retry = Retry(total=2, backoff_factor=1.0, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def get(url: str, **kw) -> requests.Response:
    resp = session().get(url, timeout=TIMEOUT, **kw)
    resp.raise_for_status()
    return resp


def get_json(url: str, **kw):
    return get(url, headers={"Accept": "application/json"}, **kw).json()


def get_text(url: str, **kw) -> str:
    return get(url, **kw).text
