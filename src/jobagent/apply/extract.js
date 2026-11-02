// Snapshot of the application form on the current page, for the planner and the filler.
// Every control gets a stable data-ja-id so Python can target it later.
// Usage: page.evaluate(SOURCE, rootSelector)  ->  {fields, buttons, errors, captcha, url, title, text}
(rootSelector) => {
  const W = window;
  W.__jaSeq = W.__jaSeq || 0;
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const text = (el) => (el ? clean(el.innerText || el.textContent || '') : '');
  const byId = (id) => (id ? document.getElementById(id) : null);
  const tagId = (el, prefix = 'f') => {
    if (!el.dataset.jaId) el.dataset.jaId = prefix + ++W.__jaSeq;
    return el.dataset.jaId;
  };
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const CONTROL = 'input, select, textarea, button, [role="combobox"], [role="radio"], [role="checkbox"]';

  const ownLabel = (el) => {
    const ids = el.getAttribute('aria-labelledby');
    if (ids) {
      const t = clean(ids.split(/\s+/).map((i) => text(byId(i))).join(' '));
      if (t) return t;
    }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && text(l)) return text(l);
    }
    const wrap = el.closest('label');
    if (wrap && text(wrap)) return text(wrap);
    return clean(el.getAttribute('aria-label') || '');
  };

  // Button captions that sit next to upload inputs; never the question itself.
  const WIDGET_TEXT = /^(attach|upload|upload file|upload a file|dropbox|google drive|enter manually|or drag and drop here|drag and drop|choose file|browse|select file)$/i;

  // Question text: nearest label-like text that comes before the control inside its ancestors.
  const containerLabel = (el) => {
    let node = el;
    for (let depth = 0; depth < 9 && node && node.parentElement && node !== document.body; depth++) {
      const parent = node.parentElement;
      if (parent.tagName === 'FIELDSET') {
        const lg = parent.querySelector(':scope > legend, :scope > label');
        if (lg && text(lg)) return text(lg);
      }
      const ids = parent.getAttribute('aria-labelledby');
      if (ids && parent !== el) {
        const t = clean(ids.split(/\s+/).map((i) => text(byId(i))).join(' '));
        if (t && !WIDGET_TEXT.test(t)) return t;
      }
      for (const sib of parent.children) {
        if (sib === node) break;
        if (sib.matches(CONTROL) || sib.querySelector(CONTROL)) continue;
        if (el.id && sib.matches(`label[for="${CSS.escape(el.id)}"]`) && WIDGET_TEXT.test(text(sib))) continue;
        const t = text(sib);
        if (t && t.length < 500 && !WIDGET_TEXT.test(t)) return t;
      }
      node = parent;
    }
    return '';
  };

  const labelElement = (el) =>
    (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) ||
    (el.name && document.querySelector(`label[for="${CSS.escape(el.name)}"]`)) || el.closest('label');

  const isRequired = (el, label, container) => {
    const lab = labelElement(el);
    return !!(el.required || el.getAttribute('aria-required') === 'true' ||
       /[*✱]/.test(label) || /\(required\)/i.test(label) ||
       (lab && /required/i.test(lab.className || '')) ||
       (container && container.querySelector && container.querySelector('[aria-required="true"], .required, abbr[title="required"]')));
  };
  const hiddenProxy = (el) =>
    el.getAttribute('aria-hidden') === 'true' ||
    (el.tabIndex === -1 && parseFloat(getComputedStyle(el).opacity) < 0.05);
  const stripMarks = (s) => clean(s.replace(/[*✱]/g, ' ').replace(/\(required\)/ig, ' '));

  // rootSelector is a priority list ("a, b, c"): use the first selector that matches, not the first element in the page
  const root = (rootSelector || '').split(',').map((s) => s.trim()).filter(Boolean)
    .map((s) => document.querySelector(s)).find(Boolean) || document.body;
  const fields = [];
  const seenGroups = new Set();

  const optionText = (input) => {
    const t = ownLabel(input) || clean(input.value);
    return t;
  };
  const groupOf = (input) => {
    const type = input.type || input.getAttribute('role');
    const name = input.name;
    const scope = input.closest('fieldset, [role="radiogroup"], [role="group"]') || input.form || root;
    let members;
    if (name) members = Array.from(scope.querySelectorAll(`input[type="${type}"][name="${CSS.escape(name)}"]`));
    if (!members || members.length === 0) members = Array.from(scope.querySelectorAll(`input[type="${type}"]`));
    return { scope, members, key: (name || '') + '|' + (scope.dataset.jaId || tagId(scope, 'g')) };
  };
  const commonAncestor = (els) => {
    let a = els[0].parentElement;
    while (a && !els.every((e) => a.contains(e))) a = a.parentElement;
    return a || root;
  };

  // Toggle-button questions (Ashby Yes/No): >=2 sibling buttons with aria-pressed, plus a hidden checkbox.
  const consumed = new Set();
  const toggleParents = new Set(Array.from(root.querySelectorAll('button[aria-pressed]')).map((b) => b.parentElement));
  for (const parent of toggleParents) {
    const btns = Array.from(parent.querySelectorAll(':scope > button[aria-pressed]')).filter(visible);
    if (btns.length < 2) continue;
    parent.querySelectorAll('input').forEach((i) => consumed.add(i));
    const hidden = parent.querySelector('input[name]');
    const lab = hidden && document.querySelector(`label[for="${CSS.escape(hidden.name)}"]`);
    const label = (lab && text(lab)) || containerLabel(parent);
    fields.push({
      id: tagId(parent, 'g'), kind: 'buttons', label: stripMarks(label),
      required: /[*✱]/.test(label) || !!(lab && /required/i.test(lab.className || '')),
      options: btns.map((b) => text(b)), option_ids: btns.map((b) => tagId(b, 'o')),
      value: btns.filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => text(b)),
    });
  }

  // Workday date pickers (MM/YYYY, MM/DD/YYYY, YYYY): one hidden spinbutton input per part behind an "MM/YYYY"
  // display, so the loop below never sees them. Report each picker as one date_parts field with its format.
  const PART = { Month: 'MM', Day: 'DD', Year: 'YYYY' };
  const pickers = new Map();
  for (const inp of root.querySelectorAll('input[data-automation-id^="dateSection"]')) {
    const m = (inp.getAttribute('data-automation-id') || '').match(/^dateSection(Month|Day|Year)-input$/);
    const box = m && (inp.closest('[data-automation-id="dateInputWrapper"]') || inp.closest('[role="group"]'));
    if (!box) continue;
    consumed.add(inp);
    if (!pickers.has(box)) pickers.set(box, []);
    pickers.get(box).push({ inp, part: m[1] });
  }
  // RBC titles them "Your Work Experience 2" / "Your Experience 1" / "Your Education 1", Marvell "Professional
  // Experience 1": all named "Work Experience N" / "Education N" like everywhere else
  const ENTRY = /^(your\s+)?(professional\s+)?(work experience|experience|employment|education)\s*\d+$/i;
  const entryLabel = (t) => /education/i.test(t) ? t.replace(/^.*?education/i, 'Education')
    : t.replace(/^.*?(experience|employment)/i, 'Work Experience');
  const entryName = (el) => {  // "Work Experience 2": which repeated block a From/To picker belongs to
    for (let a = el.parentElement, d = 0; a && a !== root && d < 10; a = a.parentElement, d++) {
      const ids = a.getAttribute('aria-labelledby');
      const t = ids ? clean(ids.split(/\s+/).map((i) => text(byId(i))).join(' ')) : '';
      if (ENTRY.test(t)) return entryLabel(t);
      const h = Array.from(a.children).find((c) => c.matches('h2, h3, h4, h5, [role="heading"]') && ENTRY.test(text(c)));
      if (h) return entryLabel(text(h));
    }
    return '';
  };
  for (const [box, parts] of pickers) {
    if (!visible(box) || parts.some((p) => p.inp.disabled)) continue;
    const wrap = box.closest('[data-automation-id^="formField"]');
    const lab = (wrap && wrap.querySelector('label, legend')) || labelElement(parts[0].inp);
    const label = (lab && text(lab)) || containerLabel(box);
    const entry = entryName(box);
    const got = parts.map((p) => (p.inp.value || p.inp.getAttribute('aria-valuenow') || '').replace(/\D/g, ''));
    fields.push({
      id: tagId(box, 'd'), kind: 'date_parts', label: stripMarks(entry ? `${entry} — ${label}` : label),
      required: /[*✱]/.test(label) || parts.some((p) => p.inp.getAttribute('aria-required') === 'true'),
      format: parts.map((p) => PART[p.part]).join('/'), part_ids: parts.map((p) => tagId(p.inp)),
      value: got.every(Boolean) ? got.join('/') : '',
    });
  }

  for (const el of root.querySelectorAll('input, select, textarea, [role="combobox"], button[aria-haspopup="listbox"]')) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && ['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) continue;
    if (el.disabled || consumed.has(el)) continue;
    if (type !== 'file' && type !== 'radio' && type !== 'checkbox' && hiddenProxy(el)) continue;

    if (tag === 'input' && (type === 'radio' || type === 'checkbox')) {
      const g = groupOf(el);
      if (seenGroups.has(g.key)) continue;
      seenGroups.add(g.key);
      const members = g.members.filter((m) => visible(m) || visible(m.closest('label')) || (m.id && visible(document.querySelector(`label[for="${CSS.escape(m.id)}"]`))));
      if (members.length === 0) continue;
      if (type === 'checkbox' && members.length === 1) {
        const m = members[0];
        const own = ownLabel(m);
        const q = containerLabel(m);
        const label = own && q && own !== q ? `${q} — ${own}` : own || q;
        fields.push({ id: tagId(m), kind: 'checkbox_single', label: stripMarks(label), required: isRequired(m, label, null), value: m.checked, options: [] });
        continue;
      }
      const anc = g.scope && g.scope !== root && g.scope.contains(members[0]) && g.scope.tagName !== 'FORM' ? g.scope : commonAncestor(members);
      let label = '';
      for (const c of anc.children) {
        if (c.matches('legend, label, [class*="label" i], [class*="question" i]') && !c.querySelector(CONTROL)) {
          label = text(c);
          if (label) break;
        }
      }
      if (!label) {
        const ids = anc.getAttribute && anc.getAttribute('aria-labelledby');
        if (ids) label = clean(ids.split(/\s+/).map((i) => text(byId(i))).join(' '));
      }
      if (!label) label = containerLabel(anc);
      const opts = members.map((m) => ({ id: tagId(m), text: optionText(m), checked: m.checked }));
      fields.push({
        id: tagId(anc, 'g'), kind: type === 'radio' ? 'radio' : 'checkbox', label: stripMarks(label),
        required: isRequired(members[0], label, anc) || members.some((m) => m.required),
        options: opts.map((o) => o.text), option_ids: opts.map((o) => o.id),
        value: opts.filter((o) => o.checked).map((o) => o.text),
      });
      continue;
    }

    if (tag === 'input' && type === 'file') {
      const own = ownLabel(el);
      const q = containerLabel(el);
      const label = clean(`${q} ${own && own !== q && !WIDGET_TEXT.test(own) ? own : ''}`) || clean(el.name || el.id);
      fields.push({ id: tagId(el), kind: 'file', label: stripMarks(label), required: isRequired(el, label, null), value: el.files && el.files.length ? el.files[0].name : '', accept: el.accept || '' });
      continue;
    }

    if (!visible(el)) continue;
    // Workday dropdown buttons announce themselves as "Select One Required": that's a caption, not the question
    const own = ownLabel(el);
    // ...and once picked, as "No Required" / "F-1 OPT Required": the current value, not the question (HP, RBC)
    const current = clean(tag === 'input' ? el.value : text(el)).toLowerCase();
    const generic = /^(select( one)?|choose( one)?|search|please select)( required)?$/i.test(own) ||
      (!!current && own.replace(/\s*required$/i, '').toLowerCase() === current);
    const label = (generic ? containerLabel(el) || own : own || containerLabel(el)) ||
      clean(el.getAttribute('placeholder') || el.name || '');
    const base = { id: tagId(el), label: stripMarks(label), required: isRequired(el, label, null), placeholder: el.getAttribute('placeholder') || '' };

    if (tag === 'select') {
      const opts = Array.from(el.options).map((o) => clean(o.text)).filter((t) => t && !/^(select|choose|please select|--)/i.test(t));
      fields.push({ ...base, kind: 'select', options: opts.slice(0, 400), option_count: opts.length, value: el.selectedIndex >= 0 && el.value ? clean(el.options[el.selectedIndex].text) : '' });
    } else if (el.getAttribute('data-uxi-widget-type') === 'selectinput') {
      // Workday search-and-pick prompt; selection is announced as "1 item selected, <value>"
      const box = el.closest('[data-automation-id="multiSelectContainer"]') || el.parentElement;
      const note = text(box && box.querySelector('[data-automation-id="promptAriaInstruction"]'));
      const m = note.match(/selected,\s*(.+)$/i);
      const pills = box ? Array.from(box.querySelectorAll('[data-automation-id="selectedItem"]')).map(text).filter(Boolean) : [];
      fields.push({ ...base, kind: 'combobox', editable: true, prompt: true, options: [], value: m ? m[1] : pills.join(', ') });
    } else if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox' || el.getAttribute('aria-autocomplete')) {
      let value = tag === 'input' ? el.value : text(el);
      const control = el.closest('[class*="control"], [class*="Control"]');
      const shown = control && control.querySelector('[class*="singleValue"], [class*="single-value"], [class*="multiValue"], [class*="multi-value"]');
      if (shown) value = text(shown);
      if (/^(select|select one|select\.\.\.|choose)/i.test(value)) value = '';
      fields.push({ ...base, kind: 'combobox', editable: tag === 'input', options: [], value });
    } else if (tag === 'textarea') {
      fields.push({ ...base, kind: 'textarea', value: el.value, maxlength: el.maxLength > 0 ? el.maxLength : null });
    } else if (tag === 'input') {
      fields.push({ ...base, kind: ['email', 'tel', 'url', 'number', 'date', 'month'].includes(type) ? type : 'text', value: el.value });
    }
  }

  // page order (toggle groups and date pickers were collected first), so the planner reads questions in context
  const at = (f) => document.querySelector(`[data-ja-id="${f.id}"]`);
  // fields inside a repeated block carry its name ("Education 1 — Overall Result (GPA)"), so answers go to the right entry
  for (const f of fields) {
    const el = at(f);
    const entry = el && entryName(el);
    if (entry && !f.label.startsWith(entry)) f.label = `${entry} — ${f.label}`;
  }
  // Greenhouse repeats its education / employment blocks with numbered ids (school--0, degree--1, …) and no titles
  for (const f of fields) {
    const el = at(f);
    const m = el && !/^(work experience|education) \d+ —/i.test(f.label) && (el.id || '').match(/--(\d+)$/);
    if (!m) continue;
    const n = m[1];
    let kind = null;
    for (let a = el.parentElement, d = 0; a && d < 8 && !kind; a = a.parentElement, d++) {
      if (a.querySelector(`[id="school--${n}"]`)) kind = 'Education';
      else if (a.querySelector(`[id^="company"][id$="--${n}"], [id^="employer"][id$="--${n}"]`)) kind = 'Work Experience';
    }
    if (kind) f.label = `${kind} ${Number(n) + 1} — ${f.label}`;
  }
  fields.sort((a, b) => {
    const x = at(a), y = at(b);
    return x && y && x !== y ? (x.compareDocumentPosition(y) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1) : 0;
  });

  const buttons = [];
  for (const b of document.querySelectorAll('button, [role="button"], input[type="submit"], a')) {
    if (!visible(b)) continue;
    const t = clean(b.innerText || b.value || b.getAttribute('aria-label') || '');
    if (!t || t.length > 60) continue;
    buttons.push({ id: tagId(b, 'b'), text: t, tag: b.tagName.toLowerCase(), auto: b.getAttribute('data-automation-id') || '' });
  }

  const errors = [];
  for (const e of document.querySelectorAll('[role="alert"], [aria-live="assertive"], [class*="error" i], [data-automation-id*="error" i]')) {
    if (!visible(e) || e.matches('input, select, textarea')) continue;
    const t = text(e);
    if (t && t.length < 300 && !errors.includes(t)) errors.push(t);
  }

  // A challenge popup is a large visible captcha iframe; the small invisible-mode badge (~60px tall) doesn't count.
  const frames = Array.from(document.querySelectorAll('iframe')).filter(visible).map((f) => {
    const r = f.getBoundingClientRect();
    let host = '';
    try { host = new URL(f.src).hostname; } catch (e) { /* about:blank etc. */ }
    return { src: f.src || '', host, w: Math.round(r.width), h: Math.round(r.height) };
  });
  const captcha = frames.some((f) => f.h > 100 && /recaptcha|hcaptcha|challenges\.cloudflare\.com|turnstile|arkoselabs|funcaptcha/.test(f.src)) ||
    /just a moment|verify you are human|are you a robot|access (is temporarily restricted|denied)|unusual activity from your (device|network)|automated \(bot\) activity/i
      .test(document.title + ' ' + text(document.body).slice(0, 800));

  // head + tail of the page: confirmations and code prompts often sit below a long job description
  const body = text(document.body);
  const excerpt = body.length > 8000 ? body.slice(0, 4000) + ' … ' + body.slice(-4000) : body;
  return { fields, buttons, errors: errors.slice(0, 15), captcha, url: location.href, title: document.title, text: excerpt,
           frames: frames.filter((f) => f.h > 40).map((f) => `${f.host} ${f.w}x${f.h}`).slice(0, 6) };
}
