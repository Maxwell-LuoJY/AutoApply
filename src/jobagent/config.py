"""Project paths, YAML config, and LLM credentials."""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(os.environ.get("JOBAGENT_HOME") or Path(__file__).resolve().parents[2])
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "jobagent.db"
SHORTLIST_DIR = ROOT / "shortlists"
RUN_LOG_DIR = ROOT / "run_logs"
APPLICATIONS_CSV = ROOT / "applications.csv"


def _load(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        raise SystemExit(f"缺少 {path}：先运行 `uv run jobagent init`，再按 config/{name.replace('.yaml', '.example.yaml')} 的注释填写\n"
                         f"(missing {path}: run `uv run jobagent init`, then fill it in)")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@lru_cache
def settings() -> dict:
    return _load("settings.yaml")


@lru_cache
def profile() -> dict:
    return _load("profile.yaml")


def degree_level(edu: dict) -> str:
    """master | bachelor | phd | associate | other: education[].level, else read from the degree name."""
    if edu.get("level"):
        return str(edu["level"]).lower()
    d = str(edu.get("degree", "")).lower()
    for level, pattern in (("phd", r"ph\.?\s?d|doctor"), ("master", r"master|\bm\.?\s?(s|sc|eng|a|phil)\b|\bmba\b"),
                           ("bachelor", r"bachelor|\bb\.?\s?(s|sc|eng|e|a|tech)\b"), ("associate", r"associate")):
        if re.search(pattern, d):
            return level
    return "other"


def applicant_level(p: dict | None = None) -> str:
    """The degree the applicant is studying for now (the first education entry)."""
    edu = (p or profile())["education"]
    return degree_level(edu[0]) if edu else "other"


def llm_credentials() -> tuple[list[str], str, str]:
    """Return ([api keys], base_url, model) from .env. Extra keys: LLM_API_KEY_2 … LLM_API_KEY_9 (same endpoint/model)."""
    load_dotenv(ROOT / ".env")
    missing = [n for n in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL") if not os.environ.get(n)]
    if missing:
        raise RuntimeError(f"缺少 {', '.join(missing)}，请写在 {ROOT / '.env'}")
    keys = [os.environ["LLM_API_KEY"]] + [os.environ[k] for k in (f"LLM_API_KEY_{i}" for i in range(2, 10)) if os.environ.get(k)]
    return list(dict.fromkeys(keys)), os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"]
