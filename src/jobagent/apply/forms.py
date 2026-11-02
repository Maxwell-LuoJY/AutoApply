"""Form snapshots (extract.js), dropdown option discovery, and filling single fields."""
from __future__ import annotations

import difflib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

JS = Path(__file__).with_name("extract.js").read_text(encoding="utf-8")

TEXT_KINDS = {"text", "email", "tel", "url", "number", "date", "month", "textarea"}
CHOICE_KINDS = {"select", "radio", "buttons", "combobox", "checkbox"}
_NO_OPTIONS = re.compile(r"(?i)^(no options|no results|no matches|no items|loading|searching|type to search|start typing)")

# Marks the currently open dropdown's options with data-ja-opt=<index> and returns their texts.
# Selected "pills" of other Workday widgets also use promptOption, so anything inside a selected-items list is skipped.
_OPTIONS_JS = """() => {
  const sel = '[role="option"], [id*="-option-"], [data-automation-id="promptOption"]';
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
                       return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  document.querySelectorAll('[data-ja-opt]').forEach((e) => e.removeAttribute('data-ja-opt'));
  const picked = [];
  for (const e of document.querySelectorAll(sel)) {
    if (!vis(e) || e.closest('[data-automation-id="selectedItemList"]')) continue;
    if (picked.some((p) => p.contains(e) || e.contains(p))) continue;
    const t = (e.innerText || '').replace(/\\s+/g, ' ').trim();
    if (!t) continue;
    e.setAttribute('data-ja-opt', String(picked.length));
    picked.push(e);
  }
  return picked.map((e) => (e.innerText || '').replace(/\\s+/g, ' ').trim());
}"""


# Scroll the open dropdown's list one screen further (Workday renders only the options in view). True if it moved.
_SCROLL_OPTIONS_JS = """() => { const o = document.querySelector('[data-ja-opt]'); if (!o) return false;
  for (let a = o.parentElement; a; a = a.parentElement) {
    const s = getComputedStyle(a);
    if (/(auto|scroll)/.test(s.overflowY) && a.scrollHeight > a.clientHeight + 2) {
      const before = a.scrollTop; a.scrollTop = before + Math.max(40, a.clientHeight - 30); return a.scrollTop > before; } }
  return false; }"""


@dataclass
class Snapshot:
    fields: list[dict]
    buttons: list[dict]
    errors: list[str]
    captcha: bool
    url: str
    title: str
    text: str
    frames: list[str]           # visible iframes ("host WxH"), for diagnosing stuck submits


def snapshot(page: Page, root: str | None = None) -> Snapshot:
    for attempt in range(4):
        try:
            snap = Snapshot(**page.evaluate(JS, root))
        except PlaywrightError as e:
            # the page navigated mid-read (e.g. the confirmation page loading right after Submit): read it again
            if attempt == 3 or not re.search(r"(?i)execution context was destroyed|navigat|frame was detached", str(e)):
                raise
            page.wait_for_timeout(1500)
            continue
        seen: dict[tuple, int] = {}
        for f in snap.fields:  # position among same-label fields on this page
            k = (f["kind"], f.get("label") or "")
            f["nth"], seen[k] = seen.get(k, 0), seen.get(k, 0) + 1
        return snap
    raise AssertionError("unreachable")


def norm(s) -> str:
    return re.sub(r"[^a-z0-9+]+", " ", str(s).lower()).strip()


_DEGREE_WORDS = {"bachelor", "bachelors", "master", "masters", "doctor", "doctorate", "phd", "associate", "associates", "mba"}
_NEGATION = {"not", "no", "never", "non"}
_DEGREE_LEVEL = {"bachelors": "bachelor", "masters": "master", "doctorate": "doctor", "phd": "doctor", "associates": "associate"}


def _conflicts(a: str, b: str) -> bool:
    """Near-identical strings that still mean different things: another degree level, or one of them negated."""
    wa, wb = set(a.split()), set(b.split())
    # "bachelor s degree" and "bachelors degree" are the same level: compare levels, not spellings
    la, lb = {_DEGREE_LEVEL.get(x, x) for x in wa & _DEGREE_WORDS}, {_DEGREE_LEVEL.get(x, x) for x in wb & _DEGREE_WORDS}
    if la and lb and not (la & lb):
        return True
    return bool(wa & _NEGATION) != bool(wb & _NEGATION)


def best_option(want, options: list[str], fuzzy: bool = True) -> str | None:
    """Pick the option matching `want`: exact, then word-prefix, then whole-word containment, then fuzzy
    (never across degree levels or negations: "Master of Science" is not "Bachelor of Science")."""
    w = norm(want)
    if not w or not options:
        return None
    pairs = [(o, norm(o)) for o in options]
    if set(w.split()) & _DEGREE_WORDS and not _other_field(w):
        # a degree: never one naming another field, even as a prefix match (J&J: "Master of Science" picked
        # "Master of Science in Medicine or Surgery")
        pairs = [(o, n) for o, n in pairs if not _other_field(n)]
    exact = [o for o, n in pairs if n == w]
    if exact:
        return exact[0]
    prefix = [o for o, n in pairs if n and (n.startswith(w + " ") or w.startswith(n + " "))]
    if prefix:
        return min(prefix, key=len)
    contains = [o for o, n in pairs if n and (f" {w} " in f" {n} " or (len(n) > 4 and f" {n} " in f" {w} "))]
    if contains:
        return min(contains, key=len)
    if not fuzzy:
        return None
    words = set(w.split())
    if len(words) >= 2:  # "Computer Science" → "Computer and Information Science" (J&J's field-of-study list)
        within = [o for o, n in pairs if n and words <= set(n.split()) and not _conflicts(w, n)]
        if within:
            return min(within, key=len)
    close = [c for c in difflib.get_close_matches(w, [n for _, n in pairs], n=3, cutoff=0.75) if not _conflicts(w, c)]
    if close:
        return next(o for o, n in pairs if n == close[0])
    for group in _SYNONYMS:  # same thing, different word ("Mobile" when the list says "Cell": Regeneron)
        if w in group:
            if (hit := next((o for o, n in pairs if n in group and n != w), None)):
                return hit
            # one synonym inside a longer option: "Personal Cell" (Nelnet), but never "Mobile Landline"-style mixes
            words_in = [o for o, n in pairs if any(f" {g} " in f" {n} " for g in group) and not _conflicts(w, n)]
            if words_in:
                return min(words_in, key=len)
    level = next((lvl for lvl, words in _LEVELS.items() if set(w.split()) & words), None)
    if level:  # "Bachelor's degree" when the list only has abbreviations (Wawa: A.A. / B.A. / B.S. / M.S. / PhD)
        # "B.E/B.Tech", and the abbreviation in brackets: "Bachelor of Science (B.S.)" (Regeneron)
        parts = {o: {norm(x).replace(" ", "") for x in re.split(r"[/,()]", o)} for o, _ in pairs}
        for abbr in _ABBREVIATIONS[level]:
            if (hit := next((o for o, n in pairs if abbr in parts[o] and not _other_field(n)), None)):
                return hit
        # a general option of that level: "University/Bachelors Degree or Equivalent" (J&J)
        same = [o for o, n in pairs if set(n.split()) & _LEVELS[level] and not _conflicts(w, n) and not _other_field(n)]
        if same:
            return min(same, key=lambda o: (not re.search(r"\b(degree|science|engineering)\b", norm(o)), len(o)))
    return None


_STRONG_OTHER = re.compile(r"\b(medicine|surgery|health|laws?|nursing|music|education|pharmacy|divinity)\b")
_WEAK_OTHER = re.compile(r"\b(arts?|business|administration|management)\b")


def _other_field(n: str) -> bool:
    """A degree naming another field than CS / AI engineering: "Master of Science in Medicine or Surgery", "Master of
    Business Administration"; "Arts or Science" is a general one."""
    return bool(_STRONG_OTHER.search(n) or (_WEAK_OTHER.search(n) and "science" not in n.split()))


_LEVELS = {"bachelor": {"bachelor", "bachelors", "undergraduate"}, "master": {"master", "masters", "graduate"},
           "phd": {"phd", "doctorate", "doctoral", "doctor"}}
# most specific first: the applicant's degrees are a B.Eng. and an M.S.
# no "mba": a business degree is not the M.S. (Expedia: "Master of Science" fell through to MBA)
_ABBREVIATIONS = {"bachelor": ["beng", "be", "btech", "bs", "bsc", "ba"], "master": ["ms", "msc", "ma"],
                  "phd": ["phd", "dphil"]}


_SYNONYMS = [{"mobile", "cell", "cellular", "cell phone", "mobile phone", "cellphone"},
             # RBC lists "USA": "United States" matched nothing and the box ended up on "Other"
             {"united states", "united states of america", "usa", "u s a", "us", "u s", "america"}]


def _loc(page: Page, jid: str):
    return page.locator(f'[data-ja-id="{jid}"]').first


def visible_option_texts(page: Page) -> list[str]:
    """Texts of the open dropdown's options; also tags them so click_option(i) can target one."""
    try:
        return page.evaluate(_OPTIONS_JS)
    except PlaywrightError:
        return []


def click_option(page: Page, index: int) -> None:
    page.locator(f'[data-ja-opt="{index}"]').first.click(timeout=5000)


# pre-filled dropdowns whose options are read again: resume parsing / drafts often hold a wrong pick (RBC's visa: F-1 OPT)
_RELOAD = re.compile(r"(?i)\bdegree\b|(visa|immigration) (status|type)")


def load_dropdown_options(page: Page, fields: list[dict], limit: int = 1000) -> None:
    """Open each empty custom dropdown once to read its options. Type-ahead ones (no list until typing) stay empty."""
    for f in fields:
        prefilled_degree = bool(f.get("value")) and _RELOAD.search(f.get("label") or "")
        if f["kind"] != "combobox" or f.get("options") or (f.get("value") and not prefilled_degree):
            continue  # pre-filled degree dropdowns are re-read: resume parsing often picks the wrong level
        loc = _loc(page, f["id"])
        try:
            loc.scroll_into_view_if_needed(timeout=3000)
            loc.click(timeout=3000)
            page.wait_for_timeout(700)
            texts = [t for t in visible_option_texts(page) if not _NO_OPTIONS.match(t)]
            for _ in range(40):  # long lists render a screenful at a time (Wawa's degrees: B.S. was out of view)
                if not page.evaluate(_SCROLL_OPTIONS_JS):
                    break
                page.wait_for_timeout(250)
                texts += [t for t in visible_option_texts(page) if not _NO_OPTIONS.match(t) and t not in texts]
            if _RELOAD.search(f.get("label") or "") and len(texts) >= 6:
                # lists that don't scroll as a container: walk them by keyboard, to the end (J&J / Regeneron list
                # hundreds of international degrees before the bachelor's ones; it stops once nothing new shows)
                texts += _arrow_scan(page, texts, batches=150)
            f["options"], f["option_count"] = texts[:limit], len(texts)
        except PlaywrightError:
            pass
        finally:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)


def _arrow_scan(page: Page, seen: list[str], want: str | None = None, batches: int = 30) -> list[str]:
    """Press ArrowDown through an open list so it renders the options further down. Returns the new option texts;
    with `want`, stops at the first batch that shows a matching option (still highlighted, not picked)."""
    new, stale = [], 0
    for _ in range(batches):
        for _ in range(5):
            page.keyboard.press("ArrowDown")
        page.wait_for_timeout(150)
        texts = [t for t in visible_option_texts(page) if not _NO_OPTIONS.match(t)]
        fresh = [t for t in texts if t not in seen and t not in new]
        new += fresh
        if want is not None and best_option(want, texts):
            break
        stale = 0 if fresh else stale + 1
        if stale >= 2:
            break
    return new


def _set_input_checked(page: Page, jid: str, want: bool = True) -> None:
    el = _loc(page, jid)
    if el.is_checked() == want:
        return
    eid = el.get_attribute("id")
    if eid:
        lab = page.locator(f'label[for="{eid}"]').first
        if lab.count() and lab.is_visible():
            lab.click(timeout=5000)
            if el.is_checked() == want:
                return
    try:
        el.set_checked(want, force=True, timeout=5000)
    except PlaywrightError:
        el.evaluate("e => e.click()")


def _search_terms(value: str) -> list[str]:
    """Type-ahead searches are often prefix matches: try the value, then without a leading article, then shorter."""
    terms = [value, re.sub(r"(?i)^(the|a|an)\s+", "", value)]
    words = terms[-1].split()
    if len(words) > 3:
        terms.append(" ".join(words[:3]))
    return list(dict.fromkeys(t for t in terms if t))


def _fill_combobox(page: Page, f: dict, value: str) -> str | None:
    """Pick an option in a custom dropdown / type-ahead. A school must match by name, else "Other" (if offered)."""
    school = bool(_SCHOOL_LABEL.search(f.get("label") or ""))
    err = _fill_combobox_once(page, f, value, school)
    if err and school and norm(value) != "other":
        page.keyboard.press("Escape")
        if _fill_combobox_once(page, f, "Other", True) is None:
            return None
    return err


_SCHOOL_LABEL = re.compile(r"(?i)school|university|institution|college")


def _fill_combobox_once(page: Page, f: dict, value: str, school: bool) -> str | None:
    loc = _loc(page, f["id"])
    texts: list[str] = []
    for term in _search_terms(value) if f.get("editable") else [value]:
        loc.scroll_into_view_if_needed(timeout=5000)
        loc.click(timeout=5000)
        if f.get("editable"):
            loc.fill("")
            loc.press_sequentially(term[:60], delay=30)
        deadline, nudged, jumped, scrolls = time.time() + 7, False, False, 0
        while time.time() < deadline:
            page.wait_for_timeout(400)
            texts = [t for t in visible_option_texts(page) if not _NO_OPTIONS.match(t)]
            if not texts:
                if not nudged and time.time() > deadline - 5:
                    loc.press("ArrowDown")  # some menus only open on keyboard input
                    nudged = True
                continue
            opt = (best_option(value, texts, fuzzy=False) or best_option(term, texts, fuzzy=False)) if school else \
                (best_option(value, texts) or best_option(term, texts))
            if opt is None and _HEARD_LABEL.search(f.get("label") or ""):
                # "how did you hear" (unimportant, per the user): a neutral choice like Other, else the first option
                real = [t for t in texts if not re.match(r"(?i)^\s*(select( one)?|choose( one)?|please select)\s*$", t)]
                opt = next((o for o in (best_option(c, real) for c in _FALLBACK_LEAF) if o), real[0] if real else None)
            if opt is None and f.get("editable") and len(texts) == 1 and not school:
                opt = texts[0]  # type-ahead narrowed to a single hit (never for schools: it can be a different school)
            if opt is None and not f.get("editable") and not jumped:
                page.keyboard.type(value[:12], delay=60)  # long listboxes scroll to the typed prefix
                jumped, deadline = True, time.time() + 4
                continue
            if opt is None and not f.get("editable") and scrolls < 40 and page.evaluate(_SCROLL_OPTIONS_JS):
                scrolls, deadline = scrolls + 1, time.time() + 2  # not in view yet: scroll the list further
                continue
            if opt is None and not f.get("editable") and scrolls < 40:
                scrolls = 40  # once: walk the list by keyboard until the option is rendered
                _arrow_scan(page, texts, want=value, batches=150)
                deadline = time.time() + 2
                continue
            if opt is None:
                break
            click_option(page, texts.index(opt))
            page.wait_for_timeout(300)
            return None
        page.keyboard.press("Escape")
    shown = "、".join(texts[:6]) or "无"
    if (dump := os.environ.get("JOBAGENT_DEBUG_DIR")):  # what the open list really looks like, for debugging
        try:
            loc.click(timeout=3000)
            page.wait_for_timeout(800)
            visible_option_texts(page)  # tags the open list's options with data-ja-opt
            html = page.evaluate("""() => { const o = document.querySelector('[data-ja-opt="0"]'); if (!o) return 'no option';
              const chain = []; for (let a = o; a && a !== document.body; a = a.parentElement) {
                const st = getComputedStyle(a);
                chain.push([a.tagName, a.getAttribute('role'), a.getAttribute('data-automation-id'), a.scrollHeight, a.clientHeight,
                            st.overflowY, a.children.length].join(':')); }
              const list = o.closest('[role="listbox"]') || o.parentElement;
              return JSON.stringify(chain) + ' ||| ' + list.outerHTML.slice(0, 30000); }""")
            Path(dump, f"dropdown-{re.sub(r'[^a-z0-9]+', '-', (f.get('label') or '').lower())[:40]}.html").write_text(html)
            page.keyboard.press("Escape")
        except PlaywrightError:
            pass
    return f"下拉框里找不到 {value!r}（看到的选项：{shown}）"


def _prompt_selected(page: Page, f: dict) -> bool:
    note = _loc(page, f["id"]).evaluate(
        "e => (e.closest('[data-automation-id=\"multiSelectContainer\"]')"
        "?.querySelector('[data-automation-id=\"promptAriaInstruction\"]')?.innerText || '')")
    return bool(re.search(r"(?i)\b[1-9]\d* items? selected", note))


def _pill_texts(page: Page, f: dict) -> list[str]:
    try:
        return _loc(page, f["id"]).evaluate(
            "e => Array.from((e.closest('[data-automation-id=\"multiSelectContainer\"]') || e.parentElement)"
            ".querySelectorAll('[data-automation-id=\"selectedItem\"]')).map((p) => p.innerText.trim())")
    except PlaywrightError:
        return []


def _pill_count(page: Page, f: dict) -> int:
    """Selected items shown as pills in a Workday prompt (multi-select skills add one pill per pick)."""
    try:
        return _loc(page, f["id"]).evaluate(
            "e => (e.closest('[data-automation-id=\"multiSelectContainer\"]') || e.parentElement)"
            ".querySelectorAll('[data-automation-id=\"selectedItem\"]').length")
    except PlaywrightError:
        return 0


_FALLBACK_LEAF = ["Other", "Online", "Internet", "Website", "Job Board", "LinkedIn"]
_HEARD_LABEL = re.compile(r"(?i)how did you (hear|learn|find out)|where did you (hear|find|learn)|referral source")


def skill_match(want: str, options: list[str]) -> str | None:
    """Skills: the exact name, or the name plus a qualifier ("Python (Programming Language)"); never "Python IDLE"."""
    w = norm(want)
    exact = [o for o in options if norm(o) == w]
    if exact:
        return exact[0]
    qualified = [o for o in options if re.match(rf"(?i)^\s*{re.escape(want.strip())}\s*\(", o)]
    return min(qualified, key=len) if qualified else None


def _fill_prompt(page: Page, f: dict, value: str, strict: bool = False) -> str | None:
    """Workday prompt: pick a top-level option, drill into sub-lists if it's a category, confirm the selection.
    A school that isn't in the list becomes "Other" (Workday's own instruction for unlisted schools)."""
    school = bool(re.search(r"(?i)school|university|institution|college", f.get("label") or ""))
    if not strict:
        _clear_wrong_pills(page, f, value)
    err = _fill_prompt_once(page, f, value, strict, school)
    if err and school and norm(value) != "other":
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        if _fill_prompt_once(page, f, "Other", False, True) is None:
            return None
    return err


def _remove_pills_not_matching(page: Page, f: dict, wanted: list[str]) -> None:
    """Multi-pick prompts (skills): drop selected items that don't match any wanted value."""
    box = page.locator('[data-automation-id="multiSelectContainer"]').filter(has=_loc(page, f["id"])).first
    for _ in range(30):
        pills = box.locator('[data-automation-id="selectedItem"]')
        texts = [t.strip() for t in pills.all_inner_texts()]
        bad = next((i for i, t in enumerate(texts) if not any(skill_match(w, [t]) for w in wanted)), None)
        if bad is None:
            return
        try:
            pills.nth(bad).locator('[data-automation-id="DELETE_charm"]').first.click(force=True, timeout=3000)
            page.wait_for_timeout(600)
        except PlaywrightError:
            return


def _clear_wrong_pills(page: Page, f: dict, value: str) -> None:
    """A single-pick prompt already holding something else: remove it first, or the new pick never replaces it."""
    have = _pill_texts(page, f)
    if not have or any(best_option(value, [h], fuzzy=False) for h in have):
        return
    box = page.locator('[data-automation-id="multiSelectContainer"]').filter(has=_loc(page, f["id"])).first
    for _ in range(len(have)):
        try:
            charm = box.locator('[data-automation-id="DELETE_charm"]').first
            if charm.count():
                charm.click(force=True, timeout=3000)  # a real click: Workday ignores synthetic events here
            else:
                pill = box.locator('[data-automation-id="selectedItem"]').first
                pill.focus(timeout=3000)
                page.keyboard.press("Delete")      # the pill's own hint: "press delete to clear value"
            page.wait_for_timeout(800)
        except PlaywrightError:
            break


def _fill_prompt_once(page: Page, f: dict, value: str, strict: bool, school: bool) -> str | None:
    loc = _loc(page, f["id"])
    loc.scroll_into_view_if_needed(timeout=5000)
    try:
        loc.click(timeout=5000)
    except PlaywrightError:
        loc.click(force=True, timeout=5000)  # an already-selected pill sits on top of the search box
    page.wait_for_timeout(1000)

    pills_before = _pill_count(page, f)

    def done() -> bool:
        if _prompt_selected(page, f) or _pill_count(page, f) > pills_before:
            if school and not any(best_option(value, [p], fuzzy=False) for p in _pill_texts(page, f)):
                # Workday auto-picks a lone search hit (an unrelated "… University" for a school it doesn't list)
                _clear_wrong_pills(page, f, value)
                loc.click(force=True, timeout=5000)
                page.wait_for_timeout(500)
                return False
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            return True
        return False

    texts: list[str] = []
    for depth in range(4):
        texts = [t for t in visible_option_texts(page) if not _NO_OPTIONS.match(t)]
        opt = skill_match(value, texts) if strict else best_option(value, texts)
        if opt is None and depth == 0 and texts and _HEARD_LABEL.search(f.get("label") or ""):
            # "how did you hear" (unimportant, per the user): a neutral choice like Other, else the first option
            opt = next((o for o in (best_option(c, texts) for c in _FALLBACK_LEAF) if o), texts[0])
        if opt is None and depth == 0:  # not in the first list: search for it
            for term in _search_terms(value):
                loc.fill(term[:40])
                loc.press("Enter")
                # school lists are searched on the server and can take a few seconds
                page.wait_for_timeout(4000 if re.search(r"(?i)school|university|institution", f.get("label") or "") else 1800)
                if done():  # a single search hit gets selected automatically
                    return None
                texts = [t for t in visible_option_texts(page) if not _NO_OPTIONS.match(t)]
                if strict:
                    opt = skill_match(value, texts) or skill_match(term, texts)
                elif school:  # a school must match by name; one unrelated search hit is not good enough
                    opt = best_option(value, texts, fuzzy=False) or best_option(term, texts, fuzzy=False)
                else:
                    opt = best_option(value, texts) or best_option(term, texts) or (texts[0] if len(texts) == 1 else None)
                if opt is not None:
                    break
        if opt is None and depth > 0 and not strict:  # inside a category the planner didn't see: the most generic leaf
            opt = next((o for o in (best_option(c, texts) for c in _FALLBACK_LEAF) if o), texts[0] if texts else None)
        if opt is None:
            break
        try:
            click_option(page, texts.index(opt))
        except PlaywrightError:
            pass  # the list may have closed because the selection already happened
        page.wait_for_timeout(1200)
        if done():
            return None
    page.keyboard.press("Escape")
    return f"选不中 {value!r}（看到的选项：{'、'.join(texts[:6]) or '无'}）"


_MONTHS = {name: i for i, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def date_parts(value, fmt: str) -> list[str] | None:
    """Split a date answer ("07/2026", "Jul 2026", "2026-07-15", "10/28/2026") into the picker's parts, e.g. MM/YYYY."""
    v = str(value).strip().lower()
    year = next((n for n in re.findall(r"\d+", v) if len(n) == 4), None)
    small = [int(n) for n in re.findall(r"\d+", v) if len(n) <= 2]
    named = next((i for name, i in _MONTHS.items() if re.search(rf"\b{name}", v)), None)
    month = named or (small.pop(0) if small else None)
    day = small.pop(0) if small else 1
    want = fmt.split("/")
    if year is None or ("MM" in want and not (month and 1 <= month <= 12)) or not 1 <= day <= 31:
        return None
    return [{"MM": f"{month or 1:02d}", "DD": f"{day:02d}", "YYYY": year}[w] for w in want]


_PART_VALUE_JS = "e => (e.value || e.getAttribute('aria-valuenow') || e.getAttribute('aria-valuetext') || '').replace(/\\D/g, '')"


def _fill_date_parts(page: Page, f: dict, value) -> str | None:
    """Workday date picker: focus each part (month, day, year) and type its digits, then check what stuck."""
    parts = date_parts(value, f["format"])
    if parts is None:
        return f"日期格式不对：{value!r}（需要 {f['format']}）"
    for pid, digits in zip(f["part_ids"], parts):
        el = _loc(page, pid)
        try:
            el.click(timeout=3000, force=True)
        except PlaywrightError:
            el.focus(timeout=3000)
        page.keyboard.type(digits, delay=60)
        page.wait_for_timeout(150)
    page.keyboard.press("Tab")
    page.wait_for_timeout(300)
    got = [_loc(page, pid).evaluate(_PART_VALUE_JS) for pid in f["part_ids"]]
    if [int(g) if g else None for g in got] != [int(d) for d in parts]:
        return f"日期没填上：想填 {'/'.join(parts)}，页面上是 {'/'.join(g or '?' for g in got)}"
    return None


def fill_field(page: Page, f: dict, value, resolve_file: Callable[[str], Path | None]) -> str | None:
    """Set one field. Returns an error message (instead of raising) when this field can't be set."""
    kind, loc = f["kind"], _loc(page, f["id"])
    try:
        if kind in TEXT_KINDS:
            loc.scroll_into_view_if_needed(timeout=5000)
            loc.fill(str(value), timeout=8000)
        elif kind == "select":
            opt = best_option(value, f.get("options") or [])
            if opt is None:
                return f"找不到选项 {value!r}"
            loc.select_option(label=opt, timeout=8000)
        elif kind in ("radio", "buttons"):
            opt = best_option(value, f["options"])
            if opt is None:
                return f"找不到选项 {value!r}"
            oid = f["option_ids"][f["options"].index(opt)]
            if kind == "buttons":
                _loc(page, oid).click(timeout=5000)
            else:
                _set_input_checked(page, oid, True)
        elif kind == "checkbox":
            for want in value if isinstance(value, list) else [value]:
                opt = best_option(want, f["options"])
                if opt is None:
                    return f"找不到选项 {want!r}"
                _set_input_checked(page, f["option_ids"][f["options"].index(opt)], True)
        elif kind == "checkbox_single":
            _set_input_checked(page, f["id"], value is True or norm(value) in ("true", "yes", "1"))
        elif kind == "combobox" and f.get("prompt") and re.search(r"(?i)skill", f.get("label") or "") \
                and len(parts := [v.strip() for v in re.split(r"[,;、]", str(value)) if v.strip()]) > 1:
            # Workday "Type to Add Skills" takes one skill per pick: add each one not already there; ok if any stuck
            _remove_pills_not_matching(page, f, parts)  # e.g. "Python IDLE" picked by an earlier, looser run
            have = _pill_texts(page, f)
            todo = [v for v in parts[:40] if not any(skill_match(v, [h]) for h in have)]
            errs = [e for e in (_fill_prompt(page, f, v, strict=True) for v in todo) if e]
            return errs[0] if todo and len(errs) == len(todo) and not have else None
        elif kind == "combobox":
            return _fill_prompt(page, f, str(value)) if f.get("prompt") else _fill_combobox(page, f, str(value))
        elif kind == "date_parts":
            return _fill_date_parts(page, f, value)
        elif kind == "file":
            path = resolve_file(str(value))
            if path is None:
                return f"没有可上传的文件：{value!r}"
            loc.set_input_files(str(path), timeout=15000)
        else:
            return f"不认识的字段类型 {kind}"
    except PlaywrightError as e:
        return f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
    return None


# Workday keeps the same URL and page header (the job title) on every step: the active step and the step's heading tell them apart
_PAGE_SIG_JS = """() => { const q = (s) => { const e = document.querySelector(s); return e ? e.innerText.trim() : ''; };
  return [location.pathname, q('[data-automation-id="pageHeaderTitleText"], h1'),
          q('[data-automation-id="progressBarActiveStep"]') || q('[aria-current="step"]'),
          q('[data-automation-id="applyFlowPage"] h2') || q('main h2') || q('h2')].join('|'); }"""


def page_signature(page: Page) -> str:
    """Which page / form step we're on: a change mid-fill means the page moved on (e.g. an Enter submitted it)."""
    try:
        return page.evaluate(_PAGE_SIG_JS)
    except PlaywrightError:
        return ""


def is_empty(f: dict) -> bool:
    v = f.get("value")
    return v in (None, "", [], False)


def missing_required(fields: list[dict]) -> list[str]:
    return [f["label"] or f["id"] for f in fields if f.get("required") and is_empty(f)]
