"""First-run setup: `jobagent init` copies the example config into place, `jobagent check` says what is still missing.

Both print Chinese and English, because they are the first thing a new user (or the agent setting things up) runs.
"""
from __future__ import annotations

import imaplib
import os
import shutil
from pathlib import Path

import yaml
from dotenv import dotenv_values

from . import config

EXAMPLES = [  # (example file, the file the program reads)
    ("config/settings.example.yaml", "config/settings.yaml"),
    ("config/profile.example.yaml", "config/profile.yaml"),
    ("config/answers.example.yaml", "config/answers.yaml"),
    (".env.example", ".env"),
    ("credentials.example.txt", "credentials.txt"),
]
SECRET = {".env", "credentials.txt"}


def init() -> None:
    for example, target in EXAMPLES:
        src, dst = config.ROOT / example, config.ROOT / target
        if dst.exists():
            print(f"· 已存在，未改动 / exists, left as is: {target}")
            continue
        shutil.copyfile(src, dst)
        if target in SECRET:
            dst.chmod(0o600)
        print(f"✓ 已创建 / created: {target}  (from {example})")
    for d in ("materials", "data"):
        (config.ROOT / d).mkdir(exist_ok=True)
    print("\n下一步 / Next:\n"
          "  1. 把简历放进 materials/，填写 config/profile.yaml 和 .env\n"
          "     Put your resume in materials/, fill in config/profile.yaml and .env\n"
          "  2. uv run jobagent check          # 检查还缺什么 / see what is still missing\n"
          "  3. uv run jobagent resume parse   # 可选：把简历解析成 config/resumes/*.yaml / optional")


def _ok(msg: str) -> tuple[str, str]:
    return "✅", msg


def _warn(msg: str) -> tuple[str, str]:
    return "⚠️ ", msg


def _bad(msg: str) -> tuple[str, str]:
    return "❌", msg


def _example_profile() -> dict:
    path = config.ROOT / "config/profile.example.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


def _profile_checks(p: dict) -> list[tuple[str, str]]:
    out = []
    missing = [k for k in ("name", "contact", "education", "availability", "work_authorization", "eeo", "defaults", "resumes")
               if not p.get(k)]
    if missing:
        return [_bad(f"profile.yaml 缺少 / missing: {', '.join(missing)}")]
    ex = _example_profile()
    same = [label for label, get in (
        ("name", lambda d: (d["name"]["first"], d["name"]["last"])),
        ("contact.email", lambda d: d["contact"]["email"]),
        ("contact.phone", lambda d: d["contact"]["phone"]),
        ("contact.address", lambda d: d["contact"]["address"]["street"]),
        ("education", lambda d: [e["school"] for e in d["education"]]),
    ) if ex and (v := _safe(get, p)) is not None and v == _safe(get, ex)]
    if same:
        out.append(_bad(f"profile.yaml 还是示例申请人的信息 / still the example applicant: {', '.join(same)}"))
    else:
        out.append(_ok(f"profile.yaml：{p['name']['first']} {p['name']['last']}，{p['contact']['email']}"))
    for kind, files in (p.get("resumes") or {}).items():
        paths = [config.ROOT / f for f in (files or {}).values() if f]
        if any(x.exists() for x in paths):
            out.append(_ok(f"简历 / resume {kind}: {next(x for x in paths if x.exists()).relative_to(config.ROOT)}"))
        else:
            out.append(_bad(f"简历 {kind} 文件不存在 / resume file not found: {', '.join(str(x.relative_to(config.ROOT)) for x in paths)}"))
    if p.get("transcript") and not (config.ROOT / p["transcript"]).exists():
        out.append(_warn(f"成绩单不存在（只在申请表强制要求时才用）/ transcript not found: {p['transcript']}"))
    return out


def _safe(get, d):
    try:
        return get(d)
    except (KeyError, TypeError, IndexError):
        return None


def _env_checks(test_llm: bool, test_mail: bool) -> list[tuple[str, str]]:
    out = []
    env = {**dotenv_values(config.ROOT / ".env"), **{k: v for k, v in os.environ.items() if k.startswith(("LLM_", "GMAIL_"))}}
    missing = [k for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL") if not env.get(k) or "your-" in str(env.get(k))]
    if missing:
        out.append(_bad(f".env 缺少 LLM 配置 / missing: {', '.join(missing)}"))
    elif test_llm:
        try:
            from .llm import LLM
            reply = LLM(rps=1.0).json_chat('Reply with JSON: {"ok": true}', "ping", max_tokens=200)
            out.append(_ok(f"LLM 接口可用 / LLM reachable: {env['LLM_MODEL']} → {reply}"))
        except Exception as e:  # noqa: BLE001  any failure is the answer here
            out.append(_bad(f"LLM 调用失败 / LLM call failed: {type(e).__name__}: {str(e)[:200]}"))
    else:
        out.append(_ok(f"LLM：{env['LLM_MODEL']} @ {env['LLM_BASE_URL']}（加 --llm 实际调用一次 / add --llm to test）"))
    if not env.get("GMAIL_APP_PASSWORD"):
        out.append(_warn("没有配置 Gmail（可选）：读不了验证码 / 确认信，需要验证码的申请会转为手动 / "
                         "Gmail not set (optional): applications that need an emailed code become manual"))
    elif test_mail:
        try:
            with imaplib.IMAP4_SSL("imap.gmail.com") as m:
                m.login(env.get("GMAIL_ADDRESS") or "", str(env["GMAIL_APP_PASSWORD"]).replace(" ", ""))
            out.append(_ok(f"Gmail IMAP 登录成功 / login OK: {env.get('GMAIL_ADDRESS')}"))
        except Exception as e:  # noqa: BLE001
            out.append(_bad(f"Gmail IMAP 登录失败 / login failed: {str(e)[:200]}"))
    else:
        out.append(_ok(f"Gmail：{env.get('GMAIL_ADDRESS')}（加 --mail 实际登录一次 / add --mail to test）"))
    return out


def _credentials_check() -> tuple[str, str]:
    from .apply.runner import read_credentials
    creds = read_credentials(config.ROOT / "credentials.txt")
    if not creds or creds[0].endswith("@example.com"):
        return _warn("credentials.txt 没填（可选）：Workday 申请需要用它登录 / 注册 / "
                     "credentials.txt not filled (optional): Workday needs it to sign in / sign up")
    return _ok(f"credentials.txt：{creds[0]}")


def _browser_check() -> tuple[str, str]:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            path = Path(pw.chromium.executable_path)
        if path.exists():
            return _ok("Playwright Chromium 已安装 / installed")
    except Exception:  # noqa: BLE001
        pass
    return _bad("Playwright Chromium 没装：运行 `uv run playwright install chromium` / not installed")


def check(test_llm: bool = False, test_mail: bool = False) -> bool:
    """Print what is ready and what is missing. True when nothing required is missing."""
    rows: list[tuple[str, str]] = []
    for _, target in EXAMPLES[:3]:
        if not (config.ROOT / target).exists():
            rows.append(_bad(f"缺少 / missing {target}：先运行 `uv run jobagent init`"))
    if not any(r[0] == "❌" for r in rows):
        p = config.profile()
        rows += _profile_checks(p)
        s = config.settings()
        mode = s["applying"].get("mode")
        rows.append(_ok(f"settings.yaml：term {s.get('term')}，applying.mode = {mode}")
                    if mode == "dry_run" else
                    _warn(f"settings.yaml：applying.mode = {mode}，`jobagent apply` 会真实提交 / will really submit"))
        from . import resume
        stale = [k for k in resume.KINDS if k in p["resumes"] and not resume.structured(k)]
        if stale:
            rows.append(_warn(f"还没解析简历 {', '.join(stale)}（可选）：`uv run jobagent resume parse` / resume not parsed (optional)"))
    rows += _env_checks(test_llm, test_mail)
    rows.append(_credentials_check())
    rows.append(_browser_check())
    for mark, msg in rows:
        print(f"{mark} {msg}")
    ready = not any(r[0] == "❌" for r in rows)
    print("\n" + ("可以开始了：先跑 `uv run jobagent run`，再 `uv run jobagent apply --dry-run` / Ready: start with a dry run"
                  if ready else "还有 ❌ 项要处理 / Fix the ❌ items first"))
    return ready
