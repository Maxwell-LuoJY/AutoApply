"""Deterministic rules: URL identity, ATS detection, hard filters, resume choice."""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Posting

AI_CATEGORIES = {"AI/ML/Data", "Data Science, AI & Machine Learning"}

# ---------------------------------------------------------------- URL identity

_TRACKING = re.compile(r"^(utm_\w+|ref|gh_src)$", re.I)


def normalize_url(url: str) -> str:
    p = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)]
    path = re.sub(r"/(apply|application)/?$", "", p.path).rstrip("/")
    return urlunsplit((p.scheme.lower() or "https", p.netloc.lower(), path, urlencode(query), ""))


def canonical_key(url: str) -> str:
    """Stable identity for a posting, so the same job from two lists collapses to one row."""
    u = normalize_url(url)
    p = urlsplit(u)
    qs = dict(parse_qsl(p.query))
    if "greenhouse.io" in p.netloc or "gh_jid" in qs:
        m = re.search(r"/jobs/(\d+)", p.path)
        jid = qs.get("gh_jid") or qs.get("token") or (m.group(1) if m else None)
        if jid:
            return f"greenhouse:{jid}"
    if "myworkdayjobs.com" in p.netloc or "myworkdaysite.com" in p.netloc:
        path = re.sub(r"^/[a-z]{2}-[A-Z]{2}(?=/)", "", p.path)
        return f"workday:{p.netloc}{path}".lower()
    if p.netloc in ("jobs.lever.co", "jobs.ashbyhq.com"):
        return f"{p.netloc}{p.path}".lower()
    return u


def job_id(url: str) -> str:
    return hashlib.sha1(canonical_key(url).encode()).hexdigest()[:12]


def company_title_key(company: str, title: str) -> str:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
    return f"{norm(company)}|{norm(title)}"


# ---------------------------------------------------------------- ATS detection

_ATS = [
    ("workday", r"\.myworkdayjobs\.com|\.myworkdaysite\.com"),
    ("greenhouse", r"greenhouse\.io|[?&]gh_jid="),
    ("lever", r"jobs\.lever\.co"),
    ("ashby", r"jobs\.ashbyhq\.com"),
    ("smartrecruiters", r"smartrecruiters\.com"),
    ("successfactors", r"successfactors|[?&]ats=successfactors|jobs\.sap\.com"),
    ("oracle", r"\.oraclecloud\.com/hcmUI"),
    ("icims", r"\.icims\.com"),
    ("tiktok", r"lifeattiktok\.com|jobs\.bytedance\.com"),
    ("amazon", r"amazon\.jobs"),
    ("google", r"google\.com/about/careers"),
]


def detect_ats(url: str) -> str:
    for name, pattern in _ATS:
        if re.search(pattern, url, re.I):
            return name
    return "other"


# ---------------------------------------------------------------- location

US_STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
    "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split()
)
_US = re.compile(r"\b(USA|U\.S\.A?\.?|United States|US)\b")
_NON_US = re.compile(
    r"\b(Canada|UK|United Kingdom|England|Scotland|London|Ireland|Dublin|India|Bangalore|Bengaluru|"
    r"Hyderabad|China|Beijing|Shanghai|Shenzhen|Singapore|Germany|Berlin|Munich|France|Paris|"
    r"Netherlands|Amsterdam|Poland|Israel|Tel Aviv|Japan|Tokyo|Korea|Seoul|Taiwan|Taipei|Hong Kong|"
    r"Australia|Sydney|Melbourne|Mexico|Brazil|Spain|Switzerland|Zurich|Sweden|Denmark|Italy|"
    r"Romania|Czech|Hungary|Portugal|Philippines|Vietnam|Malaysia|Indonesia|UAE|Dubai)\b",
    re.I,
)
_US_STATE_NAMES = re.compile(
    r"\b(Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|"
    r"Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|"
    r"Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|"
    r"New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|"
    r"Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|"
    r"West Virginia|Wisconsin|Wyoming)\b",
    re.I,
)


def is_us_location(loc: str) -> bool:
    """True for U.S. (or ambiguous) locations; the LLM double-checks ambiguous ones against the JD."""
    s = loc.strip()
    if _US.search(s):
        return True
    if _NON_US.search(s):
        return False
    m = re.search(r",\s*([A-Z]{2})\b", s)
    if m:
        return m.group(1) in US_STATES
    if _US_STATE_NAMES.search(s):
        return True
    # "Remote", "Hybrid", "3 locations", bare city names: both lists are U.S.-focused, keep them.
    return True


# ---------------------------------------------------------------- hard filters

_INTERN = re.compile(r"(?i)intern|co-?op|apprentice")
_FULLTIME = re.compile(r"(?i)\bnew\s*grad|\bfull[\s-]?time\b|\bentry[\s-]level\b")
_UNDERGRAD = re.compile(r"(?i)\bundergrad")
_PHD = re.compile(r"(?i)\bph\.?\s?d\b")
_MASTERS = re.compile(r"(?i)master|\bm\.?s\.?\b|graduate student")
_BACHELOR = re.compile(r"(?i)\bundergrad|\bbachelor|\bb\.?s\.?\b")
_OTHER_TERM = re.compile(r"(?i)\b(fall|autumn|winter|spring|summer)\s*'?(20)?(2[5-9])\b")
_GRAD_CONTEXT = re.compile(r"(?i)grad|class of")


# Roles reserved for one school's students, or for attendees of a diversity conference / program
_ONE_SCHOOL = re.compile(r"\b(?:University of [A-Z][\w&.-]*(?: [A-Z][\w&.-]*)?|[A-Z][\w&.-]* (?:State )?University)\b")
_EVENT_ONLY = re.compile(r"(?i)society of women engineers|\bswe conference|grace hopper|\bghc\b|\bnsbe\b|\bshpe\b|\bsase\b|"
                         r"\bostem\b|afrotech|women in (?:stem|tech|engineering)|hbcu|\bveterans? program")


def blocked_company(company: str, entries: list | None, today: date | None = None) -> str | None:
    """Skip reason when the company is on the blocklist (settings.yaml `blocked_companies`), else None.
    An entry is a company name, or {name, until, reason}; after `until` the entry no longer applies.
    Names match whole words from the start of the company name: "Wing" matches "Wing LLC", not "Wingstop"."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()  # noqa: E731
    name, today = norm(company), today or date.today()
    for e in entries or []:
        e = e if isinstance(e, dict) else {"name": e}
        block, until = norm(e.get("name") or ""), e.get("until")
        until = date.fromisoformat(str(until)) if until else None
        if not block or (until and today > until) or not (name == block or name.startswith(block + " ")):
            continue
        return "禁用列表：" + "，".join(x for x in (e["name"], e.get("reason"), until and f"{until} 之后恢复") if x)
    return None


def applicant_schools(profile: dict) -> tuple[str, ...]:
    """The applicant's schools and their other names (profile.yaml education[].school / aliases)."""
    return tuple(str(n) for e in profile.get("education") or [] for n in [e.get("school"), *(e.get("aliases") or [])] if n)


def _is_applicant_school(name: str, schools: tuple[str, ...]) -> bool:
    squash = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
    n = squash(name)
    return any(s and (s in n or n in s) for s in map(squash, schools))


def hard_filter(p: Posting, term: str = "Summer 2027", level: str = "master", citizen: bool = False,
                schools: tuple[str, ...] = ()) -> str | None:
    """Return a skip reason (Chinese, for the logs), or None if the posting passes.
    level: the degree the applicant is studying for (master | bachelor | phd); citizen: U.S. citizen or permanent
    resident; schools: the applicant's schools, so a role for one school's students is kept when it is theirs."""
    if not p.active:
        return "岗位已关闭"
    if p.sponsorship == "U.S. Citizenship is Required" and not citizen:
        return "要求美国公民（Simplify 标注）"
    if p.locations and not any(is_us_location(loc) for loc in p.locations):
        return f"非美国地点：{'; '.join(p.locations)}"
    t = p.title
    if _FULLTIME.search(t) and not _INTERN.search(t):
        return "非实习岗位（岗位名称）"
    if level != "bachelor" and _UNDERGRAD.search(t):
        return "仅限本科生（岗位名称）"
    if level == "bachelor" and (_PHD.search(t) or _MASTERS.search(t)) and not _BACHELOR.search(t):
        return "仅限研究生（岗位名称）"
    if level != "phd" and _PHD.search(t) and not _MASTERS.search(t):
        return "仅限 PhD（岗位名称）"
    if level != "phd" and p.degrees and set(p.degrees) <= {"PhD"}:
        return "仅限 PhD（Simplify 标注）"
    if (m := _ONE_SCHOOL.search(t)) and not _is_applicant_school(m.group(0), schools):
        return f"只招某所学校的学生（岗位名称含 {m.group(0)}）"
    if (m := _EVENT_ONLY.search(t)):
        return f"面向特定群体 / 活动（岗位名称含 {m.group(0)}）"
    want_season, want_year = term.lower().split()[0], term[-2:]
    terms_in_title = [
        (m.group(1).lower(), m.group(3))
        for m in _OTHER_TERM.finditer(t)
        if not _GRAD_CONTEXT.search(t[max(0, m.start() - 20): m.start()])  # "Grad Year: Spring 2028" is not a term
    ]
    if terms_in_title and (want_season, want_year) not in terms_in_title:
        season, year = terms_in_title[0]
        return f"非 {term}（岗位名称含 {season.title()} 20{year}）"
    return None


# ---------------------------------------------------------------- resume choice

_AI_TITLE = re.compile(
    r"(?i)data scien|machine learning|\bml\b|\bai\b|artificial intelligence|applied scien|"
    r"research scien|\bllms?\b|\bnlp\b|computer vision|deep learning|\bgen\s?ai\b|generative"
)
_ENGINEERING_TITLE = re.compile(
    r"(?i)\b(software|swe|developer|data engineer\w*|platform|infrastructure|backend|back-end|"
    r"frontend|front-end|full[\s-]?stack|devops|sre|firmware|embedded)\b"
)


# Business analysis, consulting and data roles use the AIE resume too (user, 2026-10-29)
_BUSINESS_DATA_TITLE = re.compile(
    r"(?i)\bdata\b|analytic|analyst|business|consult|strategy|insight|\bbi\b|power bi|tableau|reporting|\bquant"
)
# ...unless it is a software role that merely touches data ("Data Infra Software Engineering Intern")
_SOFTWARE_TITLE = re.compile(
    r"(?i)\b(software|swe|developer|backend|back-end|frontend|front-end|full[\s-]?stack|devops|sre|firmware|"
    r"embedded|mobile|ios|android|web)\b"
)
_ANALYSIS_TITLE = re.compile(r"(?i)analytic|analyst|consult|business|strategy|insight|\bbi\b|power bi|tableau|reporting")


def pick_resume(title: str, category: str) -> str:
    """AIE for DS/AI/ML, business-analysis, consulting and data roles; SDE for software engineering and the rest."""
    if _AI_TITLE.search(title):
        return "AIE"
    if _BUSINESS_DATA_TITLE.search(title) and not (_SOFTWARE_TITLE.search(title) and not _ANALYSIS_TITLE.search(title)):
        return "AIE"
    if category in AI_CATEGORIES and not _ENGINEERING_TITLE.search(title):
        return "AIE"
    return "SDE"


def csv_category(category: str) -> str:
    return "DS/AI" if category in AI_CATEGORIES else "SWE"


# ---------------------------------------------------------------- apply order

# Systems where unattended submission has never worked go to the back: generic career sites (no adapter submits them)
# and Lever (always an hCaptcha on submit).
_UNATTENDED_TIER = {"greenhouse": 0, "ashby": 0, "workday": 0, "lever": 2}
DAY = 86400


def too_old(date_posted: int, max_age_days: int | None, now: float) -> str | None:
    """Skip reason for a posting older than `max_age_days` (user, 2026-11-01: 发布 30 天以上的不投), or None.
    A posting with no date is kept."""
    if max_age_days and date_posted and now - date_posted > max_age_days * DAY:
        return f"发布超过 {max_age_days} 天（{datetime.fromtimestamp(date_posted):%Y-%m-%d}）"
    return None


def posted_today(date_posted: int, now: float) -> bool:
    """"当日发布": posted in the last 24 hours, i.e. since the previous daily run. The lists date some postings only by
    day (midnight UTC), so a calendar-day test would depend on the time zone."""
    return bool(date_posted) and now - date_posted < DAY


def apply_order(rows: list, now: float, pinned: list[str] = ()) -> list:
    """Order the apply queue (user, 2026-11-01): jobs the user pinned for the next run, then jobs posted in the last
    24 hours, then the best resume match, then the newest posting."""
    def key(r):
        if r["id"] in pinned:
            return (0, pinned.index(r["id"]))
        fit = r["fit"] if r["fit"] is not None else -1  # not rated yet: after every rated job
        return 1, _UNATTENDED_TIER.get(r["ats"], 1), not posted_today(r["date_posted"], now), -fit, -r["date_posted"]
    return sorted(rows, key=key)
