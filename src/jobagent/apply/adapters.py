"""Per-ATS knowledge: where the form starts, which buttons advance it, what success looks like, how to sign in."""
from __future__ import annotations

import html as htmllib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from urllib.parse import parse_qsl, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..rules import normalize_url
from .forms import Snapshot

log = logging.getLogger("jobagent")


class Skip(Exception):
    """Rules say don't apply (closed, essay required, cover letter required)."""


class Blocked(Exception):
    """Needs a human: CAPTCHA, unsupported login, unanswerable required question."""


class Failed(Exception):
    """Technical failure; retried on a later run."""


class Unconfirmed(Exception):
    """Submit was clicked but no confirmation was seen. Never retried automatically."""


_SUCCESS = re.compile(
    r"(?i)thank(s| you)[^.]{0,60}(appl|submi|interest)|application (has been |was )?(successfully )?(submitted|received)|"
    r"we('ve| have) received your application|successfully submitted|submission (was )?successful|application submitted"
)
# a status line, not job-description advice such as "If you have already applied for this position ... you may reapply"
_APPLIED = re.compile(r"(?i)(?<!if )(?<!when )(?<!once )you applied for this (?:job|position) on ([A-Za-z]+ \d{1,2}, \d{4})|"
                      r"(?<!if )(?<!when )(?<!once )you(?:'ve| have) already applied (?:for|to) this (?:job|position)")
_CLOSED = re.compile(
    r"(?i)no longer (accepting applications|available|open|active)|position (has been )?(filled|closed)|"
    r"job (posting )?(is )?(no longer|not) (available|found|active)|this (job|position|posting) (has )?(expired|closed)|"
    r"the page you (are looking for|requested) (doesn.t|does not|could not)|job not found"
)
_COOKIES = re.compile(r"(?i)^(accept( all)?( cookies)?|allow all( cookies)?|i accept|agree|got it|ok)$")


def click_button(page: Page, btn: dict) -> None:
    page.locator(f'[data-ja-id="{btn["id"]}"]').first.click(timeout=10000)


def settle(page: Page, ms: int = 2500) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
    except PlaywrightError:
        pass
    page.wait_for_timeout(ms)


def dismiss_cookies(page: Page, snap: Snapshot) -> None:
    for b in snap.buttons:
        if _COOKIES.match(b["text"]):
            try:
                click_button(page, b)
                page.wait_for_timeout(500)
            except PlaywrightError:
                pass
            return


def already_applied_reason(when: str) -> str:
    return f"已经申请过这个岗位{f'（{when}）' if when else ''}：招聘网站显示已申请，不用再投"


class Adapter:
    name = "generic"
    root: str | None = None
    NEXT = re.compile(r"(?i)^(next|continue|save (and|&) continue|next step|proceed)$")
    SUBMIT = re.compile(r"(?i)^(submit( (my |your )?application)?|send( my)? application|finish|complete application)$")
    APPLY = re.compile(r"(?i)^(apply|apply now|apply for this (job|position|role)|i'?m interested|start application)$")

    def start_url(self, row) -> str:
        return row["url"]

    def open(self, page: Page, row, ctx, snapshot) -> None:
        page.goto(self.start_url(row), wait_until="domcontentloaded", timeout=45000)
        settle(page)
        snap = snapshot(page, None)
        dismiss_cookies(page, snap)
        if self.is_closed(snap):
            raise Skip("岗位已关闭或不再接受申请")
        if not [f for f in snap.fields if f["kind"] != "file"]:
            apply_btn = next((b for b in snap.buttons if self.APPLY.match(b["text"])), None)
            if apply_btn:
                try:
                    with page.context.expect_page(timeout=4000) as popup:
                        click_button(page, apply_btn)
                    new = popup.value
                    settle(new, 1000)
                    url = new.url
                    new.close()
                    page.goto(url, wait_until="domcontentloaded")
                except PlaywrightError:
                    pass  # no new tab: the click opened the form in place (or did nothing)
                settle(page)

    def relevant(self, f: dict) -> bool:
        return True

    def _entries(self, page: Page) -> dict[str, int]:
        """Entries per section: the highest "Work Experience N" / "Education N" among field labels (the first entry
        of a section may have no Delete button, so buttons alone undercount). Only sections present on the page."""
        from .forms import snapshot
        sections = page.evaluate(_ENTRIES_JS, None)  # also tags each section's Add button
        out = {k: 0 for k in sections}
        for f in snapshot(page, self.root).fields:
            m = re.match(r"(?i)^(work experience|education) (\d+) —", f.get("label") or "")
            if m:
                k = "work" if m.group(1).lower().startswith("work") else "edu"
                if k in out:
                    out[k] = max(out[k], int(m.group(2)))
        page.evaluate(_ENTRIES_JS, None)  # re-tag: the snapshot doesn't touch buttons, but keep tags fresh
        return out

    def prepare(self, page: Page, ctx) -> bool:
        """Get the page ready before its fields are read (e.g. add missing entries). True if the page changed."""
        return False

    def check(self, page: Page, snap: Snapshot, ctx) -> list[str]:
        """Problems that must be fixed before leaving this page (e.g. missing work-experience entries)."""
        return []

    def check_before_submit(self, page: Page, ctx) -> list[str]:
        """Last look at the final page before Submit; any problem stops the submission."""
        return []

    def back_button(self, snap: Snapshot) -> dict | None:
        """The button to the previous step of a multi-page form (single-page forms have none)."""
        return None

    def handle_auth(self, page: Page, snap: Snapshot, ctx) -> bool:
        if page.locator('input[type="password"]:visible').count():
            raise Blocked("需要登录/注册账号，这个网站暂不支持自动登录，请手动投")
        return False

    def next_button(self, snap: Snapshot) -> dict | None:
        return next((b for b in snap.buttons if self.NEXT.match(b["text"])), None)

    def submit_button(self, snap: Snapshot) -> dict | None:
        return next((b for b in reversed(snap.buttons) if self.SUBMIT.match(b["text"])), None)

    def is_success(self, snap: Snapshot) -> bool:
        return bool(_SUCCESS.search(snap.text) or re.search(r"(?i)/(confirmation|thanks|thank-you|success)\b", snap.url))

    def is_closed(self, snap: Snapshot) -> bool:
        return bool(_CLOSED.search(snap.text[:3000]))

    def already_applied(self, snap: Snapshot) -> str | None:
        """'You've already applied for this job' / 'You applied for this job on October 27, 2026' → the date, or ''."""
        m = _APPLIED.search(snap.text[:3000])
        return None if m is None else (m.group(1) or "")

    def email_code_needed(self, page: Page, snap: Snapshot) -> bool:
        return False

    def recover(self, page: Page, snap: Snapshot) -> bool:
        """Handle a transient error page (e.g. "Something went wrong, refresh the page"). True if it reloaded."""
        if re.search(r"(?i)something went wrong", snap.text) and re.search(r"(?i)refresh the page|try again", snap.text):
            page.reload(wait_until="domcontentloaded")
            settle(page, 4000)
            return True
        return False

    def email_code(self, ctx, since: datetime) -> str | None:
        return None


# Greenhouse blocks: count school--N / company…--N ids; with a kind, click that block's "Add another" button
# (the one inside the smallest container holding the block's first field).
_GH_BLOCKS_JS = """(add) => {
  const ids = Array.from(document.querySelectorAll('[id]')).map((e) => e.id);
  // employment ids are "company-name--0" on some boards and "company-name-0" on others
  const count = (re) => new Set(ids.filter((i) => re.test(i)).map((i) => i.match(/(\\d+)$/)[1])).size;
  const out = {edu: count(/^school--\\d+$/), work: count(/^(company|employer)[\\w-]*?-{1,2}\\d+$/)};
  if (!add) return out;
  const first = document.getElementById(add === 'edu' ? 'school--0' : ids.find((i) => /^(company|employer)[\\w-]*?-{1,2}0$/.test(i)));
  for (let a = first && first.parentElement; a; a = a.parentElement) {
    const btn = Array.from(a.querySelectorAll('button, a')).find((b) => /^\\+?\\s*add another/i.test((b.innerText || '').trim()));
    if (btn) { btn.click(); return true; }
  }
  return false;
}"""


class Greenhouse(Adapter):
    name = "greenhouse"
    root = "#application-form, form#application_form, form"
    SUBMIT = re.compile(r"(?i)^submit( application)?$")

    def start_url(self, row) -> str:
        # The embed form never redirects; job-boards.greenhouse.io/{board}/jobs/{id} redirects to the company's
        # own careers site when the company has set one up, and that page hides the form in an iframe.
        url = row["url"]
        m = re.search(r"greenhouse\.io/([\w-]+)/jobs/(\d+)", url)
        if m and m.group(1) != "embed":
            board, jid = m.group(1), m.group(2)
        else:
            qs = dict(parse_qsl(urlsplit(url).query))
            jid = qs.get("gh_jid") or qs.get("token")
            via = json.loads(row["screen_json"] or "{}").get("jd_via", "")
            board = qs.get("for") or (via.split(":", 1)[1] if via.startswith("greenhouse:") else None)
        return f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={jid}" if board and jid else url

    def open(self, page: Page, row, ctx, snapshot) -> None:
        page.goto(self.start_url(row), wait_until="domcontentloaded", timeout=45000)
        settle(page)
        if "greenhouse.io" not in urlsplit(page.url).netloc:
            # company careers page with the Greenhouse form in an iframe: open the iframe itself
            for _ in range(10):
                frame = next((f for f in page.frames if "greenhouse.io/embed/job_app" in f.url), None)
                if frame:
                    page.goto(frame.url, wait_until="domcontentloaded", timeout=45000)
                    settle(page)
                    break
                page.wait_for_timeout(1000)
        snap = snapshot(page, None)
        dismiss_cookies(page, snap)
        if self.is_closed(snap):
            raise Skip("岗位已关闭或不再接受申请")

    def prepare(self, page: Page, ctx) -> bool:
        """Education / employment blocks: "Add another" until every school and job on the resume has one."""
        changed, targets = False, resume_targets(ctx)
        for _ in range(8):
            counts = page.evaluate(_GH_BLOCKS_JS, None)
            short = [k for k, n in targets.items() if counts.get(k) and counts[k] < n]
            if not short or not page.evaluate(_GH_BLOCKS_JS, short[0]):
                break
            page.wait_for_timeout(1000)
            changed = True
        return changed

    def check(self, page: Page, snap: Snapshot, ctx) -> list[str]:
        counts, targets = page.evaluate(_GH_BLOCKS_JS, None), resume_targets(ctx)
        names = {"work": "工作经历", "edu": "教育经历"}
        problems = [f"{names[k]}只有 {counts[k]} 段（简历上有 {n} 段）"
                    for k, n in targets.items() if counts.get(k) and counts[k] < n]
        # a section the block counter can't read (Samsung's "company-name-0" ids once): never submit unchecked
        problems += [f"页面上有{names[k]}栏，但程序认不出它的结构，无法确认填满"
                     for k in sections_on_page(snap) if targets.get(k) and not counts.get(k)]
        return problems + entry_problems(snap, ctx)

    def email_code_needed(self, page: Page, snap: Snapshot) -> bool:
        # the prompt shows up next to the submit button, far below a long job description
        # Playwright evaluates this regex in JavaScript: use re.IGNORECASE, not an inline (?i)
        return page.get_by_text(re.compile(r"security code|verification code", re.IGNORECASE)).count() > 0

    def email_code(self, ctx, since: datetime) -> str | None:
        if ctx.mailbox is None:
            raise Blocked("Greenhouse 要求输入邮箱验证码：请在 .env 配置 GMAIL_APP_PASSWORD（见 README）")
        # "Copy and paste this code into the security code field on your application: XXXXXXXX"
        company = ctx.row["company"] if ctx.row is not None else None
        return ctx.mailbox.wait_for("greenhouse", re.compile(r"(?i)(?:application|code)\s*:\s*([A-Za-z0-9]{8})\b"), since,
                                    about=company)


class Lever(Adapter):
    name = "lever"
    root = "form"
    SUBMIT = re.compile(r"(?i)^submit( application)?$")

    def start_url(self, row) -> str:
        return normalize_url(row["url"]) + "/apply"


class Ashby(Adapter):
    name = "ashby"
    root = "form, main"
    SUBMIT = re.compile(r"(?i)^submit( application)?$")

    def start_url(self, row) -> str:
        return normalize_url(row["url"]) + "/application"

    def relevant(self, f: dict) -> bool:
        # the "autofill from resume" drop zone at the top is not a form question
        return not (f["kind"] == "file" and not re.search(r"(?i)resume|cv|cover|transcript", f["label"]))


# Workday "My Experience": count "Work Experience N" / "Education N" entries per section and tag each section's
# Add / Add Another button with data-ja-add=work|edu. Walks the page in document order between section headings.
_ENTRIES_JS = """(deleteLast) => {
  const txt = (e) => (e.innerText || '').replace(/\\s+/g, ' ').trim();
  // RBC: "Your Experience", Marvell: "Professional Experience" (never the page title "My Experience")
  const SECTION = {work: /^(your |professional )?(work )?experience$|^(your )?(employment|work) history$/i,
                   edu: /^(your )?education$/i};
  const out = {}, dels = {}; let cur = null, curTag = null;
  document.querySelectorAll('[data-ja-add]').forEach((e) => e.removeAttribute('data-ja-add'));
  for (const el of document.querySelectorAll('h1, h2, h3, h4, h5, [role="heading"], button')) {
    const t = txt(el);
    if (el.tagName !== 'BUTTON') {
      const key = Object.keys(SECTION).find((k) => SECTION[k].test(t));
      if (key) { cur = key; curTag = el.tagName; out[key] = out[key] || 0; dels[key] = dels[key] || []; continue; }
      if (cur && el.tagName === curTag) cur = null;   // the next section started
      continue;
    }
    if (!cur) continue;
    if (/^delete$/i.test(t)) { out[cur] += 1; dels[cur].push(el); }   // every entry has its own Delete button
    else if (/^add( another)?$/i.test(t) && !el.closest('[data-ja-add]')) el.setAttribute('data-ja-add', cur);
  }
  if (deleteLast) {
    const list = dels[deleteLast] || [];
    if (!list.length) return false;
    list[list.length - 1].click();
    return true;
  }
  return out;
}"""

# Click the delete button next to every uploaded resume file (a name starting with `prefix`, the part all the
# applicant's resume file names share) that isn't the current one.
_REMOVE_OLD_RESUMES_JS = """([current, prefix]) => {
  const txt = (e) => (e.innerText || '').trim();
  let n = 0;
  for (const el of document.querySelectorAll('div, span, a, p')) {
    const t = txt(el);
    if (el.children.length || !t.toLowerCase().startsWith(prefix.toLowerCase()) || !/\\.(pdf|docx?)$/i.test(t)
        || t === current) continue;
    let box = el;
    for (let d = 0; d < 6 && box; d++, box = box.parentElement) {
      const del = Array.from(box.querySelectorAll('button')).find((b) =>
        /delete|remove|trash/i.test((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-automation-id') || '') + ' ' + txt(b)));
      if (del) { del.click(); n += 1; break; }
    }
  }
  return n;
}"""


def resume_targets(ctx) -> dict[str, int]:
    """How many jobs / schools the resume lists (from config/resumes/<KIND>.yaml)."""
    from ..resume import structured
    data = structured(ctx.row["resume"]) or {}
    return {"work": len(data.get("experience") or []), "edu": len(data.get("education") or [])}


def entry_problems(snap: Snapshot, ctx) -> list[str]:
    """Experience / education entries on any site: every resume school and job present (sites may re-sort them),
    and no entry with an empty school, degree, job title, company or start date."""
    from ..resume import structured
    from .forms import best_option
    data = structured(ctx.row["resume"]) or {}
    bare = lambda v: re.sub(r"(?i)^the\s+", "", v or "").strip()  # noqa: E731
    problems, on_page = [], {"edu": [], "work": []}
    for f in snap.fields:
        label = f.get("label") or ""
        if re.match(r"(?i)^education \d+ — (school or university|school|university)\b", label):
            on_page["edu"].append(bare(f.get("value")))
        elif re.match(r"(?i)^work experience \d+ — (company|company name|employer)\b", label):
            on_page["work"].append(bare(f.get("value")))
    others = sum(norm_other(v) == "other" for v in on_page["edu"])
    for school in (e.get("school", "") for e in data.get("education") or []):
        if on_page["edu"] and not best_option(bare(school), [v for v in on_page["edu"] if v], fuzzy=False):
            if others:
                others -= 1  # "Other": the school isn't in this employer's list
            else:
                problems.append(f"教育经历里没有 {school}（页面上是：{'、'.join(v or '空' for v in on_page['edu'])}）")
    for job in (e.get("company", "") for e in data.get("experience") or []):
        first = re.split(r"\s+[—-]\s+", job)[0]
        if on_page["work"] and not any(best_option(first, [v], fuzzy=False) or best_option(job, [v], fuzzy=False)
                                       for v in on_page["work"] if v):
            problems.append(f"工作经历里没有 {job}（页面上是：{'、'.join(v or '空' for v in on_page['work'])}）")
    key = re.compile(r"(?i)^(work experience \d+|education \d+) — (job title|title|company|company name|from|"
                     r"start date year|school or university|school|degree)\b")
    empty = [f["label"] for f in snap.fields if key.match(f.get("label") or "") and not f.get("value")]
    if empty:
        problems.append("这些经历字段是空的：" + "；".join(empty[:6]))
    return problems


_SECTION_LABELS = {
    "work": (re.compile(r"(?i)^(company( name)?|employer( name)?)$"), re.compile(r"(?i)^(job )?title$")),
    "edu": (re.compile(r"(?i)^(school( name)?|university|school or university)$"),),
}


def sections_on_page(snap: Snapshot) -> set[str]:
    """Experience / education sections the form shows, judged from field labels alone ("Company name" + "Title")."""
    bases = [re.sub(r"^.*—\s*", "", f.get("label") or "").strip() for f in snap.fields]
    return {k for k, pats in _SECTION_LABELS.items() if all(any(p.match(b) for b in bases) for p in pats)}


def norm_other(v: str) -> str:
    return re.sub(r"[^a-z]+", "", v.lower())


class Workday(Adapter):
    """Multi-step Workday flow: Apply → (sign in / create account) → steps with Save and Continue → Submit."""

    name = "workday"
    root = '[data-automation-id="applyFlowPage"], main, body'
    SUBMIT = re.compile(r"(?i)^submit$")

    def open(self, page: Page, row, ctx, snapshot) -> None:
        page.goto(row["url"], wait_until="domcontentloaded", timeout=45000)
        settle(page, 3500)
        snap = snapshot(page, None)
        dismiss_cookies(page, snap)
        if self.is_closed(snap):
            raise Skip("岗位已关闭或不再接受申请")
        for _ in range(10):  # the job page renders grey placeholders first (Palo Alto Networks: >4 s)
            apply = page.locator('[data-automation-id="adventureButton"]').first
            if not apply.count():
                # an earlier attempt left a draft: the job page then offers "Continue Application" instead of Apply
                apply = page.locator('a:visible, button:visible').filter(
                    has_text=re.compile(r"^\s*continue application\s*$", re.IGNORECASE)).first  # no inline (?i): Playwright passes it to JS
            if apply.count() or self.already_applied(snapshot(page, None)) is not None:
                break
            page.wait_for_timeout(2000)
        if not apply.count():
            if (when := self.already_applied(snapshot(page, None))) is not None:
                raise Skip(already_applied_reason(when))
            raise Failed("Workday 页面上找不到 Apply 按钮")
        apply.click()
        settle(page, 2500)
        for auto in ("autofillWithResume", "applyManually"):  # let Workday parse the resume, then correct it
            btn = page.locator(f'[data-automation-id="{auto}"]').first
            if btn.count() and btn.is_visible():
                btn.click()
                settle(page, 2500)
                break

    def _targets(self, ctx) -> dict[str, int]:
        return resume_targets(ctx)

    def prepare(self, page: Page, ctx) -> bool:
        """My Experience: add Work Experience / Education entries until the resume's count, drop old resume files."""
        changed = self._remove_old_resumes(page, ctx)
        targets = self._targets(ctx)
        for _ in range(8):  # resume parsing can invent entries (e.g. a project read as a job): delete the extras
            entries = self._entries(page)
            extra = [k for k, n in targets.items() if n and entries.get(k, 0) > n]
            if not extra:
                break
            if not page.evaluate(_ENTRIES_JS, extra[0]):
                break
            page.wait_for_timeout(1500)
            changed = True
        for _ in range(8):
            entries = self._entries(page)
            short = [k for k, n in targets.items() if k in entries and entries[k] < n]
            if not short:
                break
            try:
                page.locator(f'[data-ja-add="{short[0]}"]').first.click(timeout=5000)
            except PlaywrightError:
                break
            page.wait_for_timeout(1500)
            changed = True
        return changed

    def check(self, page: Page, snap: Snapshot, ctx) -> list[str]:
        problems = []
        entries, targets = self._entries(page), self._targets(ctx)
        names = {"work": "工作经历", "edu": "教育经历"}
        for k, n in targets.items():
            if k in entries and entries[k] < n:
                problems.append(f"{names[k]}只有 {entries[k]} 段（简历上有 {n} 段）")
        return problems + entry_problems(snap, ctx)

    def check_before_submit(self, page: Page, ctx) -> list[str]:
        """Review page: every resume job and school must appear (when the form has those sections at all)."""
        text = page.locator("body").inner_text()
        targets, problems = self._targets(ctx), []
        work = r"(?:your |professional )?(?:work )?experience"  # RBC "Your Experience", Marvell "Professional Experience"
        for key, section, name in (("work", work, "Work Experience"), ("edu", r"(?:your )?education", "Education")):
            n = targets.get(key, 0)
            if not n or not re.search(rf"(?im)^\s*{section}\s*$", text):
                continue  # this form has no such section
            got = len(set(re.findall(rf"(?im)^\s*{section} (\d+)\s*$", text)))
            if got < n:
                problems.append(f"Review 页上{name}只有 {got} 段（简历上有 {n} 段）")
        return problems

    def back_button(self, snap: Snapshot) -> dict | None:
        return next((b for b in snap.buttons if re.match(r"(?i)^(back|previous)$", b["text"])), None)

    def _remove_old_resumes(self, page: Page, ctx) -> bool:
        """Drafts keep earlier uploads; leave only the current resume file attached."""
        from ..resume import source
        stems = [PurePath(str(v)).stem for r in ctx.profile["resumes"].values() for v in (r or {}).values() if v]
        prefix = os.path.commonprefix(stems)
        if len(prefix) < 4:
            return False  # nothing tells the applicant's own files apart from others: leave them
        try:
            current = source(ctx.row["resume"] if ctx.row["resume"] in ("AIE", "SDE") else "SDE").name
            n = page.evaluate(_REMOVE_OLD_RESUMES_JS, [current, prefix])
        except PlaywrightError:
            return False
        if n:
            log.info("   删掉了草稿里 %d 份旧简历", n)
            page.wait_for_timeout(1500)
        return bool(n)

    def relevant(self, f: dict) -> bool:
        # page-header widgets (site search, language and settings menus) aren't application questions
        return not re.search(r"(?i)^search|skip to main content", f.get("label", ""))

    def _auth_page(self, page: Page) -> bool:
        return page.locator('input[type="password"]:visible, [data-automation-id="password"]:visible, '
                            '[data-automation-id="signInSubmitButton"]:visible, '
                            '[data-automation-id="createAccountSubmitButton"]:visible, '
                            'button:has-text("Sign in with email"):visible').count() > 0

    _NEEDS_VERIFY = re.compile(r"(?i)verify your (email|account)|verification (email|link)|activate your account|"
                               r"check your email|confirm your email|(is|has) not (yet )?(been )?(verified|activated)|"
                               r"resend (account )?verification")
    _EXISTS = re.compile(r"(?i)already (exists|in use|registered|associated)|account with this email|already have an account")

    def _visible(self, page: Page, auto: str):
        loc = page.locator(f'[data-automation-id="{auto}"]:visible').first
        return loc if loc.count() else None

    def handle_auth(self, page: Page, snap: Snapshot, ctx) -> bool:
        """Sign in; if there's no account, create one; whenever Workday asks to verify the email address,
        open the activation link from that company's email, then sign in again and carry on with the application."""
        if not self._auth_page(page):
            return False
        if ctx.mode != "submit" and not ctx.accounts_in_dry_run:
            raise Blocked("试运行停在 Workday 登录/注册页（试运行默认不登录、不注册账号）")
        if ctx.credentials is None:
            raise Blocked("Workday 需要账号：credentials.txt 里没有找到 email / password")
        email_addr, password = ctx.credentials
        ctx.workday_auth_attempts += 1
        if ctx.workday_auth_attempts > 6:
            raise Blocked("Workday 登录/注册多次没有成功（密码可能不符合该公司要求），请手动投")

        def fill(auto, value):
            if (loc := self._visible(page, auto)):
                loc.fill(value)

        def press(auto):
            page.locator(f'[data-automation-id="{auto}"]').first.click(force=True)
            settle(page, 3500)

        def body() -> str:
            return page.locator("body").inner_text()[:4000]

        if (btn := page.locator('button:has-text("Sign in with email"):visible').first).count():
            btn.click()  # newer tenants show Apple / Google / email choices first
            settle(page, 1500)

        # 1) Sign in first: the account may already exist on this company's Workday.
        if not ctx.workday_signin_tried and self._visible(page, "createAccountSubmitButton") and self._visible(page, "signInLink"):
            self._visible(page, "signInLink").click()
            settle(page, 1500)
        if self._visible(page, "signInSubmitButton"):
            ctx.workday_signin_tried = True
            fill("email", email_addr)
            fill("password", password)
            press("signInSubmitButton")
            if not self._auth_page(page):
                return True  # signed in
            if self._NEEDS_VERIFY.search(body()):
                self._verify_via_email(page, ctx, fresh=False)  # account made earlier, never activated
                return True
            if ctx.workday_account_created:
                return True  # just created and verified: let the next pass sign in again
            if (create := self._visible(page, "createAccountLink")):
                create.click()  # no account here yet
                settle(page, 1500)

        # 2) Create the account; Workday usually emails an activation link.
        if self._visible(page, "createAccountSubmitButton"):
            since = datetime.now(timezone.utc)
            fill("email", email_addr)
            fill("password", password)
            fill("verifyPassword", password)
            box = page.locator('[data-automation-id="createAccountCheckbox"]').first
            if box.count() and not box.is_checked():
                box.check(force=True)
            press("createAccountSubmitButton")
            ctx.workday_account_created = True
            for _ in range(8):  # some tenants sign the new account straight in, a few seconds later
                if not self._auth_page(page):
                    return True
                text = body()
                if self._EXISTS.search(text) or self._NEEDS_VERIFY.search(text):
                    break
                page.wait_for_timeout(2000)
            if self._EXISTS.search(text):
                if (link := self._visible(page, "signInLink")):
                    link.click()
                    settle(page, 1500)
                return True
            self._verify_via_email(page, ctx, fresh=True, since=since)
        return True

    def _verify_via_email(self, page: Page, ctx, fresh: bool, since: datetime | None = None) -> None:
        """Open the /activate/ link Workday emailed for this site, then come back to the sign-in page."""
        if ctx.mailbox is None:
            raise Blocked("Workday 账号需要邮箱验证：请在 .env 配置 GMAIL_APP_PASSWORD（见 README）")
        host = urlsplit(page.url).netloc
        tenant = host.split(".")[0]
        # Activation links are valid for 24 h, so an account created on an earlier run can use an older email.
        day_ago = datetime.now(timezone.utc) - timedelta(hours=23)
        pattern = re.compile(rf"(https://{re.escape(host)}/[^\s\"'<>]*?/activate/[^\s\"'<>]+)")
        log.info("   Workday（%s）需要验证邮箱，等待验证邮件……", tenant)
        sender = f"{tenant}@otp.workday.com"
        link = None
        if fresh and since:  # a new account: wait for its own email first
            link = ctx.mailbox.wait_for(sender, pattern, since, timeout=150)
        # "already registered but never activated" also lands here: use the newest activation email from today
        link = link or ctx.mailbox.wait_for(sender, pattern, day_ago, timeout=60 if fresh else 180) or \
            ctx.mailbox.wait_for("workday.com", pattern, day_ago, timeout=20)
        if not link:
            raise Blocked(f"Workday（{tenant}）的账号验证邮件几分钟内都没收到，请手动验证后再投")
        page.goto(htmllib.unescape(link), wait_until="domcontentloaded", timeout=45000)
        settle(page, 4000)
        ctx.workday_verified = True
        # The link redirects back to the application; Workday then asks to sign in with the now-active account.
        if (sign_in := self._visible(page, "signInLink")):
            sign_in.click()
            settle(page, 1500)

    def next_button(self, snap: Snapshot) -> dict | None:
        return next((b for b in snap.buttons if b.get("auto") == "pageFooterNextButton" and not self.SUBMIT.match(b["text"])), None) \
            or super().next_button(snap)

    def submit_button(self, snap: Snapshot) -> dict | None:
        return next((b for b in snap.buttons if b.get("auto") == "pageFooterNextButton" and self.SUBMIT.match(b["text"])), None)

    def is_success(self, snap: Snapshot) -> bool:
        return bool(re.search(r"(?i)application submitted|successfully submitted|thank(s| you) for applying", snap.text))


ADAPTERS = {a.name: a for a in (Greenhouse(), Lever(), Ashby(), Workday())}


def adapter_for(ats: str) -> Adapter:
    return ADAPTERS.get(ats) or Adapter()
