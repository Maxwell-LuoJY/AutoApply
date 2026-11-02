"""Apply to shortlisted jobs: open → fill (one or more steps) → submit, or stop before submit in dry-run → record."""
from __future__ import annotations

import fcntl
import logging
import os
import queue
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .. import config, history, pipeline
from ..db import DB
from ..llm import LLM
from openai import RateLimitError
from ..rules import blocked_company, csv_category, too_old
from . import answers, audit, forms
from .adapters import (Adapter, Blocked, Failed, Skip, Unconfirmed, adapter_for, already_applied_reason, click_button,
                       settle)
from .browser import BrowserSession
from .mail import Mailbox

log = logging.getLogger("jobagent")

MAX_STEPS = 25   # pages plus waits for slow pages / refreshes (J&J ran out at 12); stuck pages are caught separately
# Anything not submitted goes on the manual-apply list and is never retried automatically,
# so a job you apply to by hand can't be applied to again by the agent.
STAGE = {"submitted": "applied", "skip": "skip", "blocked": "manual", "unconfirmed": "manual", "failed": "manual"}
CSV_STATUS = {"submitted": history.SUBMITTED, "skip": history.SKIPPED, "blocked": history.FAILED,
              "unconfirmed": history.FAILED, "failed": history.FAILED}
LABEL = {"submitted": "✅ 已提交", "dry_run": "🟡 试运行：已填完", "skip": "⏭ 跳过", "blocked": "✋ 需人工处理",
         "unconfirmed": "❓ 未确认", "failed": "❌ 失败", "retry": "🔁 稍后重试"}
SOURCE_LABEL = {"Simplify": "SimplifyJobs", "SpeedyApply": "SpeedyApply"}
# unknown sites that show no fillable form need a person (usually an account wall); known ATSes get retried
_MANUAL = {True: "（这个网站可能需要先登录，请手动投）", False: ""}


@dataclass
class Outcome:
    status: str
    reason: str
    answers: list[dict] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    url: str = ""


@dataclass
class ApplyContext:
    llm: LLM
    profile: dict
    mode: str                                  # dry_run | submit
    mailbox: Mailbox | None
    credentials: tuple[str, str] | None
    shots_dir: Path
    accounts_in_dry_run: bool = False          # allow Workday sign-in / sign-up during dry runs
    headed: bool = False                       # a person can see the window (and finish a CAPTCHA themselves)
    workday_auth_attempts: int = 0
    workday_signin_tried: bool = False
    workday_account_created: bool = False
    workday_verified: bool = False
    row: object = None                         # the job being worked on (one context per worker thread)
    answer_cache: dict = field(default_factory=dict)   # this job's LLM answers, reused when a page is re-filled
    corrections: dict = field(default_factory=dict)    # fixes from the pre-submit check: (label, nth) → value
    audit_rounds: int = 0                              # correction rounds used by the pre-submit check
    seen: list = field(default_factory=list)           # every field shown on earlier pages, with what it displayed
    field_options: dict = field(default_factory=dict)  # (label, nth) → dropdown options read while filling
    audit_settled: dict = field(default_factory=dict)  # (label, value) that stayed after re-entering the fix


def read_credentials(path: Path) -> tuple[str, str] | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    e = re.search(r"(?im)^\s*(?:email|e-mail|邮箱)\s*[:：=]\s*(\S+)", text)
    p = re.search(r"(?im)^\s*(?:password|密码)\s*[:：=]\s*(\S+)", text)
    return (e.group(1), p.group(1)) if e and p else None


def resolve_file(profile: dict, row, what: str, accept: str) -> Path | None:
    if what == "resume":
        r = profile["resumes"].get(row["resume"] or "SDE") or profile["resumes"]["SDE"]
        a = (accept or "").lower()
        pdf, docx = config.ROOT / (r.get("pdf") or r["docx"]), config.ROOT / (r.get("docx") or r["pdf"])
        use_docx = (bool(a) and "pdf" not in a and "doc" in a) or not pdf.exists()  # no PDF exported yet: send the .docx
        return docx if use_docx else pdf
    if what == "transcript":
        return config.ROOT / profile["transcript"]
    return None


def _show(v) -> str:
    if isinstance(v, list):
        return "、".join(map(str, v))
    if isinstance(v, bool):
        return "勾选" if v else "不勾选"
    return str(v)


def _uploaded(page: Page, path: Path | None) -> bool:
    """The file's name is already listed on the page (Workday redraws the upload box after each upload)."""
    try:
        return bool(path) and page.get_by_text(path.name, exact=False).count() > 0
    except PlaywrightError:
        return False


def _fill_step(page: Page, adapter: Adapter, snap: forms.Snapshot, row, ctx: ApplyContext, resume_pdf: Path, qa: list) -> None:
    done, handled, errors = set(), set(), []
    for pass_no in range(3):  # later passes pick up conditional questions revealed by earlier answers
        if pass_no and adapter.prepare(page, ctx):  # an upload can make Workday re-parse the resume and add entries
            snap = forms.snapshot(page, adapter.root)
        todo = [f for f in snap.fields if adapter.relevant(f) and f["id"] not in done]
        if pass_no:  # redrawn copies of fields already answered in this step aren't new questions
            todo = [f for f in todo if not ((f["kind"], f["label"], f.get("nth", 0)) in handled
                                            and (f["kind"] == "file" or not forms.is_empty(f)))]
        if not todo:
            break
        forms.load_dropdown_options(page, todo)
        ctx.field_options.update({(f["label"], f.get("nth", 0)): f["options"] for f in todo if f.get("options")})
        plan = answers.plan(todo, ctx.llm, ctx.profile, row, resume_pdf, ctx.answer_cache)
        if plan.cover_letter_required:
            raise Skip("申请表必填 cover letter")
        if plan.open_ended:
            raise Skip("需要开放式回答（题目要求不用 AI 写，需要你自己写）：" + "；".join(plan.open_ended[:3]))
        if plan.conflicts:
            raise Blocked("工作授权类问题的答案和资料不一致，已停下：" + "；".join(plan.conflicts[:3]))
        if plan.unknown and _only_extra_entries(plan.unknown, ctx) and adapter.prepare(page, ctx):
            # a re-parse after the upload added an entry the resume doesn't have (J&J: a blank Work Experience 5):
            # prepare() deleted it; read the page again instead of giving up
            log.info("   多出来的经历条目已删除，重新读取这一页")
            page.wait_for_timeout(1500)
            snap = forms.snapshot(page, adapter.root)
            continue
        if plan.unknown:
            raise Blocked("有必填题答不了：" + "；".join(plan.unknown[:4]) + "（可在 config/answers.yaml 补充答案后重新排队）")
        for it in plan.items:  # the pre-submit check found this field wrong earlier: enter its correction instead
            if (fix := audit.correction(ctx, it.field)) is not None:
                it.value, it.basis = fix, "audit-fix"
        retry = []
        sig = forms.page_signature(page)
        for it in sorted(plan.items, key=lambda it: it.field["kind"] != "file"):
            if forms.page_signature(page) != sig:
                log.info("   页面在填写途中跳到了下一步，剩下的字段留给下一步处理")
                return
            f = it.field
            if f["kind"] == "file" and _uploaded(page, resolve_file(ctx.profile, row, str(it.value), f.get("accept", ""))):
                continue  # already uploaded; uploading again would attach a second copy
            err = forms.fill_field(page, f, it.value, lambda v, f=f: resolve_file(ctx.profile, row, v, f.get("accept", "")))
            qa.append({"label": f["label"], "kind": f["kind"], "value": _show(it.value), "basis": it.basis, "error": err,
                       "nth": f.get("nth", 0)})
            if f["kind"] == "combobox":
                page.keyboard.press("Escape")  # never leave a dropdown open over the next fields
            if err and f.get("required"):
                retry.append((it, len(qa) - 1))
            if f["kind"] == "file" and not err:
                page.wait_for_timeout(3500)  # let resume parsing finish before typing answers
        if retry:  # Workday redraws sections after a pick, so look the field up again instead of reusing a stale id
            page.keyboard.press("Escape")
            page.wait_for_timeout(600)
            fresh = {(g["kind"], g["label"], g.get("nth", 0)): g for g in forms.snapshot(page, adapter.root).fields}
        for it, qa_index in retry:  # dropdowns occasionally fail to open on the first try
            page.keyboard.press("Escape")
            page.wait_for_timeout(600)
            f = fresh.get((it.field["kind"], it.field["label"], it.field.get("nth", 0)), it.field)
            if f is not it.field and f["kind"] != "file" and isinstance(f.get("value"), str) and isinstance(it.value, str) \
                    and f["value"] and forms.best_option(it.value, [f["value"]], fuzzy=False):
                qa[qa_index]["error"] = None  # the redrawn field already holds the wanted value (e.g. auto-selected)
                continue
            err = forms.fill_field(page, f, it.value, lambda v, f=f: resolve_file(ctx.profile, row, v, f.get("accept", "")))
            qa[qa_index]["error"] = err
            if err:
                errors.append(f"{f['label'][:60]}：{err}")
        done |= {f["id"] for f in todo}
        handled |= {(f["kind"], f["label"], f.get("nth", 0)) for f in todo}
        page.wait_for_timeout(800)
        snap = forms.snapshot(page, adapter.root)
    if errors:
        raise Failed("必填项填写失败：" + "；".join(errors[:4]))
    _match_education_to_schools(page, adapter, row, ctx, qa)


def _only_extra_entries(labels: list[str], ctx: ApplyContext) -> bool:
    """Every unanswerable field belongs to an entry past the resume's count ("Work Experience 5" for 4 jobs)."""
    from .adapters import resume_targets
    targets = resume_targets(ctx)
    for label in labels:
        m = re.match(r"(?i)^(work experience|education) (\d+) —", label)
        if not m or int(m.group(2)) <= targets["work" if m.group(1).lower().startswith("work") else "edu"]:
            return False
    return bool(labels)


def _match_education_to_schools(page: Page, adapter: Adapter, row, ctx: ApplyContext, qa: list) -> None:
    """Now that every entry shows its school / company, give each the degree, major, job title and description of
    that resume entry (no LLM). The planner may have gone by position: HPE gave the bachelor's school the Master's."""
    snap = forms.snapshot(page, adapter.root)
    fields = [{**f, "options": f.get("options") or ctx.field_options.get((f.get("label"), f.get("nth", 0))) or []}
              for f in snap.fields if adapter.relevant(f)]
    fixes = {**answers._education_by_school(fields, row), **answers._experience_by_company(fields, row)}
    for f_id, it in fixes.items():
        err = forms.fill_field(page, it.field, it.value, lambda v: None)
        if err and forms.norm(it.field.get("label")) == "field of study":
            # the list may lack the major (RBC has no "Artificial Intelligence"): the nearest broader field
            for broader in ("Computer Science", "Computer Engineering", "Engineering"):
                page.keyboard.press("Escape")
                if not (err := forms.fill_field(page, it.field, broader, lambda v: None)):
                    it = answers.Item(it.field, broader, it.basis)
                    break
        if it.field["kind"] == "combobox":
            page.keyboard.press("Escape")
        log.info("   按简历更正「%s」：%s → %s%s", it.field.get("label"), (audit.shown(it.field) or "空")[:40], str(it.value)[:40],
                 f"（失败：{err}）" if err else "")
        qa.append({"label": it.field["label"], "kind": it.field["kind"], "value": _show(it.value), "basis": "resume",
                   "error": err, "nth": it.field.get("nth", 0)})


def _final_audit(page: Page, adapter: Adapter, ctx: ApplyContext, qa: list, shot) -> None:
    """Read back what the page displays and compare it with the facts (rules, then an LLM judge).
    Returns when everything matches. Problems that can't be corrected within audit.MAX_ROUNDS raise Failed: nothing is
    submitted."""
    found: list[audit.Problem] = []
    while True:
        snap = forms.snapshot(page, adapter.root)
        if (missing := forms.missing_required([f for f in snap.fields if adapter.relevant(f)])):
            raise Failed("这些必填项没填上：" + "；".join(missing[:5]))
        try:
            items, found = audit.run(page, adapter, snap, ctx, qa)
        except (PlaywrightError, RateLimitError):
            raise
        except Exception as e:  # no verdict means no submission
            shot("before-submit")
            raise Failed(f"提交前总核对没完成（{type(e).__name__}: {str(e)[:120]}），没有提交") from e
        # a field re-entered as asked that still shows the same value: that is the closest the site offers
        found = [p for p in found if (forms.norm(p.field), p.shown) not in ctx.audit_settled or p.fix is None]
        if not found:
            log.info("   🔎 提交前总核对通过")
            return
        log.info("   🔎 提交前总核对发现 %d 处问题：%s", len(found), audit.describe(found))
        if ctx.audit_rounds >= audit.MAX_ROUNDS or any(p.fix is None for p in found):
            break
        ctx.audit_rounds += 1
        audit.remember(ctx, found)
        earlier = [p for p in found if p.item is None]
        if earlier:  # a review page: the wrong values were entered on earlier pages
            if not _fix_on_earlier_pages(page, adapter, earlier, shot, ctx.audit_settled):
                break
        if (here := [p for p in found if p.item is not None]) and not audit.fix_here(page, items, here):
            break
        log.info("   已修改，重新核对")
    shot("before-submit")
    raise Failed("提交前总核对没通过，没有提交：" + audit.describe(found))


def _fix_on_earlier_pages(page: Page, adapter: Adapter, problems: list, shot=lambda tag: None,
                          settled: dict | None = None) -> bool:
    """A review page shows wrong values entered on earlier pages. Go Back until every one of those fields has been seen,
    then forward page by page re-entering ONLY those fields, Next, … back to the review page. Nothing else on those pages
    is refilled (refilling a whole Workday page once dropped an education entry). True if every fix went in."""
    want = {(forms.norm(p.field), p.nth): p.fix for p in problems}

    def key(f: dict):
        label, nth = forms.norm(f.get("label") or ""), f.get("nth", 0)
        if (hit := next((k for k in ((label, nth), (label, None)) if k in want), None)):
            return hit
        # the same question read a little differently on another visit (a trailing note, "Required"): one label
        # starting with the other, long enough not to be a generic word like "Name"
        return next((k for k in want if k[1] in (None, nth) and min(len(k[0]), len(label)) >= 25
                     and (label.startswith(k[0]) or k[0].startswith(label))), None)

    seen, backs = set(), 0
    while seen != set(want) and backs < 8:
        back = adapter.back_button(forms.snapshot(page, adapter.root))
        if back is None:
            break
        click_button(page, back)
        settle(page, 3000)
        backs += 1
        for _ in range(7):  # Workday re-renders a step for a few seconds after Back (Elanco: fields missing at 3 s)
            fields = forms.snapshot(page, adapter.root).fields
            if any(adapter.relevant(f) for f in fields):
                break
            page.wait_for_timeout(1500)
        seen |= {k for f in fields if (k := key(f))}
    if seen != set(want):
        log.info("   往回翻了 %d 页，没找到要改的字段：%s", backs, "；".join(p.field for p in problems if
                                                                (forms.norm(p.field), p.nth) not in seen)[:120])
        return False
    log.info("   返回 %d 页修改：%s", backs, "；".join(p.field[:40] for p in problems))
    for n in range(backs):
        snap = forms.snapshot(page, adapter.root)
        touched = []
        for f in snap.fields:
            if (k := key(f)) and (err := forms.fill_field(page, f, want[k], lambda v: None)):
                if settled is not None and re.search(r"(?i)field of study|major|discipline|degree", f.get("label") or "") \
                        and re.search(r"选不中|找不到", err):
                    # the list doesn't offer it (RBC has no "Artificial Intelligence"): the current pick is the closest
                    log.info("   「%s」的列表里没有「%s」，保留现在的「%s」", f.get("label"), want[k], audit.shown(f))
                    page.keyboard.press("Escape")
                    settled[(forms.norm(f.get("label")), audit.shown(f))] = True
                    continue
                log.info("   修改失败：%s（%s）", f.get("label"), err)
                return False
            if k:
                touched.append((f.get("label"), f.get("nth", 0), want[k]))
                # Tab, not Escape: move focus on so Workday commits the pick (Elanco: an Escape-closed pick showed
                # on the page but "Save and Continue" saved the old value)
                page.keyboard.press("Tab")
                page.wait_for_timeout(500)
        if touched:  # read the fixed fields back: a pick that didn't stick must not pass as fixed
            page.wait_for_timeout(1500)
            now = {(g.get("label"), g.get("nth", 0)): audit.shown(g) for g in forms.snapshot(page, adapter.root).fields}
            for label, nth, value in touched:
                log.info("   修改「%s」→「%s」，页面现在显示「%s」", (label or "")[:50], str(value)[:50], now.get((label, nth), "?")[:50])
                before = next((p.shown for p in problems if forms.norm(p.field) == forms.norm(label)), None)
                if before and now.get((label, nth)) == before:
                    if settled is not None and re.search(r"(?i)field of study|major|discipline|degree", label or ""):
                        settled[(forms.norm(label), before)] = True  # nothing closer in the list (HP: "Computer Science and Mathematics")
                    else:  # any other field must take the fix (RBC: school country stayed "Other" and was submitted)
                        log.info("   修改没生效：「%s」还是「%s」", (label or "")[:50], before[:50])
                        return False
            shot(f"fix{n + 1}")
        nxt, sig = adapter.next_button(snap), forms.page_signature(page)
        if nxt is None:
            return False
        click_button(page, nxt)
        settle(page, 3000)
        for _ in range(8):  # Workday saves the step before moving on: give it up to ~15 s
            if forms.page_signature(page) != sig:
                break
            page.wait_for_timeout(1500)
        if forms.page_signature(page) == sig:
            log.info("   修改后点下一步没有前进：%s", "；".join(forms.snapshot(page, adapter.root).errors[:2]))
            return False
    return adapter.submit_button(forms.snapshot(page, adapter.root)) is not None


def _enter_code(page: Page, snap: forms.Snapshot, before_ids: set[str], code: str) -> None:
    """Type the emailed code into the boxes that appeared after submit (one box, or one box per character)."""
    textual = ("text", "number", "tel")
    boxes = [f for f in snap.fields if f["kind"] in textual and f["id"] not in before_ids] or \
            [f for f in snap.fields if f["kind"] in textual and re.search(r"(?i)security|code", f["label"])]
    if not boxes:
        raise Failed("收到了验证码，但页面上找不到输入框")
    if len(boxes) >= len(code):
        for f, ch in zip(boxes, code):
            page.locator(f'[data-ja-id="{f["id"]}"]').fill(ch)
    else:
        page.locator(f'[data-ja-id="{boxes[0]["id"]}"]').fill(code)
    page.wait_for_timeout(800)


def _submit(page: Page, adapter: Adapter, sub: dict, ctx: ApplyContext, qa: list, shots: list, shot) -> Outcome:
    since = datetime.now(timezone.utc)
    before_ids = {f["id"] for f in forms.snapshot(page, None).fields}
    shot("before-submit")
    click_button(page, sub)
    deadline, code_done, waited_for_human, snap = time.time() + 30, False, False, None
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        try:
            snap = forms.snapshot(page, None)
        except PlaywrightError:
            continue  # still navigating to the confirmation page; a failure here must not hide a real submission
        if adapter.is_success(snap):
            shot("submitted")
            return Outcome("submitted", "提交成功（看到确认页面）", qa, shots, page.url)
        if snap.captcha:
            if not ctx.headed:
                shot("captcha")
                raise Blocked("提交时出现人机验证（不会自动处理）：用 --headed 运行可以在窗口里自己完成，或手动投")
            if not waited_for_human:
                # never solved by the agent: a person completes it in the visible window, then we continue
                shot("captcha")
                log.warning("   ⚠ 出现人机验证：请在弹出的浏览器窗口里完成它（等待 2 分钟）")
                waited_for_human, deadline = True, time.time() + 120
        if not code_done and adapter.email_code_needed(page, snap):
            shot("code-requested")
            code = adapter.email_code(ctx, since)
            if not code:
                raise Blocked("3 分钟内没收到邮箱验证码")
            _enter_code(page, snap, before_ids, code)
            again = adapter.submit_button(forms.snapshot(page, adapter.root))
            if again:
                click_button(page, again)
            code_done, deadline = True, time.time() + 30
    shot("after-submit")
    if waited_for_human:
        raise Blocked("人机验证 2 分钟内没有完成，申请没有提交，请手动投")
    if snap and snap.errors:
        raise Failed("提交后页面报错：" + "；".join(snap.errors[:3]))
    overlays = f"（页面上的弹窗：{'、'.join(snap.frames)}）" if snap and snap.frames else ""
    raise Unconfirmed("点了提交但 30 秒内没看到成功提示，可能已经提交，请查邮箱确认" + overlays)


def apply_job(session: BrowserSession, row, ctx: ApplyContext) -> Outcome:
    adapter = adapter_for(row["ats"])
    page = session.new_page()
    qa: list[dict] = []
    shots: list[str] = []
    ctx.workday_auth_attempts, ctx.row = 0, row
    ctx.workday_signin_tried = ctx.workday_account_created = ctx.workday_verified = False
    ctx.answer_cache, ctx.corrections, ctx.audit_rounds, ctx.seen, ctx.field_options = {}, {}, 0, [], {}
    ctx.audit_settled = {}
    resume_pdf = resolve_file(ctx.profile, row, "resume", "")

    def shot(tag: str) -> None:
        # a dry run of a job that was already submitted must not overwrite the real submission's screenshots
        path = ctx.shots_dir / f"{row['id']}{'-dry' if ctx.mode == 'dry_run' else ''}-{tag}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            shots.append(str(path))
        except PlaywrightError:
            pass

    try:
        adapter.open(page, row, ctx, forms.snapshot)
        last_sig, stuck, reloads, empty_waits = None, 0, 0, 0
        while True:  # Workday's "Something went wrong, refresh the page" can break a click mid-step: reload, go on
            try:
                for step in range(1, MAX_STEPS + 1):
                    snap = forms.snapshot(page, adapter.root)
                    if reloads < 2 and adapter.recover(page, snap):
                        reloads += 1
                        continue
                    if snap.captcha:
                        raise Blocked("遇到人机验证或网站拒绝自动访问（不会尝试绕过），请手动投")
                    if not any(adapter.relevant(f) for f in snap.fields) and (when := adapter.already_applied(snap)) is not None:
                        raise Skip(already_applied_reason(when))  # only a page without a form can be an "already applied" notice
                    if adapter.is_closed(snap):
                        raise Skip("岗位已关闭或不再接受申请")
                    if adapter.handle_auth(page, snap, ctx):
                        continue
                    fields = [f for f in snap.fields if adapter.relevant(f)]
                    if not fields and not adapter.submit_button(snap) and empty_waits < 4:
                        # no fields yet: still loading (Workday after a refresh showed only a spinner and a Next button;
                        # clicking it saved My Information unfilled, with the parser's "Middle Name: <first name>")
                        empty_waits += 1  # single-page apps often render the form a few seconds after navigation
                        page.wait_for_timeout(5000)
                        continue
                    if fields:
                        empty_waits = 0  # the wait allowance is per page
                    if not fields and not adapter.submit_button(snap) and not adapter.next_button(snap):
                        raise (Blocked if adapter.name == "generic" else Failed)("页面上没有找到申请表" + _MANUAL[adapter.name == "generic"])
                    if fields and adapter.prepare(page, ctx):  # e.g. Workday: add missing experience / education entries
                        snap = forms.snapshot(page, adapter.root)
                    if fields:
                        try:
                            _fill_step(page, adapter, snap, row, ctx, resume_pdf, qa)
                        except Failed as e:
                            if "Timeout" not in str(e):
                                raise
                            # the page was still re-rendering (Workday autofill keeps redrawing for a few seconds): once more
                            log.info("   页面还在刷新，等 5 秒重填这一页")
                            page.wait_for_timeout(5000)
                            _fill_step(page, adapter, forms.snapshot(page, adapter.root), row, ctx, resume_pdf, qa)
                    snap = forms.snapshot(page, adapter.root)
                    if fields and (problems := adapter.check(page, snap, ctx)):
                        adapter.prepare(page, ctx)  # one more round: add entries, fill what is still empty
                        _fill_step(page, adapter, forms.snapshot(page, adapter.root), row, ctx, resume_pdf, qa)
                        snap = forms.snapshot(page, adapter.root)
                        if (problems := adapter.check(page, snap, ctx)):
                            raise Failed("经历没填全，没有提交：" + "；".join(problems))
                    shot(f"step{step}")
                    # what each field shows as we leave the page (incl. values the site pre-filled): the review check refers to them
                    ctx.seen.extend({"label": f.get("label"), "kind": f["kind"], "nth": f.get("nth", 0), "value": audit.shown(f),
                                     "options": f.get("options") or ctx.field_options.get((f.get("label"), f.get("nth", 0))) or []}
                                    for f in snap.fields if adapter.relevant(f) and f["kind"] != "file" and f.get("label"))
                    nxt, sub = adapter.next_button(snap), adapter.submit_button(snap)
                    if nxt and not sub:
                        sig = (snap.url, tuple(f["label"] for f in snap.fields))
                        stuck = stuck + 1 if sig == last_sig else 0
                        if stuck >= 2:
                            detail = snap.errors[:3] or forms.missing_required(snap.fields)[:3]
                            raise Failed("点了下一步但一直停在同一页：" + "；".join(detail))
                        last_sig = sig
                        click_button(page, nxt)
                        settle(page, 3000)
                        continue
                    if not sub and reloads < 2 and adapter.recover(page, forms.snapshot(page, adapter.root)):
                        reloads += 1  # Workday's "Something went wrong" page, shown right after filling (Elanco)
                        log.info("   页面出错，刷新后继续")
                        continue
                    if not sub:
                        raise (Blocked if adapter.name == "generic" else Failed)("找不到「下一步」或「提交」按钮" + _MANUAL[adapter.name == "generic"])
                    missing = forms.missing_required([f for f in snap.fields if adapter.relevant(f)])
                    if missing:
                        raise Failed("这些必填项没填上：" + "；".join(missing[:5]))
                    if (problems := adapter.check_before_submit(page, ctx)):
                        shot("before-submit")
                        raise Failed("提交前检查没通过，没有提交：" + "；".join(problems))
                    _final_audit(page, adapter, ctx, qa, shot)
                    if ctx.mode != "submit":
                        shot("before-submit")
                        return Outcome("dry_run", "已填完，停在提交前（试运行，没有提交）", qa, shots, page.url)
                    return _submit(page, adapter, sub, ctx, qa, shots, shot)
                raise Failed(f"超过 {MAX_STEPS} 步还没到提交页")
            except PlaywrightError:
                if reloads < 2 and adapter.recover(page, forms.snapshot(page, adapter.root)):
                    reloads += 1
                    log.info("   页面出错，刷新后继续")
                    continue
                raise
    except (Skip, Blocked, Failed, Unconfirmed) as e:
        shot("end")
        status = {Skip: "skip", Blocked: "blocked", Failed: "failed", Unconfirmed: "unconfirmed"}[type(e)]
        return Outcome(status, str(e), qa, shots, page.url)
    except PlaywrightError as e:
        shot("error")
        return Outcome("failed", f"浏览器操作出错：{str(e).splitlines()[0][:200]}", qa, shots, page.url)
    except Exception as e:  # LLM/API/anything else: fail this job, keep the batch going
        shot("error")
        msg = f"{type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}"
        if isinstance(e, RateLimitError) or "限流" in str(e):
            return Outcome("retry", f"API 限流，稍后重试：{msg}", qa, shots, page.url)  # nothing was submitted
        log.exception("处理 %s 时出错", row["id"])
        return Outcome("failed", msg, qa, shots, page.url)
    finally:
        page.close()


def _record(db: DB, row, out: Outcome, mode: str) -> None:
    db.add_attempt(row["id"], mode, out.status, out.reason, out.answers, out.screenshots, out.url)
    if mode == "submit" and out.status != "retry":
        db.update(row["id"], stage=STAGE[out.status], reason=out.reason)
        pipeline.unpin(db, row["id"])  # a pin is for one real attempt
        history.append(config.APPLICATIONS_CSV, [{
            "申请时间": history.now_str(), "公司名称": row["company"], "岗位名称": row["title"],
            "类别": csv_category(row["category"] or ""), "使用简历": row["resume"] or "", "申请链接": row["url"],
            "来源": SOURCE_LABEL.get(row["source"], row["source"]), "状态": CSV_STATUS[out.status],
            "失败原因": "" if out.status == "submitted" else out.reason,
        }])
    db.commit()


def candidates(db: DB, ids: list[str] | None) -> list:
    if ids:
        rows = [db.get(i) for i in ids]
        missing = [i for i, r in zip(ids, rows) if r is None]
        if missing:
            raise SystemExit(f"找不到岗位：{', '.join(missing)}")
    else:
        hist = history.load(config.APPLICATIONS_CSV)
        max_failures = config.settings()["applying"]["max_failures"]
        max_age, now, pins = config.settings().get("max_age_days"), time.time(), pipeline.pinned(db)
        rows = [r for r in db.rows("stage = 'eligible' AND active = 1")
                if not hist.blocking_status(r["url"], r["company"], r["title"], max_failures)
                and (r["id"] in pins or not too_old(r["date_posted"], max_age, now))]
    keep = []
    for r in rows:  # the blocklist holds even for jobs named on the command line
        if (reason := blocked_company(r["company"], config.settings().get("blocked_companies"))):
            log.warning("跳过 %s — %s：%s", r["company"], r["title"], reason)
        else:
            keep.append(r)
    return keep


SCREEN_POLL_SECONDS = 30  # while older jobs are screened in the background, how often to look for newly passed ones


class _Dispatcher:
    """Hands jobs to workers. Never two jobs at the same company at once (Workday sessions, emailed codes),
    and never more in flight than submissions still needed, so a batch can't overshoot its target."""

    def __init__(self, rows: list, target: int, max_attempts: int, low_water: int):
        self.queue, self.target, self.max_attempts, self.low_water = list(rows), target, max_attempts, low_water
        self.in_flight = self.succeeded = self.attempts = 0
        self.busy: set[str] = set()
        self.retries: dict[str, int] = {}
        self.exhausted = self.stopped = False
        self.cv = threading.Condition()

    @staticmethod
    def _key(row) -> str:
        return re.sub(r"[^a-z0-9]+", "", row["company"].lower())

    def _finished(self) -> bool:
        idle = self.in_flight == 0
        return self.succeeded >= self.target or (self.attempts >= self.max_attempts and idle) or \
            (self.exhausted and not self.queue and idle)

    def take(self):
        with self.cv:
            while not self.stopped:
                if self.succeeded + self.in_flight < self.target and self.attempts < self.max_attempts:
                    for i, r in enumerate(self.queue):
                        if self._key(r) not in self.busy:
                            self.queue.pop(i)
                            self.busy.add(self._key(r))
                            self.in_flight += 1
                            self.attempts += 1
                            return r
                if self._finished():
                    self.stopped = True
                    self.cv.notify_all()
                    break
                self.cv.wait(timeout=1.0)
            return None

    def may_retry(self, row) -> bool:
        with self.cv:
            n = self.retries.get(row["id"], 0)
            if n >= 2:
                return False
            self.retries[row["id"]] = n + 1
            return True

    def done(self, row, success: bool, requeue: bool = False) -> None:
        with self.cv:
            self.busy.discard(self._key(row))
            self.in_flight -= 1
            self.succeeded += int(success)
            if requeue:  # transient failure: back of the queue, and it doesn't use up an attempt
                self.queue.append(row)
                self.attempts -= 1
            self.stopped = self.stopped or self._finished()
            self.cv.notify_all()

    def add(self, rows: list) -> None:
        with self.cv:
            self.queue.extend(rows)
            self.cv.notify_all()

    def wants_more(self) -> bool:
        with self.cv:
            return not self.stopped and not self.exhausted and len(self.queue) <= self.low_water

    def no_more(self) -> None:
        with self.cv:
            self.exhausted = True
            self.stopped = self.stopped or self._finished()
            self.cv.notify_all()


class AlreadyRunning(RuntimeError):
    """Another `jobagent apply` / `daily` holds the browser profiles."""


@contextmanager
def _apply_lock():
    f = (config.DATA_DIR / "apply.lock").open("w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise AlreadyRunning("另一个投递任务正在运行（两个任务不能同时用浏览器资料夹），这次没有投递") from None
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def run_apply(db: DB, mode: str, target: int | None = None, ids: list[str] | None = None,
              headless: bool | None = None, allow_accounts: bool = False, workers: int | None = None,
              llm: LLM | None = None, screening: threading.Thread | None = None) -> list[tuple[object, Outcome]]:
    """Apply until `target` jobs are submitted (or filled, in dry-run), with parallel browser workers.
    Failures are skipped and land on the manual-apply list; the queue is topped up by screening more jobs.
    `screening` is a background screen of older jobs (pipeline.BacklogScreen): while it runs, the queue is topped up
    with what it has passed so far instead of screening here."""
    with _apply_lock():
        return _run_apply(db, mode, target, ids, headless, allow_accounts, workers, llm, screening)


def _run_apply(db: DB, mode: str, target: int | None, ids: list[str] | None, headless: bool | None,
               allow_accounts: bool, workers: int | None, llm: LLM | None = None,
               screening: threading.Thread | None = None) -> list[tuple[object, Outcome]]:
    cfg = config.settings()["applying"]
    target = target or (len(ids) if ids else cfg.get("batch_size", 20))
    workers = max(1, min(workers or cfg.get("workers", 4), target))
    headless = cfg.get("headless", True) if headless is None else headless
    unsupported = set(cfg.get("unsupported") or [])
    held = [forms.norm(c) for c in cfg.get("hold_companies") or []]
    profile = config.profile()
    llm = llm or LLM(rps=config.settings()["screening"].get("llm_rps", 1.0))
    mailbox = Mailbox.from_env(profile["contact"]["email"])
    credentials = read_credentials(config.ROOT / "credentials.txt")
    shots_dir = config.DATA_DIR / "screenshots" / datetime.now().strftime("%Y-%m-%d")
    shots_dir.mkdir(parents=True, exist_ok=True)

    results: list[tuple[object, Outcome]] = []
    seen: set[str] = set()
    if not ids:
        try:
            pipeline.score_fit(db, llm)  # the queue is ordered by fit, so rate jobs screened before scores existed
        except Exception as e:  # unrated jobs just go after rated ones
            log.warning("打匹配分失败：%s", e)

    def intake(rows: list) -> list:
        """Unsupported systems go straight to the manual list; the rest are returned for the workers."""
        keep = []
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            if r["ats"] in unsupported and not ids:
                out = Outcome("blocked", f"{r['ats']} 需要账号登录、邮箱验证码或不允许自动化访问，请手动投")
                _record(db, r, out, mode)
                results.append((r, out))
            elif not ids and any(forms.norm(r["company"]).startswith(h) for h in held):
                out = Outcome("blocked", "你在 agent 之外已经投过这家公司（settings.yaml 的 hold_companies），"
                                         "为避免重复投递没有自动投，请确认后再决定")
                _record(db, r, out, mode)
                results.append((r, out))
            else:
                keep.append(r)
        return pipeline.queue_order(db, keep)

    dispatcher = _Dispatcher(intake(candidates(db, ids)), target, cfg.get("max_attempts_per_batch", target * 4), workers)
    if ids:
        dispatcher.no_more()
    out_q: queue.Queue = queue.Queue()

    def worker(i: int) -> None:
        ctx = ApplyContext(llm=llm, profile=profile, mode=mode, mailbox=mailbox, credentials=credentials,
                           shots_dir=shots_dir, headed=not headless,
                           accounts_in_dry_run=allow_accounts or bool(cfg.get("accounts_in_dry_run", False)))
        profile_dir = config.DATA_DIR / ("browser-profile" if i == 0 else f"browser-profile-{i}")
        try:
            with BrowserSession(profile_dir, headless=headless) as session:
                while (row := dispatcher.take()) is not None:
                    log.info("→ [w%d] %s — %s（%s，%s 简历）", i + 1, row["company"], row["title"], row["ats"], row["resume"])
                    out = apply_job(session, row, ctx)
                    requeue = out.status == "retry" and dispatcher.may_retry(row)
                    dispatcher.done(row, out.status in ("submitted", "dry_run"), requeue=requeue)
                    out_q.put((row, out, requeue))
        except Exception:
            log.exception("worker %d 崩溃", i + 1)

    threads = [threading.Thread(target=worker, args=(i,), name=f"apply-{i + 1}", daemon=True) for i in range(workers)]
    for t in threads:
        t.start()

    def drain(block: bool) -> bool:
        try:
            row, out, requeued = out_q.get(timeout=2 if block else 0)
        except queue.Empty:
            return False
        if out.status == "retry" and not requeued:
            out = Outcome("failed", out.reason, out.answers, out.screenshots, out.url)  # retried enough
        _record(db, row, out, mode)
        log.info("   %s %s — %s：%s", LABEL[out.status], row["company"], row["title"][:50], out.reason)
        if not requeued:
            results.append((row, out))
        return True

    refills, polled = 0, 0.0
    while any(t.is_alive() for t in threads):
        if drain(block=True):
            continue
        if dispatcher.wants_more():
            if screening is not None and screening.is_alive():
                if time.monotonic() - polled >= SCREEN_POLL_SECONDS:  # take what the background screen has passed
                    polled = time.monotonic()
                    if more := intake(candidates(db, None)):
                        log.info("后台筛选又通过 %d 个岗位，加入队列", len(more))
                        dispatcher.add(more)
            elif refills < cfg.get("max_refills", 6):
                refills += 1
                log.info("待投岗位快用完了，再筛选一批新岗位……")
                pipeline.screen_pending(db, llm=llm)
                more = intake(candidates(db, None))
                dispatcher.add(more) if more else dispatcher.no_more()
            else:
                dispatcher.no_more()
    while drain(block=False):
        pass
    if llm.usage:
        log.info("LLM 调用：%s", "，".join(f"{m} {n} 次" for m, n in llm.usage.most_common()))
    return results


def write_review(results: list, mode: str) -> Path:
    """Append this run to reviews/YYYY-MM-DD.md: summary table plus every answer given, with screenshots."""
    day = datetime.now().strftime("%Y-%m-%d")
    out_dir = config.ROOT / "reviews"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{day}.md"
    rel = lambda p: os.path.relpath(p, out_dir)  # noqa: E731
    cell = lambda s: str(s or "").replace("|", "\\|").replace("\n", " ")  # noqa: E731
    mode_label = "真实投递" if mode == "submit" else "试运行（不提交）"

    lines = [f"## {datetime.now():%H:%M} · {mode_label} · {len(results)} 个岗位", "",
             "| # | 结果 | 公司 | 岗位 | 系统 | 说明 |", "|---|---|---|---|---|---|"]
    for i, (row, o) in enumerate(results, 1):
        lines.append(f"| {i} | {LABEL[o.status]} | {cell(row['company'])} | [{cell(row['title'])}]({row['url']}) "
                     f"| {row['ats']} | {cell(o.reason)} |")
    for i, (row, o) in enumerate(results, 1):
        if not o.answers and not o.screenshots:
            continue
        lines += ["", f"### {i}. {row['company']} — {row['title']}", "",
                  f"{LABEL[o.status]} · {o.reason} · 简历 {row['resume']} · ID `{row['id']}`", ""]
        if o.screenshots:
            lines.append("截图：" + " · ".join(f"[{Path(p).stem.split('-', 1)[-1]}]({rel(p)})" for p in o.screenshots))
            lines.append("")
        if o.answers:
            lines += ["| 题目 | 填写 | 依据 | 问题 |", "|---|---|---|---|"]
            lines += [f"| {cell(a['label'][:90])} | {cell(a['value'][:120])} | {a['basis']} | {cell(a.get('error') or '')} |"
                      for a in o.answers]
    header = "" if path.exists() else f"# 投递记录 {day}\n\n"
    sep = "\n---\n\n" if path.exists() else ""
    with path.open("a", encoding="utf-8") as f:
        f.write(header + sep + "\n".join(lines) + "\n")
    return path
