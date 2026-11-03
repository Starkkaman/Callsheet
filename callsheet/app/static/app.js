/* Callsheet: Daily Brief page + Setup.
   Hash routes: #brief (default), #setup (the step lives in memory), #eval (the owner-run writer evaluation,
   only when its tools are turned on in Setup, Delivery, Advanced) and #knowme (0.6.0: Get to know me, from Memory).
   Fetches api/brief and api/state (relative URLs: works under the HA ingress prefix). Brief: v3 frame A on desktop,
   v3 frame B's structure and order under 700px. Setup: v3 setup view, six steps wired to the step-2 API.
   Data is only ever inserted as text (textContent), never as HTML. */
(function () {
  'use strict';
  var app = document.getElementById('app');
  var main = document.getElementById('main');
  var LOOKS = ['bold', 'clean', 'easy'];
  var MODES = ['auto', 'light', 'dark'];
  var LOOKNAME = { bold: 'Bold', clean: 'Clean', easy: 'Easy read' }, MODENAME = { auto: 'Auto', light: 'Light', dark: 'Dark' };
  var KEY_LOOK = 'callsheet.look', KEY_MODE = 'callsheet.mode';
  var STEPS = ['About you', 'Areas', 'VIPs', 'Call times', 'Delivery', 'Review'];
  var DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
  var DAYNAME = { mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun' };

  /* ---------- small helpers ---------- */
  function load(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }
  function save(key, val) { try { window.localStorage.setItem(key, val); } catch (e) { /* storage blocked: keep in memory only */ } }
  function onMedia(q, fn) {
    var m = window.matchMedia(q);
    if (m.addEventListener) m.addEventListener('change', fn); else if (m.addListener) m.addListener(fn);
    return m;
  }
  function add(el, kids) {
    if (kids == null || kids === false) return el;
    if (!Array.isArray(kids)) kids = [kids];
    kids.forEach(function (c) {
      if (c == null || c === false) return;
      if (Array.isArray(c)) { add(el, c); return; }
      el.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
    });
    return el;
  }
  function h(tag, attrs, kids) {
    var el = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v == null || v === false) return;
      if (k === 'class') el.className = v; else el.setAttribute(k, v === true ? '' : String(v));
    });
    return add(el, kids);
  }
  function on(el, ev, fn) { el.addEventListener(ev, fn); return el; }
  /* a button with a click handler */
  function btn(cls, label, attrs, fn) {
    var a = attrs || {}; a.type = 'button'; a['class'] = cls;
    return on(h('button', a, label), 'click', fn);
  }
  /* inline error slot next to a control: filled with the server's plain sentence on failure */
  function errBox() { return h('p', { class: 'err', role: 'alert' }); }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
  function clampColour(c) { c = parseInt(c, 10); return c >= 1 && c <= 4 ? c : 4; }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  /* "06:30" stays as is; an ISO date-time becomes local HH:MM (with the day when it isn't today) */
  function fmtWhen(v, withDay) {
    if (!v) return '';
    if (/^\d{1,2}:\d{2}$/.test(String(v))) return String(v);
    var d = new Date(v);
    if (isNaN(d.getTime())) return String(v);
    /* show it in Callsheet's time zone (settings), like the brief, not the browser's */
    var tz = (S && S.settings && S.settings.timezone) || undefined;
    var t, day, today;
    try {
      t = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: tz });
      day = d.toLocaleDateString('en-CA', { timeZone: tz });
      today = new Date().toLocaleDateString('en-CA', { timeZone: tz });
    } catch (e) {
      t = pad(d.getHours()) + ':' + pad(d.getMinutes());
      day = d.toDateString(); today = new Date().toDateString(); tz = undefined;
    }
    if (!withDay || day === today) return t;
    return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', timeZone: tz }) + ', ' + t;
  }
  function safeHttps(u) { return typeof u === 'string' && /^https:/i.test(u) ? u : null; }
  function cssq(s) { return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, '\\$&'); }

  /* ---------- api: JSON in and out; mutations carry X-Callsheet: 1 ---------- */
  function api(method, path, body) {
    var opt = { method: method, cache: 'no-store', headers: { Accept: 'application/json' } };
    if (method !== 'GET') {
      opt.headers['X-Callsheet'] = '1';
      if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    }
    return fetch(path, opt).then(function (r) {
      return r.text().then(function (t) {
        var j = null;
        try { j = t ? JSON.parse(t) : null; } catch (e) { j = null; }
        if (!r.ok) throw new Error(j && j.error ? String(j.error) : 'Something went wrong (HTTP ' + r.status + ').');
        return j;
      });
    }, function () { throw new Error('Callsheet did not answer. Check the connection and try again.'); });
  }
  /* run a request: disable the buttons, clear the error, show the server's error inline on failure.
     Calls ok(result) on success; never rejects (no unhandled rejections). */
  var queue = Promise.resolve();
  function run(btns, err, fn, ok) {
    btns = [].concat(btns).filter(Boolean);
    btns.forEach(function (b) { b.disabled = true; b.setAttribute('aria-busy', 'true'); });
    if (err) err.textContent = '';
    function done() { btns.forEach(function (b) { b.disabled = false; b.removeAttribute('aria-busy'); }); }
    /* one mutation at a time, in click order: fn() runs (and builds its body from current state) when its turn comes */
    var p = queue.then(function () { return fn(); });
    queue = p.then(function () { }, function () { });
    return p.then(function (v) { done(); if (ok) return ok(v); }, function (e) {
      done();
      if (err) err.textContent = e && e.message ? e.message : 'Something went wrong.';
    }).catch(function (e) { if (err) err.textContent = e && e.message ? e.message : 'Something went wrong.'; });
  }

  /* ---------- look + mode ---------- */
  var S = null;            /* api/state */
  var lookPending = 0;     /* look/mode PATCHes in flight: a state reload must not undo them */
  function press(attr, val) {
    document.querySelectorAll('[' + attr + ']').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.getAttribute(attr) === val));
    });
  }
  function syncBg() {
    var cs = getComputedStyle(app);
    document.body.style.background = cs.backgroundColor;
    document.documentElement.style.colorScheme = cs.colorScheme;
  }
  function setLook(v, persist) {
    if (LOOKS.indexOf(v) < 0) v = 'bold';
    app.dataset.look = v; press('data-set-look', v); syncBg();
    document.querySelectorAll('.wi textarea').forEach(fitTa);   /* the look changes the font size */
    if (persist) save(KEY_LOOK, v);
  }
  function setMode(v, persist) {
    if (MODES.indexOf(v) < 0) v = 'auto';
    app.dataset.mode = v; press('data-set-mode', v);
    document.querySelectorAll('.mini').forEach(function (m) { m.dataset.mode = v; });
    syncBg();
    if (persist) save(KEY_MODE, v);
  }
  setLook(load(KEY_LOOK)); setMode(load(KEY_MODE));
  onMedia('(prefers-color-scheme: dark)', syncBg);
  onMedia('(max-width: 700px)', syncBg);
  /* look/mode also live in the server's settings (shared between devices) */
  function pushLook(key, val, src) {
    if (!S || !S.settings || S.settings[key] === val) return;
    var host = src && src.closest ? src.closest('[data-errhost]') : null;
    var err = host ? host.querySelector('.err') : document.getElementById('look-err');
    var body = {}; body[key] = val;
    lookPending++;
    run([], err, function () { return api('PATCH', 'api/settings', body); }, function () { S.settings[key] = val; })
      .then(function () { lookPending--; });
  }

  var lookBtn = document.getElementById('look-btn'), lookPanel = document.getElementById('look-panel');
  function openPanel(open) { lookPanel.hidden = !open; lookBtn.setAttribute('aria-expanded', String(open)); }
  lookBtn.addEventListener('click', function () { openPanel(lookPanel.hidden); });
  lookPanel.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { openPanel(false); lookBtn.focus(); }
  });
  document.addEventListener('click', function (e) {
    var t = e.target.closest ? e.target.closest('button') : null;
    if (!t || t.disabled) return;
    if (t.dataset.setLook) { setLook(t.dataset.setLook, true); pushLook('look', app.dataset.look, t); }
    else if (t.dataset.setMode) { setMode(t.dataset.setMode, true); pushLook('mode', app.dataset.mode, t); }
  });

  /* ---------- header: route buttons, last refreshed, refresh ---------- */
  var setupBtn = document.getElementById('setup-btn'), backBtn = document.getElementById('back-btn');
  var fresh = document.getElementById('fresh'), freshT = document.getElementById('fresh-t'), hdrDate = document.getElementById('hdr-date');
  var refreshBtn = document.getElementById('refresh-btn'), refreshErr = document.getElementById('refresh-err');
  setupBtn.addEventListener('click', function () { ui.step = 1; location.hash = 'setup'; });
  backBtn.addEventListener('click', function () { location.hash = 'brief'; });
  refreshBtn.addEventListener('click', function () {
    run([refreshBtn], refreshErr, function () { return api('POST', 'api/sync', {}); }, function (ls) {
      if (S && ls) S.last_sync = ls;
      if (ls && ls.ok === false) refreshErr.textContent = 'Refreshed with problems: see Heads up on Agenda.';
      return showBrief();
    });
  });

  /* ---------- render: brief (tabs-v6) ----------
     Tabs Briefing · Replies · Agenda · Areas · Tasks (later). One-line rows open a detail panel (desktop: side
     panel, phone: full-screen sheet). Ticking a row marks it done (api/done); the notepad menu hides rows that
     don't need a reply (api/suppress) or dismisses heads-ups and events on this device until tomorrow.
     Times are the brief's display strings (12h/24h from the server); only the timeline's hour labels are
     formatted here, from brief.time_format. */
  function status(text) { main.textContent = ''; add(main, h('p', { class: 'status', role: 'status' }, text)); clearLayer(); }
  function noAccounts() {
    return !S || !(S.areas || []).some(function (a) { return (a.accounts || []).length || a.signal; });  /* 0.5.1: or Signal linked */
  }
  var ICONS = document.getElementById('icons');
  function ico(k, label) {
    var src = ICONS && ICONS.content ? ICONS.content.querySelector('[data-i="' + k + '"]') : null;
    if (!src) return null;
    var s = src.cloneNode(true);
    if (label) { s.setAttribute('role', 'img'); s.setAttribute('aria-label', label); }
    else { s.setAttribute('aria-hidden', 'true'); s.setAttribute('focusable', 'false'); }
    return s;
  }
  var KEY_TAB = 'callsheet.tab', KEY_DISMISS = 'callsheet.dismissed';
  /* 0.6.0 chat: Chat takes the place of Areas (area summaries are in Briefing, Replies has area chips) */
  var TABS = [['briefing', 'Briefing', 'Brief', 'brief'], ['replies', 'Replies', 'Replies', 'mail'], ['agenda', 'Agenda', 'Agenda', 'cal'],
    ['chat', 'Chat', 'Chat', 'chat'], ['projects', 'Projects', 'Projects', 'tasks']];  /* 0.7.0: Projects replaces Tasks */
  var LIVE_TABS = ['briefing', 'replies', 'agenda', 'chat', 'projects'];
  var OLD_TABS = { areas: 'replies' };  /* 0.6.0 chat: a saved tab or #areas link from before opens Replies */
  function tabFromHash(hash) {          /* #briefing, #replies, #agenda, #chat (and the old #areas) open the brief there */
    var t = String(hash || '').replace(/^#/, '');
    t = OLD_TABS[t] || t;
    return LIVE_TABS.indexOf(t) >= 0 ? t : null;
  }
  function savedTab() { var t = load(KEY_TAB); t = OLD_TABS[t] || t; return LIVE_TABS.indexOf(t) >= 0 ? t : 'briefing'; }
  /* one reason dot per row, strongest first; the legend uses the same words */
  var REASONS = [['vip', 'VIP'], ['men', 'Mentioned'], ['q', 'Question'], ['due', 'Deadline'], ['wait', 'Waiting']];
  var RNAME = {}; REASONS.forEach(function (x) { RNAME[x[0]] = x[1]; });
  var phoneMq = window.matchMedia('(max-width: 700px)');
  function isPhone() { return phoneMq.matches; }

  var B = null;            /* the brief being shown */
  var U = {                /* brief UI state; survives re-renders and refreshes */
    tab: tabFromHash(location.hash) || savedTab(),
    open: null,            /* {kind: 'reply'|'event'|'item', id} ('item': an index item from a chat source chip, 0.6.0) */
    pop: null,             /* notepad menu {kind: 'reply'|'event'|'heads'|'para', id, anc, sub} */
    doneOpen: false, area: null, more: false, drafts: {}, toast: null, busy: false
  };
  var toastTimer = null;

  /* dismissed on this device until tomorrow: {day: brief date, keys: [...]} */
  function dismissed() {
    var d = null;
    try { d = JSON.parse(load(KEY_DISMISS) || 'null'); } catch (e) { d = null; }
    var day = B && B.date || '';
    if (!d || d.day !== day || !Array.isArray(d.keys)) d = { day: day, keys: [] };
    return d;
  }
  function isDismissed(k) { return dismissed().keys.indexOf(k) >= 0; }
  function setDismissed(k, on) {
    var d = dismissed(), i = d.keys.indexOf(k);
    if (on && i < 0) d.keys.push(k);
    if (!on && i >= 0) d.keys.splice(i, 1);
    d.keys = d.keys.slice(-300);
    save(KEY_DISMISS, JSON.stringify(d));
  }
  function hashOf(s) { var x = 0; s = String(s || ''); for (var i = 0; i < s.length; i++) x = (x * 31 + s.charCodeAt(i)) | 0; return String(x >>> 0); }

  /* ----- data helpers ----- */
  function areaOf(id) {
    var a = ((B && B.areas) || []).filter(function (x) { return x.id === id; })[0];
    return a ? { id: a.id, name: a.name, cls: 'a' + clampColour(a.colour) } : { id: id, name: String(id || ''), cls: 'an' };
  }
  function tagOf(id) { var a = areaOf(id); return a.name ? h('span', { class: 'tag ' + a.cls }, a.name) : null; }
  function replies() { return (B && B.replies) || []; }
  function events() { return ((B && B.schedule) || []).filter(function (e) { return !isDismissed('ev:' + (e.key || e.id)); }); }
  function replyById(id) { return replies().filter(function (r) { return r.id === id; })[0] || null; }
  function eventById(id) { return ((B && B.schedule) || []).filter(function (e) { return e.id === id; })[0] || null; }
  function gistSum(r) { return (r.gist && r.gist.summary) || r.short || r.text || ''; }
  function srcKind(r) {
    if (r.via === 'slack' || /^slack/i.test(r.source || '')) return 'slack';
    if (r.via === 'mail' || /^(mail|gmail)/i.test(r.source || '')) return 'mail';
    return 'signal';   /* Signal (0.5.1) and other chat sources: the speech-bubble glyph */
  }
  function srcName(r) { var k = srcKind(r); return k === 'mail' ? 'Gmail' : k === 'slack' ? 'Slack' : (r.source || 'Message'); }
  function reasonOf(r) {
    var rs = (Array.isArray(r.reasons) ? r.reasons : []).map(String);
    function find(re) { return rs.filter(function (x) { return re.test(x); })[0]; }
    var x;
    if (r.vip || find(/^VIP$/)) return ['vip', 'VIP'];
    if ((x = find(/^Mentioned|In your thread|^Replied to you/))) return ['men', x];
    if ((x = find(/question/i))) return ['q', x];
    if ((x = find(/deadline/i))) return ['due', x];
    if ((x = find(/^Waiting/))) return ['wait', r.wait ? 'Waiting' : x]; /* the wait text carries the days */
    return null;
  }
  /* minutes since midnight from a display or HH:MM string ("9:30 am", "4:00–5:00 pm", "14:30"); null if none */
  function clockMatch(s) {
    s = String(s || '');
    var m = /\b(\d{1,2})(?::([0-5]\d))?\s*(?:[–-]\s*\d{1,2}(?::\d{2})?\s*)?(am|pm)\b/i.exec(s);
    if (m && +m[1] >= 1 && +m[1] <= 12) return { m: (+m[1] % 12 + (m[3].toLowerCase() === 'pm' ? 12 : 0)) * 60 + +(m[2] || 0), text: m[0] };
    m = /\b([01]?\d|2[0-4]):([0-5]\d)\b/.exec(s);
    return m ? { m: +m[1] * 60 + +m[2], text: m[0] } : null;
  }
  function parseClock(s) { var c = clockMatch(s); return c ? c.m : null; }
  function startMin(e) { var v = parseClock(e.start_hhmm || e.time); return v == null ? null : v; }
  function endMin(e) {
    var s = startMin(e), v = parseClock(e.end_hhmm || e.end);
    if (s == null) return null;
    return v == null || v <= s ? s + 30 : v;
  }
  /* "now" in Callsheet's time zone, only when the brief is today's */
  function nowMin() {
    var tz = (S && S.settings && S.settings.timezone) || undefined, d = new Date(), day, hm;
    try {
      day = d.toLocaleDateString('en-CA', { timeZone: tz });
      hm = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: tz });
    } catch (e) { day = d.toLocaleDateString('en-CA'); hm = pad(d.getHours()) + ':' + pad(d.getMinutes()); }
    if (!B || B.date !== day) return null;
    return parseClock(hm);
  }
  /* a reply whose exact details name a time today: [minutes, the time as written] (Agenda pins, "Due today") */
  function todayTime(r) {
    var ds = (r.gist && Array.isArray(r.gist.details)) ? r.gist.details : [];
    var dm = String((B && B.date_short) || '').split(' ').slice(1).join(' ');
    for (var i = 0; i < ds.length; i++) {
      var v = String(ds[i].value || '');
      var onlyTime = /^\s*\d{1,2}(:\d{2})?\s*(am|pm)?(\s*[–-]\s*\d{1,2}(:\d{2})?\s*(am|pm)?)?\s*$/i.test(v);   /* "4:30 pm": today */
      if (!(onlyTime || /\b(today|tonight)\b/i.test(v) || (dm && v.indexOf(dm) >= 0))) continue;
      var c = clockMatch(v);
      if (c) return [c.m, c.text];
    }
    return null;
  }
  function hourLabel(hr) {
    if (B && B.time_format === '24h') return pad(hr % 24) + ':00';
    return (hr % 12 || 12) + (hr % 24 >= 12 ? ' pm' : ' am');
  }
  function openReplies() { return replies(); }
  function vipCount() { return replies().filter(function (r) { return reasonOf(r) && reasonOf(r)[0] === 'vip'; }).length; }

  /* ----- shared pieces ----- */
  function npBtn(kind, id, anc, wide) {
    var open = !!(U.pop && U.pop.anc === anc), nt = noteOf(kind, id);
    return h('button', { type: 'button', class: 'np' + (wide ? ' wide' : '') + (nt ? ' has' : ''), 'data-np': kind + ':' + id, 'data-anc': anc,
      'aria-haspopup': 'true', 'aria-expanded': String(open), 'aria-label': wide ? null : (nt ? 'Note and dismiss (has a note)' : 'Note and dismiss'),
      title: nt ? 'Note saved: ' + nt.text : 'Note for Callsheet, or dismiss' },
    [ico('note'), wide ? (nt ? 'Note saved' : 'Note or dismiss') : null]);
  }
  function rdot(r) {
    var x = reasonOf(r);
    if (!x) return h('span', { class: 'rdot none', 'aria-hidden': 'true' });
    var all = (Array.isArray(r.reasons) ? r.reasons : []).join(' · ') || x[1];
    return h('span', { class: 'rdot ' + x[0], role: 'img', 'aria-label': x[1], title: all });
  }
  function newDot(r) {
    return r.new && B.previous_at ? h('span', { class: 'newdot', role: 'img', 'aria-label': 'New since ' + B.previous_at, title: 'New since ' + B.previous_at }) : null;
  }
  /* the sender's-claim check (0.4.1): the rules flag a row whose latest incoming message says something was done */
  var CLAIM_NOTE = 'This message says something was approved, paid, signed or released. Check before replying.';
  /* the 0.5.2 security fix (sync.guard_claims): markers only, the page owns these fixed lines */
  var HIDDEN_LINES = {
    claim: 'No suggested reply: this message says something was done. Check before replying.',
    brief_claim: "Suggested reply hidden: it stated something as done while a message in today's brief claims something was done. Check before replying."
  };
  var CHECK_LABEL = 'Check this: it may repeat a claim from a message.';
  function hiddenLine(r) { return Object.prototype.hasOwnProperty.call(HIDDEN_LINES, r.suggestion_hidden) ? HIDDEN_LINES[r.suggestion_hidden] : null; }
  function gistCheck(r) { return !!(r.gist && r.gist.check === true); }
  function checkLabel(tag, on) { return on ? h(tag, { class: 'chk' }, CHECK_LABEL) : null; }
  function claimMark(r) {
    if (r.claim !== true && gistCheck(r)) return h('span', { class: 'cmk', 'aria-hidden': 'true', title: CHECK_LABEL }, '!');
    return r.claim === true ? h('span', { class: 'cmk', 'aria-hidden': 'true', title: CLAIM_NOTE }, '!') : null;
  }
  /* the wait (0.4.1): the rules' wording ("waiting 11 days, last nudge 8 days ago") with a middle dot; it
     stands in for the row's age, which would repeat the last nudge's day count */
  function waitOf(r) {
    return typeof r.wait === 'string' && r.wait ? r.wait.split(', ').join(' · ') : '';
  }
  function ageOf(r) { return waitOf(r) || r.age || ''; }
  /* the list's compact form: "waiting 11d · nudge 8d", "waiting 3d", "waiting today" */
  function waitShort(r) {
    return waitOf(r).replace(/^waiting since today/, 'waiting today').replace(/ (\d+) days?/g, ' $1d')
      .replace(/last nudge (\S+)(?: ago)?/, 'nudge $1');
  }
  function rowEl(r, where) {
    var sel = U.open && U.open.kind === 'reply' && U.open.id === r.id;
    return h('div', { class: 'row' + (sel ? ' sel' : ''), 'data-row': r.id }, [
      h('button', { type: 'button', class: 'ck', 'data-ck': r.id, 'aria-pressed': 'false', 'aria-label': 'Mark done: ' + r.who }, h('span', { class: 'bx' })),
      h('button', { type: 'button', class: 'rmain', 'data-open': 'reply:' + r.id,
        'aria-label': r.who + ': ' + gistSum(r) + (ageOf(r) ? ', ' + ageOf(r) : '') + (reasonOf(r) ? ', ' + reasonOf(r)[1] : '') + (r.new && B.previous_at ? ', new' : '') + (r.claim === true ? ', says something was done: check' : gistCheck(r) ? ', ' + CHECK_LABEL : '') }, [
        h('span', { class: 'ri' }, ico(srcKind(r), srcName(r))),
        h('span', { class: 'tx' }, [newDot(r), h('span', { class: 'who' }, r.who), h('span', { class: 'sum' }, gistSum(r))]),
        claimMark(r), h('span', { class: 'age', title: waitOf(r) || null }, waitShort(r) || ageOf(r)), rdot(r)]),
      npBtn('reply', r.id, where + '-' + r.id)]);
  }
  function doneRows() {
    var ds = (B && B.done) || [];
    if (!ds.length) return null;
    return [h('button', { type: 'button', class: 'dfold', 'data-donefold': '1', 'aria-expanded': String(U.doneOpen) },
      'Done today (' + ds.length + ') ' + (U.doneOpen ? '▾' : '▸')),
    U.doneOpen ? h('div', { class: 'list' }, ds.map(function (d) {
      return h('div', { class: 'row isdone' }, [
        h('button', { type: 'button', class: 'ck', 'data-unck': d.id, 'aria-pressed': 'true', 'aria-label': 'Not done: ' + d.who }, h('span', { class: 'bx' })),
        h('div', { class: 'rmain' }, [h('span', { class: 'tx' }, [h('span', { class: 'who' }, d.who), h('span', { class: 'sum' }, d.summary || '')]),
          h('span', { class: 'age' }, d.done_at || '')]),
        h('span')]);
    })) : null];
  }
  function hiddenLine() {
    var n = parseInt(B && B.hidden, 10) || 0;
    return n > 0 ? h('p', { class: 'hidline' }, [n + ' hidden · ', h('button', { type: 'button', class: 'tbtn', 'data-manage': '1' }, 'Manage')]) : null;
  }
  function legend() {
    return h('div', { class: 'legend' }, REASONS.map(function (x) {
      return h('span', null, [h('span', { class: 'rdot ' + x[0], 'aria-hidden': 'true' }), x[1]]);
    }).concat([h('span', { class: 'hint' }, 'Tick when done.')]));
  }
  function foot() {
    return h('div', { class: 'foot' }, [h('span', null, B.sources || ''), h('span', null, 'Nothing was sent. Drafts wait for you.')]);
  }

  /* ----- tabs ----- */
  function T_briefing() {
    var sum = B.summary && typeof B.summary.text === 'string' && B.summary.text ? B.summary : null;
    var top = (sum && Array.isArray(sum.top) && sum.top.length ? sum.top : B.top) || [];
    var doneIds = ((B.done) || []).map(function (d) { return d.id; });
    var nRep = openReplies().length, evs = events();
    var head = h('div', { class: 'label muted kicker' }, [B.kind || 'Brief', B.written_at ? ' · ' + B.written_at : '', B.previous_at ? ' · since ' + B.previous_at : '']);
    var text = sum ? [h('p', { class: 'daytext' + (U.more ? ' all' : ''), id: 'bv-daytext' }, sum.text), checkLabel('p', sum.check === true),
      sum.text.length > 260 ? h('button', { type: 'button', class: 'tbtn', 'data-more': '1', 'aria-expanded': String(U.more), 'aria-controls': 'bv-daytext' }, U.more ? 'Less' : 'More') : null]
      : h('h2', { class: 'headline' }, plural(evs.length, 'event', 'events') + ' today, ' + plural(nRep, 'reply', 'replies') + ' waiting.');
    /* 0.6.1: the writer is still working on this brief: a quiet line instead of the summary (the page asks again) */
    if (B.writing && !sum) text = [text, h('p', { class: 'writing muted', role: 'status' }, [B.writing.text || 'Writing the summary…',
      B.writing.since ? ' Started ' + B.writing.since + '.' : ''])];
    var topEl = top.length ? [h('h2', { class: 'sub-h' }, 'Top of the day'), h('ol', { class: 'topl' }, top.slice(0, 5).map(function (t) {
      var isEv = t.kind === 'event', gone = !isEv && !replyById(t.ref), dn = gone && doneIds.indexOf(t.ref) >= 0;
      var kids = [h('span', { class: 'ri' }, ico(isEv ? 'cal' : 'mail')), h('span', { class: 'tt' }, [t.text, checkLabel('span', t.check === true)])];
      return h('li', null, gone ? h('span', { class: 'tline' + (dn ? ' isdone' : '') }, kids)
        : h('button', { type: 'button', class: 'tline', 'data-open': (isEv ? 'event:' : 'reply:') + t.ref }, kids));
    }))] : null;
    var areas = sum && sum.areas ? (B.areas || []).filter(function (a) { return sum.areas[a.id] && !isDismissed('ar:' + a.id + ':' + hashOf(sum.areas[a.id])); }) : [];
    var byArea = areas.length ? [h('h2', { class: 'sub-h' }, 'By area'), h('div', { class: 'paras' }, areas.map(function (a) {
      var chk = areaCheck(sum, a.id);
      return h('div', { class: 'para cn' }, [
        h('button', { type: 'button', class: 'pmain', 'data-goarea': a.id, 'aria-label': a.name + ': ' + sum.areas[a.id] + (chk ? ' ' + CHECK_LABEL : '') + ' (show its replies)' }, [
          h('span', { class: 'ptag' }, tagOf(a.id)), h('span', { class: 'ptext' }, sum.areas[a.id]), checkLabel('span', chk)]),
        npBtn('para', a.id, 'pa-' + a.id)]);
    }))] : null;
    /* at a glance: next event, due today, waiting */
    var now = nowMin(), next = null, gl = [];
    evs.forEach(function (e) { var s = startMin(e); if (!next && s != null && (now == null || s >= now)) next = e; });
    if (next) gl.push(h('li', null, [h('span', { class: 'label' }, 'Next'), h('span', { class: 'disp' }, next.time || ''), next.short || next.title]));
    var due = openReplies().map(function (r) { var t = todayTime(r); return t ? [t[0], r, t[1]] : null; }).filter(Boolean).sort(function (a, b) { return a[0] - b[0]; });
    if (due.length) gl.push(h('li', null, [h('span', { class: 'label' }, 'Due today'), h('span', { class: 'disp' }, due[0][2]),
      due[0][1].who + (due.length > 1 ? ' and ' + (due.length - 1) + ' more' : '')]));
    var clashes = evs.filter(function (e) { return e.clash; });
    if (clashes.length) gl.push(h('li', null, [h('span', { class: 'label' }, 'Clash'), h('span', { class: 'disp alert' }, clashes[0].time || ''), clashes[0].short || clashes[0].title]));
    var nv = vipCount();
    gl.push(h('li', null, [h('span', { class: 'label' }, 'Waiting'), h('span', { class: 'disp' }, String(nRep)),
      nRep ? plural(nRep, 'reply', 'replies') + (nv ? ', ' + nv + ' from VIPs' : '') : 'Nothing waiting']));
    var aside = h('aside', { class: 'glance-w', 'aria-label': 'At a glance' }, [h('h2', { class: 'sub-h' }, 'At a glance'), h('ul', { class: 'glance' }, gl)]);
    return [h('div', { class: 'bf' }, [h('article', null, [head, text, briefProjects(), topEl, byArea]), aside])];
  }
  function T_replies() {
    var all = openReplies(), nNew = B.previous_at ? all.filter(function (r) { return r.new; }).length : 0;
    var rs = U.area ? all.filter(function (r) { return r.area === U.area; }) : all;  /* 0.6.0 chat: the area chips */
    return [h('h2', { class: 'sec-h' }, ['Needs a reply ', h('span', { class: 'n' }, all.length + (nNew ? ' · ' + nNew + ' new since ' + B.previous_at : '') + (vipCount() ? ' · VIPs first' : ''))]),
      areaChips(all),
      rs.length ? legend() : null,
      rs.length ? h('div', { class: 'list' }, rs.map(function (r) { return rowEl(r, 'rl'); })) : h('p', { class: 'empty' }, U.area ? 'Nothing waiting in this area.' : 'Nothing waiting.'),
      hiddenLine(), doneRows()];
  }
  function timeline() {
    var phone = isPhone(), H = phone ? 56 : 64, evs = events(), now = nowMin();
    var t0 = 7 * 60, t1 = 22 * 60;
    evs.forEach(function (e) { var s = startMin(e), en = endMin(e); if (s == null) return; t0 = Math.min(t0, Math.floor(s / 60) * 60); t1 = Math.max(t1, Math.min(24 * 60, Math.ceil(en / 60) * 60)); });
    function top(m) { return (m - t0) / 60 * H; }
    var height = top(t1);
    var ax = h('div', { class: 'col ax', 'aria-hidden': 'true' }), tc = h('div', { class: 'col tc' }), pc = h('div', { class: 'col pc' });
    var lines = h('div', { class: 'hls', 'aria-hidden': 'true' });
    for (var m = t0; m <= t1; m += 60) {
      var lab = h('span', null, hourLabel(m / 60)); lab.style.top = top(m) + 'px'; ax.appendChild(lab);
      var hl = h('div', { class: 'hl' }); hl.style.top = top(m) + 'px'; lines.appendChild(hl);
    }
    [ax, tc, pc].forEach(function (c) { c.style.height = height + 'px'; });
    /* clusters of overlapping events side by side; a red marker beside each clash */
    var timed = evs.filter(function (e) { return startMin(e) != null; }).slice().sort(function (a, b) { return startMin(a) - startMin(b) || endMin(a) - endMin(b); });
    var clusters = [], cur = null;
    timed.forEach(function (e) {
      if (cur && startMin(e) < cur.end) { cur.evs.push(e); cur.end = Math.max(cur.end, endMin(e)); }
      else { cur = { evs: [e], start: startMin(e), end: endMin(e) }; clusters.push(cur); }
    });
    clusters.forEach(function (c) {
      var lanes = [], lane = {};
      c.evs.forEach(function (e) {
        var i = 0; while (i < lanes.length && lanes[i] > startMin(e)) i++;
        lanes[i] = endMin(e); lane[e.id] = i;
      });
      var n = lanes.length;
      if (n > 1) {
        var cm = h('div', { class: 'cmark', role: 'img', 'aria-label': 'Clash' });
        cm.style.top = top(c.start) + 'px'; cm.style.height = (top(c.end) - top(c.start)) + 'px'; tc.appendChild(cm);
      }
      c.evs.forEach(function (e) {
        var hh = Math.max(top(endMin(e)) - top(startMin(e)), 26), cl = n > 1 || !!e.clash, a = areaOf(e.area);
        var sel = U.open && U.open.kind === 'event' && U.open.id === e.id;
        var blk = h('div', { class: 'blk ' + a.cls + (cl ? ' clash' : '') + (sel ? ' sel' : '') }, [
          h('button', { type: 'button', class: 'bopen', 'data-open': 'event:' + e.id,
            'aria-label': (e.time || '') + (e.end ? '–' + e.end : '') + ' ' + e.title + (cl ? ', clash' : '') }, [
            h('span', { class: 'bt' }, (hh < 40 ? (e.time || '') + ' ' : '') + e.title),
            hh >= 40 ? h('span', { class: 'bm' }, (e.time || '') + (e.end ? '–' + e.end : '') + (cl ? ' · Clash' : '')) : null,
            hh >= 60 && (e.note || (e.flag && e.flag.text)) ? h('span', { class: 'bm' }, e.note || e.flag.text) : null]),
          npBtn('event', e.id, 'tl-' + e.id)]);
        blk.style.top = top(startMin(e)) + 'px'; blk.style.height = hh + 'px';
        blk.style.left = 'calc(' + (lane[e.id] * 100 / n) + '% + ' + (lane[e.id] ? 2 : 0) + 'px)';
        blk.style.width = 'calc(' + (100 / n) + '% - ' + (n > 1 ? 2 : 0) + 'px)';
        tc.appendChild(blk);
      });
    });
    /* replies whose exact details name a time today, pinned at that time */
    var lastPin = -100, nPins = 0;
    openReplies().map(function (r) { var t = todayTime(r); return t && t[0] >= t0 && t[0] <= t1 ? [t[0], r] : null; }).filter(Boolean)
      .sort(function (a, b) { return a[0] - b[0]; }).forEach(function (p) {
        var r = p[1], y = Math.max(top(p[0]) - 2, lastPin + 28), rk = reasonOf(r);
        lastPin = y; nPins++;
        var sel = U.open && U.open.kind === 'reply' && U.open.id === r.id;
        var pin = h('div', { class: 'pin' + (rk && rk[0] === 'due' ? ' due' : '') + (sel ? ' sel' : '') }, [
          h('button', { type: 'button', class: 'pm', 'data-open': 'reply:' + r.id, title: r.who + ': ' + gistSum(r) }, [
            h('span', { class: 'ri' }, ico(srcKind(r), srcName(r))), h('span', { class: 'who' }, String(r.who).split(' ')[0]), h('span', { class: 'pt' }, gistSum(r))]),
          npBtn('reply', r.id, 'pin-' + r.id)]);
        pin.style.top = y + 'px';
        pc.appendChild(pin);
      });
    var kids = [lines, ax, h('div', { class: 'col' }, [h('div', { class: 'colh label' }, 'Calendar'), tc]),
      nPins ? h('div', { class: 'col' }, [h('div', { class: 'colh label' }, phone ? 'Messages' : 'Messages about that time'), pc]) : null];
    if (now != null && now >= t0 && now <= t1) {
      var nl = h('div', { class: 'nowl', 'aria-hidden': 'true' }); nl.style.top = top(now) + 'px'; kids.push(nl);
      var nt = h('span', { class: 'nowt' }, 'Now'); nt.style.top = top(now) + 'px'; ax.appendChild(nt);
    }
    var tl = h('div', { class: 'tl' + (nPins ? '' : ' nopins') }, kids);
    tl.style.height = height + 'px';
    return tl;
  }
  function headsStrip() {
    var hs = ((B && B.heads_up) || []).filter(function (x) { return !isDismissed('hu:' + hashOf(x)); });
    if (!hs.length) return null;
    return h('section', { class: 'hstrip', 'aria-label': 'Heads up' }, [h('h2', { class: 'sub-h' }, 'Heads up'),
      h('ul', null, hs.map(function (x) { var k = hashOf(x); return h('li', { class: 'cn' }, [h('span', null, x), npBtn('heads', k, 'hu-' + k)]); }))]);
  }
  function T_agenda() {
    var evs = events();
    return [h('h2', { class: 'sec-h' }, ['Today ', h('span', { class: 'n' }, [B.date_short || '', evs.some(function (e) { return e.clash; }) ? ' · clashes in red' : ''])]),
      headsStrip(),
      evs.length ? timeline() : h('p', { class: 'empty' }, 'Nothing on your calendar today.'),
      possibleSection()];
  }
  function areaCheck(sum, id) { return !!(sum && Array.isArray(sum.areas_check) && sum.areas_check.indexOf(id) >= 0); }
  /* ---------- 0.7.0 projects: the Projects tab (list, timeline, slipped, weekly review, settings) ----------
     Everything shown comes from api/projects and is rendered as text (h()). Drafted projects stay "Suggested"
     until the owner adds them; nothing here sends anything or writes outside Callsheet. */
  var PJ = { d: null, loading: false, err: '', view: 'list', open: null, menu: null, edit: null, form: {}, rv: { step: 'slipped', i: 0 },
    gh: { repos: null, pick: null, err: '', tok: '' }, timer: null };
  var PJ_STATUS = { on: 'On track', behind: 'Behind', wait: 'Waiting', paused: 'Paused', sugg: 'Suggested', done: 'Finished' };
  var PJ_KIND = { late: 'Date passed', quiet: 'Gone quiet', owe: 'You owe a reply' };
  var PJ_REVIEW_WHEN = { mon_am: 'Monday mornings', fri_pm: 'Friday afternoons', sun_pm: 'Sunday evenings', off: 'Any time you like' };
  var PJ_SCOPE_SUGG = ['What’s blocking this?', 'What did I promise?', 'Who’s waiting on me?'];
  function pjLoad() {
    if (PJ.loading) return;
    PJ.loading = true;
    api('GET', 'api/projects').then(function (d) { PJ.loading = false; PJ.d = d; PJ.err = ''; pjPoll(); if (U.tab === 'projects') renderBrief(); },
      function (e) { PJ.loading = false; PJ.err = e.message; if (U.tab === 'projects') renderBrief(); });
  }
  function pjPoll() {
    clearTimeout(PJ.timer); PJ.timer = null;
    if (PJ.d && PJ.d.ai && PJ.d.ai.state === 'running') PJ.timer = setTimeout(function () { PJ.loading = false; pjLoad(); }, 2000);
  }
  function pjDo(method, path, body, b, after) {
    if (b) b.disabled = true;
    return api(method, path, body).then(function (d) {
      if (d && d.projects) PJ.d = d;
      PJ.err = ''; PJ.menu = null;
      if (after) after(d);
      pjPoll(); renderBrief();
      return d;
    }, function (e) { if (b) b.disabled = false; PJ.err = e.message; renderBrief(); });
  }
  function pjById(id) { return PJ.d ? PJ.d.projects.filter(function (p) { return p.id === id; })[0] || null : null; }
  function pjSlip(k) { return PJ.d ? PJ.d.slipped.filter(function (x) { return x.key === k; })[0] || null : null; }
  function sdot(st) { return h('span', { class: 'sdot ' + st, 'aria-hidden': 'true' }); }
  function kdot(k) { return h('span', { class: 'kdot ' + k, 'aria-hidden': 'true' }); }
  function pjGo(view) { PJ.view = view; PJ.open = null; PJ.menu = null; PJ.edit = null; renderBrief(); }
  function pjOpen(id) { PJ.open = id; PJ.menu = null; PJ.edit = null; renderBrief(); }
  function pjMenu(key, items) {
    var open = PJ.menu === key;
    return h('span', { class: 'pjm' }, [btn('np', ico('note', 'Notes and options'), { 'aria-expanded': String(open), 'aria-haspopup': 'true' },
      function () { PJ.menu = open ? null : key; renderBrief(); }),
    open ? h('ul', { class: 'pjpop', role: 'menu' }, items.map(function (it) {
      return h('li', { role: 'none' }, btn('pjmi', it[0], { role: 'menuitem' }, it[1]));
    })) : null]);
  }
  function projMenu(p) {
    return pjMenu('p:' + p.id, [['Add a note for Callsheet', function () { PJ.open = p.id; PJ.menu = null; PJ.focusNotes = true; renderBrief(); }],
      ['Not a project', function (e) { pjDo('POST', 'api/projects/' + p.id + '/not', {}, e.currentTarget); }],
      ['Pause it', function (e) { pjDo('PATCH', 'api/projects/' + p.id, { status: 'paused' }, e.currentTarget); }],
      ['Hide from the list', function (e) { pjDo('POST', 'api/projects/' + p.id + '/hidden', {}, e.currentTarget); }]]);
  }
  function slipMenu(x) {
    function mark(a) { return function (e) { pjDo('POST', 'api/projects/slipped/' + x.key, { action: a }, e.currentTarget); }; }
    return pjMenu('s:' + x.key, [['Add a note for Callsheet', function () { PJ.open = 'slip:' + x.key; PJ.menu = null; PJ.focusNotes = true; renderBrief(); }],
      ["It's fine, not slipped", mark('fine')], ['Remind me next week', mark('snooze')], ['Dismiss', mark('dismiss')]]);
  }
  function pjRow(p) {
    var sel = PJ.open === p.id;
    return h('li', { class: 'row pjrow' + (sel ? ' sel' : '') }, [h('span', { class: 'scell', title: PJ_STATUS[p.status] }, sdot(p.status)),
      btn('rmain', [h('span', { class: 'tx' }, [h('span', { class: 'who' }, p.name), h('span', { class: 'sum' }, p.next_step || (p.state === 'suggested' ? (p.why[0] || '') : 'No next step yet'))]),
        p.short ? h('span', { class: 'nd' + (p.late ? ' late' : '') }, p.short) : null, tagOf(p.area)],
        { 'aria-current': sel ? 'true' : null, 'aria-label': p.name + ', ' + PJ_STATUS[p.status] + (p.next_step ? ', next: ' + p.next_step : '') }, function () { pjOpen(p.id); }),
      projMenu(p)]);
  }
  function pjHead() {
    var d = PJ.d, c = d.counts, act = d.projects.filter(function (p) { return p.state === 'active' && p.set_status !== 'paused' && p.set_status !== 'done'; });
    var views = [['list', 'List', 'Projects'], ['timeline', 'Timeline', null], ['slipped', 'Slipped', 'Slipped'], ['review', 'Weekly review', 'Review']];
    return [h('div', { class: 'pjhd' }, [h('h2', { class: 'sec-h' }, ['Projects ', h('span', { class: 'n' }, plural(act.length, 'active', 'active') + (d.today_label ? ' · ' + d.today_label : ''))]),
      h('span', { class: 'sp' }), btn('tbtn', 'Settings', { 'aria-pressed': String(PJ.view === 'setup') }, function () { pjGo('setup'); })]),
    h('div', { class: 'live' }, [h('span', { class: 'livedot', 'aria-hidden': 'true' }), 'From your notes, mail, calendar' + (d.github && d.github.repos.length ? ' and GitHub' : '') + '.',
      d.draft ? h('span', null, ['Found ', h('b', null, String(d.draft.found)), ' ' + (d.draft.found === 1 ? 'project' : 'projects') + ' last time.']) : null]),
    h('div', { class: 'vnav', role: 'group', 'aria-label': 'Views' }, views.map(function (v) {
      var on = PJ.view === v[0];
      return h('button', { type: 'button', class: 'chip' + (v[2] ? '' : ' d-only'), 'aria-pressed': String(on), 'data-pjv': v[0] }, [
        h('span', { class: 'd-only' }, v[1]), h('span', { class: 'p-only' }, v[2] || v[1]),
        v[0] === 'slipped' && c.slipped ? h('span', { class: 'badge', 'aria-label': c.slipped + ' slipped' }, String(c.slipped)) : null,
        v[0] === 'review' && d.review.ready ? h('span', { class: 'badge', 'aria-label': 'ready' }, '1') : null]);
    }))];
  }
  function pjList() {
    var d = PJ.d, c = d.counts, top = d.slipped.filter(function (x) { return !x.done; })[0];
    var act = d.projects.filter(function (p) { return p.state === 'active' && p.set_status !== 'paused' && p.set_status !== 'done'; });
    var sug = d.projects.filter(function (p) { return p.state === 'suggested'; });
    var paused = d.projects.filter(function (p) { return p.state === 'active' && (p.set_status === 'paused' || p.set_status === 'done'); });
    var tools = h('div', { class: 'pjtools' }, [btn('act ghost', d.draft ? 'Look for projects again' : 'Find projects', null, function (e) { pjDo('POST', 'api/projects/build', {}, e.currentTarget); }),
      sug.length ? btn('act ghost', d.ai && d.ai.state === 'running' ? 'Asking the AI…' : 'Suggest names and next steps', { disabled: d.ai && d.ai.state === 'running' }, function (e) { pjDo('POST', 'api/projects/suggest', {}, e.currentTarget); }) : null,
      btn('act ghost', 'New project', null, function () { PJ.edit = 'new'; PJ.form = { name: '', area: '', status: 'on', next_step: '', next_due: '', people: '' }; PJ.open = null; renderBrief(); })]);
    var aiNote = d.ai && d.ai.state === 'error' ? h('p', { class: 'err', role: 'alert' }, d.ai.error) : d.ai && d.ai.state === 'done' ?
      h('p', { class: 'fhint' }, 'Suggestions by ' + (d.ai.by || 'the AI') + ': ' + plural(d.ai.count, 'project', 'projects') + (d.ai.dropped ? ', ' + d.ai.dropped + ' left out (they looked like instructions or links)' : '') + '. Nothing changes until you add it.') : null;
    return [c.slipped && top ? h('p', { class: 'slipsum' }, [kdot(top.kind), h('b', null, plural(c.slipped, 'thing slipped', 'things slipped')), h('span', { class: 'sp' }, ' · ' + top.title),
      btn('tbtn', 'See them', null, function () { pjGo('slipped'); })]) : null,
    tools, aiNote,
    h('h3', { class: 'grp-h' }, ['Projects ', h('span', { class: 'n' }, act.length + ' active · late first')]),
    act.length ? h('ul', { class: 'list' }, act.map(pjRow)) : h('p', { class: 'empty' }, sug.length ? 'Add a suggested project below, or make a new one.' : 'No projects yet. Find projects, or make a new one.'),
    sug.length ? [h('h3', { class: 'grp-h' }, ['Suggested ', h('span', { class: 'n' }, sug.length + ' · found in your notes and threads')]),
      h('ul', { class: 'list dashed' }, sug.map(pjRow))] : null,
    paused.length ? [h('h3', { class: 'grp-h' }, ['Paused ', h('span', { class: 'n' }, String(paused.length))]), h('ul', { class: 'list' }, paused.map(pjRow))] : null,
    d.not.length ? h('p', { class: 'fhint' }, ['Not projects: ', d.not.map(function (n, i) { return [i ? ', ' : '', btn('tbtn', n.name, { title: 'Bring it back' }, function (e) { pjDo('POST', 'api/projects/' + n.id + '/undo', {}, e.currentTarget); })]; })]) : null];
  }
  function pjField(label, key, attrs, full) {
    var a = attrs || {}, el;
    if (a.options) {
      el = h('select', { class: 'inp', id: 'pjf-' + key }, a.options.map(function (o) { return h('option', { value: o[0], selected: String(PJ.form[key] || '') === o[0] ? true : null }, o[1]); }));
    } else {
      el = h('input', { class: 'inp', id: 'pjf-' + key, type: a.type || 'text', maxlength: a.max || '160', placeholder: a.ph || null });
      el.value = PJ.form[key] || '';
    }
    on(el, 'input', function () { PJ.form[key] = el.value; }); on(el, 'change', function () { PJ.form[key] = el.value; });
    return h('div', { class: 'ef' + (full ? ' full' : '') }, [h('label', { for: 'pjf-' + key }, label), el]);
  }
  function pjEditForm(p) {
    var areas = [['', 'No area']].concat((B.areas || []).map(function (a) { return [a.id, a.name]; }));
    var stats = (PJ.d.statuses || []).map(function (s) { return s; });
    var body = function () {
      return { name: PJ.form.name, area: PJ.form.area || '', status: PJ.form.status || 'on', next_step: PJ.form.next_step || '', next_due: PJ.form.next_due || '',
        people: String(PJ.form.people || '').split(',').map(function (x) { return x.trim(); }).filter(Boolean) };
    };
    var save = p ? (p.state === 'suggested' ? 'Save and add project' : 'Save') : 'Add project';
    return h('div', { class: 'pjedit' }, [h('div', { class: 'efg' }, [pjField('Project', 'name', { max: '60' }, true), pjField('Area', 'area', { options: areas }), pjField('Status', 'status', { options: stats }),
      pjField('Next step', 'next_step', { max: '140' }, true), pjField('By', 'next_due', { type: 'date' }), pjField('People', 'people', { ph: 'Names, separated by commas' })]),
    h('div', { class: 'eacts' }, [btn('act', save, null, function (e) {
      var b = body();
      if (!p) pjDo('POST', 'api/projects', b, e.currentTarget, function () { PJ.edit = null; });
      else if (p.state === 'suggested') pjDo('POST', 'api/projects/' + p.id + '/accept', b, e.currentTarget, function () { PJ.edit = null; });
      else pjDo('PATCH', 'api/projects/' + p.id, b, e.currentTarget, function () { PJ.edit = null; });
    }), btn('tbtn', 'Cancel', null, function () { PJ.edit = null; renderBrief(); })])]);
  }
  function pjStartEdit(p) {
    var ai = p.ai || {};
    PJ.edit = p.id;
    PJ.form = { name: p.state === 'suggested' && ai.name && !ai.warning ? ai.name : p.name, area: p.area || '', status: p.set_status === 'paused' && p.state === 'suggested' ? 'on' : p.set_status,
      next_step: p.state === 'suggested' && ai.next_step && !ai.warning ? ai.next_step : p.next_step, next_due: p.next_due || '', people: (p.people || []).join(', ') };
    renderBrief();
  }
  function pjAsk(p) {
    clearTimeout(CH.timer); CH.timer = null;
    CH.conv = null; CH.cid = null; save(KEY_CHAT, ''); CH.input = ''; CH.err = ''; CH.earlier = false; CH.scroll = true;
    CH.scope = { project: p.id, name: p.name };
    setTab('chat');
  }
  function pjNotes(val, saveFn, label) {
    var ta = h('textarea', { class: 'inp', id: 'pj-notes', rows: '3', maxlength: '2000', 'aria-label': label || 'Notes' });
    ta.value = val || '';
    return [h('h3', { class: 'plab' }, 'Notes'), ta, h('div', { class: 'eacts' }, btn('tbtn', 'Save notes', null, function (e) { saveFn(ta.value, e.currentTarget); }))];
  }
  function pjPanel(p) {
    var sug = p.state === 'suggested', ai = p.ai || null;
    if (PJ.edit === p.id) return [h('div', { class: 'phd' }, [h('h2', { id: 'pj-ptitle' }, sug ? 'Edit, then add' : 'Edit project')]), pjEditForm(p)];
    return [h('div', { class: 'phd' }, [h('h2', { id: 'pj-ptitle' }, p.name), h('div', { class: 'pmeta' }, [h('span', { class: 'pjst' }, [sdot(p.status), PJ_STATUS[p.status]]), tagOf(p.area),
      p.last_label ? h('span', null, 'Last activity ' + p.last_label) : null])]),
    sug ? h('div', { class: 'sugbox' }, [h('span', { class: 'label' }, 'Suggested by Callsheet, because of'), h('ul', null, p.why.map(function (w) { return h('li', null, w); })),
      ai ? h('p', { class: 'aisug' }, ['The AI suggests: ', h('b', null, ai.name || p.name), ai.next_step ? ' · Next: ' + ai.next_step : '']) : null,
      ai && ai.warning ? h('p', { class: 'pflag claim' }, ai.warning) : null,
      h('div', { class: 'dbtns' }, [btn('act', 'Add project', null, function (e) { pjDo('POST', 'api/projects/' + p.id + '/accept', ai && !ai.warning ? { use_ai: true } : {}, e.currentTarget); }),
        btn('act ghost', 'Edit first', null, function () { pjStartEdit(p); }),
        btn('tbtn', 'Not a project', null, function (e) { pjDo('POST', 'api/projects/' + p.id + '/not', {}, e.currentTarget, function () { PJ.open = null; }); })])]) : null,
    h('div', { class: 'nextb' }, [h('span', { class: 'label' }, 'Next step'), h('span', { class: 'what' }, p.next_step || 'No next step yet'),
      p.next_label ? h('span', { class: 'when' + (p.late ? ' late' : '') }, p.next_label) : null]),
    p.milestones.length ? [h('h3', { class: 'plab' }, 'Dates'), h('ul', { class: 'mst' }, p.milestones.map(function (m) {
      return h('li', { class: m.state || null }, [h('span', { class: 'd' }, m.label), h('span', { class: 'm', 'aria-hidden': 'true' }), h('span', { class: 'w' }, [m.title,
        m.state === 'missed' ? h('span', { class: 's' }, 'Passed') : m.state === 'next' ? h('span', { class: 's' }, 'Next') : m.state === 'done' ? h('span', { class: 's' }, 'Done') : null])]);
    }))] : null,
    h('h3', { class: 'plab' }, 'Tasks'),
    p.tasks.length ? h('ul', { class: 'tlist list' }, p.tasks.map(function (t) {
      return h('li', { class: 'row' + (t.done ? ' isdone' : '') }, [btn('ck', h('span', { class: 'bx' }), { 'aria-pressed': String(t.done), 'aria-label': (t.done ? 'Done: ' : 'Mark done: ') + t.text },
        function (e) { pjDo('PATCH', 'api/projects/' + p.id + '/tasks/' + t.id, { done: !t.done }, e.currentTarget); }),
      h('span', { class: 'rmain' }, [h('span', { class: 'tx' }, h('span', { class: 'sum' }, t.text)), t.due_label ? h('span', { class: 'nd' + (t.late ? ' late' : '') }, t.due_label) : null]),
      pjMenu('t:' + t.id, [['Add a note for Callsheet', function () { PJ.menu = null; PJ.focusNotes = true; renderBrief(); }],
        ['Dismiss', function (e) { pjDo('DELETE', 'api/projects/' + p.id + '/tasks/' + t.id, undefined, e.currentTarget); }]])]);
    })) : h('p', { class: 'fhint' }, 'No tasks yet.'),
    p.suggested_tasks.length ? h('div', { class: 'sugbox small' }, [h('span', { class: 'label' }, 'Found in your note'), h('ul', null, p.suggested_tasks.map(function (t) {
      return h('li', null, [t.text + (t.due_label ? ' · ' + t.due_label : ''), ' ', btn('tbtn', 'Add', { 'aria-label': 'Add task: ' + t.text }, function (e) { pjDo('POST', 'api/projects/' + p.id + '/tasks', { skey: t.skey }, e.currentTarget); })]);
    }))]) : null,
    p.state === 'active' ? pjAddTask(p) : null,
    p.people.length ? [h('h3', { class: 'plab' }, 'People'), h('div', { class: 'ppl' }, p.people.map(function (n) { return h('span', { class: 'pchip' }, n); }))] : null,
    p.chips.length ? [h('h3', { class: 'plab' }, 'From'), h('div', { class: 'srcl' }, p.chips.map(function (c) {
      var kids = [ico(SRC_ICON[c.source] || 'note'), h('span', null, c.title), h('small', null, c.small)];
      var u = safeHttps(c.url);
      return u ? h('a', { class: 'schip', href: u, target: '_blank', rel: 'noopener noreferrer' }, kids) : h('span', { class: 'schip' }, kids);
    }))] : null,
    p.last_week && p.last_week.length ? [h('h3', { class: 'plab' }, 'Last week'), h('ul', { class: 'lastwk' }, p.last_week.map(function (l) {
      return h('li', null, [h('span', { class: 'ri' }, ico(SRC_ICON[l.source] || 'note')), l.text]);
    }))] : null,
    h('div', { class: 'eacts' }, [p.state === 'active' ? btn('act', [ico('chat'), 'Ask about this project'], null, function () { pjAsk(p); }) : null,
      btn('act ghost', 'Edit', null, function () { pjStartEdit(p); })]),
    p.state === 'active' ? pjNotes(p.notes, function (v, b) { pjDo('PATCH', 'api/projects/' + p.id, { notes: v }, b); }) : null];
  }
  function pjAddTask(p) {
    var inp = h('input', { class: 'inp', id: 'pj-task', type: 'text', maxlength: '160', placeholder: 'A task for this project' });
    var due = h('input', { class: 'inp', id: 'pj-task-due', type: 'date', 'aria-label': 'By' });
    inp.value = PJ.form.task || ''; on(inp, 'input', function () { PJ.form.task = inp.value; });
    return h('div', { class: 'addt' }, [h('label', { class: 'sr', for: 'pj-task' }, 'New task'), inp, due,
      btn('act ghost', 'Add a task', null, function (e) { pjDo('POST', 'api/projects/' + p.id + '/tasks', { text: inp.value, due: due.value || '' }, e.currentTarget, function () { PJ.form.task = ''; }); })]);
  }
  function slipRow(x) {
    var sel = PJ.open === 'slip:' + x.key;
    return h('li', { class: 'row pjrow' + (sel ? ' sel' : '') + (x.done ? ' isdone' : '') }, [h('span', { class: 'scell', title: PJ_KIND[x.kind] }, kdot(x.kind)),
      btn('rmain', [h('span', { class: 'tx' }, [h('span', { class: 'sum' }, x.title)]), h('span', { class: 'nd' + (x.kind === 'late' ? ' late' : '') }, x.ago), tagOf(x.area)],
        { 'aria-current': sel ? 'true' : null, 'aria-label': PJ_KIND[x.kind] + ': ' + x.title + ', ' + x.ago }, function () { pjOpen('slip:' + x.key); }),
      slipMenu(x)]);
  }
  function pjSlipped() {
    var d = PJ.d, xs = d.slipped;
    var groups = [['late', 'Date passed'], ['quiet', 'Gone quiet'], ['owe', 'You owe a reply']];
    return [h('div', { class: 'legend' }, groups.map(function (g) { return h('span', null, [kdot(g[0]), g[1]]); })),
      xs.length ? groups.map(function (g) {
        var these = xs.filter(function (x) { return x.kind === g[0]; });
        return these.length ? [h('h3', { class: 'grp-h' }, [g[1] + ' ', h('span', { class: 'n' }, String(these.length))]), h('ul', { class: 'list' }, these.map(slipRow))] : null;
      }) : h('p', { class: 'empty' }, 'Nothing slipped. Callsheet looks again when your mail, chat and notes update.'),
      h('p', { class: 'fhint' }, 'Quiet after ' + d.settings.quiet_days + ' days; a reply you owe after ' + plural(d.settings.owe_days, 'day', 'days') + '. Change it in Settings.')];
  }
  function slipPanel(x) {
    var proj = x.project ? pjById(x.project) : null, u = safeHttps(x.url);
    return [h('div', { class: 'phd' }, [h('h2', { id: 'pj-ptitle' }, x.title), h('div', { class: 'pmeta' }, [h('span', { class: 'pjst' }, [kdot(x.kind), PJ_KIND[x.kind]]), h('span', null, x.ago), tagOf(x.area)])]),
      x.quote ? [h('h3', { class: 'plab' }, 'Exact words'), h('blockquote', { class: 'quote' }, x.quote), h('p', { class: 'fhint' }, (x.qs || '') + ' · quoted exactly')]
        : [h('h3', { class: 'plab' }, 'What Callsheet saw'), h('p', null, x.seen || '')],
      proj ? [h('h3', { class: 'plab' }, 'Project'), btn('pchip', [sdot(proj.status), proj.name], null, function () { PJ.view = 'list'; pjOpen(proj.id); })] : null,
      h('div', { class: 'eacts' }, [x.done ? btn('act ghost', 'Reopen', null, function (e) { pjDo('POST', 'api/projects/slipped/' + x.key, { action: 'reopen' }, e.currentTarget); })
        : btn('act', 'Mark done', null, function (e) { pjDo('POST', 'api/projects/slipped/' + x.key, { action: 'done' }, e.currentTarget); }),
      u ? h('a', { class: 'act ghost', href: u, target: '_blank', rel: 'noopener noreferrer' }, 'Open in ' + (x.open || 'its app')) : null]),
      pjNotes(x.note, function (v, b) { pjDo('POST', 'api/projects/slipped/' + x.key, { action: 'note', note: v }, b); }, 'A note about this')];
  }
  function pjDay(iso) { var p = String(iso || '').split('-'); return p.length === 3 ? Date.UTC(+p[0], +p[1] - 1, +p[2]) / 864e5 : null; }
  function pjTimeline() {
    var d = PJ.d, tl = d.timeline, start = pjDay(tl.start), act = d.projects.filter(function (p) { return p.state === 'active' && p.set_status !== 'done'; });
    function pos(iso) { var x = pjDay(iso); return x == null ? null : (x - start) / tl.days * 100; }
    function inside(v) { return v != null && v >= 0 && v <= 100; }
    var rows = act.map(function (p) {
      var ms = p.milestones.filter(function (m) { return inside(pos(m.date)); });
      var ps = ms.map(function (m) { return pos(m.date); }), lo = ps.length ? Math.min.apply(null, ps) : null, hi = ps.length ? Math.max.apply(null, ps) : null;
      var sel = PJ.open === p.id;
      return h('div', { class: 'gt-r ' + p.status + (sel ? ' sel' : '') }, [btn('gt-l', [sdot(p.status), h('span', null, p.name)], { 'aria-label': p.name + ', ' + PJ_STATUS[p.status] }, function () { pjOpen(p.id); }),
        h('div', { class: 'trk' }, [lo != null && hi > lo ? h('span', { class: 'gbar' + (p.set_status === 'paused' ? ' dash' : ''), style: 'left:' + lo.toFixed(2) + '%;width:' + (hi - lo).toFixed(2) + '%' }) : null,
          ms.map(function (m) {
            return h('span', { class: 'gms ' + (m.state || ''), style: 'left:' + pos(m.date).toFixed(2) + '%', title: m.label + ': ' + m.title, role: 'img', 'aria-label': m.label + ': ' + m.title + (m.state === 'missed' ? ' (passed)' : '') });
          })])]);
    });
    var nowLeft = (tl.today + 0.5) / tl.days * 100;
    return [h('h3', { class: 'grp-h' }, ['Next six weeks ', h('span', { class: 'n' }, 'From your notes, mail and calendar')]),
      h('div', { class: 'legend' }, [h('span', null, [sdot('on'), 'On track']), h('span', null, [sdot('behind'), 'Behind']), h('span', null, [sdot('wait'), 'Waiting']),
        h('span', null, [sdot('paused'), 'Paused']), h('span', null, [h('span', { class: 'gms lg' }), 'A date']), h('span', null, [h('span', { class: 'gms lg missed' }), 'Missed'])]),
      act.length ? h('div', { class: 'gt' }, [h('div', { class: 'gt-r h' }, [h('span', { class: 'gt-l' }, ''), h('div', { class: 'trk' }, [
        h('div', { class: 'wkh' }, tl.weeks.map(function (w, i) { return h('span', null, i === tl.this_week ? 'This week · ' + w : w); })),
        h('span', { class: 'gnow', style: 'left:' + nowLeft.toFixed(2) + '%', 'aria-hidden': 'true' })])]),
      rows.map(function (r) { add(r.querySelector('.trk'), h('span', { class: 'gnow', style: 'left:' + nowLeft.toFixed(2) + '%', 'aria-hidden': 'true' })); return r; })])
        : h('p', { class: 'empty' }, 'Add a project to see its dates here.'),
      h('p', { class: 'fhint' }, 'Red line: today, ' + d.today_label + '. The week before this one shows the dates you missed.')];
  }
  function pjReview() {
    var d = PJ.d, rv = d.review, order = rv.order.map(pjById).filter(Boolean), R = PJ.rv;
    var steps = [['slipped', 'Slipped'], ['projects', 'Projects'], ['next', 'Next week'], ['done', 'Done']];
    var idx = steps.map(function (s) { return s[0]; }).indexOf(R.step);
    function go(step, i) { PJ.rv = { step: step, i: i || 0 }; renderBrief(); }
    var stepsEl = h('ol', { class: 'pjsteps', 'aria-label': 'Review steps' }, steps.map(function (s, i) {
      return h('li', { class: i < idx ? 'done' : i === idx ? 'on' : null, 'aria-current': i === idx ? 'step' : null }, [h('span', { class: 'num' }, String(i + 1)), s[1]]);
    }));
    var body;
    if (R.step === 'slipped') {
      var xs = d.slipped.filter(function (x) { return !x.done; });
      body = [h('h3', { class: 'rvq' }, xs.length ? plural(xs.length, 'thing slipped', 'things slipped') + ' since last week' : 'Nothing slipped.'),
        xs.length ? h('ul', { class: 'list' }, xs.map(function (x) {
          function mark(a) { return function (e) { pjDo('POST', 'api/projects/slipped/' + x.key, { action: a }, e.currentTarget); }; }
          return h('li', { class: 'row pjrow rvslip' }, [h('span', { class: 'scell' }, kdot(x.kind)), h('div', { class: 'rmain' }, [h('span', { class: 'tx' }, [h('span', { class: 'sum' }, x.title)]), h('span', { class: 'nd' }, x.ago)]),
            h('div', { class: 'rvacts' }, [btn('tbtn', 'Done', null, mark('done')), btn('tbtn', 'It’s fine', null, mark('fine')), btn('tbtn', 'Next week', null, mark('snooze'))])]);
        })) : null,
        h('div', { class: 'qnav' }, [h('span', { class: 'sp' }), btn('act', 'Next', null, function () { go(order.length ? 'projects' : 'next'); })])];
    } else if (R.step === 'projects' && order.length) {
      var i = Math.min(R.i, order.length - 1), p = order[i], f = PJ.form.rv && PJ.form.rv.id === p.id ? PJ.form.rv : (PJ.form.rv = { id: p.id, status: p.set_status, next_step: p.next_step, next_due: p.next_due });
      var nx = h('input', { class: 'inp', id: 'rv-next', type: 'text', maxlength: '140' }); nx.value = f.next_step || ''; on(nx, 'input', function () { f.next_step = nx.value; });
      var by = h('input', { class: 'inp', id: 'rv-by', type: 'date' }); by.value = f.next_due || ''; on(by, 'change', function () { f.next_due = by.value; });
      var last = i === order.length - 1;
      var next = function (skip) {
        return function (e) {
          var b = { action: 'answer', project_id: p.id };
          if (skip) b.skip = true; else { b.status = f.status; b.next_step = f.next_step || ''; b.next_due = f.next_due || ''; }
          pjDo('POST', 'api/projects/review', b, e.currentTarget, function () { PJ.form.rv = null; PJ.rv = last ? { step: 'next', i: 0 } : { step: 'projects', i: i + 1 }; });
        };
      };
      body = [h('p', { class: 'qmeta' }, 'Project ' + (i + 1) + ' of ' + order.length), h('h3', { class: 'rvq' }, p.name + ': how is it going?'),
        p.last_week && p.last_week.length ? [h('span', { class: 'label' }, 'Last week'), h('ul', { class: 'lastwk' }, p.last_week.map(function (l) { return h('li', null, [h('span', { class: 'ri' }, ico(SRC_ICON[l.source] || 'note')), l.text]); }))]
          : h('p', { class: 'fhint' }, 'Nothing new from its sources last week.'),
        h('div', { class: 'picks row5', role: 'radiogroup', 'aria-label': 'Status' }, (d.statuses || []).map(function (s) {
          return h('button', { type: 'button', class: 'pick', role: 'radio', 'aria-checked': String(f.status === s[0]), 'data-rvst': s[0] }, [sdot(s[0]), s[1]]);
        })),
        h('div', { class: 'efg' }, [h('div', { class: 'ef full' }, [h('label', { for: 'rv-next' }, 'Next step'), nx]), h('div', { class: 'ef' }, [h('label', { for: 'rv-by' }, 'By'), by])]),
        h('div', { class: 'qnav' }, [btn('tbtn', 'Back', null, function () { if (i) go('projects', i - 1); else go('slipped'); }), h('span', { class: 'sp' }),
          btn('act ghost', 'Skip', null, next(true)), btn('act', last ? 'Next' : 'Next project', null, next(false))])];
    } else if (R.step === 'next' || R.step === 'projects') {
      body = [h('h3', { class: 'rvq' }, 'Next week'), rv.next_week.length ? h('ul', { class: 'mst' }, rv.next_week.map(function (m) {
        return h('li', null, [h('span', { class: 'd' }, m.label), h('span', { class: 'm', 'aria-hidden': 'true' }), h('span', { class: 'w' }, [m.title, h('span', { class: 's' }, m.project)])]);
      })) : h('p', { class: 'fhint' }, 'No dates next week.'),
      h('div', { class: 'qnav' }, [btn('tbtn', 'Back', null, function () { go(order.length ? 'projects' : 'slipped', Math.max(0, order.length - 1)); }), h('span', { class: 'sp' }),
        btn('act', 'Finish the review', null, function (e) { pjDo('POST', 'api/projects/review', { action: 'done' }, e.currentTarget, function () { PJ.rv = { step: 'done', i: 0 }; }); })])];
    } else {
      body = [h('h3', { class: 'rvq' }, "That's every project."), h('p', null, 'Your answers are saved in the projects. Nothing was sent.'),
        h('div', { class: 'qnav' }, [btn('tbtn', 'Start again', null, function (e) { pjDo('POST', 'api/projects/review', { action: 'start' }, e.currentTarget, function () { PJ.rv = { step: 'slipped', i: 0 }; }); }), h('span', { class: 'sp' }),
          btn('act', 'Back to the list', null, function () { pjGo('list'); })])];
    }
    var cur = R.step === 'projects' && order.length ? order[Math.min(R.i, order.length - 1)] : null;
    var rail = h('aside', { class: 'pjrail', 'aria-label': 'This review' }, [h('h3', { class: 'plab' }, 'This review'),
      h('ul', null, order.map(function (p, i) {
        var a = rv.answers[p.id];
        return h('li', { class: cur && cur.id === p.id ? 'on' : null }, [sdot(p.status), h('span', { class: 'nm' }, p.name),
          cur && cur.id === p.id ? h('span', { class: 'ans' }, 'Reviewing now') : a ? [h('span', { class: 'ans' }, a), btn('tbtn', 'Change', { 'aria-label': 'Change ' + p.name }, function () { go('projects', i); })] : null]);
      })), h('p', { class: 'fhint' }, PJ_REVIEW_WHEN[rv.setting] + ', or start it any time. Answers update the projects; nothing is sent.')]);
    return h('div', { class: 'pjrv' }, [h('div', { class: 'pjrv-main' }, [stepsEl, body]), rail]);
  }
  function pjSeg(label, key, opts, cur, fmt) {
    return h('div', { class: 'setrow' }, [h('span', { class: 'sl' }, label), h('div', { class: 'seg', role: 'radiogroup', 'aria-label': label }, opts.map(function (o) {
      var v = Array.isArray(o) ? o[0] : o, t = Array.isArray(o) ? o[1] : fmt(o);
      return h('button', { type: 'button', role: 'radio', 'aria-checked': String(cur === v), 'data-pjset': key, 'data-v': String(v) }, t);
    }))]);
  }
  function pjSetup() {
    var d = PJ.d, s = d.settings, gh = d.github, G = PJ.gh;
    var finds = [['notes', 'Obsidian notes'], ['calendar', 'Calendar series'], ['threads', 'Recurring threads'], ['github', 'GitHub repos']];
    var tok = h('input', { class: 'inp', id: 'gh-token', type: 'password', autocomplete: 'off', spellcheck: 'false', placeholder: 'github_pat_…' });
    var ghEl = !gh.token_set ? [h('p', { class: 'fhint' }, 'Read-only. On GitHub, make a fine-grained token with Contents, Issues and Pull requests set to Read, for the repositories you want. Callsheet only reads; it never writes to GitHub.'),
      h('div', { class: 'addt' }, [h('label', { class: 'sr', for: 'gh-token' }, 'GitHub token'), tok, btn('act', 'Save token', null, function (e) {
        var b = e.currentTarget; b.disabled = true;
        api('PUT', 'api/github/token', { token: tok.value }).then(function () { tok.value = ''; G.err = ''; PJ.loading = false; pjLoad(); }, function (er) { b.disabled = false; G.err = er.message; renderBrief(); });
      })])]
      : [h('p', null, ['Token saved ', h('span', { class: 'muted' }, '(hidden; Callsheet never shows it)'), '. Following ' + plural(gh.repos.length, 'repository', 'repositories') + (gh.repos.length ? ': ' + gh.repos.join(', ') : '') + '.']),
        G.repos ? h('div', { class: 'chips', role: 'group', 'aria-label': 'Repositories' }, G.repos.map(function (r) {
          var onp = G.pick.indexOf(r.name) >= 0;
          return h('button', { type: 'button', class: 'chip', 'aria-pressed': String(onp), 'data-ghr': r.name }, r.name + (r.private ? ' · private' : ''));
        })) : null,
        h('div', { class: 'eacts' }, [G.repos ? btn('act', 'Save repositories', null, function (e) {
          var b = e.currentTarget; b.disabled = true;
          api('PUT', 'api/github/repos', { repos: G.pick }).then(function () { G.repos = null; G.err = ''; PJ.loading = false; pjLoad(); }, function (er) { b.disabled = false; G.err = er.message; renderBrief(); });
        }) : btn('act ghost', 'Pick repositories', null, function (e) {
          var b = e.currentTarget; b.disabled = true;
          api('POST', 'api/github/list', {}).then(function (r) { G.repos = r.repos; G.pick = r.following.slice(); G.err = ''; renderBrief(); }, function (er) { b.disabled = false; G.err = er.message; renderBrief(); });
        }), btn('tbtn', 'Forget GitHub', null, function (e) {
          var b = e.currentTarget; b.disabled = true;
          api('DELETE', 'api/github').then(function () { G.repos = null; PJ.loading = false; pjLoad(); }, function (er) { b.disabled = false; G.err = er.message; renderBrief(); });
        })]),
        h('p', { class: 'fhint' }, 'New repositories are read at the next index update (Setup, Memory, Get to know me).')];
    return [h('h3', { class: 'grp-h' }, 'Find projects in'), h('div', { class: 'chips', role: 'group', 'aria-label': 'Find projects in' }, finds.map(function (f) {
      return h('button', { type: 'button', class: 'chip', 'aria-pressed': String(!!s.find[f[0]]), 'data-pjfind': f[0] }, f[1]);
    })), h('p', { class: 'fhint' }, 'New projects always arrive as Suggested. Nothing becomes a project until you add it.'),
    h('h3', { class: 'grp-h' }, 'Remind me when'),
    h('div', { class: 'setrow' }, [h('span', { class: 'sl' }, 'A date passes'), h('span', { class: 'muted' }, 'Always')]),
    pjSeg('A thread goes quiet for', 'quiet_days', d.quiet_choices, s.quiet_days, function (n) { return n + ' days'; }),
    pjSeg('I owe a reply for', 'owe_days', d.owe_choices, s.owe_days, function (n) { return plural(n, 'day', 'days'); }),
    h('h3', { class: 'grp-h' }, 'Weekly review'), pjSeg('Weekly review', 'review', d.reviews, s.review),
    h('p', { class: 'fhint' }, 'A phone notification when it’s ready, if phone alerts are on.'),
    h('h3', { class: 'grp-h' }, ['GitHub ', h('span', { class: 'n' }, 'read-only')]), ghEl, G.err ? h('p', { class: 'err', role: 'alert' }, G.err) : null,
    h('div', { class: 'optbox locked' }, [h('span', { class: 'label' }, [ico('lock'), ' What stays where']), h('ul', null, [
      h('li', null, 'Projects, tasks and notes live in Callsheet only.'),
      h('li', null, 'Obsidian, mail, Slack, Signal, calendars and GitHub are read, never changed.'),
      h('li', null, 'The weekly review sends one phone notification with counts only.')])])];
  }
  function T_projects() {
    if (!PJ.d && !PJ.loading && !PJ.err) pjLoad();
    if (!PJ.d) return [h('h2', { class: 'sec-h' }, 'Projects'), PJ.err ? h('p', { class: 'err', role: 'alert' }, PJ.err) : h('p', { class: 'status', role: 'status' }, 'Loading projects…')];
    var content = PJ.view === 'timeline' ? pjTimeline() : PJ.view === 'slipped' ? pjSlipped() : PJ.view === 'review' ? pjReview() : PJ.view === 'setup' ? pjSetup() : pjList();
    var open = null;
    if (PJ.edit === 'new') open = [h('div', { class: 'phd' }, h('h2', { id: 'pj-ptitle' }, 'New project')), pjEditForm(null)];
    else if (PJ.open && PJ.open.indexOf('slip:') === 0) { var x = pjSlip(PJ.open.slice(5)); open = x ? slipPanel(x) : null; }
    else if (PJ.open) { var p = pjById(PJ.open); open = p ? pjPanel(p) : null; }
    var panel = open ? h('aside', { class: 'pjpanel', 'aria-labelledby': 'pj-ptitle' }, [btn('pclose', ico('close', 'Close'), { 'data-pjclose': '1' }, function () { PJ.open = null; PJ.edit = null; renderBrief(); }), open]) : null;
    if (PJ.focusNotes) { PJ.focusNotes = false; setTimeout(function () { var n = document.getElementById('pj-notes'); if (n) n.focus(); }, 0); }
    var phone = isPhone();
    return [h('div', { class: 'pj' + (panel ? ' open' : '') }, [phone && panel ? null : h('div', { class: 'pjmain' }, [pjHead(), PJ.err ? h('p', { class: 'err', role: 'alert' }, PJ.err) : null, content]), panel])];
  }
  /* view chips, setting segments and repo chips (delegated: these buttons carry data-*) */
  document.addEventListener('click', function (e) {
    var t = e.target.closest ? e.target.closest('button') : null;
    if (!t || ui.view !== 'brief' || U.tab !== 'projects') return;
    var dv = t.dataset;
    if (dv.pjv) { if (dv.pjv === 'review' && PJ.view !== 'review') PJ.rv = { step: 'slipped', i: 0 }; pjGo(dv.pjv); }
    else if (dv.pjset) { var v = dv.pjset === 'review' ? dv.v : parseInt(dv.v, 10), b = {}; b[dv.pjset] = v; pjDo('PATCH', 'api/projects/settings', b, t); }
    else if (dv.pjfind) { var f = {}; f[dv.pjfind] = t.getAttribute('aria-pressed') !== 'true'; pjDo('PATCH', 'api/projects/settings', { find: f }, t); }
    else if (dv.rvst) { if (PJ.form.rv) PJ.form.rv.status = dv.rvst; renderBrief(); }
    else if (dv.ghr) { var i = PJ.gh.pick.indexOf(dv.ghr); if (i >= 0) PJ.gh.pick.splice(i, 1); else PJ.gh.pick.push(dv.ghr); renderBrief(); }
  });
  /* the Briefing's Slipped line and the weekly review card */
  function briefProjects() {
    var bp = B.projects;
    if (!bp) return null;
    var line = bp.slipped && bp.top ? h('p', { class: 'slipsum brief-slip' }, [kdot(bp.top.kind), h('b', null, plural(bp.slipped, 'thing slipped', 'things slipped')), h('span', { class: 'sp' }, ' · ' + bp.top.title),
      btn('tbtn', 'See them', null, function () { PJ.view = 'slipped'; PJ.open = null; PJ.d = null; setTab('projects'); })]) : null;
    var card = bp.review && bp.review.ready ? h('div', { class: 'rvcard' }, [h('h3', null, 'Weekly review'),
      h('p', null, plural(bp.review.projects, 'project', 'projects') + ' to look at' + (bp.slipped ? ', ' + bp.slipped + ' slipped' : '') + '. About five minutes.'),
      btn('act', 'Start the review', null, function () { PJ.view = 'review'; PJ.rv = { step: 'slipped', i: 0 }; PJ.open = null; PJ.d = null; setTab('projects'); })]) : null;
    return line || card ? h('div', { class: 'bproj' }, [line, card]) : null;
  }
  /* 0.7.0: Chat about one project ("Ask about this project"): the server filters the existing read-only tools */
  function chatScope() { return CH.conv ? (CH.conv.scope || null) : (CH.scope || null); }
  function chatScopeRow() {
    var sc = chatScope();
    if (!sc) return null;
    return h('div', { class: 'cscope' }, [h('span', { class: 'cchip' }, ['Only about ', h('b', null, sc.name),
      btn('cx', '×', { 'aria-label': 'Stop: ask about everything' }, function () { chatNew(); })]),
    btn('tbtn', 'Back to project', null, function () { PJ.view = 'list'; PJ.open = sc.project; PJ.edit = null; setTab('projects'); })]);
  }
  var TFN = { briefing: T_briefing, replies: T_replies, agenda: T_agenda, chat: T_chat, projects: T_projects };  /* 0.6.0 chat, 0.7.0 projects */

  /* ----- detail panel ----- */
  function panelReply(r) {
    var rk = reasonOf(r), ds = (r.gist && Array.isArray(r.gist.details)) ? r.gist.details : [];
    var sug = typeof r.suggestion === 'string' && r.suggestion ? r.suggestion : null, link = safeHttps(r.link);
    var ta = null;
    if (sug) { ta = h('textarea', { class: 'inp draft', id: 'bv-draft', 'data-draft': r.id, rows: '4' }); ta.value = U.drafts[r.id] != null ? U.drafts[r.id] : sug; }
    return [h('div', { class: 'phd' }, [h('h2', { id: 'bv-ptitle' }, r.who), h('div', { class: 'pmeta' }, [tagOf(r.area),
      h('span', { class: 's' }, [ico(srcKind(r)), r.source || srcName(r)]), ageOf(r) ? h('span', null, ageOf(r)) : null,
      rk ? h('span', { class: 's' }, [rdot(r), rk[1]]) : null, r.new && B.previous_at ? h('span', { class: 's' }, [newDot(r), 'New']) : null])]),
    r.claim === true ? h('p', { class: 'pflag claim' }, CLAIM_NOTE) : null,
    h('h3', { class: 'plab' }, 'What they want'), h('p', { class: 'what' }, gistSum(r)), checkLabel('p', gistCheck(r)),
    ds.length ? [h('h3', { class: 'plab' }, 'Exact details'), h('div', { class: 'facts' }, ds.map(function (d) {
      return h('span', { class: 'fact' }, [h('b', null, String(d.label || '') + ':'), String(d.value || '')]);
    })), h('div', { class: 'fhint' }, 'Quoted exactly from the message.')] : null,
    sug ? [h('h3', { class: 'plab' }, h('label', { for: 'bv-draft' }, 'Suggested reply')),
      typeof r.suggestion_warning === 'string' && r.suggestion_warning ? h('p', { class: 'err sugwarn' }, r.suggestion_warning) : null,
      ta, h('div', { class: 'dbtns' }, [h('button', { type: 'button', class: 'act', 'data-copy': r.id }, 'Copy'),
        h('span', { class: 'hint' }, 'Edit it here. Nothing is sent for you.')])] : hiddenLine(r) ? h('p', { class: 'pflag sughide' }, hiddenLine(r)) : null,
    h('div', { class: 'pacts' }, [
      link ? h('a', { class: 'act', href: link, target: '_blank', rel: 'noopener noreferrer' }, 'Open in ' + srcName(r)) : null,
      h('button', { type: 'button', class: 'act ghost', 'data-markdone': r.id }, 'Mark done'),
      npBtn('reply', r.id, 'pa-' + r.id, true)])];
  }
  function panelEvent(e) {
    var fl = e.flag && e.flag.text ? e.flag.text : null;
    return [h('div', { class: 'phd' }, [h('h2', { id: 'bv-ptitle' }, e.title), h('div', { class: 'pmeta' }, [tagOf(e.area),
      h('span', { class: 's' }, [ico('cal'), 'Calendar']), h('span', null, (e.time || '') + (e.end ? '–' + e.end : ''))])]),
    fl ? h('p', { class: 'pflag' }, fl) : null,
    e.note ? [h('h3', { class: 'plab' }, 'Note'), h('p', { class: 'what' }, e.note)] : null,
    h('div', { class: 'pacts' }, [npBtn('event', e.id, 'pe-' + e.id, true)])];
  }
  function panelEl() {
    if (!U.open) return null;
    if (U.open.kind === 'item') return itemPanel();  /* 0.6.0 chat: a source chip's item */
    var r = U.open.kind === 'reply' ? replyById(U.open.id) : eventById(U.open.id);
    if (!r) { U.open = null; return null; }
    var phone = isPhone(), isEv = U.open.kind === 'event';
    var closeB = h('button', { type: 'button', class: 'xbtn', 'data-close': '1' }, phone ? [ico('back'), 'Back'] : [ico('close'), 'Close']);
    var attrs = { class: 'panel', 'aria-labelledby': 'bv-ptitle' };
    if (phone) { attrs.role = 'dialog'; attrs['aria-modal'] = 'true'; }
    return h('aside', attrs, h('div', { class: 'panel-in' }, [
      h('div', { class: 'ptop' }, [phone ? closeB : h('span', { class: 'label lbl' }, isEv ? 'Event' : 'Message'), h('span', { class: 'sp' }),
        phone ? null : closeB]),
      h('div', { class: 'pbody' }, isEv ? panelEvent(r) : panelReply(r))]));
  }

  /* ----- notepad menu and toast (in a layer on .app, outside main) ----- */
  var layer = h('div', { class: 'bv-layer' });
  app.appendChild(layer);
  function clearLayer() { layer.textContent = ''; }
  function popEl() {
    var P = U.pop;
    if (!P) return null;
    if (P.mode === 'note') return notePop(P);
    var items = [noteItem(P)];
    if (P.kind === 'reply') {
      var r = replyById(P.id);
      if (!r) { U.pop = null; return null; }
      items.push(h('button', { type: 'button', 'data-pnnr': '1', 'aria-expanded': String(!!P.sub) }, 'Doesn’t need a reply ' + (P.sub ? '▾' : '▸')));
      if (P.sub) {
        var o = [['thread', 'Just this one', srcKind(r) === 'slack' ? 'Hide this thread' : 'Hide this conversation'],
          ['sender', 'Never for this sender', 'Hide everything from ' + r.who]];
        if (srcKind(r) === 'mail') o.push(['domain', 'Never for their domain', 'Hide everything from that domain']);
        items.push(h('div', { class: 'sub' }, o.map(function (x) {
          return h('button', { type: 'button', 'data-sup': x[0] }, [x[1], h('span', { class: 's' }, x[2])]);
        })));
      }
    } else {
      items.push(h('button', { type: 'button', 'data-dismiss': '1' }, ['Dismiss', h('span', { class: 's' }, 'On this device, until tomorrow')]));
    }
    return h('div', { class: 'pop', role: 'group', 'aria-label': 'Note and dismiss', 'data-pop': '1' }, items);
  }
  function placePop() {
    var el = layer.querySelector('[data-pop]');
    if (!el || !U.pop) return;
    var a = document.querySelector('[data-anc="' + cssq(U.pop.anc) + '"]');
    if (!a) { U.pop = null; el.remove(); return; }
    var ar = a.getBoundingClientRect(), pr = app.getBoundingClientRect(), w = el.offsetWidth, vw = document.documentElement.clientWidth;
    var left = Math.max(4, Math.min(ar.right - w, vw - w - 4)) - pr.left;
    var topY = ar.bottom + 4;
    if (topY + el.offsetHeight > window.innerHeight - 4 && ar.top - el.offsetHeight - 4 > 0) topY = ar.top - el.offsetHeight - 4;
    el.style.left = left + 'px'; el.style.top = (topY - pr.top) + 'px';
  }
  function toastEl() {
    var T = U.toast;
    if (!T) return null;
    return h('div', { class: 'toast fix' + (T.err ? ' bad' : ''), role: T.err ? 'alert' : 'status' }, [h('b', null, T.b), T.d ? h('span', { class: 'd' }, T.d) : null,
      T.undo ? h('button', { type: 'button', class: 'tbtn', 'data-undo': '1' }, 'Undo') : null,
      h('button', { type: 'button', class: 'tx', 'data-toastx': '1', 'aria-label': 'Close' }, ico('close'))]);
  }
  function showToast(b, d, undo, err) {
    U.toast = { b: b, d: d, undo: undo || null, err: !!err, id: Date.now() };
    var id = U.toast.id;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { if (U.toast && U.toast.id === id) { U.toast = null; renderLayer(); } }, undo ? 8000 : 6000);
  }
  function renderLayer() {
    if (ui.view !== 'brief' || !B) { clearLayer(); return; }
    layer.textContent = '';
    add(layer, [popEl(), toastEl()]);
    placePop();
  }

  /* ----- 0.5.0: notes to Callsheet (memory.md) and possible events -----
     A note belongs to a brief item through its ref (the row, event or possible event key; 'area:<id>' for a
     briefing paragraph; 'hu:<hash>' for a heads-up). The server adds the metadata (people, emails, area, topics)
     from its own copy of the brief; api/brief lists the saved notes by ref in B.notes. */
  var NOTE_MAX = 500;
  function peById(id) { return ((B && B.possible) || []).filter(function (p) { return p.id === id; })[0] || null; }
  function possibles() { return ((B && B.possible) || []).filter(function (p) { return !isDismissed('pe:' + (p.key || p.id)); }); }
  function headsText(k) { return ((B && B.heads_up) || []).filter(function (x) { return hashOf(x) === k; })[0] || ''; }
  function noteRef(kind, id) {
    var x;
    if (kind === 'reply') { x = replyById(id); return x && x.key ? x.key : null; }
    if (kind === 'event') { x = eventById(id); return x && x.key ? x.key : null; }
    if (kind === 'pe') { x = peById(id); return x && x.key ? 'pe:' + x.key : null; }
    if (kind === 'para') return 'area:' + id;
    if (kind === 'heads') return 'hu:' + id;
    return null;
  }
  function noteOf(kind, id) {
    var ref = noteRef(kind, id), n = B && B.notes;
    return ref && n && typeof n === 'object' && n[ref] && typeof n[ref].text === 'string' ? n[ref] : null;
  }
  function clipText(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
  function noteItem(P) {
    var nt = noteOf(P.kind, P.id), ok = !B.sample && !!noteRef(P.kind, P.id);
    return h('button', { type: 'button', 'data-pnote': '1', disabled: !ok }, [nt ? 'Edit note for Callsheet…' : 'Note for Callsheet…',
      h('span', { class: 's' }, !ok ? 'Works once Callsheet has read your accounts' : nt ? clipText(nt.text, 90) : 'Read before each brief when it fits the day')]);
  }
  function notePop(P) {
    var nt = noteOf(P.kind, P.id);
    var ta = h('textarea', { class: 'inp', id: 'bv-note', rows: '3', maxlength: String(NOTE_MAX), 'data-ntext': '1',
      placeholder: 'e.g. “Jordan’s volunteer requests can wait until Sunday.”' });
    ta.value = P.text != null ? P.text : (nt ? nt.text : '');
    return h('div', { class: 'pop', role: 'group', 'aria-label': 'Note for Callsheet', 'data-pop': '1' }, h('div', { class: 'nt' }, [
      h('label', { class: 'label', for: 'bv-note' }, 'Note for Callsheet'), ta,
      P.err ? h('p', { class: 'err', role: 'alert' }, P.err) : null,
      h('div', { class: 'btns' }, [h('button', { type: 'button', class: 'act', 'data-nsave': '1', disabled: !!P.busy }, 'Save'),
        h('button', { type: 'button', class: 'tbtn', 'data-ncancel': '1' }, 'Cancel'),
        nt ? h('button', { type: 'button', class: 'tbtn', 'data-ndel': '1', disabled: !!P.busy }, 'Delete note') : null]),
      h('div', { class: 'hint' }, 'Callsheet reads it before each brief when it fits the day. All notes are in Setup → Delivery → Memory.')]));
  }
  function focusLayer(sel) { var f = layer.querySelector(sel); if (f) f.focus(); }
  function noteAction(d, P) {
    if (d.pnote) { P.mode = 'note'; P.text = null; P.err = null; renderLayer(); focusLayer('[data-ntext]'); return; }
    if (d.ncancel) { P.mode = null; P.err = null; renderLayer(); focusLayer('[data-pnote]'); return; }
    var nt = noteOf(P.kind, P.id), ref = noteRef(P.kind, P.id), anc = P.anc, req;
    if (!ref || P.busy) return;
    if (d.nsave) {
      var txt = String(P.text != null ? P.text : (nt ? nt.text : '')).trim();
      if (!txt) { P.err = 'Write the note first.'; renderLayer(); focusLayer('[data-ntext]'); return; }
      var body = { kind: P.kind, id: P.id, ref: ref, text: txt };
      if (P.kind === 'heads') body.item = headsText(P.id);
      req = nt ? function () { return api('PATCH', 'api/memory/notes/' + encodeURIComponent(nt.id), { text: txt }); }
        : function () { return api('POST', 'api/memory/notes', body); };
    } else if (nt) req = function () { return api('DELETE', 'api/memory/notes/' + encodeURIComponent(nt.id)); };
    else return;
    P.busy = true; P.err = null; renderLayer();
    mutate(req).then(function (res) {
      B.notes = B.notes && typeof B.notes === 'object' ? B.notes : {};
      if (d.nsave) B.notes[ref] = { id: res && res.id, text: res && typeof res.text === 'string' ? res.text : '' };
      else delete B.notes[ref];
      MEM.data = null; /* Setup → Memory loads the file again */
      if (U.pop === P) U.pop = null;
      showToast(d.nsave ? 'Note saved.' : 'Note deleted.', d.nsave ? 'Callsheet reads it before each brief when it fits the day.' : 'It’s gone from the memory too.');
      renderBrief('[data-anc="' + cssq(anc) + '"]');
    }, function (e) {
      if (U.pop !== P) return;
      P.busy = false; P.err = e && e.message ? e.message : 'Something went wrong.';
      renderLayer(); focusLayer('[data-ntext]');
    });
  }
  /* Possible events: dates the rules found in a reply's latest message (not on the calendar yet). "Add to
     calendar" is a link the server built (a filled-in Google Calendar page in the right account), opened in a
     new tab like the Gmail links; the owner saves it there. */
  function peCard(p) {
    var url = safeHttps(p.url), r = replyById(p.row), slackRow = p.via === 'slack';
    return h('div', { class: 'pe cn' + (p.kind === 'deadline' ? ' due' : '') }, [
      h('div', { class: 'l1' }, [h('span', { class: 'ri' }, ico(slackRow ? 'slack' : 'mail', slackRow ? 'Slack' : 'Gmail')),
        h('span', { class: 'w' }, p.title || ''), h('span', { class: 'when' }, p.when || ''), tagOf(p.area)]),
      h('div', { class: 'from' }, [(p.kind === 'deadline' ? 'Possible deadline' : 'Possible event') + ' · from ',
        r ? h('button', { type: 'button', class: 'tbtn plain', 'data-open': 'reply:' + r.id }, p.who || '') : (p.who || ''),
        p.phrase ? ' · “' + p.phrase + '”' : '']),
      url ? h('div', { class: 'acts' }, h('a', { class: 'act', href: url, target: '_blank', rel: 'noopener noreferrer',
        'aria-label': 'Add to calendar: ' + (p.title || '') + ', ' + (p.when || '') + ' (opens Google Calendar)' }, 'Add to calendar')) : null,
      npBtn('pe', p.id, 'pe-' + p.id)]);
  }
  function possibleSection() {
    var ps = possibles();
    if (!ps.length) return null;
    return h('section', { class: 'pesec', 'aria-labelledby': 'pe-h' }, [
      h('h2', { class: 'sec-h', id: 'pe-h' }, ['Possible events ', h('span', { class: 'n' }, ps.length + ' · found in your mail and chat')]),
      h('p', { class: 'pehint' }, 'Not on your calendar yet. Add to calendar opens a filled-in Google Calendar page; nothing is saved until you save it there.'),
      h('div', { class: 'pgrid' }, ps.map(peCard))]);
  }

  /* ----- the whole view ----- */
  function render(d) { setBrief(d); renderBrief(); pollWriting(); }
  /* 0.6.1: while api/brief says "writing", ask again every poll_s seconds; render only when the writer is done */
  var writePoll = null;
  function pollWriting() {
    if (writePoll) { clearTimeout(writePoll); writePoll = null; }
    if (!B || !B.writing || ui.view !== 'brief') return;
    writePoll = setTimeout(function () {
      writePoll = null;
      if (ui.view !== 'brief') return;
      api('GET', 'api/brief').then(function (d) {
        if (ui.view !== 'brief') return;
        if (d && d.writing) { B.writing = d.writing; pollWriting(); } else render(d);
      }, function () { pollWriting(); });
    }, Math.max(5, Number(B.writing.poll_s) || 10) * 1000);
  }
  function setBrief(d) {
    B = d || {};
    /* the sample brief has no row ids: give it local ones (nothing is saved for the sample) */
    (B.replies || []).forEach(function (r, i) { if (!r.id) r.id = 'x' + (i + 1); });
    (B.schedule || []).forEach(function (e, i) { if (!e.id) e.id = 'xs' + (i + 1); });
    (B.done || []).forEach(function (r, i) { if (!r.id) r.id = 'xd' + (i + 1); });
    if (U.open && U.open.kind !== 'item' && !(U.open.kind === 'reply' ? replyById(U.open.id) : eventById(U.open.id))) U.open = null;
  }
  function renderBrief(focusSel) {
    if (ui.view !== 'brief' || !B) return;
    var phone = isPhone();
    var panelScroll = main.querySelector('.panel-in'), ps = panelScroll ? panelScroll.scrollTop : 0, sy = window.scrollY;
    document.title = 'Daily Brief · ' + (B.date_short || '');
    document.getElementById('sample-tag').hidden = !B.sample;
    hdrDate.textContent = (phone ? B.date_short : B.date_long) || '';
    freshT.textContent = B.written_at ? (phone ? 'Refreshed ' : 'Last refreshed ') + B.written_at : 'Not refreshed yet';

    var banner = S && !S.setup_done && noAccounts() ? h('div', { class: 'banner', role: 'region', 'aria-label': 'Setup' }, [
      h('p', null, 'This is sample data. Set up Callsheet to see your own day.'),
      btn('btn primary', 'Start setup', null, function () { ui.step = 1; location.hash = 'setup'; })]) : null;
    var nOpen = openReplies().length;
    var tabs = h('nav', { class: 'tabbar', role: 'tablist', 'aria-label': 'Sections' }, TABS.map(function (t) {
      var on = U.tab === t[0], later = t[0] === 'tasks';
      return h('button', { type: 'button', role: 'tab', id: 'tab-' + t[0], 'data-tab': t[0], 'aria-selected': String(on), 'aria-controls': later ? null : 'bv-pane',
        tabindex: on ? '0' : '-1', disabled: later, class: later ? 'later' : null, title: later ? 'Coming later' : null }, [
        ico(t[3]), h('span', { class: 'd-only' }, t[1]), h('span', { class: 'p-only' }, t[2]),
        t[0] === 'replies' && nOpen ? h('span', { class: 'badge', 'aria-label': nOpen + ' open' }, String(nOpen)) : null,
        later ? h('span', { class: 'soon d-only' }, 'Coming later') : null]);
    }));
    var panel = panelEl();
    var pane = h('div', { class: 'pane', id: 'bv-pane', role: 'tabpanel', 'aria-labelledby': 'tab-' + U.tab, inert: phone && panel ? true : null },
      [TFN[U.tab](), foot()]);
    var root = h('div', { class: 'bv brief' + (phone && panel ? ' sheet-open' : '') }, [banner, tabs,
      h('div', { class: 'body' + (panel ? ' open' : '') }, [pane, panel])]);
    if (phone && panel) tabs.setAttribute('inert', '');
    main.textContent = '';
    add(main, root);
    document.querySelector('header.top').toggleAttribute('inert', !!(phone && panel));
    document.documentElement.classList.toggle('bv-lock', !!(phone && panel));
    if (!phone || !panel) window.scrollTo(window.scrollX, sy);
    var pin = main.querySelector('.panel-in'); if (pin && ps) pin.scrollTop = ps;
    syncTabsH();
    renderLayer();
    syncBg();
    if (U.tab === 'chat') chatAfterRender();  /* 0.6.0 chat: scroll to the latest message, phone height */
    if (focusSel) { var f = document.querySelector(focusSel); if (f) f.focus({ preventScroll: true }); }
  }
  function syncTabsH() {
    var tb = main.querySelector('.tabbar');
    if (tb && !isPhone()) document.documentElement.style.setProperty('--tabs-h', tb.offsetHeight + 'px');
  }
  onMedia('(max-width: 700px)', function () { if (ui.view === 'brief' && B) { U.pop = null; renderBrief(); } });
  window.addEventListener('resize', function () { if (ui.view === 'brief' && U.pop) placePop(); syncTabsH(); });
  window.addEventListener('scroll', function () { if (ui.view === 'brief' && U.pop) placePop(); }, { passive: true, capture: true });

  /* ----- actions ----- */
  function mutate(fn) { var p = queue.then(fn); queue = p.then(function () { }, function () { }); return p; }
  function failToast(e) { showToast('Didn’t work.', e && e.message ? e.message : 'Something went wrong.', null, true); renderLayer(); }
  function refetch() {
    return api('GET', 'api/brief').then(function (d) { setBrief(d); });
  }
  function rowLabel(id) { var r = replyById(id); return r ? r.who : ''; }
  function markDone(id) {
    var r = replyById(id);
    if (!r) return;
    var after = function (doneList) {
      B.replies = replies().filter(function (x) { return x !== r; });
      if (doneList) B.done = doneList; else B.done = [{ id: r.id, who: r.who, summary: gistSum(r), done_at: '' }].concat(B.done || []);
      if (U.open && U.open.id === id) U.open = null;
      U.pop = null;
      showToast('Marked done.', r.who + ' · comes back if they write again', { kind: 'done', id: id, row: r });
      renderBrief('[data-undo]');
    };
    if (B.sample) { after(null); return; }
    mutate(function () { return api('POST', 'api/done', { row_id: id, done: true, key: r.key }); })
      .then(function (res) { after(res && Array.isArray(res.done) ? res.done : null); }, function (e) { renderBrief(); failToast(e); if (/changed/.test(e.message)) refetch().then(function () { renderBrief(); }); });
  }
  function unDone(id, focusSel) {
    if (B.sample) {
      var row = U.toast && U.toast.undo && U.toast.undo.id === id ? U.toast.undo.row : null;
      B.done = (B.done || []).filter(function (d) { return d.id !== id; });
      if (row) B.replies = replies().concat([row]).sort(byId);
      U.toast = null; renderBrief(focusSel); return;
    }
    mutate(function () { return api('POST', 'api/done', { row_id: id, done: false }); })
      .then(function () { return refetch(); })
      .then(function () { U.toast = null; renderBrief(focusSel); }, failToast);
  }
  function byId(a, b) { return (parseInt(String(a.id).replace(/\D/g, ''), 10) || 0) - (parseInt(String(b.id).replace(/\D/g, ''), 10) || 0); }
  function suppress(id, scope) {
    var r = replyById(id);
    if (!r) return;
    U.pop = null;
    var before = replies().slice();
    var after = function (res) {
      var still = replies().map(function (x) { return x.id; });
      var gone = before.filter(function (x) { return still.indexOf(x.id) < 0; });
      if (U.open && U.open.kind === 'reply' && still.indexOf(U.open.id) < 0) U.open = null;
      var rule = res && res.rule, label = rule && rule.label;
      showToast('Hidden.', scope === 'thread' ? r.who + ' · just this one' : 'Won’t ask again for ' + (label || r.who),
        { kind: 'suppress', rule: rule && rule.id, rows: gone });
      renderBrief('[data-undo]');
    };
    if (B.sample) {
      B.replies = replies().filter(function (x) { return x !== r; }); B.hidden = (parseInt(B.hidden, 10) || 0) + 1;
      after(null); return;
    }
    mutate(function () { return api('POST', 'api/suppress', { row_id: id, scope: scope, key: r.key }); })
      .then(function (res) { return refetch().then(function () { after(res); }); },
        function (e) { renderBrief(); failToast(e); });
  }
  function unSuppress(u) {
    var back = function () {
      var have = replies().map(function (x) { return x.id; });
      var rows = (u.rows || []).filter(function (x) { return have.indexOf(x.id) < 0; });
      B.replies = replies().concat(rows).sort(byId);
      B.hidden = Math.max(0, (parseInt(B.hidden, 10) || 0) - rows.length);
      U.toast = null;
      renderBrief(rows.length ? '[data-open="reply:' + cssq(rows[0].id) + '"]' : null);
    };
    if (B.sample || !u.rule) { back(); return; }
    mutate(function () { return api('DELETE', 'api/suppress/' + encodeURIComponent(u.rule)); }).then(back, failToast);
  }
  function dismiss(P) {
    var key = P.kind === 'event' ? 'ev:' + ((eventById(P.id) || {}).key || P.id) : P.kind === 'heads' ? 'hu:' + P.id
      : P.kind === 'pe' ? 'pe:' + ((peById(P.id) || {}).key || P.id)
      : 'ar:' + P.id + ':' + hashOf(((B.summary || {}).areas || {})[P.id]);
    var what = P.kind === 'event' ? (eventById(P.id) || {}).title : P.kind === 'heads' ? 'Heads up'
      : P.kind === 'pe' ? (peById(P.id) || {}).title : areaOf(P.id).name + ' summary';
    setDismissed(key, true);
    if (U.open && U.open.kind === 'event' && P.kind === 'event' && U.open.id === P.id) U.open = null;
    U.pop = null;
    showToast('Dismissed.', (what || '') + ' · until tomorrow', { kind: 'dismiss', key: key });
    renderBrief('[data-undo]');
  }
  function copyDraft(b) {
    var ta = main.querySelector('#bv-draft');
    if (!ta) return;
    var s = ta.value;
    function said(t) { b.textContent = t; setTimeout(function () { b.textContent = 'Copy'; }, 2000); }
    function selectIt() {
      var ok = false;
      try { ta.focus(); ta.select(); ok = document.execCommand && document.execCommand('copy'); } catch (e) { ok = false; }
      said(ok ? 'Copied' : 'Selected: press Ctrl+C');
    }
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(s).then(function () { said('Copied'); }, selectIt);
    else selectIt();
  }
  function openItem(spec) {
    var i = spec.indexOf(':'), kind = spec.slice(0, i), id = spec.slice(i + 1);
    U.open = { kind: kind, id: id };
    U.pop = null;
    renderBrief('.panel [data-close]');
  }
  function closePanel() {
    var o = U.open;
    U.open = null;
    renderBrief(o ? '[data-open="' + cssq(o.kind + ':' + o.id) + '"]' : null);
  }
  function setTab(t, focus) {
    if (LIVE_TABS.indexOf(t) < 0) return;
    U.tab = t; U.pop = null; save(KEY_TAB, t);
    if (tabFromHash(location.hash) && history.replaceState) history.replaceState(null, '', '#' + t);  /* 0.6.0 chat: a tab link follows the tab */
    if (t !== 'replies') U.area = null;
    if (U.open && U.open.kind === 'item' && t !== 'chat') U.open = null;  /* 0.6.0 chat */
    renderBrief(focus ? '[data-tab="' + t + '"]' : null);
    if (!isPhone()) { var tb = main.querySelector('.tabbar'); if (tb && tb.getBoundingClientRect().top < 0) window.scrollTo(0, 0); }
    else window.scrollTo(0, 0);
  }
  function inBrief(t) { return ui.view === 'brief' && B && t && (main.contains(t) || layer.contains(t)); }
  document.addEventListener('click', function (e) {
    if (ui.view !== 'brief' || !B) return;
    var t = e.target.closest ? e.target.closest('button') : null;
    /* a click outside the open menu closes it */
    if (U.pop && !e.target.closest('[data-pop]') && !(t && t.dataset.np && inBrief(t))) {
      U.pop = null; renderLayer();
      main.querySelectorAll('[data-np][aria-expanded="true"]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
    }
    if (!t || t.disabled || !inBrief(t)) return;
    var d = t.dataset, P = U.pop;
    if (d.tab) setTab(d.tab, true);
    else if (d.open) openItem(d.open);
    else if (d.close) closePanel();
    else if (d.ck) {
      var rowEl = t.closest('.row');
      t.setAttribute('aria-pressed', 'true'); if (rowEl) rowEl.classList.add('gone');
      setTimeout(function () { markDone(d.ck); }, 380);
    }
    else if (d.unck) unDone(d.unck, '[data-donefold]');
    else if (d.markdone) markDone(d.markdone);
    else if (d.donefold) { U.doneOpen = !U.doneOpen; renderBrief('[data-donefold]'); }
    else if (d.more) { U.more = !U.more; renderBrief('[data-more]'); }
    else if (d.goarea) { U.area = d.goarea; setTab('replies'); var ah = main.querySelector('.tabbar [data-tab="replies"]'); if (ah) ah.focus({ preventScroll: true }); }
    else if (d.afil) { U.area = d.afil === 'all' ? null : d.afil; renderBrief('[data-afil="' + cssq(d.afil) + '"]'); }  /* 0.6.0 chat */
    else if (d.manage) { ui.step = 3; ui.focusNext = '#nnr-h'; location.hash = 'setup'; }
    else if (d.copy) copyDraft(t);
    else if (d.np) {
      var k = d.np.indexOf(':');
      U.pop = P && P.anc === d.anc ? null : { kind: d.np.slice(0, k), id: d.np.slice(k + 1), anc: d.anc, sub: false };
      renderLayer();
      var fb = U.pop ? layer.querySelector('[data-pop] button') : null;
      if (fb) fb.focus(); else t.setAttribute('aria-expanded', 'false');
      main.querySelectorAll('[data-np]').forEach(function (b) { b.setAttribute('aria-expanded', String(!!(U.pop && b.dataset.anc === U.pop.anc))); });
    }
    else if (d.pnnr && P) { P.sub = !P.sub; renderLayer(); var nb = layer.querySelector(P.sub ? '[data-sup]' : '[data-pnnr]'); if (nb) nb.focus(); }
    else if (d.sup && P) suppress(P.id, d.sup);
    else if ((d.pnote || d.ncancel || d.nsave || d.ndel) && P) noteAction(d, P);
    else if (d.dismiss && P) dismiss(P);
    else if (d.undo) {
      var u = U.toast && U.toast.undo;
      if (!u) return;
      if (u.kind === 'done') unDone(u.id, '[data-open="reply:' + cssq(u.id) + '"]');
      else if (u.kind === 'suppress') unSuppress(u);
      else if (u.kind === 'dismiss') { setDismissed(u.key, false); U.toast = null; renderBrief(); }
    }
    else if (d.toastx) { U.toast = null; renderLayer(); }
  });
  document.addEventListener('input', function (e) {
    var t = e.target;
    if (ui.view === 'brief' && t.dataset && t.dataset.draft) U.drafts[t.dataset.draft] = t.value;
    if (ui.view === 'brief' && t.dataset && t.dataset.ntext && U.pop) U.pop.text = t.value;
  });
  document.addEventListener('keydown', function (e) {
    if (ui.view !== 'brief' || !B) return;
    if (e.key === 'Escape') {
      if (U.pop) { var anc = U.pop.anc; U.pop = null; renderLayer(); var a = main.querySelector('[data-anc="' + cssq(anc) + '"]'); if (a) { a.setAttribute('aria-expanded', 'false'); a.focus(); } e.preventDefault(); }
      else if (U.open && lookPanel.hidden) { closePanel(); e.preventDefault(); }
      return;
    }
    var t = e.target;
    if (t.dataset && t.dataset.tab && (e.key === 'ArrowRight' || e.key === 'ArrowLeft' || e.key === 'Home' || e.key === 'End')) {
      var i = LIVE_TABS.indexOf(t.dataset.tab), n = LIVE_TABS.length;
      i = e.key === 'Home' ? 0 : e.key === 'End' ? n - 1 : (i + (e.key === 'ArrowRight' ? 1 : -1) + n) % n;
      setTab(LIVE_TABS[i], true); e.preventDefault();
    }
  });

  /* ---------- state ---------- */
  var ui = {
    view: null, step: 1,
    openCals: {},        /* account id -> calendars disclosure open */
    connect: {},         /* area id -> {flow_id, auth_url} while a sign-in runs */
    confirm: null,       /* 'key' | 'acct:<id>' | 'area:<id>' */
    replaceKey: false,
    slackOpen: {},       /* area id -> Slack connect panel open */
    slackMan: null,      /* api/slack/manifest, fetched when a connect panel first opens */
    slackIgn: {},        /* workspace id -> "Ignore channels" open */
    slackCh: {},         /* workspace id -> {channels, stale} | {loading} | {error} */
    slackAll: {},        /* workspace id -> show every channel chip */
    prof: null,          /* writing profile shown in Setup (default: the active one) */
    profMode: null,      /* 'copy' | 'rename' while that inline form is open */
    wdraft: {},          /* '<profile id>:<section>' -> unsaved text */
    wexOpen: false,      /* Writing instructions: the fixed style examples unfolded */
    focusNext: null,     /* focus target for the next Setup render (the brief's "Manage" link) */
    newArea: { name: '', colour: 2 }
  };
  function applyServerLook() {
    var st = S && S.settings;
    if (!st) return;
    if (lookPending) { st.look = app.dataset.look; st.mode = app.dataset.mode; return; }
    if (LOOKS.indexOf(st.look) >= 0 && st.look !== app.dataset.look) setLook(st.look, true);
    if (MODES.indexOf(st.mode) >= 0 && st.mode !== app.dataset.mode) setMode(st.mode, true);
  }
  /* ---------- version check: this page's version (the meta that server.py fills in) against the server's ---------- */
  var PAGE_VERSION = (function () {
    var m = document.querySelector('meta[name="callsheet-version"]');
    return m ? m.getAttribute('content') || '' : '';
  })();
  var updateNote = document.getElementById('update-note');
  function checkVersion(v) {
    if (!updateNote || typeof v !== 'string' || !v || v === PAGE_VERSION || !updateNote.hidden) return;
    updateNote.textContent = '';
    add(updateNote, [h('p', null, 'Callsheet was updated. Reload to see the new version.'),
      btn('btn primary', 'Reload', null, function () { location.reload(); })]);
    updateNote.hidden = false;
  }
  function loadState() {
    return api('GET', 'api/state').then(function (s) {
      S = s || {};
      checkVersion(S.version);
      S.areas = S.areas || []; S.vips = S.vips || []; S.settings = S.settings || {};
      S.google_client = S.google_client || {}; S.last_sync = S.last_sync || {};
      applyServerLook();
      return S;
    });
  }

  /* ---------- routing ---------- */
  var briefSeq = 0;
  function showBrief() {
    var seq = ++briefSeq;
    var st = loadState().catch(function () { return null; });   /* the brief still works without state */
    var br = api('GET', 'api/brief');
    return Promise.all([br, st]).then(function (r) { if (seq === briefSeq && ui.view === 'brief') render(r[0]); },
      function (e) { if (seq === briefSeq && ui.view === 'brief') status('Could not load the brief (' + e.message + ')'); });
  }
  function showSetup(focusSel) {
    var p = S ? Promise.resolve(S) : loadState();
    return p.then(function () { if (ui.view === 'setup') renderSetup(focusSel || '#wiz-title'); },
      function (e) { if (ui.view === 'setup') status('Could not load the setup (' + e.message + ')'); });
  }
  function route() {
    var v = location.hash === '#setup' ? 'setup' : location.hash === '#eval' ? 'eval' : location.hash === '#knowme' ? 'knowme' : location.hash === '#profile' ? 'profile' : location.hash === '#profile-questions' ? 'profileq' : 'brief';  /* 0.6.0 index: #knowme; 0.6.0 profile: #profile, #profile-questions */
    var changed = v !== ui.view;
    ui.view = v;
    setupBtn.hidden = v !== 'brief'; backBtn.hidden = v === 'brief'; fresh.hidden = v !== 'brief'; refreshBtn.hidden = v !== 'brief';
    if (v !== 'brief') {   /* leaving the brief: no menu, toast, sheet lock or inert header */
      clearLayer(); document.documentElement.classList.remove('bv-lock'); document.querySelector('header.top').removeAttribute('inert');
    }
    if (v !== 'eval') stopEvalPoll();
    if (v !== 'knowme') stopKnowPoll();  /* 0.6.0 index */
    if (v !== 'profile' && v !== 'profileq') stopProfilePoll();  /* 0.6.0 profile */
    refreshErr.textContent = '';
    var ht = v === 'brief' ? tabFromHash(location.hash) : null;  /* 0.6.0 chat: #replies, #chat, … (and #areas) */
    if (ht && ht !== U.tab) { U.tab = ht; save(KEY_TAB, ht); if (ht !== 'replies') U.area = null; }
    if (!changed && main.querySelector('.sheet, .bv')) { if (ht && B) renderBrief(); return; }
    window.scrollTo(0, 0);
    if (v === 'setup') {
      var fs = ui.focusNext; ui.focusNext = null; ui.confirm = null;
      loadState().then(function () { return showSetup(fs); }, function () { return showSetup(fs); });
    }
    else if (v === 'eval') showEval();
    else if (v === 'knowme') showKnow('#kn-title');  /* 0.6.0 index */
    else if (v === 'profile' || v === 'profileq') showProfile(v);  /* 0.6.0 profile */
    else showBrief();
  }
  window.addEventListener('hashchange', route);

  /* ---------- render: setup ---------- */
  function go(n) { ui.step = Math.max(1, Math.min(6, n)); ui.confirm = null; renderSetup('#wiz-title'); window.scrollTo(0, 0); }
  function reload(focusKey) {
    return loadState().then(function () { renderSetup(focusKey ? '[data-fk="' + cssq(focusKey) + '"]' : null); },
      function (e) { var w = document.getElementById('wiz-err'); if (w) w.textContent = 'Could not reload (' + e.message + ')'; });
  }
  function renderSetup(focusSel) {
    var n = ui.step;
    var rail = h('nav', { class: 'rail', 'aria-label': 'Setup steps' }, STEPS.map(function (name, i) {
      var k = i + 1;
      return btn(k < n ? 'done' : k === n ? 'on' : '', [h('span', { class: 'n' }, String(k)), name],
        { 'aria-current': k === n ? 'step' : null }, function () { go(k); });
    }));
    var prail = h('div', { class: 'prail' }, [
      h('div', { class: 'label' }, 'Step ' + n + ' of 6 · ' + STEPS[n - 1]),
      h('div', { class: 'segs', 'aria-hidden': 'true' }, STEPS.map(function (x, i) { return h('i', { class: i + 1 < n ? 'done' : i + 1 === n ? 'on' : '' }); }))]);
    var body = [stepAbout, stepAreas, stepVips, stepTimes, stepDelivery, stepReview][n - 1]();
    var wnav = h('div', { class: 'wnav' }, [
      n > 1 ? btn('btn', 'Back: ' + STEPS[n - 2], null, function () { go(n - 1); }) : null,
      n < 6 ? btn('btn primary', 'Next: ' + STEPS[n], null, function () { go(n + 1); }) : null]);
    var sheet = h('div', { class: 'sheet wiz' }, [
      h('div', { class: 'mast two' }, [
        h('div', { class: 't' }, [h('div', { class: 'label' }, 'Setup'), h('h1', { id: 'wiz-title', tabindex: '-1' }, STEPS[n - 1])]),
        h('div', { class: 'box call' }, [h('div', { class: 'label' }, 'Step'), h('div', { class: 'disp' }, n + ' / 6')])]),
      rail, prail,
      h('section', { class: 'step', 'aria-labelledby': 'wiz-title' }, [h('p', { class: 'err', id: 'wiz-err', role: 'alert' }), body]),
      wnav,
      h('p', { class: 'ver' }, 'Callsheet ' + (S.version || PAGE_VERSION))]);
    main.textContent = '';
    add(main, sheet);
    press('data-set-look', app.dataset.look); press('data-set-mode', app.dataset.mode);
    syncBg();
    main.querySelectorAll('.wi textarea').forEach(fitTa);
    if (focusSel) { var f = main.querySelector(focusSel); if (f) f.focus({ preventScroll: focusSel === '#wiz-title' }); }
  }
  /* obj may be a function: the body is then built when the request's turn comes (after earlier saves landed) */
  function patchSettings(obj, err, btns, ok) {
    var body;
    return run(btns || [], err, function () { body = typeof obj === 'function' ? obj() : obj; return api('PATCH', 'api/settings', body); }, function () {
      Object.keys(body).forEach(function (k) { S.settings[k] = body[k]; });
      if (ok) ok(body);
    });
  }
  /* an On/Off switch (v3 .sw); label is the accessible name */
  function sw(pressed, label, fn) {
    return btn('sw', null, { 'aria-pressed': String(!!pressed), 'aria-label': label }, fn);
  }
  function confirmRow(text, yes, onYes) {
    var err = errBox(), yesB, noB;
    yesB = btn('btn danger', yes, null, function () { onYes(yesB, noB, err); });
    noB = btn('btn', 'Cancel', { 'data-fk': 'cancel' }, function () { var back = ui.confirm; ui.confirm = null; renderSetup('[data-fk="' + cssq('ask:' + back) + '"]'); });
    return h('div', { class: 'confirm', role: 'group', 'aria-label': text }, [h('p', null, text), h('div', { class: 'btns' }, [yesB, noB]), err]);
  }
  function askBtn(key, label) {
    return btn('tbtn', label, { 'data-fk': 'ask:' + key }, function () { ui.confirm = key; renderSetup('[data-fk="cancel"]'); });
  }

  /* step 1: about you */
  function tzList(cur) {
    var list = [];
    try { if (Intl.supportedValuesOf) list = Intl.supportedValuesOf('timeZone').slice(); } catch (e) { list = []; }
    if (!list.length) list = ['Europe/London', 'Europe/Dublin', 'Europe/Amsterdam', 'Europe/Berlin', 'Europe/Paris', 'Europe/Madrid',
      'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles', 'Asia/Singapore', 'Asia/Tokyo', 'Australia/Sydney'];
    if (list.indexOf('UTC') < 0) list.push('UTC');
    if (cur && list.indexOf(cur) < 0) list.unshift(cur);
    return list;
  }
  function miniTime() { return S && S.settings && S.settings.time_format === '24h' ? '09:30' : '9:30 am'; }
  function miniCard(look, desc) {
    return h('button', { type: 'button', class: 'lookcard', 'data-set-look': look, 'aria-pressed': String(app.dataset.look === look) },
      h('span', { class: 'app mini', 'data-look': look, 'data-mode': app.dataset.mode }, [
        h('span', { class: 'm-head' }, [h('span', { class: 'm-word' }, 'Daily Brief'), h('span', { class: 'm-date' }, 'Wed 28 Oct')]),
        h('span', { class: 'm-row' }, [h('span', { class: 'm-time' }, miniTime()), h('span', null, 'Daily stand-up'), h('span', { class: 'm-tag a2' }, 'CO. A')]),
        h('span', { class: 'm-name' }, LOOKNAME[look]), h('span', { class: 'm-desc' }, desc)]));
  }
  function stepAbout() {
    var st = S.settings, nameErr = errBox(), tzErr = errBox(), dayErr = errBox();
    var name = on(h('input', { class: 'inp', type: 'text', id: 'f-name', autocomplete: 'name', value: st.name || '' }), 'change', function () {
      patchSettings({ name: name.value.trim() }, nameErr);
    });
    var cur = st.timezone || (Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
    var tz = on(h('select', { class: 'inp', id: 'f-tz' }, tzList(cur).map(function (z) {
      return h('option', { value: z, selected: z === cur }, z.replace(/_/g, ' '));
    })), 'change', function () { patchSettings({ timezone: tz.value }, tzErr); });
    var days = h('div', { class: 'chips', role: 'group', 'aria-labelledby': 'lbl-days' }, DAYS.map(function (d) {
      var b = btn('chip', DAYNAME[d], { 'aria-pressed': String((st.workdays || []).indexOf(d) >= 0) }, function () {
        patchSettings(function () {
          var now = st.workdays || [], want = now.indexOf(d) >= 0 ? now.filter(function (x) { return x !== d; }) : now.concat([d]);
          return { workdays: DAYS.filter(function (x) { return want.indexOf(x) >= 0; }) };
        }, dayErr, [b], function (sent) { b.setAttribute('aria-pressed', String(sent.workdays.indexOf(d) >= 0)); });
      });
      return b;
    }));
    /* 12-hour or 24-hour times: the server writes the brief's display strings in this format */
    var tfErr = errBox(), tfCur = st.time_format === '24h' ? '24h' : '12h';
    var tf = h('div', { class: 'chips', role: 'group', 'aria-labelledby': 'lbl-tf' }, [['12h', '12-hour (2:30 pm)'], ['24h', '24-hour (14:30)']].map(function (x) {
      var b = btn('chip', x[1], { 'aria-pressed': String(tfCur === x[0]), 'data-tf': x[0], 'data-fk': 'tf:' + x[0] }, function () {
        if ((S.settings.time_format === '24h' ? '24h' : '12h') === x[0]) return;
        patchSettings({ time_format: x[0] }, tfErr, [].slice.call(tf.querySelectorAll('button')), function () {
          tf.querySelectorAll('button').forEach(function (y) {
            var on = y.getAttribute('data-tf') === x[0];
            y.setAttribute('aria-pressed', String(on));
          });
          document.querySelectorAll('.m-time').forEach(function (m) { m.textContent = miniTime(); });
        });
      });
      return b;
    }));
    return [
      h('div', { class: 'cols' }, [
        h('div', null, [
          h('div', { class: 'field' }, [h('label', { class: 'label', for: 'f-name' }, 'Your name'), name, nameErr]),
          h('div', { class: 'field' }, [h('label', { class: 'label', for: 'f-tz' }, 'Time zone'), h('span', { class: 'selw' }, tz), tzErr]),
          h('div', { class: 'field' }, [h('span', { class: 'label', id: 'lbl-tf' }, 'Time format'), tf, tfErr])]),
        h('div', null, [
          h('div', { class: 'field' }, [h('span', { class: 'label', id: 'lbl-days' }, 'Working days'), days, dayErr]),
          h('p', { class: 'note' }, 'Days off still get a morning brief; check-ins can skip them (step 4).')])]),
      h('div', { class: 'lookpick' }, [
        h('div', { class: 'field', role: 'group', 'aria-labelledby': 'lbl-looks', 'data-errhost': '' }, [
          h('span', { class: 'label', id: 'lbl-looks' }, 'Look'),
          h('div', { class: 'looks' }, [miniCard('bold', 'Flat colour, big type'), miniCard('clean', 'Quiet and professional'),
            miniCard('easy', 'Larger text, strongest contrast')]), errBox()]),
        h('div', { class: 'field', role: 'group', 'aria-labelledby': 'lbl-modes', 'data-errhost': '' }, [
          h('span', { class: 'label', id: 'lbl-modes' }, 'Mode'),
          h('div', { class: 'chips' }, MODES.map(function (m) {
            return h('button', { type: 'button', class: 'chip', 'data-set-mode': m, 'aria-pressed': String(app.dataset.mode === m) }, MODENAME[m]);
          })), errBox(),
          h('p', { class: 'note' }, 'Auto follows your device.')])])];
  }

  /* step 2: areas */
  function keyPanel() {
    var g = S.google_client, err = errBox();
    if (g.configured && !ui.replaceKey) {
      return h('div', { class: 'keyp' }, [
        h('h2', { class: 'h3' }, 'Google sign-in key'),
        h('div', { class: 'keyrow' }, [h('span', { class: 'st' }, 'Key ending …' + String(g.client_id_hint || '').replace(/^(…|\.\.\.)/, '')),
          btn('tbtn', 'Replace', { 'data-fk': 'key-replace' }, function () { ui.replaceKey = true; renderSetup('#f-cid'); }),
          ui.confirm === 'key' ? null : askBtn('key', 'Remove')]),
        ui.confirm === 'key' ? confirmRow('Remove the Google sign-in key? Accounts stop refreshing until you add a key again.', 'Remove', function (y, c, e) {
          run([y, c], e, function () { return api('DELETE', 'api/google-client'); }, function () { ui.confirm = null; return reload('key-save'); });
        }) : null]);
    }
    var cid = h('input', { class: 'inp', type: 'text', id: 'f-cid', autocomplete: 'off', spellcheck: 'false', 'data-fk': 'key-cid' });
    var sec = h('input', { class: 'inp', type: 'password', id: 'f-csec', autocomplete: 'off', spellcheck: 'false' });
    var save = h('button', { type: 'submit', class: 'btn primary', 'data-fk': 'key-save' }, 'Save key');
    var form = on(h('form', { class: 'keyform', novalidate: '' }, [
      h('div', { class: 'field' }, [h('label', { class: 'label', for: 'f-cid' }, 'Client ID'), cid]),
      h('div', { class: 'field' }, [h('label', { class: 'label', for: 'f-csec' }, 'Client secret'), sec]),
      h('div', { class: 'btns' }, [save, ui.replaceKey ? btn('btn', 'Cancel', null, function () { ui.replaceKey = false; renderSetup('[data-fk="key-replace"]'); }) : null]),
      err]), 'submit', function (e) {
      e.preventDefault();
      var a = cid.value.trim(), b = sec.value.trim();
      if (!a || !b) { err.textContent = 'Paste both the client ID and the client secret.'; return; }
      run([save], err, function () { return api('POST', 'api/google-client', { client_id: a, client_secret: b }); }, function () {
        sec.value = ''; ui.replaceKey = false; return reload('key-replace');
      });
    });
    return h('div', { class: 'keyp' }, [
      h('h2', { class: 'h3' }, 'Google sign-in key'),
      h('p', { class: 'note' }, 'Callsheet signs in to Google with your own key. One-time setup in Google Cloud Console:'),
      h('ol', { class: 'checklist' }, [
        h('li', null, 'Create a Google Cloud project'),
        h('li', null, 'Turn on Gmail API and Google Calendar API'),
        h('li', null, 'Consent screen: External, then Publish app ("In production")'),
        h('li', null, 'Create OAuth client: Desktop app'),
        h('li', null, 'Paste the client ID and secret below')]),
      form]);
  }
  function statusTag(acc) {
    if (acc.status === 'connected') return h('span', { class: 'st' }, 'Connected');
    return h('span', { class: 'st warn' }, acc.status === 'needs_reconnect' ? 'Reconnect needed' : 'Error');
  }
  function accountRow(a, acc) {
    var err = errBox(), calErr = errBox(), cals = acc.calendars || [], svc = acc.services || {};
    var open = !!ui.openCals[acc.id], listId = 'cals-' + acc.id;
    function svcToggle(key, label) {
      var b = sw(svc[key], label + ' for ' + acc.email, function () {
        var next;
        run([b], err, function () {
          next = { calendar: !!svc.calendar, mail: !!svc.mail }; next[key] = !svc[key];
          return api('PATCH', 'api/accounts/' + encodeURIComponent(acc.id), { services: next });
        }, function () {
          svc[key] = next[key]; acc.services = svc; b.setAttribute('aria-pressed', String(next[key]));
        });
      });
      return h('span', { class: 'svct' }, [b, h('span', { 'aria-hidden': 'true' }, label)]);
    }
    var nSel = cals.filter(function (c) { return c.selected; }).length;
    var disc = btn('tbtn disc', 'Calendars (' + nSel + ' of ' + cals.length + ')', { 'aria-expanded': String(open), 'aria-controls': listId, 'data-fk': 'cals:' + acc.id }, function () {
      ui.openCals[acc.id] = !open; renderSetup('[data-fk="' + cssq('cals:' + acc.id) + '"]');
    });
    var list = h('ul', { class: 'cals', id: listId, hidden: !open }, cals.map(function (c, i) {
      var id = 'cal-' + acc.id + '-' + i;
      var box = on(h('input', { type: 'checkbox', id: id, checked: !!c.selected, 'data-cal': String(i) }), 'change', function () {
        var want;
        run([], calErr, function () {
          var boxes = list.querySelectorAll('input[data-cal]');
          want = cals.filter(function (x, k) { return boxes[k] && boxes[k].checked; }).map(function (x) { return x.id; });
          return api('PATCH', 'api/accounts/' + encodeURIComponent(acc.id), { calendars: want });
        }, function () {
          cals.forEach(function (x) { x.selected = want.indexOf(x.id) >= 0; });
          disc.textContent = 'Calendars (' + want.length + ' of ' + cals.length + ')';
        }).then(function () { if (calErr.textContent) box.checked = !!c.selected; });
      });
      return h('li', null, [box, h('label', { for: id }, [c.summary || c.id, c.primary ? h('span', { class: 'muted' }, ' · main') : null])]);
    }));
    var key = 'acct:' + acc.id;
    return h('div', { class: 'acct' }, [
      h('div', { class: 'acct-h' }, [h('span', { class: 'em' }, acc.email), statusTag(acc)]),
      acc.status !== 'connected' && acc.error ? h('p', { class: 'acct-err' }, acc.error) : null,
      acc.status === 'needs_reconnect' ? h('p', { class: 'note' }, 'Use "Connect Google account" below and sign in as this account again.') : null,
      h('div', { class: 'svcs' }, [svcToggle('calendar', 'Calendar'), svcToggle('mail', 'Mail')]),
      err,
      h('div', { class: 'acct-f' }, [disc, ui.confirm === key ? null : askBtn(key, 'Disconnect')]),
      list, calErr,
      ui.confirm === key ? confirmRow('Disconnect ' + acc.email + '? Callsheet forgets its data.', 'Disconnect', function (y, c, e) {
        run([y, c], e, function () { return api('DELETE', 'api/accounts/' + encodeURIComponent(acc.id)); }, function () {
          ui.confirm = null; delete ui.openCals[acc.id]; return reload('connect:' + a.id);
        });
      }) : null]);
  }
  function connectBlock(a) {
    var err = errBox(), flow = ui.connect[a.id];
    if (!S.google_client.configured) {
      var hint = 'hint-' + a.id;
      return h('div', { class: 'connect' }, [
        h('button', { type: 'button', class: 'btn', disabled: true, 'aria-describedby': hint }, 'Connect Google account'),
        h('p', { class: 'note', id: hint }, 'Add the Google sign-in key above first.')]);
    }
    if (!flow) {
      var b = btn('btn', 'Connect Google account', { 'data-fk': 'connect:' + a.id }, function () {
        run([b], err, function () { return api('POST', 'api/accounts/start', { area_id: a.id }); }, function (r) {
          if (!r || !r.flow_id || !safeHttps(r.auth_url)) { err.textContent = 'Callsheet could not start the Google sign-in.'; return; }
          ui.connect[a.id] = { flow_id: r.flow_id, auth_url: r.auth_url };
          renderSetup('[data-fk="' + cssq('open:' + a.id) + '"]');
        });
      });
      return h('div', { class: 'connect' }, [b, err]);
    }
    var taId = 'paste-' + a.id;
    var ta = h('textarea', { class: 'inp', id: taId, rows: '3', spellcheck: 'false', autocomplete: 'off' });
    var fin = btn('btn primary', 'Finish', null, function () {
      var pasted = ta.value.trim();
      if (!pasted) { err.textContent = 'Paste the address from the error page first.'; return; }
      run([fin, cancel], err, function () { return api('POST', 'api/accounts/finish', { flow_id: flow.flow_id, pasted: pasted }); }, function (acc) {
        delete ui.connect[a.id];
        if (acc && acc.id) ui.openCals[acc.id] = true;
        return reload(acc && acc.id ? 'cals:' + acc.id : 'connect:' + a.id);
      });
    });
    var cancel = btn('tbtn', 'Cancel', null, function () { delete ui.connect[a.id]; renderSetup('[data-fk="' + cssq('connect:' + a.id) + '"]'); });
    return h('div', { class: 'connect flow', role: 'group', 'aria-label': 'Connect a Google account to ' + a.name }, [
      h('ol', { class: 'flowsteps' }, [
        h('li', null, h('a', { class: 'btn primary', href: flow.auth_url, target: '_blank', rel: 'noopener noreferrer', 'data-fk': 'open:' + a.id }, 'Open Google sign-in')),
        h('li', null, "After you approve, your browser shows an error page at 127.0.0.1. That's expected: copy that page's full address."),
        h('li', null, [h('label', { class: 'label', for: taId }, 'Paste the address here'), ta,
          h('div', { class: 'btns' }, [fin, cancel])])]),
      err]);
  }
  /* Slack (read-only, one workspace per area): the 3-step connect panel, or the connected workspace with its
     ignore list and Disconnect. The manifest and its create-app URL come from api/slack/manifest. */
  function copyText(text, b, label, fallbackHost) {
    function said(t) { b.textContent = t; setTimeout(function () { b.textContent = label; }, 2000); }
    function selectIt() {
      var ta = h('textarea', { class: 'inp mfst', rows: '6', readonly: '', spellcheck: 'false', 'aria-label': 'App manifest' });
      ta.value = text;
      fallbackHost.textContent = ''; add(fallbackHost, ta);
      var ok = false;
      try { ta.focus(); ta.select(); ok = document.execCommand && document.execCommand('copy'); } catch (e) { ok = false; }
      said(ok ? 'Copied' : 'Selected: press Ctrl+C');
    }
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(function () { said('Copied'); }, selectIt);
    else selectIt();
  }
  function slackName(ws) { return ws.domain ? ws.domain + '.slack.com' : (ws.team_name || 'Slack workspace'); }
  function slackTag(ws) {
    if (ws.status === 'connected') return h('span', { class: 'st' }, 'Connected');
    return h('span', { class: 'st warn' }, ws.status === 'needs_reconnect' ? 'Reconnect needed' : 'Error');
  }
  function loadChannels(ws) {
    ui.slackCh[ws.id] = { loading: true };
    return api('GET', 'api/slack/' + encodeURIComponent(ws.id) + '/channels').then(function (r) {
      ui.slackCh[ws.id] = { channels: (r && r.channels) || [], stale: !!(r && r.stale) };
    }, function (e) { ui.slackCh[ws.id] = { error: e.message }; }).then(function () {
      if (ui.view === 'setup' && ui.step === 2) renderSetup('[data-fk="' + cssq('ign:' + ws.id) + '"]');
    });
  }
  function slackWs(a, ws) {
    var open = !!ui.slackIgn[ws.id], listId = 'ign-' + ws.id, ignored = (ws.ignored || []).slice(), err = errBox();
    var reading = Math.max(0, (Number(ws.channels_total) || 0) - ignored.length);
    var disc = btn('tbtn disc', 'Ignore channels (' + ignored.length + ')', { 'aria-expanded': String(open), 'aria-controls': listId, 'data-fk': 'ign:' + ws.id }, function () {
      ui.slackIgn[ws.id] = !open;
      if (!open && !(ui.slackCh[ws.id] && ui.slackCh[ws.id].channels)) loadChannels(ws);
      renderSetup('[data-fk="' + cssq('ign:' + ws.id) + '"]');
    });
    var body = null, ch = ui.slackCh[ws.id] || {};
    if (open) {
      var kids = [h('p', { class: 'help' }, 'Tap a channel to ignore it. Ignored channels never show up in the brief.')];
      if (ch.loading) kids.push(h('p', { class: 'help', role: 'status' }, 'Loading channels…'));
      else if (ch.error) kids.push(h('p', { class: 'err', role: 'alert' }, ch.error));
      else if (ch.channels) {
        if (ch.stale) kids.push(h('p', { class: 'help' }, 'Slack didn’t answer, so this is the list from the last refresh.'));
        var chans = ch.channels, many = chans.length > 40 && !ui.slackAll[ws.id];
        kids.push(chans.length ? h('div', { class: 'chips', role: 'group', 'aria-label': 'Channels in ' + slackName(ws) },
          (many ? chans.slice(0, 40) : chans).map(function (c) {
            var on0 = ignored.indexOf(c.id) >= 0;
            var b = btn('chip', '#' + c.name, { 'aria-pressed': String(on0), 'aria-label': 'Ignore #' + c.name + (c.kind === 'private' ? ' (private)' : ''), 'data-ch': c.id }, function () {
              var next;
              run([b], err, function () {
                var cur = (ws.ignored || []).slice(), i = cur.indexOf(c.id);
                if (i >= 0) cur.splice(i, 1); else cur.push(c.id);
                next = cur;
                return api('PATCH', 'api/slack/' + encodeURIComponent(ws.id), { ignored: next });
              }, function (r) {
                ws.ignored = (r && r.ignored) || next;
                b.setAttribute('aria-pressed', String(ws.ignored.indexOf(c.id) >= 0));
                disc.textContent = 'Ignore channels (' + ws.ignored.length + ')';
                line.textContent = readingLine(Math.max(0, (Number(ws.channels_total) || 0) - ws.ignored.length));
              });
            });
            return b;
          }).concat(many ? [btn('tbtn', 'Show all ' + chans.length, { 'data-fk': 'ignall:' + ws.id }, function () {
            ui.slackAll[ws.id] = true; renderSetup('[data-fk="' + cssq('ign:' + ws.id) + '"]');
          })] : [])) : h('p', { class: 'help' }, 'You’re not in any channels in this workspace.'));
      }
      body = h('div', { class: 'ign', id: listId }, kids.concat([err]));
    } else body = h('div', { id: listId, hidden: true });
    function readingLine(n) { return 'Reading ' + plural(n, 'channel', 'channels') + ' you’re in, your DMs and group DMs'; }
    var line = h('p', { class: 'line' }, readingLine(reading));
    var key = 'slack:' + ws.id;
    return h('div', { class: 'slws' }, [
      h('div', { class: 'acct-h' }, [h('span', { class: 'em' }, slackName(ws)), slackTag(ws)]),
      ws.status !== 'connected' && ws.error ? h('p', { class: 'acct-err' }, ws.error) : null,
      ws.status === 'needs_reconnect' ? h('p', { class: 'note' }, 'Use "Reconnect Slack workspace" below and paste a new User OAuth Token.') : null,
      line, disc, body,
      ui.confirm === key ? confirmRow('Disconnect ' + slackName(ws) + '? Callsheet forgets the token and what it read. You can remove the app in Slack too.', 'Disconnect', function (y, c, e) {
        run([y, c], e, function () { return api('DELETE', 'api/slack/' + encodeURIComponent(ws.id)); }, function () {
          ui.confirm = null; delete ui.slackCh[ws.id]; delete ui.slackIgn[ws.id]; return reload('slack:' + a.id);
        });
      }) : h('div', { class: 'acct-f' }, askBtn(key, 'Disconnect'))]);
  }
  function slackBlock(a) {
    var wss = a.slack || [], open = !!ui.slackOpen[a.id], panelId = 'sp-' + a.id;
    var live = wss.filter(function (w) { return w.status !== 'needs_reconnect'; });
    var kids = [h('h4', { class: 'label slh' }, 'Slack')];
    if (!wss.length) kids.push(h('div', { class: 'acct-h none' }, [h('span', { class: 'muted' }, 'No workspace yet'), h('span', { class: 'st off' }, 'Not connected')]));
    wss.forEach(function (ws) { kids.push(slackWs(a, ws)); });
    if (!live.length) {
      var cb = btn('act', wss.length ? 'Reconnect Slack workspace' : 'Connect Slack workspace', { 'aria-expanded': String(open), 'aria-controls': panelId, 'data-fk': 'slack:' + a.id }, function () {
        ui.slackOpen[a.id] = !open;
        if (!open && (!ui.slackMan || ui.slackMan.error)) {
          api('GET', 'api/slack/manifest').then(function (m) { ui.slackMan = m || {}; }, function (e) { ui.slackMan = { error: e.message }; })
            .then(function () { if (ui.view === 'setup' && ui.step === 2) renderSetup('[data-fk="' + cssq('slack:' + a.id) + '"]'); });
        }
        renderSetup('[data-fk="' + cssq('slack:' + a.id) + '"]');
      });
      kids.push(h('div', { class: 'cbtn' }, cb));
      if (open) kids.push(slackSteps(a, panelId));
    }
    return h('div', { class: 'slk' }, kids);
  }
  function slackSteps(a, panelId) {
    var m = ui.slackMan, err = errBox(), fb = h('div', { class: 'mfw' });
    var create = m && safeHttps(m.create_url);
    var text = m && m.manifest ? JSON.stringify(m.manifest, null, 2) : (m && m.manifest_json) || '';
    var copy = btn('btn sm', 'Copy the app manifest', { disabled: !text }, function () { copyText(text, copy, 'Copy the app manifest', fb); });
    var tokId = 'tok-' + a.id;
    var tok = h('input', { class: 'inp', type: 'password', id: tokId, autocomplete: 'off', spellcheck: 'false', placeholder: 'Paste the token', 'data-fk': 'tok:' + a.id });
    var go = h('button', { type: 'submit', class: 'btn sm primary' }, 'Connect');
    var form = on(h('form', { class: 'row2', novalidate: '' }, [h('label', { class: 'sr', for: tokId }, 'Paste the token'), tok, go]), 'submit', function (e) {
      e.preventDefault();
      var v = tok.value.trim();
      if (!v) { err.textContent = 'Paste the User OAuth Token first.'; return; }
      run([go], err, function () { return api('POST', 'api/slack/connect', { area_id: a.id, token: v }); }, function (ws) {
        tok.value = ''; delete ui.slackOpen[a.id];
        return reload(ws && ws.id ? 'ign:' + ws.id : 'slack:' + a.id);
      });
    });
    return h('ol', { class: 'steps', id: panelId, 'aria-label': 'Connect a Slack workspace to ' + a.name }, [
      h('li', null, [h('span', { class: 'num', 'aria-hidden': 'true' }, '1'), h('div', null, [
        h('div', { class: 'row2' }, [
          create ? h('a', { class: 'btn sm primary', href: create, target: '_blank', rel: 'noopener', 'data-fk': 'create:' + a.id }, 'Create the Callsheet app in Slack')
            : h('button', { type: 'button', class: 'btn sm primary', disabled: true }, m ? 'Create the Callsheet app in Slack' : 'Loading…'),
          copy]),
        m && m.error ? h('p', { class: 'err', role: 'alert' }, m.error) : null, fb,
        h('p', { class: 'help' }, 'Slack opens with the app’s settings filled in. It can only read: no posting.')])]),
      h('li', null, [h('span', { class: 'num', 'aria-hidden': 'true' }, '2'), h('div', null,
        ['Install it to your workspace, then copy the User OAuth Token (it starts with ', h('code', null, 'xoxp-'), ').'])]),
      h('li', null, [h('span', { class: 'num', 'aria-hidden': 'true' }, '3'), h('div', null, [form, err])])]);
  }
  function chatNames(a) {
    var live = (a.slack || []).some(function (w) { return w.status !== 'needs_reconnect'; });
    return live && a.signal ? 'Slack and Signal' : a.signal ? 'Signal' : 'Slack';
  }
  function areaCard(a) {
    var c = clampColour(a.colour), reads = a.reads || {}, err = errBox();
    function readChip(key, label) {
      var b = btn('chip', label, { 'aria-pressed': String(key === 'writer' ? reads[key] !== false : !!reads[key]) }, function () {
        var next;
        run([b], err, function () {
          next = { calendar: !!reads.calendar, mail: !!reads.mail, chat: !!reads.chat, tasks: !!reads.tasks, writer: reads.writer !== false };
          next[key] = !next[key];
          return api('PATCH', 'api/areas/' + encodeURIComponent(a.id), { reads: next });
        }, function () {
          reads[key] = next[key]; a.reads = reads; b.setAttribute('aria-pressed', String(next[key]));
        });
      });
      return b;
    }
    function later(label, why) { return h('button', { type: 'button', class: 'chip later', disabled: true, 'aria-pressed': 'false' }, [label, h('span', { class: 'cl' }, ' · ' + (why || 'Coming later'))]); }
    var accts = a.accounts || [], key = 'area:' + a.id;
    return h('div', { class: 'acard a' + c }, [
      h('h3', { class: 'areahead', tabindex: '-1', 'data-fk': 'areah:' + a.id }, a.name),
      accts.length ? h('div', { class: 'accts' }, accts.map(function (x) { return accountRow(a, x); })) : h('p', { class: 'note none' }, 'No Google account yet.'),
      connectBlock(a),
      slackBlock(a),
      h('div', { class: 'reads' }, [h('div', { class: 'q', id: 'q-' + a.id }, 'What may the brief read?'),
        h('div', { class: 'chips', role: 'group', 'aria-labelledby': 'q-' + a.id }, [readChip('calendar', 'Calendar'), readChip('mail', 'Mail'),
          a.chat_available ? readChip('chat', 'Chat') : later('Chat', 'connect Slack or link Signal first'), readChip('writer', 'AI writer'), later('Tasks')]),
        err, h('p', { class: 'note' }, a.chat_available ? 'Chat is ' + chatNames(a) + '. Check your employer\u2019s policy first.' : 'Check your employer\u2019s policy first.')]),
      a.fixed ? null : ui.confirm === key ? confirmRow('Delete ' + a.name + '? Its accounts are disconnected and Callsheet forgets their data.', 'Delete', function (y, cn, e) {
        run([y, cn], e, function () { return api('DELETE', 'api/areas/' + encodeURIComponent(a.id)); }, function () { ui.confirm = null; return reload('add-name'); });
      }) : h('div', { class: 'acct-f' }, askBtn(key, 'Delete area'))]);
  }
  function addAreaCard() {
    var err = errBox(), na = ui.newArea;
    var name = on(h('input', { class: 'inp', type: 'text', id: 'f-area', value: na.name, autocomplete: 'off', 'data-fk': 'add-name' }), 'input', function () { na.name = name.value; });
    var sws = h('div', { class: 'swatches', role: 'group', 'aria-labelledby': 'lbl-swatch' }, [1, 2, 3, 4].map(function (k) {
      return btn('a' + k, null, { 'aria-pressed': String(na.colour === k), 'aria-label': 'Colour ' + k }, function (e) {
        na.colour = k; sws.querySelectorAll('button').forEach(function (x) { x.setAttribute('aria-pressed', String(x === e.currentTarget)); });
      });
    }));
    var addB = h('button', { type: 'submit', class: 'btn primary' }, 'Add');
    return on(h('form', { class: 'acard addcard', novalidate: '' }, [
      h('h3', { class: 'big' }, '+ Add an area'),
      h('div', null, [h('label', { class: 'label', for: 'f-area' }, 'Name'), name]),
      h('div', null, [h('span', { class: 'label', id: 'lbl-swatch' }, 'Header colour'), sws]),
      h('div', { class: 'btns' }, addB), err,
      h('p', { class: 'note' }, 'Each area gets its own column on the brief.')]), 'submit', function (e) {
      e.preventDefault();
      var nm = name.value.trim();
      if (!nm) { err.textContent = 'Give the area a name.'; return; }
      run([addB], err, function () { return api('POST', 'api/areas', { name: nm, colour: na.colour }); }, function (r) {
        ui.newArea = { name: '', colour: na.colour % 4 + 1 };
        return reload(r && r.id ? 'areah:' + r.id : 'add-name');
      });
    });
  }
  function stepAreas() {
    return [
      h('p', { class: 'intro' }, 'One area per part of your life. Personal is always there; add one per employer.'),
      keyPanel(),
      h('div', { class: 'cards' }, S.areas.map(areaCard).concat([addAreaCard()])),
      signalCard(),
      h('div', { class: 'callout' }, [h('div', { class: 'h' }, 'Always'),
        h('p', null, 'The brief reads and drafts. It never sends, accepts or deletes anything.')])];
  }

  /* step 3: VIPs */
  function areaById(id) { return S.areas.filter(function (a) { return a.id === id; })[0] || { name: String(id || ''), colour: 4 }; }
  function stepVips() {
    var err = errBox(), ageErr = errBox(), vips = S.vips;
    var rows = vips.map(function (v) {
      var a = areaById(v.area_id), rmErr = errBox();
      var rm = btn('rm', '×', { 'aria-label': 'Remove ' + (v.name || v.email) }, function () {
        run([rm], rmErr, function () { return api('DELETE', 'api/vips/' + encodeURIComponent(v.id)); }, function () { return reload('vip-name'); });
      });
      return h('tr', null, [h('td', { class: 'who' }, v.name), h('td', { class: 'em' }, v.email),
        h('td', null, h('span', { class: 'tag a' + clampColour(a.colour) }, a.name)), h('td', { class: 'x' }, [rm, rmErr])]);
    });
    var nm = h('input', { class: 'inp', type: 'text', id: 'f-vname', autocomplete: 'off', 'data-fk': 'vip-name' });
    var em = h('input', { class: 'inp', type: 'email', id: 'f-vemail', autocomplete: 'off', spellcheck: 'false' });
    var ar = h('select', { class: 'inp', id: 'f-varea' }, S.areas.map(function (a) { return h('option', { value: a.id }, a.name); }));
    var addB = h('button', { type: 'submit', class: 'btn primary' }, 'Add');
    var form = on(h('form', { class: 'vipadd', novalidate: '', 'aria-label': 'Add a VIP' }, [
      h('div', { class: 'field' }, [h('label', { class: 'label', for: 'f-vname' }, 'Name'), nm]),
      h('div', { class: 'field' }, [h('label', { class: 'label', for: 'f-vemail' }, 'Email'), em]),
      h('div', { class: 'field' }, [h('label', { class: 'label', for: 'f-varea' }, 'Area'), h('span', { class: 'selw' }, ar)]),
      h('div', { class: 'field go-f' }, addB), err]), 'submit', function (e) {
      e.preventDefault();
      if (!em.value.trim()) { err.textContent = 'Add an email address.'; return; }
      run([addB], err, function () { return api('POST', 'api/vips', { name: nm.value.trim(), email: em.value.trim(), area_id: ar.value }); },
        function () { return reload('vip-name'); });
    });
    var age = on(h('input', { class: 'inp num', type: 'number', id: 'f-age', min: '1', max: '30', step: '1', value: S.settings.reply_age_days || 2 }), 'change', function () {
      var n = parseInt(age.value, 10);
      if (!(n >= 1) || String(n) !== age.value.trim()) { ageErr.textContent = 'Enter a whole number of days.'; return; }
      patchSettings({ reply_age_days: n }, ageErr);
    });
    function fact(kids) { return h('li', null, [h('span', { class: 'cb sel', 'aria-hidden': 'true' }), h('span', null, kids)]); }
    return [
      h('p', { class: 'intro' }, 'People whose mail always counts as needing a reply.'),
      h('div', { class: 'cols' }, [
        h('div', null, [
          vips.length ? h('table', { class: 'vip' }, [
            h('thead', null, h('tr', null, [h('th', null, 'Name'), h('th', null, 'Email'), h('th', null, 'Area'), h('th', null, h('span', { class: 'sr' }, 'Remove'))])),
            h('tbody', null, rows)]) : h('p', { class: 'note' }, 'No VIPs yet.'),
          h('h2', { class: 'h3 addh' }, 'Add a VIP'), form]),
        h('div', null, [
          h('h2', { class: 'h3' }, 'Needs a reply means'),
          h('ul', { class: 'checks' }, [
            fact('Direct questions to you'),
            fact(h('label', { for: 'f-age' }, ['Unanswered for ', age, ' or more days'])),
            fact('Mentioned deadlines'),
            fact('Anyone on your VIP list')]),
          ageErr,
          h('p', { class: 'note' }, 'Only the number of days can change for now.')])]),
      neverReply()];
  }
  /* "Never needs a reply" rules (api/suppress), made from the brief's notepad menu; the brief's "N hidden · Manage" opens this */
  var NNR_KIND = { thread: 'Thread', sender: 'Sender', domain: 'Domain' };
  function fmtDay(iso) {
    var d = new Date(iso);
    if (!iso || isNaN(d.getTime())) return '';
    try { return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: (S.settings && S.settings.timezone) || undefined }); }
    catch (e) { return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }); }
  }
  function neverReply() {
    var rules = Array.isArray(S.suppress) ? S.suppress : [], err = errBox();
    var rows = rules.map(function (r, i) {
      var rm = btn('tbtn', 'Remove', { 'aria-label': 'Remove ' + (r.label || r.value), 'data-fk': 'nnr:' + r.id }, function () {
        var next = rules[i + 1] || rules[i - 1];
        run([rm], err, function () { return api('DELETE', 'api/suppress/' + encodeURIComponent(r.id)); }, function () {
          return reload(next ? 'nnr:' + next.id : 'nnr-h');
        });
      });
      return h('div', { class: 'mrow', role: 'row' }, [
        h('span', { class: 'nm', role: 'cell' }, [r.label || r.value || '', ' ', h('span', { class: 'k' }, NNR_KIND[r.kind] || r.kind || '')]),
        h('span', { class: 'src', role: 'cell' }, r.source === 'slack' ? 'Slack' : r.source === 'signal' ? 'Signal' : 'Mail'),
        h('span', { class: 'dt', role: 'cell' }, fmtDay(r.created)),
        h('span', { class: 'x', role: 'cell' }, rm)]);
    });
    return h('div', { class: 'nnr' }, [
      h('h2', { class: 'h3', id: 'nnr-h', tabindex: '-1', 'data-fk': 'nnr-h' }, ['Never needs a reply', h('span', { class: 'n' }, 'Hidden from Needs a reply')]),
      h('p', { class: 'note' }, 'Made from the brief: open a row’s menu and pick “Doesn’t need a reply”. Removed rules show their rows again at the next refresh.'),
      rules.length ? h('div', { class: 'mtab', role: 'table', 'aria-labelledby': 'nnr-h' }, [
        h('div', { class: 'mrow h', role: 'row' }, [h('span', { role: 'columnheader' }, 'Thread, sender or domain'), h('span', { role: 'columnheader' }, 'From'),
          h('span', { role: 'columnheader' }, 'Added'), h('span', { role: 'columnheader' }, h('span', { class: 'sr' }, 'Remove'))]),
        rows]) : h('p', { class: 'muted' }, 'No rules yet.'),
      err]);
  }

  /* step 4: call times */
  var CT_DEFAULT = { morning: 'Morning brief', midday: 'Midday check-in', afternoon: 'Afternoon check-in' };
  function stepTimes() {
    var st = S.settings, cts = (st.call_times || []).slice(), skipErr = errBox();
    function save(ct, patch, err, btns, ok) {
      patchSettings(function () {
        var p = typeof patch === 'function' ? patch() : patch;
        return { call_times: cts.map(function (x) { var y = {}; Object.keys(x).forEach(function (k) { y[k] = x[k]; }); if (x.id === ct.id) Object.keys(p).forEach(function (k) { y[k] = p[k]; }); return y; }) };
      }, err, btns, function (sent) { cts = sent.call_times; if (ok) ok(sent.call_times.filter(function (x) { return x.id === ct.id; })[0] || {}); });
    }
    var blocks = cts.map(function (ct) {
      var label = ct.label || CT_DEFAULT[ct.id] || ct.id, err = errBox(), id = 'ct-' + ct.id;
      var disp = h('div', { class: 'disp', 'aria-hidden': 'true' }, ct.time);
      var box;
      var inp = on(h('input', { class: 'inp', type: 'time', id: id, value: ct.time }), 'change', function () {
        if (!inp.value) { err.textContent = 'Pick a time.'; return; }
        var v = inp.value.slice(0, 5);
        save(ct, { time: v }, err, [], function () { disp.textContent = v; });
      });
      var t = sw(ct.enabled, label + ' on', function () {
        save(ct, function () { var cur = cts.filter(function (x) { return x.id === ct.id; })[0] || ct; return { enabled: !cur.enabled }; }, err, [t], function (now) {
          t.setAttribute('aria-pressed', String(!!now.enabled)); box.classList.toggle('off', !now.enabled);
        });
      });
      box = h('div', { class: 'tb' + (ct.id === 'morning' ? ' lead' : '') + (ct.enabled ? '' : ' off') }, [
        h('div', { class: 'label' }, label), disp,
        h('div', { class: 'row' }, [h('label', { class: 'sr', for: id }, label + ' time'), inp, t]), err]);
      return box;
    });
    var skip = sw(st.skip_checkins_days_off, 'Skip check-ins on days off', function () {
      patchSettings(function () { return { skip_checkins_days_off: !st.skip_checkins_days_off }; }, skipErr, [skip], function (sent) {
        skip.setAttribute('aria-pressed', String(sent.skip_checkins_days_off));
      });
    });
    return [
      h('p', { class: 'intro' }, 'Times are ' + (st.timezone || 'your time zone').replace(/_/g, ' ') + '.'),
      h('div', { class: 'times' }, blocks),
      h('div', { class: 'swrow' }, [skip, h('span', { 'aria-hidden': 'true' }, 'Skip check-ins on days off')]), skipErr,
      h('p', { class: 'note' }, 'Callsheet refreshes at these times.')];
  }

  /* step 5: delivery (fixed facts in this version) */
  function stepDelivery() {
    function row(sel, t, s, state) {
      return h('div', { class: 'opt fixed' + (sel ? ' sel' : '') }, [h('span', { class: 'cb' + (sel ? ' sel' : ''), 'aria-hidden': 'true' }),
        h('span', null, [h('span', { class: 't' }, t), h('span', { class: 's' }, s)]), h('span', { class: 'state' }, state)]);
    }
    return [
      h('p', { class: 'intro' }, 'Where the brief shows up.'),
      h('div', { class: 'opts' }, [
        row(true, 'Sidebar page in Home Assistant', 'Opens anywhere your Home Assistant does', 'On'),
        row(pushOn(), 'Phone notifications', 'via the Home Assistant app · set up below', pushOn() ? 'On' : 'Off')]),
      pushBlock(), writerBlock(), chatBlock(), writingBlock(), memoryBlock()];  /* 0.6.0 chat: chatBlock */
  }

  /* step 5: writing instructions. Profiles (Default is built in and read-only); five texts per profile, each saved
     on its own (PATCH api/writer/profiles/<id>); Reset = PATCH "" (the text becomes Default's). The safety rules
     and the style examples are fixed in the server (writer.SAFETY, writer.EXAMPLES) and always come last. */
  var WSECT = [
    ['rules', 'Global rules', 'Applies to everything Callsheet writes.'],
    ['summary', 'Day summary', 'The short paragraph at the top of Briefing.'],
    ['areas', 'Area summaries', 'The few sentences for each area.'],
    ['emails', 'Email summaries', 'The one line in each row and “What they want”.'],
    ['drafts', 'Reply drafts', 'Suggested replies you copy and send yourself.']];
  /* a writing-instructions textarea shows its whole text (up to about 16 lines, then it scrolls) */
  function fitTa(ta) { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight + 4, 400) + 'px'; }
  function refreshWriter(r) { return api('GET', 'api/writer').then(function (w) { S.writer = w || S.writer; return r; }); }
  function writingBlock() {
    var w = writerOf(), profs = Array.isArray(w.prompt_profiles) && w.prompt_profiles.length ? w.prompt_profiles
      : [{ id: 'default', name: 'Default', read_only: true }];
    var defs = w.prompt_defaults || {}, max = Number(w.prompt_max) || 1500, pmax = Number(w.profile_max) || 10;
    var active = w.active_profile || 'default';
    if (!profs.some(function (x) { return x.id === ui.prof; })) ui.prof = profs.some(function (x) { return x.id === active; }) ? active : profs[0].id;
    var prof = profs.filter(function (x) { return x.id === ui.prof; })[0], ro = !!prof.read_only || prof.id === 'default';
    var owned = profs.filter(function (x) { return !(x.read_only || x.id === 'default'); }).length;
    var err = errBox();
    function show(focusKey) { renderSetup('[data-fk="' + cssq(focusKey) + '"]'); }

    var sel = on(h('select', { class: 'inp', id: 'wi-prof', 'data-fk': 'wi-prof' }, profs.map(function (x) {
      return h('option', { value: x.id, selected: x.id === prof.id }, x.name + (x.id === 'default' ? ' (read-only)' : '') + (x.id === active ? ' · in use' : ''));
    })), 'change', function () { ui.prof = sel.value; ui.profMode = null; ui.confirm = null; show('wi-prof'); });
    var full = owned >= pmax;
    var copyB = btn('act', 'Copy profile', { 'data-fk': 'wi-copy', disabled: full || ui.profMode === 'copy', 'aria-expanded': String(ui.profMode === 'copy') }, function () {
      ui.profMode = 'copy'; ui.confirm = null; show('wi-name');
    });

    /* the inline name form (Copy profile, Rename) */
    var form = null;
    if (ui.profMode === 'copy' || (ui.profMode === 'rename' && !ro)) {
      var copying = ui.profMode === 'copy', fErr = errBox();
      var names = profs.map(function (x) { return x.name; }), guess = prof.name + ' copy', i = 2;
      while (copying && names.indexOf(guess) >= 0) guess = prof.name + ' copy ' + (i++);
      var nm = h('input', { class: 'inp', type: 'text', id: 'wi-name', maxlength: '40', autocomplete: 'off', value: copying ? guess : prof.name, 'data-fk': 'wi-name' });
      var ok = h('button', { type: 'submit', class: 'btn sm primary' }, copying ? 'Make the copy' : 'Save name');
      var cancel = btn('btn sm', 'Cancel', null, function () { ui.profMode = null; show(copying ? 'wi-copy' : 'wi-rename'); });
      form = on(h('form', { class: 'wiform', novalidate: '' }, [
        h('label', { class: 'label', for: 'wi-name' }, copying ? 'Name of the copy of ' + prof.name : 'New name for ' + prof.name),
        h('div', { class: 'row2' }, [nm, ok, cancel]), fErr]), 'submit', function (e) {
        e.preventDefault();
        var v = nm.value.trim();
        if (!v) { fErr.textContent = 'Give the profile a name.'; return; }
        run([ok, cancel], fErr, function () {
          return (copying ? api('POST', 'api/writer/profiles', { copy_from: prof.id, name: v })
            : api('PATCH', 'api/writer/profiles/' + encodeURIComponent(prof.id), { name: v })).then(refreshWriter);
        }, function (r) {
          ui.profMode = null;
          if (copying && r && r.id) ui.prof = r.id;
          show('wi-prof');
        });
      });
    }

    var useRow = [];
    if (prof.id === active) useRow.push(h('span', { class: 'st', 'data-fk': 'wi-inuse', tabindex: '-1' }, 'In use'));
    else {
      var useB = btn('act', 'Use this profile', { 'data-fk': 'wi-use' }, function () {
        run([useB], err, function () { return api('PATCH', 'api/writer', { active_profile: prof.id }); }, function (r) { S.writer = r || S.writer; show('wi-inuse'); });
      });
      useRow.push(useB);
    }
    if (!ro && ui.profMode !== 'rename') useRow.push(btn('tbtn', 'Rename', { 'data-fk': 'wi-rename' }, function () { ui.profMode = 'rename'; ui.confirm = null; show('wi-name'); }));
    var pkey = 'prof:' + prof.id;
    if (!ro && prof.id !== active && ui.confirm !== pkey) useRow.push(askBtn(pkey, 'Delete'));
    var delConfirm = !ro && prof.id !== active && ui.confirm === pkey ? confirmRow('Delete the profile ' + prof.name + '? Its texts are gone for good.', 'Delete', function (y, c, e) {
      run([y, c], e, function () { return api('DELETE', 'api/writer/profiles/' + encodeURIComponent(prof.id)); }, function (r) {
        S.writer = r || S.writer; ui.confirm = null; ui.prof = null;
        Object.keys(ui.wdraft).forEach(function (k) { if (k.indexOf(prof.id + ':') === 0) delete ui.wdraft[k]; });
        show('wi-prof');
      });
    }) : null;

    var fields = WSECT.map(function (x) {
      var k = x[0], id = 'wi-' + k, dk = prof.id + ':' + k, saved = typeof prof[k] === 'string' ? prof[k] : (defs[k] || '');
      var def = typeof defs[k] === 'string' ? defs[k] : saved, cur = ui.wdraft[dk] != null ? ui.wdraft[dk] : saved;
      var ta = h('textarea', { class: 'inp', id: id, rows: '3', maxlength: String(max), readonly: ro, 'aria-describedby': 'wih-' + k + (ro ? '' : ' wic-' + k), 'data-fk': 'wi:' + k });
      ta.value = cur;
      var top = [h('label', { for: id }, x[1])];
      if (ro) return h('div', { class: 'wfield' + (k === 'rules' ? ' global' : '') }, [h('div', { class: 'top2' }, top), h('div', { class: 'help', id: 'wih-' + k }, x[2]), ta]);
      var fErr = errBox(), said = h('span', { class: 'said', role: 'status' });
      var cnt = h('span', { class: 'cnt', id: 'wic-' + k }, cur.length + ' / ' + max + ' characters');
      var resetB = btn('tbtn', 'Reset', { 'data-fk': 'wir:' + k, 'aria-label': 'Reset ' + x[1] + ' to the Default text', disabled: saved === def && cur === def }, function () {
        if (saved === def) { delete ui.wdraft[dk]; show('wi:' + k); return; }
        var body = {}; body[k] = '';
        run([resetB, saveB], fErr, function () { return api('PATCH', 'api/writer/profiles/' + encodeURIComponent(prof.id), body).then(refreshWriter); },
          function () { delete ui.wdraft[dk]; show('wi:' + k); });
      });
      var saveB = btn('act', 'Save', { 'data-fk': 'wis:' + k, 'aria-label': 'Save ' + x[1], disabled: cur === saved }, function () {
        var body = {}; body[k] = ta.value;
        run([resetB, saveB], fErr, function () { return api('PATCH', 'api/writer/profiles/' + encodeURIComponent(prof.id), body).then(refreshWriter); },
          function () { delete ui.wdraft[dk]; show('wi:' + k); var sd = main.querySelector('[data-said="' + k + '"]'); if (sd) sd.textContent = 'Saved'; });
      });
      said.setAttribute('data-said', k);
      on(ta, 'input', function () {
        ui.wdraft[dk] = ta.value; said.textContent = ''; fitTa(ta);
        if (ta.value === saved) delete ui.wdraft[dk];
        cnt.textContent = ta.value.length + ' / ' + max + ' characters';
        cnt.classList.toggle('full', ta.value.length >= max);
        saveB.disabled = ta.value === saved; resetB.disabled = saved === def && ta.value === def;
      });
      if (cur.length >= max) cnt.classList.add('full');
      top.push(resetB);
      return h('div', { class: 'wfield' + (k === 'rules' ? ' global' : '') }, [h('div', { class: 'top2' }, top), h('div', { class: 'help', id: 'wih-' + k }, x[2]), ta,
        h('div', { class: 'wfoot' }, [cnt, h('span', { class: 'btns' }, [said, saveB])]), fErr]);
    });

    var fx = w.fixed || {};
    var exText = h('p', { class: 'fixtext', id: 'wi-ex', hidden: !ui.wexOpen }, fx.examples || '');
    var exB = btn('exfold', 'Style examples ' + (ui.wexOpen ? '▾' : '▸'),
      { 'aria-expanded': String(!!ui.wexOpen), 'aria-controls': 'wi-ex' }, function () {
        ui.wexOpen = !ui.wexOpen; exText.hidden = !ui.wexOpen;
        exB.setAttribute('aria-expanded', String(ui.wexOpen));
        exB.textContent = 'Style examples ' + (ui.wexOpen ? '▾' : '▸');
      });
    var exFold = h('div', { class: 'exwrap' }, [exB, exText]);
    return h('div', { class: 'where wi' }, [
      h('div', { class: 'shd' }, [h('h2', { class: 'h3' }, 'Writing instructions'),
        h('label', { class: 'sr', for: 'wi-prof' }, 'Profile'), h('span', { class: 'selw' }, sel), copyB]),
      h('p', { class: 'note' }, 'Tell Callsheet how to write. Plain words work best. These change tone and length, not the facts.' +
        (w.provider && w.provider !== 'off' ? '' : ' They’re used when the writer is on.')),
      full ? h('p', { class: 'note' }, 'You have ' + pmax + ' profiles, the most there can be. Delete one to make another copy.') : null,
      form,
      h('div', { class: 'wiuse' }, useRow), err, delConfirm,
      ro ? h('p', { class: 'hintro' }, 'Default is read-only. Copy it to make changes.') : null,
      fields,
      h('div', { class: 'locked', role: 'group', 'aria-labelledby': 'wi-lock' }, [
        h('div', { class: 'lh', id: 'wi-lock' }, [ico('lock'), 'Always applied (can’t be changed)']),
        /* the exact texts from the server (api/writer fixed), shown as they are */
        fx.after_drafts ? h('p', { class: 'fixtext' }, fx.after_drafts) : null,
        h('p', { class: 'fixtext' }, fx.safety || ''),
        h('p', { class: 'muted' }, 'The first rule follows your reply instructions; the rest always come last, so nothing you ' +
          'write can switch them off. The style examples are fixed too.'),
        fx.examples ? exFold : null])]);
  }

  /* step 5: the AI writer. Off / Claude / Local model; keys are write-only (the server returns hints). */
  var CLAUDE_MODELS = [
    ['claude-opus-5-5', 'Claude Opus 5.5', '$4 in / $20 out'],
    ['claude-sonnet-5-5', 'Claude Sonnet 5.5', '$2 in / $10 out'],
    ['claude-haiku-5-5', 'Claude Haiku 5.5', '$0.10 in / $0.50 out']];
  var LOCAL_URL_EG = 'http://192.168.10.157:8080/v1', LOCAL_MODEL_EG = 'gemma-4-26b-a4b-it-qat-q4_0';
  function writerOf() {
    var w = S.writer || {};
    w.claude = w.claude || {}; w.local = w.local || {};
    return w;
  }
  function writerName(w) {
    if (w.provider === 'claude') {
      var m = CLAUDE_MODELS.filter(function (x) { return x[0] === w.claude.model; })[0];
      return m ? m[1] : 'Claude';
    }
    if (w.provider === 'local') return 'Local model' + (w.local.model ? ' · ' + w.local.model : '');
    return 'rules only';
  }
  function fmtNum(n) { n = Number(n) || 0; return n.toLocaleString('en-US'); }
  function lastWriterLine() {
    var lw = S.last_writer || {};
    if (!lw.at) return 'The writer hasn\u2019t run yet.';
    if (lw.ok) return 'Last run ' + fmtWhen(lw.at, true) + ' · ' + (lw.by || 'AI writer') + ' · ' +
      fmtNum(lw.input_tokens) + ' in / ' + fmtNum(lw.output_tokens) + ' out tokens';
    return 'Last run ' + fmtWhen(lw.at, true) + ' didn\u2019t work: ' + (lw.error || 'unknown problem.');
  }
  function writerBlock() {
    var w = writerOf(), err = errBox();
    function patchWriter(body, e, btns, ok, focusKey) {
      return run(btns || [], e || err, function () { return api('PATCH', 'api/writer', typeof body === 'function' ? body() : body); }, function (r) {
        S.writer = r || S.writer;
        if (ok) ok(r);
        if (focusKey) renderSetup('[data-fk="' + cssq(focusKey) + '"]');
      });
    }
    function choice(val, t, sub) {
      var sel = (w.provider || 'off') === val;
      return btn('opt' + (sel ? ' sel' : ''), [h('span', { class: 'cb' + (sel ? ' sel' : ''), 'aria-hidden': 'true' }),
        h('span', null, [h('span', { class: 't' }, t), h('span', { class: 's' }, sub)]),
        h('span', { class: 'state' }, sel ? 'Selected' : '')], { role: 'radio', 'aria-checked': String(sel), 'data-fk': 'wp:' + val }, function (e) {
        if (sel) return;
        patchWriter({ provider: val }, err, [e.currentTarget], null, 'wp:' + val);
      });
    }
    var opts = h('div', { class: 'opts', role: 'radiogroup', 'aria-label': 'Who writes the brief' }, [
      choice('off', 'Off', 'The brief from the rules only, as before'),
      choice('claude', 'Claude (cloud)', 'Anthropic API key · only the brief\u2019s inputs are sent'),
      choice('local', 'Local model', 'Any OpenAI-compatible server on your network: llama.cpp, Ollama, LM Studio')]);

    /* Claude: key + model */
    var cErr = errBox(), cKeyHint = w.claude.key_hint;
    var cKey = h('input', { class: 'inp', type: 'password', id: 'w-ckey', autocomplete: 'off', spellcheck: 'false',
      placeholder: cKeyHint ? 'Saved key ' + cKeyHint : 'sk-ant-…', 'data-fk': 'w-ckey' });
    var cSave = btn('btn', 'Save key', { 'data-fk': 'w-csave' }, function () {
      var v = cKey.value.trim();
      if (!v) { cErr.textContent = 'Paste your Anthropic API key first.'; return; }
      patchWriter(function () { return { claude: { api_key: v } }; }, cErr, [cSave], function () { cKey.value = ''; }, 'w-csave');
    });
    var cDel = cKeyHint ? btn('tbtn', 'Remove key', { 'data-fk': 'w-cdel' }, function () {
      run([cDel], cErr, function () { return api('DELETE', 'api/writer/claude-key'); }, function (r) { S.writer = r || S.writer; renderSetup('[data-fk="w-ckey"]'); });
    }) : null;
    var cModel = on(h('select', { class: 'inp', id: 'w-cmodel' }, CLAUDE_MODELS.map(function (m) {
      return h('option', { value: m[0], selected: m[0] === (w.claude.model || 'claude-opus-5-5') }, m[1] + ' · ' + m[2]);
    })), 'change', function () { patchWriter({ claude: { model: cModel.value } }, cErr, [cModel]); });
    var cTest = btn('act', 'Test key', { 'data-fk': 'w-ctest' }, function () { testWriter('claude', cTest, cRes, cErr); });
    var cRes = h('span', { class: 'testres', role: 'status' });
    var claude = h('div', { class: 'wpanel' + (w.provider === 'claude' ? '' : ' off'), role: 'group', 'aria-labelledby': 'w-ch' }, [
      h('h3', { class: 'label wh', id: 'w-ch' }, 'Claude'),
      h('div', { class: 'lm' }, [
        h('div', { class: 'field' }, [h('label', { class: 'label', for: 'w-ckey' }, cKeyHint ? 'Anthropic API key (saved ' + cKeyHint + ')' : 'Anthropic API key'), cKey,
          h('div', { class: 'btns kb' }, [cSave, cDel])]),
        h('div', { class: 'field' }, [h('label', { class: 'label', for: 'w-cmodel' }, 'Model · USD per million tokens'), cModel]),
        h('div', { class: 'testrow' }, [cTest, cRes])]),
      cErr,
      h('p', { class: 'note' }, 'Get a key at console.anthropic.com. API use is billed by Anthropic separately from Claude subscriptions (Pro, Max).')]);

    /* Local model: URL, model, optional key, Test connection */
    var lErr = errBox(), lKeyHint = w.local.key_hint;
    var lUrl = on(h('input', { class: 'inp', type: 'url', id: 'w-lurl', value: w.local.base_url || '', placeholder: LOCAL_URL_EG, autocomplete: 'off', spellcheck: 'false' }), 'change', function () {
      patchWriter({ local: { base_url: lUrl.value.trim() } }, lErr, [], function (r) { lUrl.value = (r && r.local && r.local.base_url) || ''; });
    });
    var lModel = on(h('input', { class: 'inp', type: 'text', id: 'w-lmodel', value: w.local.model || '', placeholder: LOCAL_MODEL_EG, autocomplete: 'off', spellcheck: 'false' }), 'change', function () {
      patchWriter({ local: { model: lModel.value.trim() } }, lErr, []);
    });
    var lKey = on(h('input', { class: 'inp', type: 'password', id: 'w-lkey', autocomplete: 'off', spellcheck: 'false', placeholder: lKeyHint ? 'Saved key ' + lKeyHint : 'none' }), 'change', function () {
      var v = lKey.value.trim();
      if (!v) return;
      patchWriter({ local: { api_key: v } }, lErr, [], function () { lKey.value = ''; }, 'w-lkey');
    });
    var lDel = lKeyHint ? btn('tbtn', 'Remove key', { 'data-fk': 'w-ldel' }, function () {
      patchWriter({ local: { api_key: '' } }, lErr, [lDel], null, 'w-lkey');
    }) : null;
    var lTest = btn('act', 'Test connection', { 'data-fk': 'w-ltest' }, function () { testWriter('local', lTest, lRes, lErr); });
    var lRes = h('span', { class: 'testres', role: 'status' });
    /* Thinking (off by default) and Advanced: max tokens, context size, timeout */
    var thErr = errBox();
    var th = sw(w.local.thinking === true, 'Thinking', function () {
      patchWriter(function () { return { local: { thinking: writerOf().local.thinking !== true } }; }, thErr, [th], function (r) { th.setAttribute('aria-pressed', String(!!(r.local && r.local.thinking))); });
    });
    var advErr = errBox();
    function numField(id, key, label, min, max, note) {
      var inp = on(h('input', { class: 'inp', type: 'number', id: id, min: String(min), max: String(max), step: '1', inputmode: 'numeric', value: w.local[key] != null ? String(w.local[key]) : '' }), 'change', function () {
        var v = inp.value.trim();
        if (!v && !note) return;  /* optional fields (with a note): empty clears the setting */
        var body = { local: {} }; body.local[key] = v ? Number(v) : null;
        patchWriter(body, advErr, [], function (r) { if (r && r.local) inp.value = r.local[key] != null ? String(r.local[key]) : ''; });
      });
      return h('div', { class: 'field' }, [h('label', { class: 'label', for: id }, label), inp,
        note ? h('p', { class: 'note' }, note) : null]);
    }
    /* evaluation tools (off by default): a switch, and the way to the Writer evaluation page */
    var ev = S.eval || {}, evErr = errBox();
    var evSw = sw(ev.enabled === true, 'Show the evaluation tools', function () {
      run([evSw], evErr, function () { return api('PATCH', 'api/eval/settings', { enabled: !(S.eval && S.eval.enabled) }); }, function (r) {
        S.eval = r || S.eval; renderSetup('[data-fk="ev-sw"]');
      });
    });
    evSw.setAttribute('data-fk', 'ev-sw');
    var advP = h('div', { id: 'w-adv', hidden: !ui.writerAdv }, [
      h('div', { class: 'lm' }, [
        numField('w-lmax', 'max_tokens', 'Max tokens for the answer', 512, 32768),
        numField('w-lctx', 'context', 'Context size of the server (tokens)', 2048, 1048576),
        numField('w-ltime', 'timeout', 'Timeout (seconds)', 30, 1800),
        numField('w-lslot', 'id_slot', 'Server slot for briefs', 0, 63,
          'Optional. Pins Callsheet\u2019s requests to one llama.cpp slot. Leave empty unless you know you need it.')]),
      h('div', { class: 'swrow' }, [evSw, h('span', { 'aria-hidden': 'true' }, 'Show the evaluation tools')]), evErr,
      h('p', { class: 'note' }, 'Compare two to four writer setups on today\u2019s data, side by side and without names. It only runs when you press its button.'),
      ev.enabled ? h('div', { class: 'testrow' }, btn('act', 'Writer evaluation', { 'data-fk': 'ev-open' }, function () { location.hash = 'eval'; })) : null]);
    var adv = btn('tbtn disc', 'Advanced', { 'aria-expanded': String(!!ui.writerAdv), 'aria-controls': 'w-adv', 'data-fk': 'w-adv' }, function () {
      ui.writerAdv = !ui.writerAdv;
      adv.setAttribute('aria-expanded', String(ui.writerAdv)); advP.hidden = !ui.writerAdv;
    });
    var local = h('div', { class: 'wpanel' + (w.provider === 'local' ? '' : ' off'), role: 'group', 'aria-labelledby': 'w-lh' }, [
      h('h3', { class: 'label wh', id: 'w-lh' }, 'Local model'),
      h('div', { class: 'lm' }, [
        h('div', { class: 'field' }, [h('label', { class: 'label', for: 'w-lurl' }, 'Server URL'), lUrl]),
        h('div', { class: 'field' }, [h('label', { class: 'label', for: 'w-lmodel' }, 'Model'), lModel]),
        h('div', { class: 'field' }, [h('label', { class: 'label', for: 'w-lkey' }, lKeyHint ? 'API key (optional, saved ' + lKeyHint + ')' : 'API key (optional)'), lKey,
          lDel ? h('div', { class: 'btns kb' }, lDel) : null]),
        h('div', { class: 'testrow' }, [lTest, lRes])]),
      lErr,
      h('div', { class: 'swrow' }, [th, h('span', { 'aria-hidden': 'true' }, 'Thinking')]), thErr,
      h('p', { class: 'note' }, 'Slower. Off is recommended for this model.'),
      adv, advP, advErr,
      h('p', { class: 'note' }, 'A first run can take a minute while the server loads the model.'),
      presetsBlock(w)]);  /* 0.6.1 */

    var fbErr = errBox(), sgErr = errBox();
    var fb = sw(w.fallback_to_claude, 'If the local model fails, use Claude instead', function () {
      patchWriter(function () { return { fallback_to_claude: !writerOf().fallback_to_claude }; }, fbErr, [fb], function (r) { fb.setAttribute('aria-pressed', String(!!r.fallback_to_claude)); });
    });
    var sg = sw(w.suggest_replies !== false, 'Suggest replies', function () {
      patchWriter(function () { return { suggest_replies: writerOf().suggest_replies === false }; }, sgErr, [sg], function (r) { sg.setAttribute('aria-pressed', String(r.suggest_replies !== false)); });
    });
    var lw = S.last_writer || {};
    return h('div', { class: 'where writer' }, [
      h('h2', { class: 'h3' }, 'Writer'),
      h('p', { class: 'note' }, 'The rules decide what is in the brief. The writer adds a short summary and suggested replies you can copy. Nothing is ever sent.'),
      opts, err, claude, local,
      h('div', { class: 'swrow' }, [fb, h('span', { 'aria-hidden': 'true' }, 'If the local model fails, use Claude instead')]), fbErr,
      h('p', { class: 'note' }, 'Off keeps your data on your network.'),
      h('div', { class: 'swrow' }, [sg, h('span', { 'aria-hidden': 'true' }, 'Suggest replies')]), sgErr,
      h('p', { class: 'note' }, 'Reads the last few messages of up to 8 threads that need a reply. Areas can opt out in step 2 (AI writer).'),
      h('p', { class: 'syncst' + (lw.at && !lw.ok ? ' bad' : ''), role: 'status' }, lastWriterLine())]);
  }
  /* 0.6.1 writer presets per call time (local model only). Fast = the local settings above. The table maps each
     call kind to a preset (PATCH api/writer call_presets); presets are added with the form or the built-in
     suggestion (POST api/writer/presets, the model id is checked against the server's /v1/models). */
  var PR_EFFORTS = [['', 'Server default'], ['low', 'Low'], ['medium', 'Medium'], ['high', 'High']];
  function presetLine(p) {
    return [p.model || 'no model yet', p.thinking ? 'thinking' + (p.effort ? ' ' + p.effort : '') + ', budget ' + fmtNum(p.reasoning_budget) : 'no thinking',
      fmtNum(p.max_tokens) + ' tokens', p.timeout + ' s'].join(' · ');
  }
  function presetModels(focusSel) {
    if (ui.presetModels && ui.presetModels.busy) return;
    ui.presetModels = { busy: true, models: [], error: null };
    api('GET', 'api/writer/models').then(function (m) { ui.presetModels = { models: (m && m.models) || [], error: m && m.error }; },
      function (e) { ui.presetModels = { models: [], error: e && e.message ? e.message : 'Couldn’t load the model list.' }; })
      .then(function () { if (ui.view === 'setup' && ui.presetEdit) renderSetup(focusSel || null); });
  }
  function presetEdit(p, draft) {
    ui.presetEdit = { id: p ? p.id : null, err: '', draft: draft || (p ? JSON.parse(JSON.stringify(p)) : {
      name: '', model: '', thinking: false, effort: null, kwargs: {}, reasoning_budget: null, max_tokens: null, timeout: null, id_slot: null }) };
    if (!ui.presetModels) presetModels('#pr-name');
    renderSetup('#pr-name');
  }
  function presetForm(patchDone) {
    var e = ui.presetEdit, d = e.draft, err = errBox(), m = ui.presetModels || {};
    if (e.err) err.textContent = e.err;
    var fast = (writerOf().presets || [])[0] || {};
    function field(id, label, ctl, note) {
      return h('div', { class: 'field' }, [h('label', { class: 'label', for: id }, label), ctl, note ? h('p', { class: 'note' }, note) : null]);
    }
    function num(id, key, label, min, max, note) {
      var inp = on(h('input', { class: 'inp', type: 'number', id: id, min: String(min), max: String(max), step: '1', inputmode: 'numeric',
        value: d[key] != null ? String(d[key]) : '', placeholder: fast[key] != null ? String(fast[key]) : '', 'data-fk': id }), 'input', function () {
        var v = inp.value.trim(); d[key] = v === '' ? null : Number(v);
      });
      return field(id, label, inp, note);
    }
    var name = on(h('input', { class: 'inp', type: 'text', id: 'pr-name', value: d.name || '', maxlength: '40', autocomplete: 'off', 'data-fk': 'pr-name' }), 'input', function () { d.name = name.value; });
    var list = h('datalist', { id: 'pr-models' }, (m.models || []).map(function (x) { return h('option', { value: x }); }));
    var model = on(h('input', { class: 'inp', type: 'text', id: 'pr-model', list: 'pr-models', value: d.model || '', maxlength: '120', autocomplete: 'off', spellcheck: 'false', 'data-fk': 'pr-model' }), 'input', function () { d.model = model.value; });
    var mNote = m.busy ? 'Loading the server’s models…' : m.error ? m.error : (m.models || []).length ? 'Pick one the server lists, or type its id.' : null;
    var think = sw(!!d.thinking, 'Thinking', function () { d.thinking = !d.thinking; renderSetup('[data-fk="pr-think"]'); });
    think.setAttribute('data-fk', 'pr-think');
    var effort = on(h('select', { class: 'inp', id: 'pr-effort', 'data-fk': 'pr-effort' }, PR_EFFORTS.map(function (x) {
      return h('option', { value: x[0], selected: (d.effort || '') === x[0] }, x[1]);
    })), 'change', function () { d.effort = effort.value || null; });
    var kw = on(h('input', { class: 'inp', type: 'text', id: 'pr-kwargs', autocomplete: 'off', spellcheck: 'false', 'data-fk': 'pr-kwargs',
      value: d.kwargs && Object.keys(d.kwargs).length ? JSON.stringify(d.kwargs) : '', placeholder: '{"name": value}' }), 'input', function () { d.kwText = kw.value; });
    var save = btn('btn primary', e.id ? 'Save preset' : 'Add preset', { 'data-fk': 'pr-save' }, function () {
      var body = { model: (d.model || '').trim(), thinking: !!d.thinking, effort: d.thinking ? (d.effort || null) : null };
      if (e.id !== 'fast') body.name = (d.name || '').trim();
      ['reasoning_budget', 'max_tokens', 'timeout'].forEach(function (k) { if (d[k] != null) body[k] = d[k]; });
      if (d.id_slot != null || e.id) body.id_slot = d.id_slot != null ? d.id_slot : null;  /* new: empty = Fast's slot */
      var t = d.kwText != null ? d.kwText.trim() : null;
      if (t != null) {
        try { body.kwargs = t ? JSON.parse(t) : {}; } catch (x) { err.textContent = 'Template options must be JSON, like {"name": true}.'; return; }
      }
      run([save], err, function () {
        return e.id ? api('PATCH', 'api/writer/presets/' + encodeURIComponent(e.id), body) : api('POST', 'api/writer/presets', body);
      }, function (r) { ui.presetEdit = null; patchDone(r, 'pr-add'); });
    });
    var cancel = btn('btn', 'Cancel', { 'data-fk': 'pr-cancel' }, function () { ui.presetEdit = null; renderSetup('[data-fk="pr-add"]'); });
    return h('div', { class: 'wpanel prform', role: 'group', 'aria-labelledby': 'pr-h' }, [
      h('h3', { class: 'label wh', id: 'pr-h' }, e.id ? 'Edit ' + (d.name || 'preset') : 'New preset'),
      h('div', { class: 'lm' }, [
        e.id === 'fast' ? null : field('pr-name', 'Name', name),
        field('pr-model', 'Model id', h('span', null, [model, list]), mNote),
        h('div', { class: 'field' }, [h('span', { class: 'label' }, 'Thinking'),
          h('div', { class: 'swrow evsw' }, [think, h('span', { 'aria-hidden': 'true' }, d.thinking ? 'On' : 'Off')])]),
        d.thinking ? field('pr-effort', 'Effort', effort) : null,
        d.thinking ? num('pr-budget', 'reasoning_budget', 'Thinking budget (tokens)', 256, 32768) : null,
        num('pr-max', 'max_tokens', 'Max tokens for the answer', 512, 32768, d.thinking ? 'The thinking budget is added on top.' : null),
        num('pr-time', 'timeout', 'Timeout (seconds)', 30, 1800, d.thinking ? 'A thinking model can take minutes: 900 s is safe.' : null),
        num('pr-slot', 'id_slot', 'Server slot', 0, 63, e.id ? 'Optional. Empty: no slot.' : 'Optional. Empty: the same slot as Fast.'),
        field('pr-kwargs', 'Template options (optional)', kw, 'Extra chat-template options as JSON. Thinking is set by the switch.')]),
      h('div', { class: 'btns' }, [save, cancel, btn('tbtn', 'Reload the model list', { 'data-fk': 'pr-reload' }, function () { ui.presetModels = null; presetModels('[data-fk="pr-reload"]'); renderSetup('[data-fk="pr-reload"]'); })]),
      err]);
  }
  function presetsBlock(w) {
    var err = errBox(), ps = w.presets || [], map = w.call_presets || {}, kinds = w.call_kinds || [];
    var note = ui.presetNote ? h('p', { class: 'note warn', role: 'status' }, ui.presetNote) : null;
    function done(r, focusKey) {
      S.writer = r || S.writer;
      ui.presetNote = r && r.model_check ? r.model_check : null;
      renderSetup('[data-fk="' + cssq(focusKey) + '"]');
    }
    var times = {};
    ((S.settings && S.settings.call_times) || []).forEach(function (c) { times[c.id] = c; });
    var rows = kinds.map(function (k) {
      var c = times[k.id], sel = on(h('select', { class: 'inp', id: 'pr-map-' + k.id, 'data-fk': 'pr-map:' + k.id }, ps.map(function (p) {
        return h('option', { value: p.id, selected: (map[k.id] || 'fast') === p.id }, p.name);
      })), 'change', function () {
        var body = { call_presets: {} }; body.call_presets[k.id] = sel.value;
        run([sel], err, function () { return api('PATCH', 'api/writer', body); }, function (r) { done(r, 'pr-map:' + k.id); });
      });
      var when = k.id === 'refresh' ? 'When you press Refresh' : c ? (c.enabled ? fmtWhen(c.time) : 'Off') : '';
      return h('tr', null, [h('th', { scope: 'row' }, h('label', { for: 'pr-map-' + k.id }, k.label)), h('td', { class: 'muted' }, when), h('td', null, sel)]);
    });
    rows.push(h('tr', null, [h('th', { scope: 'row' }, 'Urgent check'), h('td', { class: 'muted' }, 'Between call times'), h('td', { class: 'muted' }, 'Never uses the writer')]));
    var list = h('ul', { class: 'review prlist' }, ps.map(function (p) {
      var used = kinds.filter(function (k) { return (map[k.id] || 'fast') === p.id; }).map(function (k) { return k.label; });
      var acts = p.builtin ? [h('span', { class: 'muted' }, 'Your settings above')] : [
        btn('tbtn', 'Edit', { 'aria-label': 'Edit ' + p.name, 'data-fk': 'pr-ed:' + p.id }, function () { presetEdit(p); }),
        (function () {
          var rm = btn('tbtn', 'Delete', { 'aria-label': 'Delete ' + p.name, 'data-fk': 'pr-rm:' + p.id }, function () {
            run([rm], err, function () { return api('DELETE', 'api/writer/presets/' + encodeURIComponent(p.id)); }, function (r) { done(r, 'pr-add'); });
          });
          return rm;
        })()];
      return h('li', null, [h('span', { class: 'nm' }, p.name), h('span', { class: 'sum' }, [presetLine(p), used.length ? h('span', { class: 'muted' }, ' · ' + used.join(', ')) : null]),
        h('span', { class: 'btns' }, acts)]);
    }));
    var sug = w.preset_suggestion, full = ps.length - 1 >= (w.preset_max || 4);
    var hasSug = sug && ps.some(function (p) { return p.name === sug.name; });
    var addSug = sug && !hasSug && !full ? btn('btn', 'Add ' + sug.label, { 'data-fk': 'pr-sug' }, function () {
      run([addSug], err, function () { return api('POST', 'api/writer/presets', { suggestion: 'careful' }); }, function (r) { done(r, 'pr-sug'); })
        .then(function () {
          if (err.textContent && /doesn.t list/.test(err.textContent)) {  /* not on this server: open it in the form to fix the model id */
            var msg = err.textContent, dr = {}; Object.keys(sug).forEach(function (k) { if (k !== 'label') dr[k] = sug[k]; });
            presetEdit(null, dr); ui.presetEdit.err = msg; renderSetup('#pr-model');
          }
        });
    }) : null;
    var add = full ? null : btn('btn', 'New preset', { 'data-fk': 'pr-add' }, function () { presetEdit(null); });
    var rl = sw(w.reload_fast !== false, 'After a careful run, load the Fast model again', function () {
      run([rl], err, function () { return api('PATCH', 'api/writer', { reload_fast: !(writerOf().reload_fast !== false) }); }, function (r) { done(r, 'pr-reload-sw'); });
    });
    rl.setAttribute('data-fk', 'pr-reload-sw');
    return h('div', { class: 'prs', role: 'group', 'aria-labelledby': 'pr-th' }, [
      h('h3', { class: 'label wh', id: 'pr-th' }, 'Presets per call time'),
      h('p', { class: 'note' }, 'Each call time can use its own preset. Fast is the model and settings above. A slower, careful preset suits the morning brief, written before you’re up.'),
      h('div', { class: 'prtbl' }, h('table', null, [h('thead', null, h('tr', null, [h('th', { scope: 'col' }, 'Call time'), h('th', { scope: 'col' }, 'When'), h('th', { scope: 'col' }, 'Preset')])),
        h('tbody', null, rows)])),
      list,
      ui.presetEdit ? presetForm(done) : h('div', { class: 'btns' }, [addSug, add]),
      note, err,
      h('div', { class: 'swrow' }, [rl, h('span', { 'aria-hidden': 'true' }, 'After a careful run, load the Fast model again')]),
      h('p', { class: 'note' }, 'One tiny request, so check-ins and Refresh start quickly. If a careful run fails or takes too long, the fast model writes the brief instead, never Claude.')]);
  }
  function testWriter(provider, b, res, err) {
    res.textContent = 'Testing…';
    run([b], err, function () { return api('POST', 'api/writer/test', { provider: provider }); }, function (r) {
      res.textContent = '';
      add(res, [h('span', { class: 'st' + (r && r.ok ? '' : ' warn') }, r && r.ok ? 'Works' : 'Didn\u2019t work'), ' ', (r && r.message) || '']);
    }).then(function () { if (res.textContent === 'Testing…') res.textContent = ''; });
  }

  /* step 6: review */
  function daysSummary(w) {
    w = DAYS.filter(function (d) { return (w || []).indexOf(d) >= 0; });
    if (!w.length) return 'no working days';
    if (w.join() === 'mon,tue,wed,thu,fri') return 'Mon–Fri';
    return w.map(function (d) { return DAYNAME[d]; }).join(', ');
  }
  function profileLine() {
    var w = writerOf(), p = (w.prompt_profiles || []).filter(function (x) { return x.id === (w.active_profile || 'default'); })[0];
    return p && p.id !== 'default' ? ' · Writing profile: ' + p.name : '';
  }
  function stepReview() {
    var st = S.settings, nAcc = 0, err = errBox();
    var nSlack = 0, sig = false;
    S.areas.forEach(function (a) { nAcc += (a.accounts || []).length; nSlack += (a.slack || []).length; sig = sig || !!a.signal; });
    var cts = (st.call_times || []).filter(function (c) { return c.enabled; });
    var morning = cts.filter(function (c) { return c.id === 'morning'; })[0], checks = cts.filter(function (c) { return c.id !== 'morning'; });
    var timesSum = [morning ? morning.time + ' brief' : 'No morning brief',
      checks.length ? checks.map(function (c) { return c.time; }).join(' and ') + (checks.length > 1 ? ' check-ins' : ' check-in') +
        (st.skip_checkins_days_off ? ', skipped on days off' : '') : 'no check-ins'].join(' · ');
    var lines = [
      [st.name || 'No name yet', (st.timezone || '').replace(/_/g, ' '), daysSummary(st.workdays), st.time_format === '24h' ? '24-hour times' : '12-hour times', 'Look: ' + LOOKNAME[app.dataset.look] + ', ' + MODENAME[app.dataset.mode]].filter(Boolean).join(' · '),
      [S.areas.map(function (a) { return a.name; }).join(', '),
        S.google_client.configured ? plural(nAcc, 'Google account', 'Google accounts') + ' connected' : 'Google sign-in key not added yet',
        nSlack ? plural(nSlack, 'Slack workspace', 'Slack workspaces') : null, sig ? 'Signal linked' : null].filter(Boolean).join(' · '),
      plural(S.vips.length, 'person', 'people') + ' · needs a reply after ' + plural(st.reply_age_days || 2, 'day', 'days') +
        ((S.suppress || []).length ? ' · ' + plural(S.suppress.length, 'never-reply rule', 'never-reply rules') : ''),
      timesSum,
      'Sidebar page in Home Assistant' + (pushOn() ? ' · Phone notifications' : '') + ' · Writer: ' + writerName(writerOf()) + profileLine()];
    var ls = S.last_sync || {};
    var refresh = btn('btn primary', 'Refresh now', { 'data-fk': 'sync' }, function () {
      run([refresh, goB], err, function () { return api('POST', 'api/sync', {}); }, function (r) { S.last_sync = r || {}; renderSetup('[data-fk="sync"]'); });
    });
    var goB = btn('btn', 'Go to my brief', null, function () {
      if (S.setup_done) { location.hash = 'brief'; return; }
      run([goB, refresh], err, function () { return api('PATCH', 'api/settings', { setup_done: true }); }, function () {
        S.setup_done = true; location.hash = 'brief';
      });
    });
    var errs = Array.isArray(ls.errors) ? ls.errors : [];
    return [
      h('p', { class: 'intro' }, [h('b', null, 'Ready.'), ' Check each part, then refresh to build your brief.']),
      h('ul', { class: 'review' }, lines.map(function (s, i) {
        return h('li', null, [h('span', { class: 'num' }, String(i + 1)), h('span', { class: 'nm' }, STEPS[i]), h('span', { class: 'sum' }, s),
          btn('tbtn', 'Edit', { 'aria-label': 'Edit ' + STEPS[i] }, function () { go(i + 1); })]);
      })),
      h('div', { class: 'go' }, [refresh, goB]),
      err,
      h('p', { class: 'syncst', role: 'status' }, ls.at ? 'Last refreshed ' + fmtWhen(ls.at, true) + (ls.ok === false ? ', with problems:' : '.') : 'Not refreshed yet.'),
      errs.length ? h('ul', { class: 'syncerr' }, errs.map(function (x) { return h('li', null, String(x)); })) : null];
  }

  /* ---------- 0.5.1: Setup → Areas → Signal: link (QR code), status, area, ignored chats, unlink, history import ----------
     signal-cli runs on the server as a read-only linked device. The QR code is an SVG the server draws from the
     code's modules; the page shows it as an <img> from a data: URL, so it is never parsed into the page. */
  var SIG = { st: null, imp: null, loading: false, err: '', timer: null, ignOpen: false, ignAll: false, unlinked: false,
              file: null, prog: '', pick: null, done: '' };
  var SIG_CHUNK = 4 * 1024 * 1024;
  function sigLoad() {
    if (SIG.loading) return;
    SIG.loading = true;
    Promise.all([api('GET', 'api/signal'), api('GET', 'api/signal/import')]).then(function (r) {
      SIG.st = r[0] || {}; SIG.imp = r[1] || {}; SIG.err = ''; SIG.at = Date.now();
    }, function (e) { SIG.err = e.message; }).then(function () { SIG.loading = false; sigRedraw(); });
  }
  /* redraws only the Signal card (the rest of Setup keeps its state and focus) */
  function sigRedraw(focusKey) {
    if (ui.view !== 'setup' || ui.step !== 2) return;
    var old = main.querySelector('.sigcard');
    if (!old) return;
    var a = document.activeElement, fk = focusKey || (a && old.contains(a) ? a.getAttribute('data-fk') : null);
    var nu = signalCard();
    old.parentNode.replaceChild(nu, old);
    if (fk) { var f = nu.querySelector('[data-fk="' + cssq(fk) + '"]'); if (f) f.focus(); }
  }
  /* while a link code waits for the phone: ask every 2 s whether it was scanned */
  function sigPollSoon() {
    if (SIG.timer) return;
    SIG.timer = setTimeout(function () {
      SIG.timer = null;
      if (ui.view !== 'setup' || ui.step !== 2 || !SIG.st || !SIG.st.link || SIG.st.link.state !== 'waiting') return;
      api('GET', 'api/signal').then(function (r) {
        SIG.st = r || SIG.st;
        if (SIG.st.linked) return reload('sig-h');  /* the areas' Chat chip changes too */
        sigRedraw();
      }, function () { sigPollSoon(); });
    }, 2000);
  }
  function sigRawPost(path, blob) {
    return fetch(path, { method: 'POST', cache: 'no-store', body: blob,
      headers: { Accept: 'application/json', 'X-Callsheet': '1', 'Content-Type': 'application/octet-stream' } }).then(function (r) {
      return r.text().then(function (t) {
        var j = null;
        try { j = t ? JSON.parse(t) : null; } catch (e) { j = null; }
        if (!r.ok) throw new Error(j && j.error ? String(j.error) : 'Something went wrong (HTTP ' + r.status + ').');
        return j;
      });
    }, function () { throw new Error('Callsheet did not answer. Check the connection and try again.'); });
  }
  function sigDay(ts) {
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return '';
    var tz = (S && S.settings && S.settings.timezone) || undefined;
    try { return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric', timeZone: tz }); } catch (e) { return d.toDateString(); }
  }
  function sigStep(n, kids) { return h('li', null, [h('span', { class: 'num', 'aria-hidden': 'true' }, String(n)), h('div', null, kids)]); }
  function signalCard() {
    var st = SIG.st, kids = [h('h3', { class: 'sighead', tabindex: '-1', 'data-fk': 'sig-h' }, 'Signal')];
    if (!st) {
      if (!SIG.loading && !SIG.err) sigLoad();
      kids.push(SIG.err ? [h('p', { class: 'err', role: 'alert' }, SIG.err),
        btn('tbtn', 'Try again', { 'data-fk': 'sig-retry' }, function () { SIG.err = ''; sigLoad(); sigRedraw(); })]
        : h('p', { class: 'note', role: 'status' }, 'Loading…'));
      return h('section', { class: 'sigcard', 'aria-label': 'Signal' }, kids);
    }
    if (!SIG.loading && Date.now() - (SIG.at || 0) > 15000) sigLoad();  /* back in Setup: refresh in the background */
    var link = st.link || { state: 'idle' }, err = errBox();
    var tag = !st.available && !st.linked ? h('span', { class: 'st off' }, 'Not available')
      : st.linked ? (st.error ? h('span', { class: 'st warn' }, 'Needs attention') : h('span', { class: 'st' }, 'Linked'))
      : link.state === 'waiting' ? h('span', { class: 'st warn' }, 'Waiting for your phone') : h('span', { class: 'st off' }, 'Not linked');
    kids.push(h('div', { class: 'acct-h' }, [h('span', { class: 'em' }, st.linked ? 'Your Signal account' : 'Read your Signal chats'), tag]));
    if (st.linked) kids.push(sigLinked(st));
    else {
      kids.push(h('p', { class: 'note' }, 'Callsheet links to Signal as an extra device, like Signal Desktop, and only reads. It never sends a message.'));
      if (SIG.unlinked) kids.push(h('p', { class: 'note sigdone', role: 'status' }, 'Signal is unlinked. On your phone, remove “Callsheet” too: Signal → Settings → Linked devices.'));
      if (!st.available) kids.push(h('p', { class: 'note' }, 'This copy of Callsheet doesn’t include signal-cli, the part that talks to Signal. Update Callsheet, then come back here.'));
      else if (link.state === 'waiting') kids.push(sigQr(link, err));
      else {
        if (link.error) kids.push(h('p', { class: 'err', role: 'alert' }, link.error));
        var lb = btn('btn sm primary', 'Link Signal', { 'data-fk': 'sig-link' }, function () {
          run([lb], err, function () { return api('POST', 'api/signal/link'); }, function (r) { SIG.st = r; SIG.unlinked = false; sigRedraw('sig-cancel'); });
        });
        kids.push(h('div', { class: 'cbtn' }, lb), h('p', { class: 'help sighelp' }, 'Starting Signal on the server can take up to a minute.'));
      }
      kids.push(err);
    }
    kids.push(sigImport(st));
    return h('section', { class: 'sigcard', 'aria-label': 'Signal' }, kids);
  }
  function sigQr(link, err) {
    var img = h('img', { class: 'sigqr', alt: 'QR code to link Signal. Scan it with Signal on your phone.', width: '232', height: '232' });
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(String(link.qr_svg || ''));
    var mins = Math.max(1, Math.ceil((Number(link.expires_in) || 0) / 60));
    var cancel = btn('tbtn', 'Cancel', { 'data-fk': 'sig-cancel' }, function () {
      run([cancel], err, function () { return api('POST', 'api/signal/link/cancel'); }, function (r) { SIG.st = r; sigRedraw('sig-link'); });
    });
    sigPollSoon();
    return h('div', { class: 'sigqrw' }, [
      h('div', { class: 'sigqrf' }, img),
      h('div', null, [
        h('ol', { class: 'steps sigsteps', 'aria-label': 'Link Signal' }, [
          sigStep(1, 'Open Signal on your phone.'),
          sigStep(2, ['Go to ', h('b', null, 'Settings → Linked devices'), ' and tap ', h('b', null, '+'), ' (Link a new device).']),
          sigStep(3, 'Scan this code. Callsheet shows up in the list as “Callsheet”.')]),
        h('p', { class: 'note sigwait', role: 'status' }, 'Waiting for your phone. This code works for ' + plural(mins, 'more minute', 'more minutes') + '.'),
        cancel])]);
  }
  function sigLinked(st) {
    var err = errBox(), out = [];
    out.push(h('dl', { class: 'sigdl' }, [
      h('dt', null, 'Number'), h('dd', null, st.number || 'Hidden'),
      h('dt', null, 'Linked'), h('dd', null, st.linked_at ? fmtWhen(st.linked_at, true) : '—'),
      h('dt', null, 'Last message'), h('dd', null, st.last_message_at ? fmtWhen(st.last_message_at, true) : 'None yet')]));
    if (st.error) out.push(h('p', { class: 'acct-err' }, st.error));
    out.push(h('p', { class: 'note' }, 'Keep Callsheet running: Signal removes a linked device that hasn’t been online for 45 days. Update Callsheet at least every 2 months too, because Signal stops working with old versions.'));
    out.push(h('p', { class: 'note' }, 'New messages are kept ' + plural(st.keep_days || 30, 'day', 'days') + '. Disappearing messages are deleted when their time is up.'));
    var sel = h('select', { class: 'inp', id: 'sig-area', 'data-fk': 'sig-area' }, S.areas.map(function (a) {
      return h('option', { value: a.id, selected: a.id === st.area_id }, a.name);
    }));
    on(sel, 'change', function () {
      run([sel], err, function () { return api('PATCH', 'api/signal', { area_id: sel.value }); }, function (r) { SIG.st = r; return reload('sig-area'); });
    });
    out.push(h('div', { class: 'sigarea' }, [h('label', { class: 'label', for: 'sig-area' }, 'Signal rows go to'), h('span', { class: 'selw' }, sel)]));
    var chats = (st.chats || []).filter(function (c) { return c.live > 0; }), ign = (st.ignored || []).slice();
    var disc = btn('tbtn disc', 'Ignore chats (' + ign.length + ')', { 'aria-expanded': String(SIG.ignOpen), 'aria-controls': 'sig-ign', 'data-fk': 'sig-ign' }, function () {
      SIG.ignOpen = !SIG.ignOpen; sigRedraw('sig-ign');
    });
    out.push(disc);
    if (SIG.ignOpen) {
      var many = chats.length > 40 && !SIG.ignAll;
      out.push(h('div', { class: 'ign', id: 'sig-ign' }, [
        h('p', { class: 'help' }, 'Tap a chat to ignore it. Ignored chats never show up in the brief.'),
        chats.length ? h('div', { class: 'chips', role: 'group', 'aria-label': 'Signal chats' }, (many ? chats.slice(0, 40) : chats).map(function (c) {
          var name = (c.title || (c.kind === 'group' ? 'Signal group' : 'Signal chat')) + (c.kind === 'group' ? ' (group)' : '');
          var b = btn('chip', name, { 'aria-pressed': String(ign.indexOf(c.id) >= 0), 'aria-label': 'Ignore ' + name }, function () {
            run([b], err, function () {
              var cur = (SIG.st.ignored || []).slice(), i = cur.indexOf(c.id);
              if (i >= 0) cur.splice(i, 1); else cur.push(c.id);
              return api('PATCH', 'api/signal', { ignored: cur });
            }, function (r) {
              SIG.st = r;
              b.setAttribute('aria-pressed', String((r.ignored || []).indexOf(c.id) >= 0));
              disc.textContent = 'Ignore chats (' + (r.ignored || []).length + ')';
            });
          });
          return b;
        }).concat(many ? [btn('tbtn', 'Show all ' + chats.length, { 'data-fk': 'sig-ignall' }, function () { SIG.ignAll = true; sigRedraw('sig-ign'); })] : []))
          : h('p', { class: 'help' }, 'No chats yet. They show up here after the first messages arrive.'),
        err]));
    } else out.push(err);
    out.push(ui.confirm === 'signal' ? confirmRow('Unlink Signal? Callsheet stops reading and deletes its Signal keys and the messages it received. Imported history stays until you delete it.', 'Unlink', function (y, c, e) {
      run([y, c], e, function () { return api('POST', 'api/signal/unlink', { confirm: true }); }, function (r) {
        ui.confirm = null; SIG.st = r; SIG.unlinked = true; SIG.ignOpen = false; return reload('sig-h');
      });
    }) : h('div', { class: 'acct-f' }, askBtn('signal', 'Unlink Signal')));
    return h('div', { class: 'siglinked' }, out);
  }
  function sigUpload(file, chat, btns, err, prog) {
    var id, off = 0;
    function next() {
      if (off >= file.size) return api('POST', 'api/signal/import/' + encodeURIComponent(id) + '/finish');
      var end = Math.min(file.size, off + SIG_CHUNK);
      return sigRawPost('api/signal/import/' + encodeURIComponent(id) + '/chunk', file.slice(off, end)).then(function () {
        off = end;
        prog.textContent = 'Uploading… ' + Math.floor(off * 100 / file.size) + '%';
        return next();
      });
    }
    run(btns, err, function () {
      return api('POST', 'api/signal/import/start', { name: file.name, size: file.size, chat: chat || null }).then(function (r) {
        id = r.id; prog.textContent = 'Uploading… 0%';
        return next();
      });
    }, function (r) { SIG.imp = r; SIG.file = null; SIG.pick = null; SIG.done = ''; sigRedraw('sig-pick'); });
  }
  function sigImport(st) {
    var imp = SIG.imp || {}, err = errBox(), staged = imp.staged || [], counts = st.counts || {};
    var out = [h('h4', { class: 'label slh' }, 'Older messages')];
    out.push(h('p', { class: 'note' }, 'Import chats from Signal Desktop’s export (main.jsonl), or one chat’s data.json from signal-export. Only text, names and times are kept: keys and phone numbers in the file are skipped. Files up to 200 MB.'));
    if (SIG.done) out.push(h('p', { class: 'note sigdone', role: 'status' }, SIG.done));
    if (staged.length) {
      var last = imp.last;
      if (last) out.push(h('p', { class: 'line' }, 'Read ' + plural(last.lines || 0, 'line', 'lines') + ': ' + plural(last.kept || 0, 'message', 'messages') + ' with text' +
        (last.skipped ? ', ' + last.skipped + ' skipped (no text, or disappearing)' : '') + (last.bad ? ', ' + last.bad + ' unreadable' : '') + '.'));
      if (!SIG.pick) SIG.pick = {};
      var pick = SIG.pick;
      function chosen() { return staged.filter(function (c) { return pick[c.chat] !== false; }); }
      var go, all;
      function label() {
        var n = chosen().length;
        go.textContent = n ? 'Import ' + plural(n, 'chat', 'chats') : 'Pick chats to import';
        go.disabled = !n;
        all.textContent = n === staged.length ? 'Select none' : 'Select all';
      }
      var list = h('ul', { class: 'sigpick', 'aria-label': 'Chats in the file', 'data-fk': 'sig-pick', tabindex: '-1' }, staged.map(function (c, i) {
        var id = 'sigp-' + i, cb = h('input', { type: 'checkbox', id: id, checked: pick[c.chat] !== false });
        on(cb, 'change', function () { pick[c.chat] = cb.checked; label(); });
        return h('li', null, [cb, h('label', { for: id }, [h('span', { class: 't' }, c.title || 'Untitled chat'),
          h('span', { class: 'm' }, (c.kind === 'group' ? 'Group · ' : '') + plural(c.count, 'message', 'messages') + ' · ' +
            (sigDay(c.first) === sigDay(c.last) ? sigDay(c.first) : sigDay(c.first) + ' to ' + sigDay(c.last)))])]);
      }));
      go = btn('btn sm primary', 'Import', { 'data-fk': 'sig-commit' }, function () {
        var ids = chosen().map(function (c) { return c.chat; });
        run([go, all, discard], err, function () { return api('POST', 'api/signal/import/commit', { chats: ids }); }, function (r) {
          SIG.imp = r; SIG.pick = null;
          SIG.done = 'Imported ' + plural(r.added || 0, 'message', 'messages') + ' from ' + plural(ids.length, 'chat', 'chats') + '.';
          return api('GET', 'api/signal').then(function (s2) { SIG.st = s2; }, function () { }).then(function () { sigRedraw('sig-file'); });
        });
      });
      all = btn('tbtn', 'Select none', { 'data-fk': 'sig-all' }, function () {
        var none = chosen().length === staged.length;
        staged.forEach(function (c) { pick[c.chat] = !none; });
        list.querySelectorAll('input[type=checkbox]').forEach(function (x) { x.checked = !none; });
        label();
      });
      var discard = btn('tbtn', 'Discard', { 'data-fk': 'sig-discard' }, function () {
        run([go, all, discard], err, function () { return api('DELETE', 'api/signal/import'); }, function (r) { SIG.imp = r; SIG.pick = null; sigRedraw('sig-file'); });
      });
      out.push(h('p', { class: 'help' }, 'Pick the chats to keep. The rest of the file is discarded.'), list,
        h('div', { class: 'btns sigbtns' }, [go, all, discard]), err);
      label();
    } else {
      var prog = h('p', { class: 'note sigprog', role: 'status' }, SIG.prog);
      var chatW, chatIn = h('input', { class: 'inp', type: 'text', id: 'sig-chat', autocomplete: 'off', placeholder: 'For example: Alex', 'data-fk': 'sig-chat' });
      var fi = h('input', { type: 'file', id: 'sig-file', class: 'sigfile', accept: '.jsonl,.json', 'data-fk': 'sig-file' });
      var up = btn('btn sm primary', 'Upload', { 'data-fk': 'sig-up', disabled: true }, function () {
        if (SIG.file) sigUpload(SIG.file, chatIn.value.trim(), [up, fi], err, prog);
      });
      on(fi, 'change', function () {
        SIG.file = (fi.files && fi.files[0]) || null; SIG.done = '';
        up.disabled = !SIG.file;
        chatW.hidden = !(SIG.file && /\.json$/i.test(SIG.file.name));
        err.textContent = SIG.file && SIG.file.size > (imp.max_bytes || 200 * 1024 * 1024) ? 'That file is larger than 200 MB.' : '';
        if (err.textContent) up.disabled = true;
      });
      chatW = h('div', { class: 'sigchat', hidden: true }, [h('label', { class: 'label', for: 'sig-chat' }, 'Chat name (the data.json’s folder)'), chatIn]);
      out.push(h('div', { class: 'sigup' }, [h('label', { class: 'label', for: 'sig-file' }, 'Export file'), fi]), chatW,
        h('div', { class: 'btns sigbtns' }, up), prog, err);
    }
    if (counts.imported) {
      out.push(h('p', { class: 'line' }, 'Imported history: ' + plural(counts.imported, 'message', 'messages') + '. It never shows up as reply rows.'));
      out.push(ui.confirm === 'sighist' ? confirmRow('Delete all imported Signal history? Messages received since linking stay.', 'Delete', function (y, c, e) {
        run([y, c], e, function () { return api('POST', 'api/signal/history/clear', { confirm: true }); }, function (r) {
          ui.confirm = null; SIG.st = r; SIG.done = ''; renderSetup('[data-fk="sig-file"]');
        });
      }) : h('div', { class: 'acct-f' }, askBtn('sighist', 'Delete imported history')));
    }
    return h('div', { class: 'sigimp' }, out);
  }

  /* ---------- 0.5.0: Setup → Delivery → Memory: the whole memory.md (view, edit, clear) ----------
     GET api/memory when the step is shown; Save = PATCH api/memory {text} (the server validates the file and says
     what's wrong); Clear = POST api/memory/reset after a confirmation. The fixed sentence the writer gets with the
     notes is shown as it is (api/memory rule). */
  var MEM = { data: null, draft: null, err: null, seq: 0, loading: false };
  function loadMemory() {
    var seq = ++MEM.seq;
    MEM.loading = true;
    function done() { MEM.loading = false; if (seq === MEM.seq && ui.view === 'setup' && ui.step === 5) renderSetup(); }
    return api('GET', 'api/memory').then(function (m) { if (seq !== MEM.seq) return; MEM.data = m; MEM.err = null; done(); },
      function (e) { if (seq !== MEM.seq) return; MEM.err = e && e.message ? e.message : 'error'; done(); });
  }
  function memoryBlock() {
    if (!MEM.data && !MEM.err && !MEM.loading) loadMemory();
    var m = MEM.data, cap = m && Number(m.cap) || 1500;
    var head = h('div', { class: 'shd' }, [h('h2', { class: 'h3', id: 'mem-h' }, 'Memory'), knowEntry(),
      btn('act ghost', 'Projects and GitHub', null, function () { PJ.view = 'setup'; PJ.open = null; location.hash = 'projects'; })]);  /* 0.7.0: Setup, Projects */  /* 0.6.0 index: entry */
    var intro = h('p', { class: 'note' }, 'Your notes to Callsheet, kept as one Markdown file on this box. Before each brief the writer gets ' +
      'the notes that fit the day (the same people, email domains, area or topic words), up to about ' + fmtNum(cap) + ' characters. ' +
      'Add notes from the notepad icon on any reply, event or briefing paragraph, or write them here.');
    if (!m) {
      return h('div', { class: 'where mem', role: 'region', 'aria-labelledby': 'mem-h' }, [head, intro,
        MEM.err ? h('p', { class: 'err', role: 'alert' }, ['Couldn’t load the memory (' + MEM.err + '). ',
          btn('tbtn', 'Try again', null, function () { MEM.err = null; loadMemory(); })]) : h('p', { class: 'muted' }, 'Loading…')]);
    }
    var max = Number(m.max) || 20000, saved = typeof m.text === 'string' ? m.text : '', cur = MEM.draft != null ? MEM.draft : saved;
    var err = errBox(), said = h('span', { class: 'said', role: 'status', 'data-said': 'mem' });
    var ta = h('textarea', { class: 'inp memta', id: 'mem-text', rows: '16', maxlength: String(max), spellcheck: 'false',
      'aria-describedby': 'mem-help mem-cnt', 'data-fk': 'mem-text' });
    ta.value = cur;
    var cnt = h('span', { class: 'cnt' + (cur.length >= max ? ' full' : ''), id: 'mem-cnt' }, fmtNum(cur.length) + ' / ' + fmtNum(max) + ' characters');
    var undoB = btn('tbtn', 'Undo changes', { 'data-fk': 'mem-undo', disabled: cur === saved }, function () { MEM.draft = null; renderSetup('[data-fk="mem-text"]'); });
    var saveB = btn('act', 'Save', { 'data-fk': 'mem-save', disabled: cur === saved }, function () {
      var text = ta.value;
      run([saveB, undoB], err, function () { return api('PATCH', 'api/memory', { text: text }); }, function (r) {
        MEM.data = r; MEM.draft = null; renderSetup('[data-fk="mem-text"]');
        var sd = main.querySelector('[data-said="mem"]'); if (sd) sd.textContent = 'Saved';
      });
    });
    on(ta, 'input', function () {
      MEM.draft = ta.value === saved ? null : ta.value; said.textContent = '';
      cnt.textContent = fmtNum(ta.value.length) + ' / ' + fmtNum(max) + ' characters';
      cnt.classList.toggle('full', ta.value.length >= max);
      saveB.disabled = undoB.disabled = ta.value === saved;
    });
    var n = (m.notes || []).length;
    var clear = ui.confirm === 'mem-reset' ? confirmRow('Clear the memory? Every note is deleted for good.', 'Clear memory', function (y, c, e) {
      run([y, c], e, function () { return api('POST', 'api/memory/reset', { confirm: true }); }, function (r) {
        MEM.data = r; MEM.draft = null; ui.confirm = null; renderSetup('[data-fk="ask:mem-reset"]');
      });
    }) : askBtn('mem-reset', 'Clear memory');
    return h('div', { class: 'where mem', role: 'region', 'aria-labelledby': 'mem-h' }, [head, intro,
      m.error ? h('p', { class: 'err', role: 'alert' }, 'The file has a problem, so no notes are read until it’s fixed: ' + m.error) : null,
      h('label', { class: 'label', for: 'mem-text' }, 'memory.md'),
      h('p', { class: 'help', id: 'mem-help' }, 'Each note starts with “### ” and a title, then “- key: value” lines (people, emails, domains, topics, area), a blank line and the note. ' +
        'A note without people, emails, domains or topics applies to its whole area. Profile and Answers hold what you saved in Get to know me.'),
      ta,
      h('div', { class: 'wfoot' }, [cnt, h('span', { class: 'btns' }, [said, undoB, saveB])]), err,
      h('div', { class: 'memfoot' }, [h('span', { class: 'muted' }, m.error ? 'Notes aren’t read right now.' : plural(n, 'note', 'notes') + ' in the memory.'), clear]),
      h('div', { class: 'locked', role: 'group', 'aria-labelledby': 'mem-lock' }, [
        h('div', { class: 'lh', id: 'mem-lock' }, [ico('lock'), 'Sent with your notes (can’t be changed)']),
        h('p', { class: 'fixtext' }, m.rule || ''),
        h('p', { class: 'muted' }, 'Your notes go to the writer in their own block, apart from mail and chat. The safety rules still come first.')])]);
  }

  /* ---------- 0.5.1: Setup → Delivery → Phone notifications (through Home Assistant's notify.mobile_app_*) ----------
     GET api/push (settings, available, last sent/error) and GET api/push/targets (the phones Home Assistant lists)
     when the step is shown; each change is PATCH api/push; "Send a test notification" = POST api/push/test.
     Errors from Home Assistant come back as plain sentences and are shown as they are (textContent). */
  var PUSH = { data: null, targets: null, tErr: null, err: null, seq: 0, loading: false };
  function loadPush() {
    var seq = ++PUSH.seq;
    PUSH.loading = true;
    function done() { PUSH.loading = false; if (seq === PUSH.seq && ui.view === 'setup' && ui.step === 5) renderSetup(); }
    var a = api('GET', 'api/push').then(function (r) { if (seq === PUSH.seq) { PUSH.data = r; PUSH.err = null; } },
      function (e) { if (seq === PUSH.seq) PUSH.err = e && e.message ? e.message : 'error'; });
    var b = api('GET', 'api/push/targets').then(function (r) {
      if (seq !== PUSH.seq) return;
      PUSH.targets = (r && r.targets) || []; PUSH.tErr = r && r.available === false ? null : (r && r.error) || null;
    }, function (e) { if (seq === PUSH.seq) { PUSH.targets = null; PUSH.tErr = e && e.message ? e.message : 'error'; } });
    return Promise.all([a, b]).then(done, done);
  }
  function pushOn() {
    var c = (PUSH.data && PUSH.data.settings) || (S && S.push) || {};
    return !!((c.targets || []).length && (c.at_call_times || c.urgent));
  }
  function pushBlock() {
    if (!PUSH.data && !PUSH.err && !PUSH.loading) loadPush();
    var head = h('div', { class: 'shd' }, [h('h2', { class: 'h3', id: 'push-h' }, 'Phone notifications')]);
    var intro = h('p', { class: 'note' }, 'Through the Home Assistant app on your phone. A notification says who wrote and the subject, ' +
      'never the message itself.');
    var d = PUSH.data;
    if (!d) {
      return h('div', { class: 'where push', role: 'region', 'aria-labelledby': 'push-h' }, [head, intro,
        PUSH.err ? h('p', { class: 'err', role: 'alert' }, ['Couldn’t load the phone settings (' + PUSH.err + '). ',
          btn('tbtn', 'Try again', null, function () { PUSH.err = null; loadPush(); })]) : h('p', { class: 'muted' }, 'Loading…')]);
    }
    var c = d.settings || {}, err = errBox();
    function patch(body, btns, focusKey, e) {
      return run(btns || [], e || err, function () { return api('PATCH', 'api/push', typeof body === 'function' ? body() : body); }, function (r) {
        PUSH.data = r || PUSH.data; if (S) S.push = PUSH.data.settings;
        renderSetup(focusKey ? '[data-fk="' + cssq(focusKey) + '"]' : null);
      });
    }
    /* phones: Home Assistant's list, plus saved ones it no longer lists (shown so they can be removed) */
    var listed = PUSH.targets || [], saved = c.targets || [], max = Number(d.max_targets) || 5;
    var names = {}, savedNames = d.names || {};
    listed.forEach(function (t) { names[t.id] = t.name; });
    var all = listed.map(function (t) { return t.id; });
    saved.forEach(function (id) { if (all.indexOf(id) < 0 && PUSH.targets) all.push(id); });
    var chips = all.map(function (id) {
      var sel = saved.indexOf(id) >= 0, gone = PUSH.targets && !names[id];
      var label = (names[id] || savedNames[id] || id) + (gone ? ' · not found' : '');
      return btn('chip' + (gone ? ' gone' : ''), label, { 'aria-pressed': String(sel), 'data-fk': 'pt:' + id, title: 'notify.' + id }, function (e) {
        patch(function () {
          var cur = ((PUSH.data && PUSH.data.settings) || {}).targets || [];
          return { targets: sel ? cur.filter(function (x) { return x !== id; }) : cur.concat([id]) };
        }, [e.currentTarget], 'pt:' + id);
      });
    });
    var tErr = PUSH.tErr ? h('p', { class: 'err', role: 'alert' }, [PUSH.tErr + ' ',
      btn('tbtn', 'Try again', { 'data-fk': 'pt-again' }, function () { PUSH.tErr = null; loadPush(); })]) : null;
    var phones = h('div', { class: 'field', role: 'group', 'aria-labelledby': 'push-to' }, [
      h('span', { class: 'label', id: 'push-to' }, 'Send to (up to ' + max + ')'),
      chips.length ? h('div', { class: 'chips' }, chips)
        : !d.available ? null
        : PUSH.targets ? h('p', { class: 'muted' }, 'Home Assistant lists no phones yet. Install the Home Assistant app on your phone and sign in, then reload this page.')
        : null,
      saved.length && !PUSH.targets ? h('p', { class: 'muted' }, 'Saved: ' + saved.map(function (id) { return savedNames[id] || id; }).join(', ')) : null,
      tErr]);
    var calls = sw(c.at_call_times, 'At call times', function () {
      patch(function () { return { at_call_times: !((PUSH.data.settings || {}).at_call_times) }; }, [calls], 'p-calls');
    });
    calls.setAttribute('data-fk', 'p-calls');
    var urg = sw(c.urgent, 'Urgent alerts between briefs', function () {
      patch(function () { return { urgent: !((PUSH.data.settings || {}).urgent) }; }, [urg], 'p-urgent');
    });
    urg.setAttribute('data-fk', 'p-urgent');
    var subj = sw(c.show_subject !== false, 'Show the subject in notifications', function () {
      patch(function () { return { show_subject: ((PUSH.data.settings || {}).show_subject === false) }; }, [subj], 'p-subj');
    });
    subj.setAttribute('data-fk', 'p-subj');
    var qErr = errBox();
    function timeIn(id, key) {
      var inp = on(h('input', { class: 'inp', type: 'time', id: id, value: c[key] || '', 'data-fk': id }), 'change', function () {
        if (!inp.value) { qErr.textContent = 'Pick a time.'; return; }
        var b = {}; b[key] = inp.value.slice(0, 5);
        patch(b, [], id, qErr);
      });
      return inp;
    }
    var perHour = on(h('select', { class: 'inp', id: 'p-max', 'data-fk': 'p-max' }, [1, 2, 3, 4, 5, 6, 8, 10].map(function (n) {
      return h('option', { value: String(n), selected: n === Number(c.max_per_hour) }, String(n));
    })), 'change', function () { patch({ max_per_hour: parseInt(perHour.value, 10) }, [perHour], 'p-max', qErr); });
    var res = h('span', { class: 'testres', role: 'status' }), tstErr = errBox();
    var test = btn('act', 'Send a test notification', { 'data-fk': 'p-test', disabled: !saved.length || !d.available }, function () {
      res.textContent = 'Sending…';
      run([test], tstErr, function () { return api('POST', 'api/push/test', {}); }, function (r) {
        PUSH.data = r || PUSH.data;
        res.textContent = '';
        add(res, [h('span', { class: 'st' + (r && r.ok ? '' : ' warn') }, r && r.ok ? 'Sent' : 'Didn’t work'), ' ', (r && r.message) || '']);
      }).then(function () { if (res.textContent === 'Sending…') res.textContent = ''; });
    });
    var le = d.last_error, ls = d.last_sent;
    var KIND = { brief: 'brief summary', urgent: 'urgent alert', 'urgent batch': 'urgent alerts', test: 'test' };
    var every = Number(d.every_min) || 10;
    return h('div', { class: 'where push', role: 'region', 'aria-labelledby': 'push-h' }, [head, intro,
      d.available ? null : h('p', { class: 'syncst bad' }, 'Phone notifications work when Callsheet runs as a Home Assistant app.'),
      phones, err,
      h('div', { class: 'swrow' }, [calls, h('span', { 'aria-hidden': 'true' }, 'At call times')]),
      h('p', { class: 'note' }, 'After each scheduled brief: how many replies and events, and the top item. Tap it to open Callsheet.'),
      h('div', { class: 'swrow' }, [urg, h('span', { 'aria-hidden': 'true' }, 'Urgent alerts between briefs')]),
      h('p', { class: 'note' }, 'Every ' + every + ' minutes between call times Callsheet looks at new mail and Slack DMs, without AI. ' +
        'It alerts for a VIP, for mail that says urgent, ASAP or emergency (or today with a deadline), or for a sender saying something is approved, paid or signed. ' +
        'Each message once. Not on days off when check-ins skip them, and not in quiet hours.'),
      h('div', { class: 'swrow' }, [subj, h('span', { 'aria-hidden': 'true' }, 'Show the subject in notifications')]),
      h('p', { class: 'note' }, 'Notification text passes through Apple’s or Google’s push service. Off: the sender’s name only (“New urgent message from Priya”).'),
      h('div', { class: 'pfields' }, [
        h('div', { class: 'field' }, [h('label', { class: 'label', for: 'p-qs' }, 'Quiet hours from'), timeIn('p-qs', 'quiet_start')]),
        h('div', { class: 'field' }, [h('label', { class: 'label', for: 'p-qe' }, 'Quiet hours until'), timeIn('p-qe', 'quiet_end')]),
        h('div', { class: 'field' }, [h('label', { class: 'label', for: 'p-max' }, 'Urgent alerts per hour'), perHour])]),
      qErr,
      h('p', { class: 'note' }, 'More than that in an hour are grouped into one “more urgent messages” alert.'),
      h('div', { class: 'testrow' }, [test, res]), tstErr,
      le && le.text ? h('p', { class: 'syncst bad', role: 'status' }, 'Last problem ' + fmtWhen(le.at, true) + ': ' + le.text)
        : h('p', { class: 'syncst', role: 'status' }, ls && ls.at ? 'Last sent ' + fmtWhen(ls.at, true) + ' (' + (KIND[ls.kind] || 'notification') + ').' : 'Nothing sent yet.')]);
  }

  /* ---------- writer evaluation (#eval): owner-run contenders (0.5.0), blind A/B, from Setup, Delivery, Advanced ---------- */
  var EV = { status: null, run: null, picks: {}, timer: null, seq: 0, edit: null, models: null, modelsBusy: false };
  var EVSTATUS = { running: 'Running', done: 'Finished', stopped: 'Stopped early', failed: 'Didn’t work', interrupted: 'Interrupted (Callsheet restarted)', unreadable: 'Can’t be read' };
  var EV_EFFORTS = [['', 'Default'], ['low', 'Low'], ['medium', 'Medium'], ['high', 'High']];
  function stopEvalPoll() { if (EV.timer) { clearTimeout(EV.timer); EV.timer = null; } }
  function fmtSecs(n) { n = Math.max(0, Math.round(Number(n) || 0)); return n < 60 ? n + ' s' : Math.floor(n / 60) + ' min ' + (n % 60) + ' s'; }
  function showEval() {
    var seq = ++EV.seq;
    stopEvalPoll();
    var st = S ? Promise.resolve(S) : loadState();
    return st.then(function () { return api('GET', 'api/eval/status'); }).then(function (r) {
      if (seq !== EV.seq || ui.view !== 'eval') return;
      EV.status = r;
      if (!EV.run || !r.runs.some(function (x) { return x.id === EV.run.id; })) EV.run = null;
      var open = EV.run ? api('GET', 'api/eval/' + encodeURIComponent(EV.run.id)) : Promise.resolve(null);
      return open.then(function (v) {
        if (seq !== EV.seq || ui.view !== 'eval') return;
        if (v) EV.run = v;
        renderEval();
        if (r.running) EV.timer = setTimeout(showEval, 3000);
      });
    }).catch(function (e) { if (seq === EV.seq && ui.view === 'eval') status('Could not load the writer evaluation (' + e.message + ')'); });
  }
  function openRun(id) {
    run([], document.getElementById('ev-err'), function () { return api('GET', 'api/eval/' + encodeURIComponent(id)); }, function (v) {
      EV.run = v; EV.picks = {}; renderEval('#ev-run');
    });
  }
  function evSettings() { return (EV.status && EV.status.settings) || { contenders: [], mode: 'each', baseline: null }; }
  function evSaved(s) { if (EV.status) EV.status.settings = s; S.eval = s; }
  function evProfiles() { return (S.writer && S.writer.prompt_profiles) || [{ id: 'default', name: 'Default' }]; }
  function evProfileName(id) { var p = evProfiles().filter(function (x) { return x.id === id; })[0]; return p ? p.name : 'Deleted profile'; }
  function evModelName(c) {
    if (c.provider !== 'claude') return c.model;
    var m = CLAUDE_MODELS.filter(function (x) { return x[0] === c.model; })[0];
    return m ? m[1] : c.model;
  }
  function evThinking(c) {
    if (!c.thinking) return 'thinking off' + (c.provider === 'claude' && c.effort ? ', effort ' + c.effort : '');
    return 'thinking on' + (c.effort ? ', effort ' + c.effort : '') + (c.budget ? ', budget ' + fmtNum(c.budget) : '');
  }
  function evSummary(c) {
    var samp = [['temperature', 'temperature'], ['top_p', 'top p'], ['top_k', 'top k']].filter(function (k) { return c[k[0]] != null; })
      .map(function (k) { return k[1] + ' ' + c[k[0]]; });
    return [(c.provider === 'claude' ? 'Claude · ' : '') + evModelName(c), evProfileName(c.profile), evThinking(c)]
      .concat(samp.length ? [samp.join(', ')] : []).join(' · ');
  }
  function evPairs(n, mode) { return mode === 'baseline' ? Math.max(0, n - 1) : n * (n - 1) / 2; }
  function evLoadModels(focusSel) {
    if (EV.modelsBusy) return;
    EV.modelsBusy = true;
    api('GET', 'api/eval/models').then(function (m) { EV.models = m; }, function (e) {
      EV.models = { local: [], local_error: e && e.message ? e.message : 'Couldn’t load the model list.', claude: [], everyday: null };
    }).then(function () { EV.modelsBusy = false; if (ui.view === 'eval' && EV.edit) renderEval(focusSel || null); });
  }
  function evEdit(c) {
    var cons = evSettings().contenders;
    EV.edit = { id: c ? c.id : null, draft: c ? JSON.parse(JSON.stringify(c)) : {
      name: 'Contender ' + (cons.length + 1), provider: 'local', model: (EV.models && EV.models.everyday) || '', thinking: false,
      effort: null, budget: null, temperature: null, top_p: null, top_k: null, profile: 'default' } };
    if (!c && !EV.edit.draft.model && S.writer && S.writer.local) EV.edit.draft.model = S.writer.local.model || '';
    if (!EV.models) evLoadModels('#ev-c-name');
    renderEval('#ev-c-name');
  }
  /* the add/edit form: plain fields; local-only and thinking-only fields appear when they apply */
  function evForm(err) {
    var e = EV.edit, d = e.draft, m = EV.models, local = d.provider !== 'claude';
    function field(id, label, ctl, note) {
      return h('div', { class: 'field' }, [h('label', { class: 'label', for: id }, label), ctl, note ? h('p', { class: 'note' }, note) : null]);
    }
    function num(id, key, label, attrs, note) {
      var a = { class: 'inp', type: 'number', id: id, inputmode: 'decimal', value: d[key] != null ? String(d[key]) : '', 'data-fk': id };
      Object.keys(attrs).forEach(function (k) { a[k] = attrs[k]; });
      var inp = on(h('input', a), 'input', function () {
        var v = inp.value.trim();
        d[key] = v === '' ? null : (isNaN(Number(v)) ? v : Number(v));
      });
      return field(id, label, inp, note);
    }
    var name = on(h('input', { class: 'inp', type: 'text', id: 'ev-c-name', value: d.name || '', maxlength: '40', autocomplete: 'off', 'data-fk': 'ev-c-name' }), 'input', function () { d.name = name.value; });
    /* model: the local server's list (+ the saved one if the server doesn't list it now) and, with a key, Claude */
    var cur = (d.provider === 'claude' ? 'claude:' : 'local:') + (d.model || '');
    var localIds = (m && m.local) || [], groups = [];
    var lopts = localIds.map(function (id) { var v = 'local:' + id; return h('option', { value: v, selected: v === cur }, id); });
    if (local && d.model && localIds.indexOf(d.model) < 0) lopts.unshift(h('option', { value: cur, selected: true }, d.model + ' (not listed by the server now)'));
    if (local && !d.model) lopts.unshift(h('option', { value: '', selected: true }, m ? 'Pick a model' : 'Loading the list…'));
    groups.push(h('optgroup', { label: 'Local server' }, lopts));
    var claude = (m && m.claude) || [];
    if (claude.length || !local) {
      groups.push(h('optgroup', { label: 'Claude' }, (claude.length ? claude : CLAUDE_MODELS.filter(function (x) { return x[0] === d.model; }).map(function (x) { return { id: x[0], label: x[1] }; }))
        .map(function (x) { var v = 'claude:' + x.id; return h('option', { value: v, selected: v === cur }, x.label); })));
    }
    var model = on(h('select', { class: 'inp', id: 'ev-c-model', 'data-fk': 'ev-c-model' }, groups), 'change', function () {
      var v = model.value, i = v.indexOf(':'), prov = v.slice(0, i) || 'local';
      var changed = prov !== d.provider;
      d.provider = prov; d.model = v.slice(i + 1);
      if (changed) renderEval('#ev-c-model');
    });
    var modelNote = m && m.local_error ? m.local_error : (local ? '' : 'The brief data goes to Anthropic for this contender.');
    var think = sw(!!d.thinking, 'Thinking', function () { d.thinking = !d.thinking; renderEval('[data-fk="ev-c-think"]'); });
    think.setAttribute('data-fk', 'ev-c-think');
    var effort = on(h('select', { class: 'inp', id: 'ev-c-effort', 'data-fk': 'ev-c-effort' }, EV_EFFORTS.map(function (x) {
      return h('option', { value: x[0], selected: (d.effort || '') === x[0] }, x[1]);
    })), 'change', function () { d.effort = effort.value || null; });
    var prof = on(h('select', { class: 'inp', id: 'ev-c-prof', 'data-fk': 'ev-c-prof' }, (evProfiles().some(function (p) { return p.id === d.profile; }) ? [] :
      [h('option', { value: d.profile, selected: true }, 'Deleted profile: pick another')]).concat(evProfiles().map(function (p) {
      return h('option', { value: p.id, selected: p.id === d.profile }, p.name);
    }))), 'change', function () { d.profile = prof.value; });
    var save = btn('btn primary', e.id ? 'Save contender' : 'Add contender', { 'data-fk': 'ev-c-save' }, function () {
      var body = {};
      ['name', 'provider', 'model', 'thinking', 'effort', 'budget', 'temperature', 'top_p', 'top_k', 'profile'].forEach(function (k) { body[k] = d[k] === undefined ? null : d[k]; });
      if (body.provider === 'claude') { body.budget = body.temperature = body.top_p = body.top_k = null; }
      else if (!body.thinking) { body.effort = body.budget = null; }
      run([save], err, function () {
        return e.id ? api('PATCH', 'api/eval/contenders/' + encodeURIComponent(e.id), body) : api('POST', 'api/eval/contenders', body);
      }, function (s) { evSaved(s); EV.edit = null; showEval().then(function () { var f = main.querySelector('[data-fk="ev-add"]'); if (f) f.focus(); }); });
    });
    var cancel = btn('btn', 'Cancel', { 'data-fk': 'ev-c-cancel' }, function () { EV.edit = null; renderEval('[data-fk="ev-add"]'); });
    return h('div', { class: 'wpanel evform', role: 'group', 'aria-labelledby': 'ev-c-h' }, [
      h('h3', { class: 'label wh', id: 'ev-c-h' }, e.id ? 'Edit ' + (d.name || 'contender') : 'New contender'),
      h('div', { class: 'lm' }, [
        field('ev-c-name', 'Name', name),
        field('ev-c-model', 'Model', model, modelNote || null),
        field('ev-c-prof', 'Writing profile', prof),
        h('div', { class: 'field' }, [h('span', { class: 'label' }, 'Thinking'),
          h('div', { class: 'swrow evsw' }, [think, h('span', { 'aria-hidden': 'true' }, d.thinking ? 'On' : 'Off')])]),
        local && !d.thinking ? null : field('ev-c-effort', 'Effort', effort, local ? null : 'Claude’s effort; Default is medium, as in the brief.'),
        local && d.thinking ? num('ev-c-budget', 'budget', 'Thinking budget (tokens)', { min: '256', max: '32768', step: '1', placeholder: String((S.writer && S.writer.local && S.writer.local.reasoning_budget) || '') }, 'Empty: your local setting.') : null,
        local ? num('ev-c-temp', 'temperature', 'Temperature', { min: '0', max: '2', step: '0.05', placeholder: 'Server default' }) : null,
        local ? num('ev-c-topp', 'top_p', 'Top p', { min: '0', max: '1', step: '0.01', placeholder: 'Server default' }) : null,
        local ? num('ev-c-topk', 'top_k', 'Top k', { min: '1', max: '1000', step: '1', placeholder: 'Server default' }) : null]),
      local ? null : h('p', { class: 'note' }, 'Claude takes no thinking budget or sampling settings.'),
      h('div', { class: 'btns' }, [save, cancel, local ? btn('tbtn', 'Reload the model list', { 'data-fk': 'ev-c-reload' }, function () { EV.models = null; renderEval('[data-fk="ev-c-reload"]'); evLoadModels('[data-fk="ev-c-reload"]'); }) : null]),
      err]);
  }
  function evContenders(err) {
    var es = evSettings(), cons = es.contenders || [], lim = (EV.status && EV.status.limits) || { min: 2, max: 4 }, busy = !!(EV.status && EV.status.running);
    var list = cons.length ? h('ul', { class: 'review evlist' }, cons.map(function (c, i) {
      var base = es.mode === 'baseline' && es.baseline === c.id;
      var rm = btn('tbtn', 'Remove', { 'aria-label': 'Remove ' + c.name, disabled: busy ? true : null, 'data-fk': 'ev-rm:' + c.id }, function () {
        run([rm], err, function () { return api('DELETE', 'api/eval/contenders/' + encodeURIComponent(c.id)); }, function (s) {
          evSaved(s); if (EV.edit && EV.edit.id === c.id) EV.edit = null; showEval().then(function () { var f = main.querySelector('[data-fk="ev-add"]'); if (f) f.focus(); });
        });
      });
      return h('li', null, [h('span', { class: 'num' }, String(i + 1)),
        h('span', { class: 'nm' }, [c.name, base ? h('span', { class: 'act evbase' }, 'Baseline') : null]),
        h('span', { class: 'sum' }, evSummary(c)),
        h('span', { class: 'evacts' }, [btn('tbtn', 'Edit', { 'aria-label': 'Edit ' + c.name, disabled: busy ? true : null, 'data-fk': 'ev-ed:' + c.id }, function () { evEdit(c); }), rm])]);
    })) : h('p', { class: 'note' }, 'No contenders yet. Add two to four: the same model with different writing profiles, or different models.');
    var addB = cons.length < lim.max && !EV.edit ? btn('btn', 'Add a contender', { 'data-fk': 'ev-add', disabled: busy ? true : null }, function () { evEdit(null); }) : null;
    /* mode: each against each, or against a baseline (+ which one) */
    function choice(val, t, sub) {
      var sel = es.mode === val;
      return btn('opt' + (sel ? ' sel' : ''), [h('span', { class: 'cb' + (sel ? ' sel' : ''), 'aria-hidden': 'true' }),
        h('span', null, [h('span', { class: 't' }, t), h('span', { class: 's' }, sub)]),
        h('span', { class: 'state' }, sel ? 'Selected' : '')], { role: 'radio', 'aria-checked': String(sel), 'data-fk': 'ev-mode:' + val }, function (ev) {
        if (sel) return;
        run([ev.currentTarget], err, function () { return api('PATCH', 'api/eval/settings', { mode: val }); }, function (s) { evSaved(s); showEval().then(function () { var f = main.querySelector('[data-fk="ev-mode:' + val + '"]'); if (f) f.focus(); }); });
      });
    }
    var n = cons.length;
    var modes = h('div', { class: 'opts', role: 'radiogroup', 'aria-label': 'How contenders are compared' }, [
      choice('each', 'Each against each', 'Every contender meets every other one' + (n >= 2 ? ': ' + plural(evPairs(n, 'each'), 'comparison', 'comparisons') : '')),
      choice('baseline', 'Against a baseline', 'Every other contender meets the baseline' + (n >= 2 ? ': ' + plural(evPairs(n, 'baseline'), 'comparison', 'comparisons') : ''))]);
    var baseSel = es.mode === 'baseline' && n ? on(h('select', { class: 'inp', id: 'ev-base', 'data-fk': 'ev-base' }, cons.map(function (c) {
      return h('option', { value: c.id, selected: c.id === es.baseline }, c.name);
    })), 'change', function () {
      run([baseSel], err, function () { return api('PATCH', 'api/eval/settings', { baseline: baseSel.value }); }, function (s) { evSaved(s); showEval().then(function () { var f = main.querySelector('#ev-base'); if (f) f.focus(); }); });
    }) : null;
    return [h('h2', { class: 'h3 where' }, 'Contenders'), list,
      EV.edit ? evForm(err) : (addB ? h('div', { class: 'btns' }, addB) : null),
      h('h3', { class: 'label evh' }, 'Compare'), modes,
      baseSel ? h('div', { class: 'field evbasef' }, [h('label', { class: 'label', for: 'ev-base' }, 'Baseline'), baseSel]) : null];
  }
  function renderEval(focusSel) {
    var r = EV.status || {}, err = h('p', { class: 'err', id: 'ev-err', role: 'alert' }), cErr = errBox();
    var runB = btn('btn primary', 'Run evaluation', { disabled: r.reason || EV.edit ? true : null, 'data-fk': 'ev-run' }, function () {
      run([runB], err, function () { return api('POST', 'api/eval/run', {}); }, function () { EV.run = null; showEval(); });
    });
    var p = r.progress, last = r.last;
    var line = p ? 'Running: step ' + p.step + ' of ' + p.steps + ' · ' + p.config + ' · ' + fmtSecs(p.elapsed_s) :
      last ? 'Last run: ' + (EVSTATUS[last.status] || last.status) + '.' : 'No runs yet.';
    var runs = r.runs || [];
    var body = [
      h('p', { class: 'intro' }, 'Compare two to four writer setups on today’s real brief data, without knowing which is which.'),
      h('p', { class: 'note' }, 'It runs only when you press the button. Callsheet reads your mail and calendar the way a refresh does and sends that data to your local model server, and to Anthropic for Claude contenders. Contenders answer one after another, then your everyday model is loaded again. With thinking on this can take half an hour or more. Your brief is never changed, and results stay in Callsheet until you download them.'),
      evContenders(cErr),
      h('div', { class: 'go' }, [runB]),
      r.reason && !p ? h('p', { class: 'note' }, r.reason) : null,
      err,
      h('p', { class: 'syncst' + (last && !p && (last.status === 'failed' || last.status === 'interrupted') ? ' bad' : ''), role: 'status' }, line),
      !p && last && last.errors && last.errors.length ? h('ul', { class: 'syncerr' }, last.errors.map(function (x) { return h('li', null, String(x)); })) : null,
      h('h2', { class: 'h3 where' }, 'Past runs'),
      runs.length ? h('ul', { class: 'review' }, runs.map(function (x, i) {
        return h('li', null, [h('span', { class: 'num' }, String(i + 1)), h('span', { class: 'nm' }, fmtWhen(x.at, true) || x.id),
          h('span', { class: 'sum' }, (EVSTATUS[x.status] || x.status) + (x.contenders ? ' · ' + plural(x.contenders, 'contender', 'contenders') : '') + (x.chosen ? ' · choices submitted' : '')),
          x.status === 'running' ? h('span') : btn('tbtn', 'Open', { 'aria-label': 'Open the run from ' + (fmtWhen(x.at, true) || x.id) }, function () { openRun(x.id); })]);
      })) : h('p', { class: 'note' }, 'Nothing yet.'),
      EV.run ? evalRun(EV.run) : null];
    var sheet = h('div', { class: 'sheet wiz' }, [
      h('div', { class: 'mast two' }, [
        h('div', { class: 't' }, [h('div', { class: 'label' }, 'Setup · Advanced'), h('h1', { id: 'ev-title', tabindex: '-1' }, 'Writer evaluation')]),
        h('div', { class: 'box call' }, [h('div', { class: 'label' }, 'Runs kept'), h('div', { class: 'disp' }, runs.length + ' / 5')])]),
      h('section', { class: 'step', 'aria-labelledby': 'ev-title' }, body),
      h('div', { class: 'wnav' }, [btn('btn', 'Back: Delivery', null, function () { EV.edit = null; ui.step = 5; location.hash = 'setup'; })])]);
    var keep = focusSel ? null : document.activeElement && document.activeElement.getAttribute('data-fk');
    main.textContent = '';
    add(main, sheet);
    press('data-set-look', app.dataset.look); press('data-set-mode', app.dataset.mode);
    syncBg();
    var f = focusSel ? main.querySelector(focusSel) : keep ? main.querySelector('[data-fk="' + cssq(keep) + '"]') : null;
    if (f) f.focus({ preventScroll: !focusSel });
  }
  function evalRun(v) {
    var pairs = v.pairs || [], rows = v.rows || [], revealed = !!v.revealed, err = errBox();
    function textBox(label, t) {
      return h('div', { class: 'field' }, [h('span', { class: 'label' }, label),
        h('div', { class: 'sugp' }, h('p', { class: 'sugtext' }, t ? String(t) : 'No usable answer.'))]);
    }
    var submit = btn('btn primary', 'Submit choices', { 'data-fk': 'ev-submit' }, function () {
      var picks = {}, missing = pairs.some(function (p) { picks[p.id] = EV.picks[p.id]; return !picks[p.id]; });
      if (missing) { err.textContent = 'Pick A, B or Same for every comparison first.'; return; }
      run([submit], err, function () { return api('POST', 'api/eval/' + encodeURIComponent(v.id) + '/choices', { picks: picks }); }, function (r) {
        EV.run = r; EV.picks = {}; showEval();
      });
    });
    var out = [
      h('h2', { class: 'h3 where', id: 'ev-run', tabindex: '-1' }, 'Run from ' + (fmtWhen(v.at, true) || v.id)),
      v.errors && v.errors.length ? h('ul', { class: 'syncerr' }, v.errors.map(function (x) { return h('li', null, String(x)); })) : null,
      h('div', { class: 'btns' }, [h('a', { class: 'btn', href: 'api/eval/' + encodeURIComponent(v.id) + '/export', download: 'callsheet-writer-eval-' + v.id + '.jsonl' }, 'Download results')]),
      h('p', { class: 'note' }, 'The download holds the brief data that was sent and every answer. Keep it private.')];
    if (!pairs.length) out.push(h('p', { class: 'note' }, 'This run has nothing to compare.'));
    else if (!revealed) out.push(h('p', { class: 'note' }, 'Read each comparison and pick the better one. Which contender wrote A or B is shown after you submit.'));
    pairs.forEach(function (p, i) {
      var pick = revealed ? p.pick : EV.picks[p.id];
      function choice(val, t) {
        var sel = pick === val;
        return btn('opt' + (sel ? ' sel' : ''), [h('span', { class: 'cb' + (sel ? ' sel' : ''), 'aria-hidden': 'true' }),
          h('span', null, h('span', { class: 't' }, t)), h('span', { class: 'state' }, sel ? 'Selected' : '')],
          { role: 'radio', 'aria-checked': String(sel), disabled: revealed ? true : null, 'data-fk': 'ev:' + p.id + ':' + val }, function () {
            EV.picks[p.id] = val; renderEval('[data-fk="' + cssq('ev:' + p.id + ':' + val) + '"]');
          });
      }
      out.push(h('div', { class: 'wpanel', role: 'group', 'aria-labelledby': 'ev-h-' + p.id }, [
        h('h3', { class: 'label wh', id: 'ev-h-' + p.id }, 'Comparison ' + (i + 1) + (revealed ? ': A = ' + (p.a_name || '?') + ', B = ' + (p.b_name || '?') : '')),
        h('div', { class: 'lm' }, [textBox('Summary A', p.a && p.a.summary), textBox('Summary B', p.b && p.b.summary)]),
        rows.map(function (row) {
          var da = p.a && p.a.drafts ? p.a.drafts[row.id] : null, db = p.b && p.b.drafts ? p.b.drafts[row.id] : null;
          return [h('p', { class: 'note' }, [h('b', null, String(row.who || '')), row.subject ? ' · ' + String(row.subject) : '']),
            h('div', { class: 'lm' }, [textBox('Draft A', da), textBox('Draft B', db)])];
        }),
        h('div', { class: 'opts', role: 'radiogroup', 'aria-label': 'Comparison ' + (i + 1) + ': which is better' }, [
          choice('A', 'A is better'), choice('B', 'B is better'), choice('same', 'Same')])]));
    });
    if (!revealed) { out.push(h('div', { class: 'go' }, submit), err); return out; }
    var rs = v.results || [];
    if (!rs.length) return out;
    function yn(x) { return x === true ? 'Yes' : x === false ? 'No' : '—'; }
    function n0(x) { return x == null ? '—' : fmtNum(x); }
    var MEASURES = [
      ['Model', function (c) { return evModelName(c); }],
      ['Writing profile', function (c) { return c.profile || '—'; }],
      ['Thinking', function (c) { return evThinking(c); }],
      ['Result', function (c) { return c.ok ? 'OK' : 'Failed'; }],
      ['Matches the answer format', function (c) { return yn(c.schema_valid); }],
      ['Time', function (c) { return c.wall_s == null ? '—' : fmtSecs(c.wall_s); }],
      ['Prompt tokens', function (c) { return n0(c.prompt_tokens); }],
      ['Answer tokens', function (c) { return n0(c.completion_tokens); }],
      ['Empty suggestions', function (c) { return n0(c.empties); }],
      ['Flagged drafts', function (c) { return n0(c.flagged); }],
      ['Picked better · worse · same', function (c) { return pairs.length ? c.wins + ' · ' + c.losses + ' · ' + c.ties : '—'; }],
      ['Problem', function (c) { return c.error || '—'; }]];
    out.push(h('h3', { class: 'h3 where' }, 'Results per contender' + (v.mode === 'baseline' && v.baseline ? ' (baseline: ' + v.baseline + ')' : '')),
      h('div', { class: 'evtbl' }, h('table', null, [
        h('thead', null, h('tr', null, [h('th', { scope: 'col' }, 'Measure')].concat(rs.map(function (c) { return h('th', { scope: 'col' }, String(c.name)); })))),
        h('tbody', null, MEASURES.map(function (k) {
          return h('tr', null, [h('th', { scope: 'row' }, k[0])].concat(rs.map(function (c) { return h('td', null, String(k[1](c))); })));
        }))])),
      h('p', { class: 'note' }, 'Flagged drafts state an approval, payment or agreement. Every other number is in the download.'));
    return out;
  }


  /* ---------- 0.6.0 index: Setup → Memory → Get to know me (#knowme): the Sources and Scan steps of knowme-v2 ----------
     GET api/index/status (sources, windows, settings, the scan's progress, index size); POST api/index/estimate when a
     window is picked; POST api/index/scan, pause, resume, cancel; PATCH api/index/settings (sources on/off, the vault
     folder); the Drive grant (POST api/index/drive/start → Google's page → paste the address → POST
     api/index/drive/finish); DELETE api/index with the status's wipe_token. The Check, Questions and Done steps come
     later (3b). Everything is inserted as text. */
  var KN = { st: null, est: {}, win: '90d', timer: null, seq: 0, grant: null, fresh: false, counting: null };
  var KNSTEPS = ['Sources', 'Scan', 'Check', 'Questions', 'Done'];
  var KNICON = { gmail: 'mail', calendar: 'cal', slack: 'slack', drive: 'drive', obsidian: 'note', signal: 'signal' };
  function stopKnowPoll() { if (KN.timer) { clearTimeout(KN.timer); KN.timer = null; } }
  function knowEntry() { return btn('act', 'Get to know me', { 'data-fk': 'kn-open' }, function () { location.hash = 'knowme'; }); }
  function knBusy(st) { var j = st && st.job; return !!(j && (j.state === 'running' || j.state === 'paused')); }
  function showKnow(focusSel) {
    var seq = ++KN.seq;
    stopKnowPoll();
    var st = S ? Promise.resolve(S) : loadState();
    return st.then(function () { return api('GET', 'api/index/status'); }).then(function (r) {
      if (seq !== KN.seq || ui.view !== 'knowme') return;
      KN.st = r;
      if (r.job && r.job.window && !KN.picked) KN.win = r.job.window;
      renderKnow(focusSel);
      if (r.job && r.job.state === 'running') KN.timer = setTimeout(function () { showKnow(); }, 3000);
    }, function (e) { if (ui.view === 'knowme') status('Could not load Get to know me (' + e.message + ')'); });
  }
  function knSecs(n) {
    if (n == null) return '—';
    n = Math.max(0, Math.round(n));
    if (n < 60) return 'under a minute';
    if (n < 3600) return Math.round(n / 60) + ' min';
    var hrs = Math.floor(n / 3600), m = Math.round((n % 3600) / 60);
    return hrs + ' h' + (m ? ' ' + m + ' min' : '');
  }
  function knBytes(b) {
    b = Number(b) || 0;
    if (b < 1e6) return 'under 1 MB';
    if (b < 1e9) return Math.round(b / 1e6) + ' MB';
    return (b / 1e9).toFixed(1) + ' GB';
  }
  function knSelected() {
    return ((KN.st && KN.st.sources) || []).filter(function (s) { return s.on && s.available; }).map(function (s) { return s.key; });
  }
  function knEstimate(win) {
    KN.counting = win;
    var key = win + ':' + knSelected().join(',');
    return api('POST', 'api/index/estimate', { window: win, sources: knSelected() }).then(function (r) {
      KN.est[key] = r; KN.counting = null; if (ui.view === 'knowme') renderKnow('[data-win="' + cssq(win) + '"]');
    }, function (e) {
      KN.est[key] = { error: e.message }; KN.counting = null; if (ui.view === 'knowme') renderKnow('[data-win="' + cssq(win) + '"]');
    });
  }
  /* "About 48,000 emails and 210 files. May take about 6 hours …" → "≈ 48,000 emails and 210 files · about 6 hours" */
  function knShort(e) {
    var what = e.summary.split('. May take')[0];
    return (/^About /.test(what) ? what.replace(/^About /, '≈ ') + ' · ' : '') + e.time;
  }
  function knEst(win) { return KN.est[win + ':' + knSelected().join(',')]; }
  function knSteps(n) {
    return h('ol', { class: 'knsteps', 'aria-label': 'Steps' }, KNSTEPS.map(function (s, i) {
      return h('li', { class: i < n ? 'ok' : i > 1 ? 'later' : null, 'aria-current': i === n ? 'step' : null }, s);
    }));
  }
  function knChip(s) {
    var locked = !s.available;
    var picked = s.on && !locked;
    var b = h('button', { class: 'cchip' + (s.key === 'drive' && locked ? ' warn' : ''), type: 'button', role: 'checkbox',
      'aria-checked': picked ? 'true' : 'false', 'aria-disabled': locked ? 'true' : null, 'data-fk': 'kn-src-' + s.key },
      [h('span', { class: 'bx', 'aria-hidden': 'true' }), ico(KNICON[s.key]), h('span', { class: 't' }, [h('b', null, s.label), h('small', null, s.detail || '')])]);
    var err = errBox();
    on(b, 'click', function () {
      if (locked) return;
      var patch = { sources: {} }; patch.sources[s.key] = !s.on;
      run([b], err, function () { return api('PATCH', 'api/index/settings', patch); }, function () { KN.est = {}; showKnow('[data-fk="kn-src-' + s.key + '"]'); });
    });
    return h('span', { class: 'cw' }, [b, err]);
  }
  function knDrive(s) {
    var missing = (s.accounts || []).filter(function (a) { return a.granted !== true; });
    if (!missing.length) return null;
    var g = KN.grant, err = errBox();
    var rows = missing.map(function (a) {
      var b = btn('tbtn', 'Grant Drive access → ' + a.email, { 'data-fk': 'kn-grant-' + a.account_id }, function () {
        run([b], err, function () { return api('POST', 'api/index/drive/start', { account_id: a.account_id }); }, function (r) {
          KN.grant = { account: a, flow: r.flow_id, url: r.auth_url }; renderKnow('#kn-paste');
        });
      });
      return h('li', null, b);
    });
    var check = btn('tbtn', 'Check again', { 'data-fk': 'kn-drive-check' }, function () {
      run([check], err, function () { return api('POST', 'api/index/drive/check', {}); }, function () { KN.est = {}; showKnow('[data-fk="kn-drive-check"]'); });
    });
    var box = [h('p', { class: 'note' }, 'Drive needs one more read-only permission for each account. Google’s page asks only for that; the access you gave before stays as it is.'),
      h('ul', { class: 'kngrant' }, rows), h('div', null, check), err];
    if (g) {
      var inp = h('input', { class: 'inp', id: 'kn-paste', type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: 'http://127.0.0.1:8765/?state=…&code=…' });
      var ferr = errBox();
      var fin = btn('act', 'Finish', { 'data-fk': 'kn-finish' }, function () {
        run([fin], ferr, function () { return api('POST', 'api/index/drive/finish', { flow_id: g.flow, pasted: inp.value }); }, function (r) {
          KN.grant = null; KN.est = {};
          showKnow('[data-fk="kn-src-drive"]');
          if (!r.granted) setTimeout(function () { var e = main.querySelector('[data-said="kn-drive"]'); if (e) e.textContent = 'Google didn’t give Drive access. Tick Drive on Google’s page and try again.'; }, 0);
        });
      });
      var link = safeHttps(g.url);
      box.push(h('ol', { class: 'kngsteps' }, [
        h('li', null, ['Open ', link ? h('a', { href: link, target: '_blank', rel: 'noopener noreferrer' }, 'Google’s page') : 'Google’s page', ' and allow Drive (read-only) for ' + g.account.email + '.']),
        h('li', null, 'Google then opens an address starting with http://127.0.0.1:8765 that doesn’t load. Copy that whole address.'),
        h('li', null, [h('label', { class: 'label', for: 'kn-paste' }, 'Paste it here'), h('div', { class: 'knrow' }, [inp, fin])])]), ferr);
    }
    box.push(h('p', { class: 'err', role: 'status', 'data-said': 'kn-drive' }));
    return h('div', { class: 'knsub' }, box);
  }
  function knVault(s) {
    var v = s.vault || {}, err = errBox();
    var inp = h('input', { class: 'inp', id: 'kn-vault', type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: '/share/Obsidian/My vault', 'data-fk': 'kn-vault' });
    inp.value = v.path || '';
    var save = btn('act', 'Save', { 'data-fk': 'kn-vault-save' }, function () {
      run([save], err, function () { return api('PATCH', 'api/index/settings', { vault: inp.value.trim() }); }, function () { KN.est = {}; showKnow('[data-fk="kn-vault"]'); });
    });
    return h('div', { class: 'knsub' }, [h('label', { class: 'label', for: 'kn-vault' }, 'Obsidian vault folder'),
      h('div', { class: 'knrow' }, [inp, save]),
      h('p', { class: 'help' }, 'Read-only. Sync the vault into Home Assistant’s share or media folder (for example with Syncthing or a network share), then type its path. Settings and attachments are skipped.'),
      v.error ? h('p', { class: 'err', role: 'alert' }, v.error) : null, err]);
  }
  function knSources(st) {
    var srcs = st.sources || [], sel = knSelected(), err = h('p', { class: 'err', id: 'kn-err', role: 'alert' });
    var wins = st.windows || [];
    var est = knEst(KN.win);
    if (!est && KN.counting !== KN.win && sel.length) setTimeout(function () { if (ui.view === 'knowme' && !knEst(KN.win) && KN.counting !== KN.win) knEstimate(KN.win); }, 0);
    var winBtns = wins.map(function (w) {
      var e = knEst(w.key), sub = e && !e.error ? knShort(e) : KN.counting === w.key ? 'Counting…' : e && e.error ? 'Couldn’t count' : 'Pick to estimate';
      return on(h('button', { class: 'wopt', type: 'button', role: 'radio', 'aria-checked': KN.win === w.key ? 'true' : 'false', 'data-win': w.key },
        [h('b', null, w.label), h('small', null, sub)]), 'click', function () {
        KN.win = w.key; KN.picked = true;
        if (!knEst(w.key)) knEstimate(w.key);
        renderKnow('[data-win="' + cssq(w.key) + '"]');
      });
    });
    var winLabel = (wins.filter(function (w) { return w.key === KN.win; })[0] || {}).label || '';
    var start = btn('btn primary', 'Start scan', { 'data-fk': 'kn-start', disabled: sel.length ? null : true }, function () {
      run([start], err, function () { return api('POST', 'api/index/scan', { window: KN.win, sources: knSelected() }); }, function () { KN.fresh = false; showKnow('[data-fk="kn-pause"]'); });
    });
    var drive = srcs.filter(function (s) { return s.key === 'drive'; })[0], obs = srcs.filter(function (s) { return s.key === 'obsidian'; })[0];
    var last = st.job && !knBusy(st) ? st.job : null;
    return [
      h('p', { class: 'intro' }, 'Callsheet reads your mail, calendars, Slack, Drive and notes here on this box and keeps a private index, so it can get to know your people and projects.'),
      last ? h('p', { class: 'note' }, 'Last scan: ' + (last.state === 'done' ? 'finished' : 'stopped') + ' · ' + fmtNum(last.read) + ' items · ' + (last.label || '')) : null,
      h('h3', { class: 'fl' }, 'What to read'),
      h('div', { class: 'cks', role: 'group', 'aria-label': 'What to read' }, srcs.map(knChip)),
      h('p', { class: 'help' }, 'Turning a source off removes what Callsheet read from it.'),
      drive && drive.on ? knDrive(drive) : null,
      obs && obs.on ? knVault(obs) : null,
      h('h3', { class: 'fl', id: 'kn-win-l' }, 'How far back'),
      h('div', { class: 'wopts', role: 'radiogroup', 'aria-labelledby': 'kn-win-l' }, winBtns),
      h('p', { class: 'hint' }, est && !est.error ? 'Storage: ' + est.size + ' on this box (' + knBytes(st.free_bytes) + ' free). Counts are estimates.' : 'Counts are estimates from your mail, calendars, Drive and notes.'),
      est && !est.error && est.warnings.length ? h('div', { class: 'warnbox', role: 'note' }, est.warnings.map(function (w, i) { return i === 0 ? h('b', null, w + ' ') : h('span', null, w + ' '); })) : null,
      est && est.error ? h('p', { class: 'err', role: 'alert' }, est.error) : null,
      h('div', { class: 'locked', role: 'note' }, [h('div', { class: 'lh' }, [ico('lock'), 'What stays where']),
        h('p', { class: 'wsw' }, 'Everything is stored on this Home Assistant box, in its own index file. It’s left out of Home Assistant backups because it can be read again. In this version the index isn’t sent to the writer.'),
        h('p', { class: 'muted' }, 'Callsheet never sends, posts or changes anything. Gmail, Calendar and Drive are read-only.')]),
      h('div', { class: 'go' }, [start, h('span', { class: 'hint' }, plural(sel.length, 'source', 'sources') + ' · ' + winLabel + (est && !est.error ? ' · ' + est.time : ''))]),
      err];
  }
  function knUnit(n, unit) { return n === 1 && /s$/.test(unit) ? unit.slice(0, -1) : unit; }  /* 1 note, 2 notes */
  function knStat(l, v) { return h('div', { class: 'stat' }, [h('span', { class: 'label' }, l), h('span', { class: 'disp' }, v)]); }
  function knScan(st) {
    var j = st.job, err = h('p', { class: 'err', id: 'kn-err', role: 'alert' });
    var paused = j.state === 'paused', ended = j.state === 'done' || j.state === 'cancelled';
    var rows = j.targets.map(function (t) {
      var skip = t.status === 'skipped', done = t.status === 'done', pct = t.total ? Math.min(100, Math.round(t.done / t.total * 100)) : done ? 100 : 0;
      var ct = skip ? 'Skipped · ' + (t.error || '') : done ? fmtNum(t.done) + ' ' + knUnit(t.done, t.unit) + ' · done' :
        t.total != null ? fmtNum(t.done) + ' of ~' + fmtNum(t.total) + ' ' + knUnit(t.total, t.unit) : fmtNum(t.done) + ' ' + knUnit(t.done, t.unit) + ' so far';
      return h('li', { class: 'knsrc' + (skip ? ' skip' : '') }, [
        h('div', { class: 'l1' }, [h('span', { class: 'ri' }, ico(KNICON[t.source])), h('span', { class: 'nm' }, t.label), h('span', { class: 'ct' }, ct)]),
        skip ? null : h('div', { class: 'bar' + (done ? ' done' : paused || ended ? ' paused' : ''), role: 'progressbar', 'aria-label': t.label,
          'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(pct) }, h('i', { style: 'width:' + pct + '%' })),
        t.status === 'waiting' || t.status === 'error' ? h('p', { class: t.status === 'error' ? 'err' : 'help' }, (t.error || '') + (t.status === 'waiting' && t.retry_at ? ' Next try at ' + fmtWhen(new Date(t.retry_at * 1000).toISOString()) + '.' : '')) : null]);
    });
    var pause = btn('act', paused ? 'Resume' : 'Pause', { 'data-fk': 'kn-pause' }, function () {
      run([pause], err, function () { return api('POST', paused ? 'api/index/resume' : 'api/index/pause', {}); }, function () { showKnow('[data-fk="kn-pause"]'); });
    });
    var stop = btn('act ghost', 'Stop', { 'data-fk': 'kn-stop' }, function () {
      run([stop], err, function () { return api('POST', 'api/index/cancel', {}); }, function () { showKnow('[data-fk="kn-new"]'); });
    });
    var again = btn('act', 'New scan', { 'data-fk': 'kn-new' }, function () { KN.fresh = true; renderKnow('[data-fk="kn-start"]'); });
    return [
      h('div', { class: 'stats' }, [knStat('Elapsed', knSecs(j.elapsed_s)), knStat(ended ? (j.state === 'done' ? 'Finished' : 'Stopped') : paused ? 'Paused' : st.waiting ? 'Waiting' : 'Time left', ended || paused || st.waiting ? '—' : j.eta_s != null ? '~' + knSecs(j.eta_s) : 'Working it out'),
        knStat('Found so far', fmtNum(j.read) + ' items')]),
      st.waiting ? h('div', { class: 'calm', role: 'status' }, st.waiting.text) : null,
      h('ul', { class: 'knsrcs' }, rows),
      ended ? h('div', { class: 'calm', role: 'status' }, [h('b', null, j.state === 'done' ? 'Done. ' : 'Stopped. '), 'Callsheet kept the ' + fmtNum(j.read) + ' items it read. New mail, events and messages are added every 30 minutes.'])
        : h('div', { class: 'calm' }, [h('b', null, 'You can close this page; Callsheet keeps going. '), 'It pauses 10 minutes before and after each call time so briefs aren’t slowed.']),
      h('div', { class: 'go' }, ended ? [j.read ? pfEntry() : null, again] : [pause, stop, h('span', { class: 'hint' }, 'Stop keeps what was read so far.')]), err];
  }
  function knWipe(st) {
    if (!KN.wipe) return btn('tbtn', 'Delete the index', { 'data-fk': 'kn-wipe' }, function () { KN.wipe = true; renderKnow('[data-fk="kn-wipe-no"]'); });
    var err = errBox(), yes, no, text = 'Delete the index? Everything Callsheet read is removed from this box. Your mail, calendars, Slack, Drive and notes are not touched.';
    yes = btn('btn danger', 'Delete the index', { 'data-fk': 'kn-wipe-yes' }, function () {
      run([yes, no], err, function () { return api('DELETE', 'api/index', { confirm: st.wipe_token }); }, function () { KN.wipe = false; KN.fresh = true; KN.est = {}; showKnow('[data-fk="kn-start"]'); });
    });
    no = btn('btn', 'Cancel', { 'data-fk': 'kn-wipe-no' }, function () { KN.wipe = false; renderKnow('[data-fk="kn-wipe"]'); });
    return h('div', { class: 'confirm', role: 'group', 'aria-label': text }, [h('p', null, text), h('div', { class: 'btns' }, [yes, no]), err]);
  }
  function renderKnow(focusSel) {
    var st = KN.st || {};
    var body;
    if (!st.available) body = [h('p', { class: 'err', role: 'alert' }, st.error || 'The index isn’t available.')];
    else if (st.job && (knBusy(st) || !KN.fresh) && st.job.kind === 'scan') body = knScan(st);
    else body = knSources(st);
    var scanning = st.available && st.job && (knBusy(st) || !KN.fresh);
    var stats = st.stats || {};
    var wipe = st.available && stats.total && !knBusy(st) ? knWipe(st) : null;
    var sheet = h('div', { class: 'sheet wiz kn' }, [
      h('div', { class: 'mast two' }, [
        h('div', { class: 't' }, [h('div', { class: 'label' }, 'Setup › Memory'), h('h1', { id: 'kn-title', tabindex: '-1' }, scanning ? 'Reading your history' : 'Get to know me')]),
        h('div', { class: 'box call' }, [h('div', { class: 'label' }, 'Index on this box'), h('div', { class: 'disp' }, knBytes(stats.bytes)),
          h('div', { class: 's' }, plural(stats.total || 0, 'item', 'items'))])]),
      h('div', { class: 'knhead' }, knSteps(scanning ? 1 : 0)),
      h('section', { class: 'step', 'aria-labelledby': 'kn-title' }, body),
      wipe ? h('div', { class: 'step knwipe' }, wipe) : null,
      h('div', { class: 'wnav' }, [btn('btn', 'Back: Delivery', null, function () { ui.step = 5; location.hash = 'setup'; })])]);
    var keep = focusSel ? null : document.activeElement && document.activeElement.getAttribute('data-fk');
    main.textContent = '';
    add(main, sheet);
    press('data-set-look', app.dataset.look); press('data-set-mode', app.dataset.mode);
    syncBg();
    var f = focusSel ? main.querySelector(focusSel) : keep ? main.querySelector('[data-fk="' + cssq(keep) + '"]') : null;
    if (f) f.focus({ preventScroll: !!keep });
  }

  /* ---------- 0.6.0 profile: Get to know me → Check (#profile), Questions (#profile-questions), Done ----------
     knowme-v2 frames 3–5. The draft, the AI's suggestions and the questions come from api/profile (index.db); only
     the owner's Save, Accept and answers write memory.md (server side, docs/know-me-profile.md). Data is text (h()). */
  var PF = { v: null, seq: 0, timer: null, edit: null, kind: null, draft: null, qi: null, other: false, done: false, enough: false };
  function stopProfilePoll() { if (PF.timer) { clearTimeout(PF.timer); PF.timer = null; } }
  function pfEntry() { return btn('act', 'Check the draft →', { 'data-fk': 'pf-open' }, function () { location.hash = 'profile'; }); }
  function pfBusy() { return !!(PF.v && PF.v.job && PF.v.job.state === 'running'); }
  function pfArea(id) { var a = (PF.v.areas || []).filter(function (x) { return x.id === id; })[0]; return a ? a.name : ''; }
  function showProfile(view, focusSel) {
    var seq = ++PF.seq;
    stopProfilePoll();
    return api('GET', 'api/profile').then(function (r) {
      if (seq !== PF.seq || (ui.view !== 'profile' && ui.view !== 'profileq')) return;
      PF.v = r;
      if (view === 'profileq' && !r.interview.at && !pfBusy() && r.draft) {
        return api('POST', 'api/profile/questions', {}).then(function (x) { PF.v = x; pfRender(focusSel); }, function (e) { PF.err = e.message; pfRender(focusSel); });
      }
      pfRender(focusSel);
    }, function (e) { if (ui.view === view) status('Could not load the profile (' + e.message + ')'); });
  }
  function pfPoll() {
    stopProfilePoll();
    if (!pfBusy()) return;
    PF.timer = setTimeout(function () { showProfile(ui.view); }, 1500);
  }
  function pfRender(focusSel) {
    var keep = focusSel ? null : document.activeElement && document.activeElement.getAttribute('data-fk');
    var q = ui.view === 'profileq';
    var sheet = h('div', { class: 'sheet wiz kn pf' }, [
      h('div', { class: 'mast two' }, [
        h('div', { class: 't' }, [h('div', { class: 'label' }, 'Setup › Memory › Get to know me'),
          h('h1', { id: 'pf-title', tabindex: '-1' }, PF.done ? 'Done' : q ? pfQTitle() : 'Check what Callsheet found')]),
        h('div', { class: 'box call' }, [h('div', { class: 'label' }, 'Draft from'), h('div', { class: 'disp' }, fmtNum(PF.v.items || 0)),
          h('div', { class: 's' }, 'items in the index')])]),
      h('div', { class: 'knhead' }, h('ol', { class: 'knsteps', 'aria-label': 'Steps' }, KNSTEPS.map(function (s, i) {
        var n = PF.done ? 4 : q ? 3 : 2;
        return h('li', { class: i < n ? 'ok' : null, 'aria-current': i === n ? 'step' : null }, s);
      }))),
      h('section', { class: 'step', 'aria-labelledby': 'pf-title' }, PF.done ? pfDone() : q ? pfQuestions() : pfCheck())]);
    main.textContent = '';
    add(main, sheet);
    syncBg();
    var f = focusSel ? main.querySelector(focusSel) : keep ? main.querySelector('[data-fk="' + cssq(keep) + '"]') : null;
    if (f) f.focus({ preventScroll: !!keep });
    pfPoll();
  }
  function pfDo(btns, err, method, path, body, focusSel) {
    return run(btns, err, function () { return api(method, path, body); }, function (r) {
      if (r && r.people) PF.v = r; else if (r && r.questions) PF.v.interview = r;
      pfRender(focusSel);
    });
  }

  /* --- Check: people, projects, recurring, reply habits --- */
  function pfCheck() {
    var v = PF.v, err = errBox();
    if (!v.draft) return [h('p', { class: 'help' }, 'Callsheet has nothing to draft from yet. Read your history first.'),
      h('div', { class: 'go' }, [btn('act', 'Get to know me', null, function () { location.hash = 'knowme'; })])];
    var days = Math.round((v.window_days || 365) / 30);
    return [
      h('p', { class: 'help pfintro' }, 'A first draft from ' + (days >= 12 ? 'the last year' : days + ' months') + ' of mail, Slack, Signal, calendars and notes. Select any row to change it. Nothing is kept until you save or accept it; the questions come next.'),
      v.memory_error ? h('p', { class: 'err', role: 'alert' }, v.memory_error) : null,
      pfAi(),
      pfPeople(), pfProjects(), pfItems('Recurring', 'pf-rec', v.recurring, function (r) { return [h('b', null, r.title), h('span', { class: 'sub' }, r.when)]; },
        function (r) { return r.title + ', ' + r.when; }),
      pfItems('Reply habits', 'pf-hab', v.habits, function (r) { return [h('b', null, r.text), h('span', { class: 'sub' }, r.sub)]; },
        function (r) { return r.text; }),
      h('div', { class: 'go pfnav' }, [
        btn('act', 'Continue to questions' + (v.interview.questions.length ? ' (' + v.interview.questions.length + ')' : ''), { 'data-fk': 'pf-next' }, function () { PF.done = false; PF.enough = false; location.hash = 'profile-questions'; }),
        btn('act ghost', 'Save and finish later', { 'data-fk': 'pf-later' }, function () { location.hash = 'knowme'; }),
        h('span', { class: 'hint' }, 'What you saved is kept. Come back any time from Setup, Memory.')]), err];
  }
  function pfAi() {
    var v = PF.v, err = errBox(), j = v.job && v.job.kind === 'suggest' ? v.job : null;
    if (j && j.state === 'running') return h('div', { class: 'calm', role: 'status' }, [h('b', null, 'Asking the AI for suggestions… '), 'This can take a minute with a local model.']);
    if (v.can_ask && v.can_ask.code === 'off') return h('p', { class: 'help' }, 'Suggested roles and project names need the AI writer. ' + v.can_ask.text);
    var b = btn('act', v.ai.at ? 'Suggest again' : 'Suggest roles and project names', { 'data-fk': 'pf-suggest' }, function () {
      pfDo([b], err, 'POST', 'api/profile/suggest', {}, '[data-fk="pf-title"]');
    });
    return h('div', { class: 'pfai' }, [b, h('span', { class: 'hint' }, 'Your writer model sees only names, counts and subject lines. What it suggests is marked “suggested” until you accept it.'),
      j && j.state === 'error' ? h('p', { class: 'err', role: 'alert' }, j.error) : null,
      v.can_ask && v.can_ask.code === 'busy' ? h('p', { class: 'help' }, v.can_ask.text) : null, err]);
  }
  function pfCard(title, sub, kids) {
    return h('div', { class: 'pfcard' }, [h('div', { class: 'pfch' }, [h('h2', null, title), sub ? h('span', { class: 'hint' }, sub) : null])].concat(kids));
  }
  function pfVal(x) {   /* what a person's row shows: saved, else suggestion, else draft */
    var s = x.saved, g = x.suggestion || {}, d = x.draft;
    return { name: s ? s.name : d.name, role: s ? s.role : g.role || '', roleSug: !s && !!g.role,
      projects: s ? s.projects : g.projects && g.projects.length ? g.projects : d.projects, projSug: !s && !!(g.projects && g.projects.length),
      area: s ? s.area : d.area };
  }
  function pfPeople() {
    var v = PF.v, rows = [h('div', { class: 'phead', 'aria-hidden': 'true' }, ['Name', 'Role', 'Projects', 'Area', 'VIP'].map(function (t) { return h('span', null, t); }))];
    v.people.forEach(function (x) {
      if (x.removed) { rows.push(pfRemoved(x.draft.name, 'people', x.key)); return; }
      if (PF.edit === x.key) { rows.push(pfEditPerson(x)); return; }
      var val = pfVal(x);
      var row = btn('prow', null, { 'data-fk': 'pf-p-' + x.key, 'aria-label': 'Edit ' + val.name }, function () { PF.edit = x.key; PF.draft = null; pfRender('[data-fk="pf-e-name"]'); });
      add(row, [h('span', { class: 'fld nm' }, [h('b', null, val.name), h('span', { class: 'sub' }, x.stat)]),
        h('span', { class: 'fld' + (val.role ? '' : ' empty') }, [val.role || 'Add a role', val.roleSug ? h('span', { class: 'sug' }, 'suggested') : null]),
        h('span', { class: 'fld' }, val.projects.length ? val.projects.map(function (n) { return h('span', { class: 'pchip' }, n); }).concat(val.projSug ? [h('span', { class: 'sug' }, 'suggested')] : []) : h('span', { class: 'empty' }, '—')),
        h('span', { class: 'fld' }, pfArea(val.area) || '—'),
        h('span', { class: 'fld vip' + (x.vip ? ' on' : '') }, x.vip ? '★ VIP' : x.vip_candidate ? 'VIP?' : '')]);
      var acts = [];
      if (x.saved) acts.push(h('span', { class: 'saved' }, '✓ Saved'));
      else if (x.suggestion) {
        var err = errBox();
        var acc = btn('btn', 'Accept', { 'data-fk': 'pf-acc-' + x.key }, function () {
          pfDo([acc], err, 'POST', 'api/profile/people/' + x.key, { name: val.name, role: val.role, projects: val.projects.slice(0, 4), area: val.area }, '[data-fk="pf-p-' + x.key + '"]');
        });
        var clr = btn('btn ghost', 'Clear', { 'data-fk': 'pf-clr-' + x.key }, function () { pfDo([clr], err, 'DELETE', 'api/profile/people/' + x.key + '/suggestion', null, '[data-fk="pf-p-' + x.key + '"]'); });
        acts.push(acc, clr, err);
      }
      rows.push(h('div', { class: 'prw' }, [row, acts.length ? h('div', { class: 'racts' }, acts) : null]));
    });
    return pfCard('People', 'Top ' + v.people.length + ' of ' + fmtNum(v.people_total || v.people.length), rows);
  }
  function pfRemoved(name, kind, key) {
    var err = errBox();
    var u = btn('btn ghost', 'Undo remove', { 'data-fk': 'pf-undo-' + key }, function () { pfDo([u], err, 'POST', 'api/profile/' + kind + '/' + key + '/restore', {}, '[data-fk="pf-undo-' + key + '"]'); });
    return h('div', { class: 'prw gone' }, [h('span', null, name + ' removed'), u, err]);
  }
  function pfKeys(el, save, cancel) {
    on(el, 'keydown', function (e) {
      if (e.key === 'Escape') { e.preventDefault(); cancel(); }
      else if (e.key === 'Enter' && e.target.tagName === 'INPUT' && !e.target.classList.contains('chipin')) { e.preventDefault(); save(); }
    });
    return el;
  }
  function pfAreaSel(cur, fk) {
    var sel = h('select', { 'data-fk': fk, 'aria-label': 'Area' }, [h('option', { value: '' }, 'No area')].concat(PF.v.areas.map(function (a) {
      return h('option', { value: a.id, selected: a.id === cur ? true : null }, a.name);
    })));
    return sel;
  }
  function pfEditPerson(x) {
    var val = pfVal(x), d = PF.draft || (PF.draft = { projects: val.projects.slice(0, 4) }), err = errBox();
    var name = h('input', { type: 'text', value: val.name, maxlength: '80', 'data-fk': 'pf-e-name', 'aria-label': 'Name' });
    var role = h('input', { type: 'text', value: val.role, maxlength: '60', 'data-fk': 'pf-e-role', 'aria-label': 'Role', placeholder: 'e.g. Accountant' });
    var area = pfAreaSel(val.area, 'pf-e-area');
    var chips = h('div', { class: 'chiped' });
    function drawChips() {
      chips.textContent = '';
      d.projects.forEach(function (n, i) {
        add(chips, h('span', { class: 'pchip' }, [n, btn('x', '×', { 'aria-label': 'Remove ' + n }, function () { d.projects.splice(i, 1); drawChips(); })]));
      });
      if (d.projects.length < 4) {
        var inp = h('input', { type: 'text', class: 'chipin', maxlength: '60', placeholder: 'Add project', 'aria-label': 'Add project', 'data-fk': 'pf-e-proj' });
        on(inp, 'keydown', function (e) {
          if ((e.key === 'Enter' || e.key === ',') && inp.value.trim()) { e.preventDefault(); d.projects.push(inp.value.trim()); drawChips(); var n = chips.querySelector('.chipin'); if (n) n.focus(); }
        });
        add(chips, inp);
      }
    }
    drawChips();
    function cancel() { var k = PF.edit; PF.edit = null; PF.draft = null; pfRender('[data-fk="pf-p-' + k + '"]'); }
    function save() {
      var pending = chips.querySelector('.chipin');
      if (pending && pending.value.trim()) d.projects.push(pending.value.trim());
      run([sv], err, function () { return api('POST', 'api/profile/people/' + x.key, { name: name.value, role: role.value, projects: d.projects.slice(0, 4), area: area.value }); },
        function (r) { PF.v = r; PF.edit = null; PF.draft = null; pfRender('[data-fk="pf-p-' + x.key + '"]'); });
    }
    var sv = btn('act', 'Save', { 'data-fk': 'pf-e-save' }, save);
    var vip = x.emails.length ? btn('btn' + (x.vip ? ' on' : ''), x.vip ? '★ VIP' : 'VIP?', { 'aria-pressed': x.vip ? 'true' : 'false', 'data-fk': 'pf-e-vip' }, function () {
      pfDo([vip], err, 'POST', 'api/profile/people/' + x.key + '/vip', { vip: !x.vip }, '[data-fk="pf-e-vip"]');
    }) : null;
    var rm = btn('btn ghost', 'Remove person', { 'data-fk': 'pf-e-rm' }, function () { PF.edit = null; pfDo([rm], err, 'DELETE', 'api/profile/people/' + x.key, null, '[data-fk="pf-undo-' + x.key + '"]'); });
    return pfKeys(h('div', { class: 'prw editing' }, [
      h('div', { class: 'efs' }, [h('label', { class: 'ef' }, [h('span', null, 'Name'), name]), h('label', { class: 'ef' }, [h('span', null, 'Role'), role]),
        h('div', { class: 'ef wide' }, [h('span', null, 'Projects'), chips]), h('label', { class: 'ef' }, [h('span', null, 'Area'), area]),
        vip ? h('div', { class: 'ef' }, [h('span', null, 'VIP'), vip]) : null]),
      x.suggestion && !x.saved ? h('p', { class: 'hint' }, 'Role and projects were suggested by the AI from subject lines. Check them before you save.') : null,
      h('div', { class: 'eacts' }, [sv, btn('btn', 'Cancel', null, cancel), rm, h('span', { class: 'hint' }, 'Enter saves · Esc cancels')]), err]), save, cancel);
  }
  function pfProjects() {
    var v = PF.v, conf = { hi: 'Sure', md: 'Fairly sure', lo: 'Not sure' };
    var rows = [h('div', { class: 'phead pj', 'aria-hidden': 'true' }, ['Project', 'Area', 'Status', 'Confidence'].map(function (t) { return h('span', null, t); }))];
    if (!v.projects.length) rows.push(h('p', { class: 'help' }, 'No recurring subjects stood out yet.'));
    v.projects.forEach(function (j) {
      if (j.removed) { rows.push(pfRemoved(j.draft.name, 'projects', j.key)); return; }
      var s = j.saved, g = j.suggestion, nm = s ? s.name : g ? g.name : j.draft.name, ar = s ? s.area : j.draft.area, stt = s ? s.status : j.draft.status;
      if (PF.edit === j.key) {
        var err = errBox();
        var name = h('input', { type: 'text', value: nm, maxlength: '60', 'data-fk': 'pf-j-name', 'aria-label': 'Project name' });
        var area = pfAreaSel(ar, 'pf-j-area');
        var st = h('select', { 'aria-label': 'Status' }, PF.v.statuses.map(function (x) { return h('option', { value: x, selected: x === stt ? true : null }, x); }));
        var cancel = function () { PF.edit = null; pfRender('[data-fk="pf-p-' + j.key + '"]'); };
        var save = function () { run([sv], err, function () { return api('POST', 'api/profile/projects/' + j.key, { name: name.value, area: area.value, status: st.value }); }, function (r) { PF.v = r; PF.edit = null; pfRender('[data-fk="pf-p-' + j.key + '"]'); }); };
        var sv = btn('act', 'Save', null, save);
        var rm = btn('btn ghost', 'Remove', null, function () { PF.edit = null; pfDo([rm], err, 'DELETE', 'api/profile/projects/' + j.key, null, '[data-fk="pf-undo-' + j.key + '"]'); });
        rows.push(pfKeys(h('div', { class: 'prw editing' }, [h('div', { class: 'efs' }, [h('label', { class: 'ef wide' }, [h('span', null, 'Project'), name]),
          h('label', { class: 'ef' }, [h('span', null, 'Area'), area]), h('label', { class: 'ef' }, [h('span', null, 'Status'), st])]),
          h('div', { class: 'eacts' }, [sv, btn('btn', 'Cancel', null, cancel), rm]), err]), save, cancel));
        return;
      }
      var row = btn('prow pj', null, { 'data-fk': 'pf-p-' + j.key, 'aria-label': 'Edit ' + nm }, function () { PF.edit = j.key; pfRender('[data-fk="pf-j-name"]'); });
      add(row, [h('span', { class: 'fld nm' }, [h('b', null, nm), !s && g ? h('span', { class: 'sug' }, 'suggested') : null, h('span', { class: 'sub' }, plural(j.docs, 'thread', 'threads'))]),
        h('span', { class: 'fld' }, pfArea(ar) || '—'), h('span', { class: 'fld' }, stt),
        h('span', { class: 'fld conf ' + j.confidence }, conf[j.confidence])]);
      rows.push(h('div', { class: 'prw' }, [row, s ? h('div', { class: 'racts' }, h('span', { class: 'saved' }, '✓ Saved')) : null]));
    });
    return pfCard('Areas & projects', 'Grouped from subject lines', rows);
  }
  function pfItems(title, fk, list, show, wording) {
    var rows = [];
    if (!list.length) rows.push(h('p', { class: 'help' }, 'Nothing stood out yet.'));
    list.forEach(function (r) {
      if (r.removed) { rows.push(pfRemoved(wording(r), 'items', r.key)); return; }
      var err = errBox();
      if (PF.edit === r.key) {
        var t = h('input', { type: 'text', value: r.saved ? r.saved.text : wording(r), maxlength: '200', 'data-fk': fk + '-in', 'aria-label': 'In your words' });
        var cancel = function () { PF.edit = null; pfRender('[data-fk="' + fk + '-' + r.key + '"]'); };
        var save = function () { run([sv], err, function () { return api('POST', 'api/profile/items/' + r.key, { text: t.value }); }, function (x) { PF.v = x; PF.edit = null; pfRender('[data-fk="' + fk + '-' + r.key + '"]'); }); };
        var sv = btn('act', 'Save', null, save);
        rows.push(pfKeys(h('div', { class: 'pitem editing' }, [h('label', { class: 'ef wide' }, [h('span', null, 'In your words'), t]),
          h('div', { class: 'eacts' }, [sv, btn('btn', 'Cancel', null, cancel)]), err]), save, cancel));
        return;
      }
      var ed = btn('btn', 'Edit', { 'data-fk': fk + '-' + r.key }, function () { PF.edit = r.key; pfRender('[data-fk="' + fk + '-in"]'); });
      var rm = btn('btn ghost', 'Remove', null, function () { pfDo([rm], err, 'DELETE', 'api/profile/items/' + r.key, null, '[data-fk="pf-undo-' + r.key + '"]'); });
      rows.push(h('div', { class: 'pitem' }, [h('div', { class: 'tx' }, r.saved ? [h('b', null, r.saved.text), h('span', { class: 'saved' }, '✓ Saved')] : show(r)),
        h('div', { class: 'racts' }, [ed, rm]), err]));
    });
    return pfCard(title, null, rows);
  }

  /* --- Questions: one at a time, quick picks plus your own words --- */
  function pfQs() { return PF.v.interview.questions; }
  function pfQTitle() { var n = pfQs().length; return n ? 'Callsheet has ' + plural(n, 'question', 'questions') : 'Questions'; }
  function pfOpen() {   /* the question to show: the chosen one, else the first not answered or skipped */
    var qs = pfQs();
    if (PF.qi != null && qs[PF.qi]) return PF.qi;
    for (var i = 0; i < qs.length; i++) if (!qs[i].answer && !qs[i].skipped) return i;
    return -1;
  }
  function pfQuestions() {
    var v = PF.v, iv = v.interview, qs = pfQs(), err = errBox();
    var j = v.job && v.job.kind === 'ask' ? v.job : null;
    if (j && j.state === 'running') return [h('div', { class: 'calm', role: 'status' }, [h('b', null, 'Working out what to ask… '), 'Callsheet looks at what it couldn’t tell from your data.'])];
    var head = [h('p', { class: 'help pfintro' }, 'It only asks what it couldn’t work out from your data. Your answers go to Memory as your own words.'),
      iv.note ? h('p', { class: 'help' }, iv.note) : null, PF.err ? h('p', { class: 'err', role: 'alert' }, PF.err) : null];
    PF.err = null;
    var i = PF.enough ? -1 : pfOpen();
    if (!qs.length || i < 0) {
      var again = btn('btn', 'Ask me again', { 'data-fk': 'pf-again' }, function () { PF.qi = null; PF.enough = false; pfDo([again], err, 'POST', 'api/profile/questions', {}, '[data-fk="pf-title"]'); });
      return head.concat([h('div', { class: 'pfend' }, [h('h2', null, !qs.length ? 'Nothing to ask right now.' : PF.enough ? 'Saved. That’s enough for now.' : 'That’s all ' + qs.length + '.'),
        h('p', { class: 'help' }, plural(iv.answered, 'answer', 'answers') + ' saved to Memory.')]),
        h('div', { class: 'go' }, [btn('act', 'Finish', { 'data-fk': 'pf-finish' }, function () { PF.done = true; pfRender('#pf-title'); }), again,
          btn('btn ghost', 'Back to the draft', null, function () { location.hash = 'profile'; })]), err, pfRail()]);
    }
    var q = qs[i];
    var box = h('div', { class: 'pfq' }, [
      h('div', { class: 'qmeta' }, [h('span', null, 'Question ' + (i + 1) + ' of ' + qs.length), h('span', { class: 'qk' }, q.label)]),
      h('div', { class: 'prog', 'aria-hidden': 'true' }, qs.map(function (x, k) { return h('span', { class: k === i ? 'cur' : x.answer ? 'ok' : x.skipped ? 'sk' : null }); })),
      h('h2', { class: 'qtext', id: 'pf-q', tabindex: '-1' }, q.question),
      h('div', { class: 'why' }, [h('b', null, 'Why I’m asking'), h('span', null, q.why)])]);
    var picks = q.picks.map(function (p, k) {
      var b = btn('pick' + (q.answer === p ? ' on' : ''), p, { 'data-fk': 'pf-pick-' + k }, function () { pfAnswer(b, err, q, { pick: k }); });
      return b;
    });
    var ta = h('textarea', { rows: '3', maxlength: '280', 'data-fk': 'pf-text', 'aria-label': 'Your answer, in your own words', placeholder: q.placeholder || 'Your answer, in your own words' });
    if (q.answer && q.picks.indexOf(q.answer) < 0) ta.value = q.answer;
    var showText = !q.picks.length || PF.other || (q.answer && q.picks.indexOf(q.answer) < 0);
    var sv = btn('act', 'Save answer', { 'data-fk': 'pf-save' }, function () { pfAnswer(sv, err, q, { text: ta.value }); });
    if (q.picks.length && q.other && !showText) picks.push(btn('pick ghost', 'Other…', { 'data-fk': 'pf-other' }, function () { PF.other = true; pfRender('[data-fk="pf-text"]'); }));
    add(box, [picks.length ? h('div', { class: 'picks' }, picks) : null,
      showText ? h('div', { class: 'obox' }, [ta, sv]) : null, err,
      h('div', { class: 'qnav' }, [
        btn('btn', 'Back', { disabled: i === 0 ? true : null }, function () { PF.qi = i - 1; PF.other = false; pfRender('#pf-q'); }),
        btn('btn', 'Skip', { 'data-fk': 'pf-skip' }, function () { PF.qi = null; PF.other = false; pfDo([], err, 'POST', 'api/profile/questions/' + q.id, { skip: true }, '#pf-q'); }),
        btn('btn ghost', 'That’s enough for now: ask me later', { 'data-fk': 'pf-enough' }, function () { PF.enough = true; pfRender('[data-fk="pf-finish"]'); })])]);
    return head.concat([h('div', { class: 'pfqwrap' }, [box, pfRail()])]);
  }
  function pfAnswer(b, err, q, body) {
    run([b], err, function () { return api('POST', 'api/profile/questions/' + q.id, body); }, function (iv) {
      PF.v.interview = iv; PF.qi = null; PF.other = false; pfRender('#pf-q');
    });
  }
  function pfRail() {
    var qs = pfQs(), done = qs.filter(function (x) { return x.answer; }), left = qs.filter(function (x) { return !x.answer && !x.skipped; }).length;
    return h('aside', { class: 'wbox pfrail', 'aria-label': 'Your answers' }, [h('h3', null, 'Your answers'),
      done.length ? h('ul', { class: 'ans' }, done.map(function (x) {
        return h('li', null, [h('span', { class: 'q' }, x.question), h('b', null, x.answer),
          btn('btn ghost', 'Change', { 'aria-label': 'Change your answer to: ' + x.question }, function () { PF.qi = qs.indexOf(x); PF.enough = false; PF.other = false; pfRender('#pf-q'); })]);
      })) : h('p', { class: 'help' }, 'Nothing answered yet.'),
      h('p', { class: 'hint' }, (left ? left + ' to go. ' : '') + 'Change any answer here, or later in Memory.')]);
  }

  /* --- Done --- */
  function pfDone() {
    var v = PF.v, ppl = v.people.filter(function (x) { return x.saved; }).length, pj = v.projects.filter(function (x) { return x.saved; }).length;
    return [h('h2', { class: 'pfend' }, 'Callsheet now knows: ' + plural(ppl, 'person', 'people') + ', ' + plural(pj, 'project', 'projects') + ', ' + plural(v.interview.answered, 'answer', 'answers')),
      h('div', { class: 'stats' }, [knStat('People', fmtNum(ppl)), knStat('Projects', fmtNum(pj)), knStat('Answers', fmtNum(v.interview.answered))]),
      h('p', { class: 'help' }, 'It is all in Memory, in your words. Briefs and drafts use it from now on.'),
      h('div', { class: 'go' }, [btn('act', 'See it in Memory (Setup)', { 'data-fk': 'pf-mem' }, function () { PF.done = false; ui.step = 5; location.hash = 'setup'; }),
        btn('act ghost pfchat', [ico('chat'), 'Open Chat'], { 'data-fk': 'pf-chat' }, function () { PF.done = false; location.hash = 'chat'; }),  /* knowme-v2 frame 5 */
        btn('btn ghost', 'Back to the draft', null, function () { PF.done = false; location.hash = 'profile'; })])];
  }

  /* ---------- 0.6.0 chat: the Chat tab (Ask Callsheet: read-only search over the index; chat.py, docs/chat.md),
     the source item panel, and Setup → Delivery → Chat. Kept in its own section (profile/interview is built on another
     branch). Data is text (h(), textContent); every URL is relative (api/chat…). ---------- */
  var KEY_CHAT = 'callsheet.chat';
  var CH = {
    ov: null,            /* GET api/chat: conversations, ready/why, label, provider, index_total, busy */
    conv: null,          /* the open conversation (GET api/chat/<id>), with .pending while Callsheet answers */
    cid: load(KEY_CHAT) || null,
    input: '', err: '', loading: false, sending: null, timer: null, fails: 0,
    items: {},           /* index item id -> api/chat/item/<id> (null while loading) */
    earlier: false, clearAsk: false, scroll: true, keepTop: null
  };
  var CHAT_SUGG = ['What’s waiting on me?', 'When did I last talk to…', 'Summarise this week'];
  var SRC_ICON = { gmail: 'mail', slack: 'slack', calendar: 'cal', drive: 'drive', obsidian: 'note', signal: 'signal', github: 'tasks' };
  var SRC_NAME = { gmail: 'Gmail', slack: 'Slack', calendar: 'Calendar', drive: 'Google Drive', obsidian: 'Obsidian', signal: 'Signal', github: 'GitHub' };
  var ITEM_KIND = { email: 'Email', sent: 'Sent email', event: 'Event', message: 'Message', note: 'Note', commit: 'Commit', issue_open: 'Open issue',
    issue_closed: 'Closed issue', pr_open: 'Open pull request', pr_closed: 'Closed pull request', pr_merged: 'Merged pull request' };

  function chatLoad() {
    if (CH.loading) return;
    CH.loading = true;
    var cid = CH.cid;
    api('GET', 'api/chat').then(function (ov) {
      CH.ov = ov || { conversations: [] };
      if (!cid) return null;
      return api('GET', 'api/chat/' + encodeURIComponent(cid)).then(function (c) { CH.conv = c; },
        function () { CH.conv = null; CH.cid = null; save(KEY_CHAT, ''); });
    }).then(function () { CH.loading = false; CH.err = ''; CH.scroll = true; chatRender(); chatPoll(); },
      function (e) { CH.loading = false; CH.err = e.message; chatRender(); });
  }
  function chatOverview() {
    return api('GET', 'api/chat').then(function (ov) { CH.ov = ov || CH.ov; chatRender(); }, function () { });
  }
  /* a full re-render of the brief while the chat is shown, keeping the focus in the question box */
  function chatRender(focusSel) {
    if (ui.view !== 'brief' || !B || U.tab !== 'chat') return;
    var a = document.activeElement;
    if (!focusSel && a && a.hasAttribute && a.hasAttribute('data-ask-in') && main.contains(a)) focusSel = '[data-ask-in]';
    renderBrief(focusSel);
  }
  /* while Callsheet answers: only the message list is replaced (the question box keeps its text and focus) */
  function chatRefreshMsgs() {
    var box = main.querySelector('.chatcol .msgs');
    if (!box || ui.view !== 'brief' || U.tab !== 'chat') return;
    var atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.textContent = '';
    add(box, chatMsgs());
    if (atEnd) box.scrollTop = box.scrollHeight;
    var sb = main.querySelector('[data-csend]');
    if (sb) sb.disabled = chatPending();
  }
  function chatPending() { return !!(CH.sending || (CH.conv && CH.conv.pending)); }
  function chatPoll() {
    clearTimeout(CH.timer); CH.timer = null;
    if (!CH.conv || !CH.conv.pending) return;
    var cid = CH.conv.id;
    CH.timer = setTimeout(function () {
      CH.timer = null;
      api('GET', 'api/chat/' + encodeURIComponent(cid)).then(function (c) {
        if (!CH.conv || CH.conv.id !== cid) return;
        CH.fails = 0; CH.conv = c;
        if (c.pending) { chatRefreshMsgs(); chatPoll(); }
        else { CH.scroll = true; chatRender(); chatOverview(); }
      }, function (e) {
        if (!CH.conv || CH.conv.id !== cid) return;
        if (++CH.fails < 5) { chatPoll(); return; }
        CH.fails = 0; CH.conv.pending = null; CH.err = e.message; chatRender();
      });
    }, 1000);
  }
  function chatAsk(text) {
    text = String(text || '').trim();
    if (!text || chatPending()) return;
    CH.sending = text; CH.err = ''; CH.input = ''; CH.scroll = true;
    chatRender('[data-ask-in]');
    api('POST', 'api/chat/ask', { conversation_id: CH.conv ? CH.conv.id : null, text: text, project_id: !CH.conv && CH.scope ? CH.scope.project : undefined }).then(function (c) {
      CH.sending = null; CH.conv = c; CH.cid = c.id; save(KEY_CHAT, c.id); CH.scroll = true;
      chatRender('[data-ask-in]'); chatPoll();
    }, function (e) { CH.sending = null; CH.input = text; CH.err = e.message; chatRender('[data-ask-in]'); });
  }
  function chatNew() {
    clearTimeout(CH.timer); CH.timer = null;
    CH.conv = null; CH.cid = null; save(KEY_CHAT, ''); CH.input = ''; CH.err = ''; CH.earlier = false; CH.scroll = true; CH.scope = null;
    if (U.open && U.open.kind === 'item') U.open = null;
    chatRender('[data-ask-in]');
    chatOverview();
  }
  function chatOpen(id) {
    api('GET', 'api/chat/' + encodeURIComponent(id)).then(function (c) {
      CH.conv = c; CH.cid = c.id; save(KEY_CHAT, c.id); CH.earlier = false; CH.err = ''; CH.scroll = true;
      chatRender('[data-ask-in]'); chatPoll();
    }, function (e) { CH.err = e.message; chatRender(); });
  }
  function chatStop(b) {
    if (!CH.conv) return;
    b.disabled = true;
    api('POST', 'api/chat/' + encodeURIComponent(CH.conv.id) + '/stop').then(function (c) { CH.conv = c; chatRefreshMsgs(); chatPoll(); },
      function (e) { b.disabled = false; CH.err = e.message; chatRender(); });
  }
  function chatClearAll(b) {
    b.disabled = true;
    api('DELETE', 'api/chat').then(function () {
      clearTimeout(CH.timer); CH.timer = null;
      CH.conv = null; CH.cid = null; save(KEY_CHAT, ''); CH.earlier = false; CH.clearAsk = false; CH.items = {};
      if (CH.ov) CH.ov.conversations = [];
      if (U.open && U.open.kind === 'item') U.open = null;
      chatRender('[data-ask-in]');
    }, function (e) { b.disabled = false; CH.err = e.message; chatRender(); });
  }
  function chatCopy(b, i) {
    var m = CH.conv && CH.conv.messages && CH.conv.messages[i];
    if (!m) return;
    var s = m.text || '';
    function said(t) { b.textContent = t; setTimeout(function () { b.textContent = 'Copy'; }, 2000); }
    function fallback() {
      var ta = h('textarea', { class: 'sr', 'aria-hidden': 'true', tabindex: '-1' }), ok = false;
      ta.value = s; document.body.appendChild(ta);
      try { ta.select(); ok = document.execCommand && document.execCommand('copy'); } catch (e) { ok = false; }
      document.body.removeChild(ta); b.focus();
      said(ok ? 'Copied' : 'Couldn’t copy');
    }
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(s).then(function () { said('Copied'); }, fallback);
    else fallback();
  }
  function chatDay(ts) {
    if (!ts) return '';
    var d = new Date(ts * 1000), tz = (S && S.settings && S.settings.timezone) || undefined;
    try {
      var day = d.toLocaleDateString('en-CA', { timeZone: tz }), today = new Date().toLocaleDateString('en-CA', { timeZone: tz });
      return day === today ? 'Today' : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: tz });
    } catch (e) { return d.toDateString(); }
  }

  function T_chat() {
    if (!CH.ov && !CH.loading && !CH.err) chatLoad();
    var old = main.querySelector('.chatcol .msgs');   /* the list being replaced: keep where the owner had scrolled */
    CH.keepTop = old ? old.scrollTop : null;
    return [chatCol()];
  }
  function chatCol() {
    var ov = CH.ov, convs = (ov && ov.conversations) || [];
    var head = h('div', { class: 'dtop' }, [h('h2', null, 'Chat'), ov && ov.label ? h('span', { class: 'cmodel' }, ov.label) : null, h('span', { class: 'sp' }),
      convs.length ? h('button', { type: 'button', class: 'tbtn', 'data-cearlier': '1', 'aria-expanded': String(CH.earlier), 'aria-controls': 'ch-earlier' }, 'Earlier chats') : null,
      h('button', { type: 'button', class: 'act ghost cnew', 'data-cnew': '1', disabled: !CH.conv && !CH.sending }, [ico('chat'), 'New chat'])]);
    var earlier = CH.earlier && convs.length ? h('div', { class: 'cearly', id: 'ch-earlier' }, [
      h('ul', null, convs.map(function (c) {
        var on = CH.conv && CH.conv.id === c.id;
        return h('li', null, h('button', { type: 'button', 'data-cconv': c.id, 'aria-current': on ? 'true' : null }, [
          h('span', { class: 't' }, c.title || 'Chat'), h('span', { class: 'd' }, (c.pending ? 'Answering… · ' : '') + chatDay(c.updated_at))]));
      })),
      CH.clearAsk ? h('div', { class: 'cclear', role: 'group', 'aria-label': 'Delete all chats' }, [
        h('p', null, 'Delete all chats? They can’t be brought back.'),
        h('button', { type: 'button', class: 'act', 'data-cclearyes': '1' }, 'Delete all'),
        h('button', { type: 'button', class: 'tbtn', 'data-cclearno': '1' }, 'Cancel')])
        : h('p', { class: 'fine' }, ['Chats stay on this box for ' + ((ov && ov.retention_days) || 30) + ' days. ',
          h('button', { type: 'button', class: 'tbtn', 'data-cclear': '1' }, 'Clear all')])]) : null;
    var body;
    if (!ov) body = CH.err ? h('div', { class: 'msgs' }, h('div', { class: 'ma' }, [h('p', { class: 'err', role: 'alert' }, CH.err),
      h('button', { type: 'button', class: 'act', 'data-cretry': '1' }, 'Try again')]))
      : h('div', { class: 'msgs' }, h('p', { class: 'muted', role: 'status' }, 'Loading…'));
    else body = h('div', { class: 'msgs', 'aria-live': 'polite' }, chatMsgs());
    return h('section', { class: 'chatcol', 'aria-label': 'Chat' }, [head, earlier, body, chatCompose(ov)]);
  }
  function chatMsgs() {
    var conv = CH.conv, msgs = (conv && conv.messages) || [], out = [];
    if (!msgs.length && !CH.sending) out.push(chatIntro());
    msgs.forEach(function (m, i) {
      out.push(m.role === 'user' ? h('div', { class: 'mq' }, [h('span', { class: 'sr' }, 'You asked: '), m.text]) : chatAnswer(m, i));
    });
    if (CH.sending) out.push(h('div', { class: 'mq' }, [h('span', { class: 'sr' }, 'You asked: '), CH.sending]), chatBusy({ text: 'Sending…', steps: [] }, false));
    else if (conv && conv.pending) out.push(chatBusy(conv.pending, true));
    return out;
  }
  function chatIntro() {
    var ov = CH.ov || {};
    var kids = [h('p', null, 'Ask about your own mail, chat, calendar and notes. Callsheet searches them here on this box and shows where each answer comes from.')];
    if (ov.index_available === false) kids.push(h('p', { class: 'err' }, 'The index isn’t available, so Callsheet can’t search. Check the app log.'));
    else if (!ov.index_total) kids.push(h('p', null, 'Callsheet hasn’t read your history yet.'),
      h('button', { type: 'button', class: 'act', 'data-cknow': '1' }, 'Get to know me'));
    return h('div', { class: 'cintro' }, kids);
  }
  function chatChip(s) {
    var sel = U.open && U.open.kind === 'item' && String(U.open.id) === String(s.id);
    var name = SRC_NAME[s.source] || s.source || 'Item';
    var instr = s.instructions === true;  /* 0.6.0: the item holds an instruction someone planted (inject.py) */
    return h('button', { type: 'button', class: 'schip' + (sel ? ' sel' : '') + (instr ? ' instr' : ''), 'data-open': 'item:' + s.id, title: 'Open: ' + (s.title || name),
      'aria-label': 'Open ' + name + ': ' + (s.title || '') + (s.who ? ', ' + s.who : '') + (s.date ? ', ' + s.date : '') + (instr ? ', contains instructions' : '') }, [
      ico(SRC_ICON[s.source] || 'note'), h('span', null, (s.who || s.title || name) + (s.date ? ' · ' + s.date : '')),
      instr ? h('b', { class: 'imark', 'aria-hidden': 'true' }, 'contains instructions') : null]);
  }
  function chatAnswer(m, i) {
    var kids = [h('span', { class: 'label' }, ['Callsheet', m.by ? h('span', { class: 'by' }, ' · ' + m.by) : null])];
    if (m.error) kids.push(h('p', { class: 'err' }, m.error));
    else if (m.stopped) kids.push(h('p', { class: 'muted' }, 'Stopped.'));
    else {
      if (m.warning) kids.push(h('p', { class: 'err sugwarn' }, m.warning));
      if (m.instr_warning) kids.push(h('p', { class: 'err sugwarn instrwarn' }, m.instr_warning));  /* 0.6.0: repeats a planted instruction */
      String(m.text || '').split(/\n{2,}/).forEach(function (p) { if (p.trim()) kids.push(h('p', null, p.trim())); });
      if (m.sources && m.sources.length) kids.push(h('div', { class: 'from' }, [h('span', { class: 'label' }, 'From'), m.sources.map(chatChip)]));
    }
    var steps = (m.steps || []).length ? h('p', { class: 'csteps' }, m.steps.join(' · ')) : null;
    kids.push(h('div', { class: 'macts' }, [steps, m.text ? h('button', { type: 'button', class: 'tbtn', 'data-ccopy': String(i) }, 'Copy') : null]));
    return h('div', { class: 'ma' }, kids);
  }
  function chatBusy(p, canStop) {
    var steps = p.steps || [];
    return h('div', { class: 'ma' }, [h('span', { class: 'label' }, 'Callsheet'),
      h('div', { class: 'busy', role: 'status' }, [h('span', { class: 'spin', 'aria-hidden': 'true' }), p.text || 'Thinking…']),
      steps.length ? h('div', { class: 'bsteps' }, steps.map(function (s) { return h('span', { class: 'bst done' }, s); })) : null,
      canStop ? h('button', { type: 'button', class: 'tbtn', 'data-cstop': '1' }, 'Stop') : null]);
  }
  function chatCompose(ov) {
    if (!ov) return null;
    if (!ov.ready) return h('div', { class: 'compose' }, [h('p', { class: 'cnote' }, ov.why || 'Set up the chat model first.'),
      h('button', { type: 'button', class: 'act', 'data-csetup': '1' }, 'Set up the chat model')]);
    var inp = h('input', { class: 'inp', id: 'ch-ask', type: 'text', 'data-ask-in': '1', maxlength: '2000', autocomplete: 'off', enterkeyhint: 'send',
      placeholder: chatScope() ? 'Ask about ' + chatScope().name + '…' : 'Ask about your mail, chat or calendar…' });
    inp.value = CH.input;
    var empty = !(CH.conv && CH.conv.messages && CH.conv.messages.length) && !CH.sending;
    return h('div', { class: 'compose' }, [
      chatScopeRow(), empty ? h('div', { class: 'sugg', role: 'group', 'aria-label': 'Suggestions' }, (chatScope() ? PJ_SCOPE_SUGG : CHAT_SUGG).map(function (s) { return h('button', { type: 'button', 'data-csug': s }, s); })) : null,
      CH.err && CH.ov ? h('p', { class: 'err', role: 'alert' }, CH.err) : null,
      h('div', { class: 'inrow' }, [h('label', { class: 'sr', for: 'ch-ask' }, 'Ask Callsheet'), inp,
        h('button', { type: 'button', class: 'act', 'data-csend': '1', 'aria-label': 'Ask', disabled: chatPending() }, [ico('send'), h('span', { class: 'd-only' }, 'Ask')])]),
      h('p', { class: 'fine' }, 'Answers come from your own mail, chat and calendar on this box. Callsheet can’t send or change anything.'
        + (ov.provider === 'claude' ? ' With Claude, the search results for each question go to Anthropic.' : ''))]);
  }
  /* after renderBrief: the latest message in view; on a phone the column fills the screen above the tab bar */
  function chatAfterRender() {
    var col = main.querySelector('.chatcol'), box = col && col.querySelector('.msgs');
    if (!col) return;
    var pane = col.closest('.pane'); if (pane) pane.classList.add('chatpane');
    if (isPhone()) {
      var tb = main.querySelector('.tabbar'), top = col.getBoundingClientRect().top + window.scrollY;
      col.style.height = Math.max(300, window.innerHeight - top - (tb ? tb.offsetHeight : 0)) + 'px';
    } else {
      var t = col.getBoundingClientRect().top + window.scrollY;   // banners above the tabs push the column down
      col.style.height = Math.max(420, Math.min(760, window.innerHeight - t - 16)) + 'px';
    }
    if (!box) return;
    if (CH.scroll) { box.scrollTop = box.scrollHeight; CH.scroll = false; }
    else if (CH.keepTop != null) box.scrollTop = CH.keepTop;
  }
  window.addEventListener('resize', function () {
    if (ui.view !== 'brief' || U.tab !== 'chat') return;
    var box = main.querySelector('.chatcol .msgs'), atEnd = box && box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    CH.keepTop = box ? box.scrollTop : null; CH.scroll = !!atEnd;
    chatAfterRender();
  });

  /* Replies: area filter chips (All + each area, with open counts); they replace the old Areas tab */
  function areaChips(all) {
    var areas = (B && B.areas) || [];
    if (areas.length < 2) return null;
    function n(id) { return all.filter(function (r) { return !id || r.area === id; }).length; }
    return [h('div', { class: 'afil', role: 'group', 'aria-label': 'Filter by area' },
      [h('button', { type: 'button', class: 'chip', 'data-afil': 'all', 'aria-pressed': String(!U.area) }, 'All ' + n(null))].concat(areas.map(function (a) {
        return h('button', { type: 'button', class: 'chip a' + clampColour(a.colour), 'data-afil': a.id, 'aria-pressed': String(U.area === a.id) }, a.name + ' ' + n(a.id));
      }))),
      h('p', { class: 'afnote' }, 'Area summaries are at the top of Briefing.')];
  }

  /* the side panel for a source chip: like a Replies detail (title, people, date, snippet, Open in …) */
  function itemPanel() {
    var id = String(U.open.id), it = CH.items[id], phone = isPhone();
    if (it === undefined) {
      CH.items[id] = null;
      api('GET', 'api/chat/item/' + encodeURIComponent(id)).then(function (r) { CH.items[id] = r || { error: 'Nothing to show.' }; }, function (e) { CH.items[id] = { error: e.message }; })
        .then(function () { if (ui.view === 'brief' && U.open && U.open.kind === 'item' && String(U.open.id) === id) renderBrief('.panel [data-close]'); });
    }
    var closeB = h('button', { type: 'button', class: 'xbtn', 'data-close': '1' }, phone ? [ico('back'), 'Back to chat'] : [ico('close'), 'Close']);
    var body;
    if (!it) body = [h('h2', { id: 'bv-ptitle', class: 'sr' }, 'Item'), h('p', { class: 'muted', role: 'status' }, 'Loading…')];
    else if (it.error) body = [h('h2', { id: 'bv-ptitle', class: 'sr' }, 'Item'), h('p', { class: 'err', role: 'alert' }, it.error)];
    else {
      var link = safeHttps(it.url), name = it.source_name || SRC_NAME[it.source] || it.source;
      body = [h('div', { class: 'phd' }, [h('h2', { id: 'bv-ptitle' }, it.title || '(no title)'),
          h('div', { class: 'pmeta' }, [it.area ? h('span', { class: 'tag ' + areaOf(it.area_id).cls }, it.area) : null,
            h('span', { class: 's' }, [ico(SRC_ICON[it.source] || 'note'), name]), it.when ? h('span', null, it.when) : null])]),
        it.people ? [h('h3', { class: 'plab' }, 'People'), h('p', { class: 'ppl' }, it.people)] : null,
        it.snippet ? [h('h3', { class: 'plab' }, 'Snippet'), h('p', { class: 'what' }, it.snippet)] : null,
        h('div', { class: 'pacts' }, link ? h('a', { class: 'act', href: link, target: '_blank', rel: 'noopener noreferrer' }, 'Open in ' + name)
          : h('span', { class: 'muted' }, it.source === 'obsidian' ? 'In your Obsidian vault' : 'No link for this item'))];
    }
    var attrs = { class: 'panel', 'aria-labelledby': 'bv-ptitle' };
    if (phone) { attrs.role = 'dialog'; attrs['aria-modal'] = 'true'; }
    return h('aside', attrs, h('div', { class: 'panel-in' }, [
      h('div', { class: 'ptop' }, [phone ? closeB : h('span', { class: 'label lbl' }, (it && ITEM_KIND[it.kind]) || 'Item'), h('span', { class: 'sp' }), phone ? null : closeB]),
      h('div', { class: 'pbody' }, body)]));
  }

  document.addEventListener('click', function (e) {
    if (ui.view !== 'brief' || !B || U.tab !== 'chat') return;
    var t = e.target.closest ? e.target.closest('button') : null;
    if (!t || t.disabled || !main.contains(t)) return;
    var d = t.dataset;
    if (d.csend) { var inp = main.querySelector('[data-ask-in]'); chatAsk(inp ? inp.value : CH.input); }
    else if (d.csug) {
      if (/…$/.test(d.csug)) { CH.input = d.csug.replace(/…$/, ' '); chatRender('[data-ask-in]'); }
      else chatAsk(d.csug);
    }
    else if (d.cnew) chatNew();
    else if (d.cstop) chatStop(t);
    else if (d.ccopy) chatCopy(t, parseInt(d.ccopy, 10));
    else if (d.cearlier) { CH.earlier = !CH.earlier; CH.clearAsk = false; chatRender('[data-cearlier]'); }
    else if (d.cconv) chatOpen(d.cconv);
    else if (d.cclear) { CH.clearAsk = true; chatRender('[data-cclearno]'); }
    else if (d.cclearno) { CH.clearAsk = false; chatRender('[data-cclear]'); }
    else if (d.cclearyes) chatClearAll(t);
    else if (d.csetup) { ui.step = 5; ui.focusNext = '#ch-h'; location.hash = 'setup'; }
    else if (d.cknow) location.hash = 'knowme';
    else if (d.cretry) { CH.err = ''; chatLoad(); }
  });
  document.addEventListener('input', function (e) {
    if (e.target.dataset && e.target.dataset.askIn) CH.input = e.target.value;
  });
  document.addEventListener('keydown', function (e) {
    var t = e.target;
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && t.dataset && t.dataset.askIn) { e.preventDefault(); chatAsk(t.value); }
  });

  /* Setup → Delivery → Chat: the chat's own model (local or Claude), independent of the writer */
  var CHS = null, CHS_LOADING = false, CHM = null, CHS_CLEAR = false;  /* settings, loaded models, the clear confirm */
  function chatSetupRender(focusKey) {
    if (ui.view !== 'setup' || ui.step !== 5) return;
    var a = document.activeElement, keep = focusKey || (a && a.getAttribute && main.contains(a) ? a.getAttribute('data-fk') : null);
    renderSetup(keep ? '[data-fk="' + cssq(keep) + '"]' : null);
  }
  function chatBlock() {
    var title = h('h2', { class: 'h3', id: 'ch-h', tabindex: '-1' }, 'Chat');
    if (!CHS || CHS.error) {
      if (!CHS && !CHS_LOADING) {
        CHS_LOADING = true;
        api('GET', 'api/chat/settings').then(function (r) { CHS = r; }, function (e) { CHS = { error: e.message }; })
          .then(function () { CHS_LOADING = false; chatSetupRender(); });
      }
      return h('div', { class: 'where chatset' }, [title, CHS && CHS.error ? h('p', { class: 'err', role: 'alert' }, CHS.error) : h('p', { class: 'note' }, 'Loading…')]);
    }
    var c = CHS, err = errBox();
    function patch(body, e, btns, focusKey) {
      return run(btns || [], e || err, function () { return api('PATCH', 'api/chat/settings', body); }, function (r) {
        CHS = r || CHS; CH.ov = null; CH.err = '';   /* the Chat tab reloads its model label and readiness */
        chatSetupRender(focusKey);
      });
    }
    function choice(val, t, sub) {
      var sel = c.provider === val;
      return btn('opt' + (sel ? ' sel' : ''), [h('span', { class: 'cb' + (sel ? ' sel' : ''), 'aria-hidden': 'true' }),
        h('span', null, [h('span', { class: 't' }, t), h('span', { class: 's' }, sub)]),
        h('span', { class: 'state' }, sel ? 'Selected' : '')], { role: 'radio', 'aria-checked': String(sel), 'data-fk': 'cp:' + val }, function (e) {
        if (!sel) patch({ provider: val }, err, [e.currentTarget], 'cp:' + val);
      });
    }
    var opts = h('div', { class: 'opts', role: 'radiogroup', 'aria-label': 'Which model answers in Chat' }, [
      choice('local', 'Local model', 'Your own OpenAI-compatible server. Questions and results stay on your network.'),
      choice('claude', 'Claude (cloud)', 'Uses the Anthropic key saved for the writer. The search results for each question go to Anthropic.')]);

    /* local: the writer's server and model, or the chat's own (server URL + a model from its /v1/models) */
    var L = c.local, lErr = errBox(), same = L.same_as_writer;
    var sameSw = sw(same, 'Same server and model as the brief writer', function () { patch({ local: { same_as_writer: !same } }, lErr, [sameSw], 'ch-same'); });
    sameSw.setAttribute('data-fk', 'ch-same');
    var own = null;
    if (!same) {
      var url = on(h('input', { class: 'inp', type: 'url', id: 'ch-url', value: L.base_url || '', placeholder: LOCAL_URL_EG, autocomplete: 'off', spellcheck: 'false', 'data-fk': 'ch-url' }), 'change', function () {
        CHM = null; patch({ local: { base_url: url.value.trim() } }, lErr, [], 'ch-url');
      });
      var models = CHM && CHM.models && CHM.models.length ? CHM.models.slice() : null;
      var model;
      if (models) {
        if (L.model && models.indexOf(L.model) < 0) models.unshift(L.model);
        model = on(h('select', { class: 'inp', id: 'ch-model', 'data-fk': 'ch-model' }, [L.model ? null : h('option', { value: '' }, 'Pick a model')].concat(models.map(function (m) {
          return h('option', { value: m, selected: m === L.model }, m);
        }))), 'change', function () { if (model.value) patch({ local: { model: model.value } }, lErr, [model], 'ch-model'); });
      } else {
        model = on(h('input', { class: 'inp', type: 'text', id: 'ch-model', value: L.model || '', placeholder: LOCAL_MODEL_EG, autocomplete: 'off', spellcheck: 'false', 'data-fk': 'ch-model' }), 'change', function () {
          patch({ local: { model: model.value.trim() } }, lErr, [], 'ch-model');
        });
      }
      var mRes = h('span', { class: 'testres', role: 'status' }, CHM && CHM.message ? CHM.message : '');
      var mLoad = btn('act', 'Load models', { 'data-fk': 'ch-mload' }, function () {
        mRes.textContent = 'Loading…';
        run([mLoad], lErr, function () { return api('POST', 'api/chat/models', { base_url: url.value.trim() }); }, function (r) {
          CHM = r || { models: [] };
          if (!CHM.ok) { CHM = null; mRes.textContent = ''; lErr.textContent = (r && r.message) || 'Couldn’t load the models.'; return; }
          chatSetupRender('ch-model');
        }).then(function () { if (mRes.textContent === 'Loading…') mRes.textContent = ''; });
      });
      var key = on(h('input', { class: 'inp', type: 'password', id: 'ch-key', autocomplete: 'off', spellcheck: 'false', placeholder: L.key_hint ? 'Saved key ' + L.key_hint : 'none', 'data-fk': 'ch-key' }), 'change', function () {
        var v = key.value.trim();
        if (v) patch({ local: { api_key: v } }, lErr, [], 'ch-key');
      });
      var kDel = L.key_hint ? btn('tbtn', 'Remove key', { 'data-fk': 'ch-kdel' }, function () { patch({ local: { api_key: '' } }, lErr, [kDel], 'ch-key'); }) : null;
      own = h('div', { class: 'lm' }, [
        h('div', { class: 'field' }, [h('label', { class: 'label', for: 'ch-url' }, 'Server URL'), url]),
        h('div', { class: 'field' }, [h('label', { class: 'label', for: 'ch-model' }, 'Model'), model, h('div', { class: 'btns kb' }, [mLoad, mRes])]),
        h('div', { class: 'field' }, [h('label', { class: 'label', for: 'ch-key' }, L.key_hint ? 'API key (optional, saved ' + L.key_hint + ')' : 'API key (optional)'), key,
          kDel ? h('div', { class: 'btns kb' }, kDel) : null])]);
    }
    var thL = sw(L.thinking === true, 'Thinking (local)', function () { patch({ local: { thinking: L.thinking !== true } }, lErr, [thL], 'ch-lth'); });
    thL.setAttribute('data-fk', 'ch-lth');
    var tools = on(h('select', { class: 'inp', id: 'ch-tools', 'data-fk': 'ch-tools' }, [['auto', 'Automatic (recommended)'], ['native', 'Server tool calling'], ['json', 'Simple JSON (for models without tool calling)']].map(function (o) {
      return h('option', { value: o[0], selected: L.tools === o[0] }, o[1]);
    })), 'change', function () { patch({ local: { tools: tools.value } }, lErr, [tools], 'ch-tools'); });
    function num(id, k, label) {
      var lim = (c.limits && c.limits[k]) || [1, 99999];
      var inp = on(h('input', { class: 'inp', type: 'number', id: id, min: String(lim[0]), max: String(lim[1]), step: '1', inputmode: 'numeric', value: String(L[k]), 'data-fk': id }), 'change', function () {
        var v = inp.value.trim(), body = { local: {} };
        if (!v) return;
        body.local[k] = Number(v);
        patch(body, lErr, [], id);
      });
      return h('div', { class: 'field' }, [h('label', { class: 'label', for: id }, label), inp]);
    }
    var local = h('div', { class: 'wpanel' + (c.provider === 'local' ? '' : ' off'), role: 'group', 'aria-labelledby': 'ch-lh' }, [
      h('h3', { class: 'label wh', id: 'ch-lh' }, 'Local model'),
      h('div', { class: 'swrow' }, [sameSw, h('span', { 'aria-hidden': 'true' }, 'Same server and model as the brief writer')]),
      same ? h('p', { class: 'note' }, L.writer.base_url && L.writer.model ? 'Uses ' + L.writer.model + ' at ' + L.writer.base_url + '.' : 'Set up the writer’s local model above first, or turn this off.') : own,
      h('div', { class: 'lm' }, [h('div', { class: 'field' }, [h('label', { class: 'label', for: 'ch-tools' }, 'Tool use'), tools]),
        num('ch-time', 'timeout', 'Timeout per step (seconds)'), num('ch-max', 'max_tokens', 'Max tokens per answer'),
        num('ch-slot', 'id_slot', 'Server slot for chat')]),
      h('div', { class: 'swrow' }, [thL, h('span', { 'aria-hidden': 'true' }, 'Thinking')]),
      lErr,
      h('p', { class: 'note' }, 'Automatic uses the server’s tool calling (llama.cpp needs --jinja) and falls back to simple JSON when the server refuses it, trying tool calling again after 10 minutes. Thinking is slower. The server slot (llama.cpp) keeps chat questions off the brief writer’s slot: use a different number from “Server slot for briefs”.')]);

    /* Claude: model, effort, thinking; the key is the writer's */
    var C = c.claude, cErr = errBox();
    var cModel = on(h('select', { class: 'inp', id: 'ch-cmodel', 'data-fk': 'ch-cmodel' }, CLAUDE_MODELS.map(function (m) {
      return h('option', { value: m[0], selected: m[0] === C.model }, m[1] + ' · ' + m[2]);
    })), 'change', function () { patch({ claude: { model: cModel.value } }, cErr, [cModel], 'ch-cmodel'); });
    var effort = on(h('select', { class: 'inp', id: 'ch-effort', 'data-fk': 'ch-effort' }, [['low', 'Low (fastest)'], ['medium', 'Medium'], ['high', 'High (most thorough)']].map(function (o) {
      return h('option', { value: o[0], selected: C.effort === o[0] }, o[1]);
    })), 'change', function () { patch({ claude: { effort: effort.value } }, cErr, [effort], 'ch-effort'); });
    var thC = sw(C.thinking === true, 'Thinking (Claude)', function () { patch({ claude: { thinking: C.thinking !== true } }, cErr, [thC], 'ch-cth'); });
    thC.setAttribute('data-fk', 'ch-cth');
    var claude = h('div', { class: 'wpanel' + (c.provider === 'claude' ? '' : ' off'), role: 'group', 'aria-labelledby': 'ch-ch' }, [
      h('h3', { class: 'label wh', id: 'ch-ch' }, 'Claude'),
      h('div', { class: 'lm' }, [h('div', { class: 'field' }, [h('label', { class: 'label', for: 'ch-cmodel' }, 'Model · USD per million tokens'), cModel]),
        h('div', { class: 'field' }, [h('label', { class: 'label', for: 'ch-effort' }, 'Effort'), effort])]),
      h('div', { class: 'swrow' }, [thC, h('span', { 'aria-hidden': 'true' }, 'Thinking')]),
      cErr,
      h('p', { class: 'note' }, C.has_key ? 'Uses the Anthropic API key saved for the writer.' : 'Add an Anthropic API key in Writer above first.')]);

    var tRes = h('span', { class: 'testres', role: 'status' }), tErr = errBox();
    var tBtn = btn('act', 'Test chat model', { 'data-fk': 'ch-test' }, function () {
      tRes.textContent = 'Testing…';
      run([tBtn], tErr, function () { return api('POST', 'api/chat/test', {}); }, function (r) {
        tRes.textContent = '';
        add(tRes, [h('span', { class: 'st' + (r && r.ok ? '' : ' warn') }, r && r.ok ? 'Works' : 'Didn’t work'), ' ', (r && r.message) || '']);
      }).then(function () { if (tRes.textContent === 'Testing…') tRes.textContent = ''; });
    });
    var clrErr = errBox(), clr;
    if (CHS_CLEAR) {
      var yes = btn('btn danger', 'Delete all chats', { 'data-fk': 'ch-clryes' }, function () {
        run([yes], clrErr, function () { return api('DELETE', 'api/chat'); }, function () {
          CHS_CLEAR = false; CH.conv = null; CH.cid = null; save(KEY_CHAT, ''); CH.ov = null; CH.items = {};
          chatSetupRender('ch-clr');
        });
      });
      clr = h('div', { class: 'confirm', role: 'group', 'aria-label': 'Delete all chats' }, [h('p', null, 'Delete all chats? They can’t be brought back.'),
        h('div', { class: 'btns' }, [yes, btn('btn', 'Cancel', { 'data-fk': 'ch-clrno' }, function () { CHS_CLEAR = false; chatSetupRender('ch-clr'); })]), clrErr]);
    } else clr = h('div', { class: 'testrow' }, btn('tbtn', 'Clear all chats', { 'data-fk': 'ch-clr' }, function () { CHS_CLEAR = true; chatSetupRender('ch-clryes'); }));
    return h('div', { class: 'where chatset' }, [
      title,
      h('p', { class: 'note' }, 'Ask about your own mail, chat, calendar and notes in the Chat tab. It searches the index on this box with read-only tools: it can’t send, reply or change anything.'),
      opts, err, local, claude,
      h('div', { class: 'testrow' }, [tBtn, tRes]), tErr,
      h('p', { class: 'syncst' + (c.ready ? '' : ' bad'), role: 'status' }, c.ready ? 'Chat uses ' + c.label + '.' : c.why),
      h('p', { class: 'note' }, 'Chats are kept on this box for ' + c.retention_days + ' days after the last message, then deleted. Nothing you ask is added to Memory.'),
      clr]);
  }
  /* ---------- end 0.6.0 chat ---------- */

  route();
})();
