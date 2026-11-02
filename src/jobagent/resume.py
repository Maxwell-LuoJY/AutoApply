"""Structured resumes: each resume file is parsed once (by the LLM) into config/resumes/<KIND>.yaml, which you can read
and correct. Form fields that just copy the resume (work experience, education, skills) are then filled straight from
it, with no LLM call per job. If the resume file changes, the YAML is stale and is ignored until `jobagent resume parse`.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from datetime import date
from pathlib import Path

import yaml

from . import config

log = logging.getLogger("jobagent")

DIR = config.CONFIG_DIR / "resumes"
KINDS = ("AIE", "SDE")
_DATE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_LEVELS = ("master", "bachelor", "phd", "associate", "other")

PROMPT = """Turn the resume below into JSON. Copy names exactly as written on the resume (company, job title, school,
degree, location); never invent or embellish anything. Keep entries in the order they appear (most recent first).

{{"experience": [{{"company": "...", "title": "...", "location": "...", "start": "YYYY-MM", "end": "YYYY-MM or present",
                  "bullets": ["one string per bullet, verbatim"]}}],
 "education": [{{"school": "...", "degree": "e.g. M.S. in Computer Science", "level": "master|bachelor|phd|associate|other",
                 "field": "the major, e.g. Computer Science", "location": "...", "start": "YYYY-MM", "end": "YYYY-MM",
                 "gpa": "as written, or empty", "honors": "as written, or empty"}}],
 "projects": [{{"name": "...", "url": "...", "tech": ["..."], "bullets": ["..."]}}],
 "skills": {{"<category as written>": ["skill", "..."]}}}}

Resume:
<resume>
{text}
</resume>"""


def source(kind: str, profile: dict | None = None) -> Path:
    """The file applications upload for this resume: the PDF, or the .docx until a PDF exists.
    A profile with only one resume (SDE) uses it for both kinds."""
    resumes = (profile or config.profile())["resumes"]
    r = resumes.get(kind) or resumes["SDE"]
    pdf = config.ROOT / (r.get("pdf") or r["docx"])
    return pdf if pdf.exists() or not r.get("docx") else config.ROOT / r["docx"]


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


def validate(data: dict, text: str) -> list[str]:
    """Problems that mean the parse can't be trusted: names not on the resume, malformed dates, missing parts."""
    problems, flat = [], _squash(text)
    if not data.get("experience") or not data.get("education"):
        problems.append("没有解析出工作经历或教育经历")
    for kind, keys in (("experience", ("company", "title")), ("education", ("school",))):
        for i, e in enumerate(data.get(kind) or [], 1):
            for k in keys:
                if not e.get(k) or _squash(e[k]) not in flat:
                    problems.append(f"{kind} #{i} 的 {k} {e.get(k)!r} 在简历原文里找不到")
            for k in ("start", "end"):
                v = str(e.get(k, ""))
                if not (_DATE.match(v) or (k == "end" and v == "present")):
                    problems.append(f"{kind} #{i} 的 {k} 日期格式不对：{v!r}")
    for i, e in enumerate(data.get("education") or [], 1):
        if e.get("level") not in _LEVELS:
            problems.append(f"education #{i} 的 level 不认识：{e.get('level')!r}")
    return problems


def parse(kind: str, llm) -> Path:
    """Parse one resume with the LLM, check it against the original text, and write config/resumes/<KIND>.yaml."""
    from .apply.answers import resume_text
    path = source(kind)
    text = resume_text(str(path))
    data = llm.json_chat("You convert resumes into structured JSON. Reply with JSON only.", PROMPT.format(text=text),
                         max_tokens=8000)
    data = {k: data.get(k) or ([] if k != "skills" else {}) for k in ("experience", "education", "projects", "skills")}
    problems = validate(data, text)
    if problems:
        raise ValueError("简历解析结果和原文对不上，没有保存：\n  " + "\n  ".join(problems))
    DIR.mkdir(parents=True, exist_ok=True)
    out = DIR / f"{kind}.yaml"
    header = (f"# 由 `jobagent resume parse` 从 {path.relative_to(config.ROOT)} 解析（{date.today()}），请核对。\n"
              "# 可以直接改这个文件；简历文件换了或改了，要重新运行 `uv run jobagent resume parse`。\n")
    body = yaml.safe_dump({"source": str(path.relative_to(config.ROOT)), "source_sha1": _sha1(path), **data},
                          allow_unicode=True, sort_keys=False, width=120)
    out.write_text(header + body, encoding="utf-8")
    _cache.clear()
    return out


_cache: dict[str, dict | None] = {}
_lock = threading.Lock()
_warned: set[str] = set()


def structured(kind: str | None) -> dict | None:
    """The parsed resume for this kind, or None when there is none or it no longer matches the resume file."""
    kind = kind if kind in KINDS else "SDE"
    with _lock:
        if kind in _cache:
            return _cache[kind]
        path, data = DIR / f"{kind}.yaml", None
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            src = source(kind)
            if data.get("source") != str(src.relative_to(config.ROOT)) or data.get("source_sha1") != _sha1(src):
                if kind not in _warned:
                    log.warning("简历 %s 改过了，结构化解析已过期：先用 LLM 读简历，运行 `uv run jobagent resume parse` 更新", kind)
                    _warned.add(kind)
                data = None
        _cache[kind] = data
        return data
