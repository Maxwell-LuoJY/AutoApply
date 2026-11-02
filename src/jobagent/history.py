"""Read and append applications.csv. The file is append-only."""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .rules import canonical_key, company_title_key

COLUMNS = ["申请时间", "公司名称", "岗位名称", "类别", "使用简历", "申请链接", "来源", "状态", "失败原因"]
SUBMITTED, FAILED, SKIPPED = "已提交", "失败", "跳过"
_ESSAY_SKIP = re.compile(r"需要开放式回答(?!（题目要求不用 AI)|开放式问答|open[- ]ended")


@dataclass
class History:
    # each entry is (CSV row number, status), so a row found by both its link and its company+title counts once
    by_url: dict[str, list[tuple[int, str]]] = field(default_factory=lambda: defaultdict(list))
    by_name: dict[str, list[tuple[int, str]]] = field(default_factory=lambda: defaultdict(list))

    def blocking_status(self, url: str, company: str, title: str, max_failures: int = 3) -> str | None:
        """Dedup: never redo 已提交/跳过; retry 失败 up to max_failures times."""
        rows = dict(self.by_url.get(canonical_key(url), []) + self.by_name.get(company_title_key(company, title), []))
        statuses = list(rows.values())
        if SUBMITTED in statuses:
            return SUBMITTED
        if SKIPPED in statuses:
            return SKIPPED
        if statuses.count(FAILED) >= max_failures:
            return f"{FAILED} {max_failures} 次"
        return None


def load(path: Path) -> History:
    hist = History()
    if not path.exists():
        return hist
    with path.open(encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f)):
            status = (row.get("状态") or "").strip()
            if status == SKIPPED and _ESSAY_SKIP.search(row.get("失败原因") or ""):
                status = "跳过（短文，规则已改）"  # 2026-10-30: essays are written by the LLM now, so these may be retried
            if row.get("申请链接"):
                hist.by_url[canonical_key(row["申请链接"])].append((i, status))
            hist.by_name[company_title_key(row.get("公司名称", ""), row.get("岗位名称", ""))].append((i, status))
    return hist


def append(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    new_file = not path.exists() or path.stat().st_size == 0
    if not new_file:
        with path.open("rb") as f:
            f.seek(-1, 2)
            needs_newline = f.read(1) != b"\n"
    with path.open("a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as f:
        if not new_file and needs_newline:
            f.write("\n")
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow({**{c: "" for c in COLUMNS}, **r})


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
