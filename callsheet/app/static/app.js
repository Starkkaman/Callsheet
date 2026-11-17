/* Callsheet: Daily Brief page + Setup.
   Hash routes: #brief (default), #setup and #setup/<place> (0.9.0: connections, day, ai, advanced; old step links
   map to a place) and #knowme (0.6.0: Get to know me, from Memory).
   Fetches api/brief and api/state (relative URLs: works under the HA ingress prefix). Brief: v3 frame A on desktop,
   v3 frame B's structure and order under 700px. Setup: home plus four places, same API as before.
   Data is only ever inserted as text (textContent), never as HTML. */
(function () {
  'use strict';
  var app = document.getElementById('app');
  var main = document.getElementById('main');
  /* One look (id calm, data-base=panel; 2026-11-13). The theme is the only
     choice: Light, Dark or Auto (Auto follows the system). A look saved before (Easy read, Bold, Clean or any of the
     retired looks) is read as this one, with its mode kept. */
  var LOOK = 'calm';
  var MODES = ['light', 'dark', 'auto'];
  var MODENAME = { auto: 'Auto', light: 'Light', dark: 'Dark' };
  /* per-device prefs {look, mode} under Callsheet's own key (0.7.1 kept the mode in callsheet.mode) */
  var KEY_PREFS = 'callsheet.prefs', KEY_MODE = 'callsheet.mode';
  var PLACES = [['connections', 'Connections'], ['day', 'Your day'], ['ai', 'AI'], ['advanced', 'Advanced']];  /* 0.9.0: Setup in four places */
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
  /* "07:30" stays as is; an ISO date-time becomes local HH:MM (with the day when it isn't today) */
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
  var ASSISTANT_DEFAULT = 'Bruce';   /* assistant.DEFAULT: the name until api/state says otherwise */
  /* the assistant's name (Setup › Your day › You, or by asking in Chat; settings assistant_name) */
  function aname() {
    var n = S && S.settings && S.settings.assistant_name;
    return typeof n === 'string' && n.trim() ? n.trim() : ASSISTANT_DEFAULT;
  }
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
  var PREFS = (function () {
    var p = null;
    try { p = JSON.parse(load(KEY_PREFS) || 'null'); } catch (e) { p = null; }
    if (!p || typeof p !== 'object') p = { mode: load(KEY_MODE) };
    var m = null;
    try { m = p.mode; } catch (e) { m = null; }
    return { look: LOOK, mode: MODES.indexOf(m) >= 0 ? m : 'auto' };   /* any saved look reads as the one look */
  })();
  function savePrefs() { save(KEY_PREFS, JSON.stringify({ look: LOOK, mode: PREFS.mode })); }
  function pressMode() { press('data-set-mode', PREFS.mode); }
  function applyMode() {
    app.dataset.look = LOOK; app.dataset.base = 'panel'; app.dataset.mode = PREFS.mode;
    pressMode(); syncBg();
  }
  function setMode(v, persist) {
    if (MODES.indexOf(v) < 0) v = 'auto';
    PREFS.mode = v; applyMode();
    if (persist) savePrefs();
  }
  setMode(PREFS.mode);
  if (load(KEY_PREFS)) savePrefs();   /* rewrites an old saved look as the one look (mode kept) */
  onMedia('(prefers-color-scheme: dark)', syncBg);
  onMedia('(max-width: 700px)', syncBg);
  /* the theme (mode) also lives in the server's settings (shared between devices) */
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
    if (t.dataset.setMode) { setMode(t.dataset.setMode, true); pushLook('mode', PREFS.mode, t); }
  });

  /* ---------- header: route buttons (0.20.0: Find, Setup and Look only; the date line and Refresh went with the
     new Today, which reads live and carries its own date and "Up to date") ---------- */
  var setupBtn = document.getElementById('setup-btn'), backBtn = document.getElementById('back-btn');
  setupBtn.addEventListener('click', function () { ui.wiz = null; openSetup('home'); });  /* 0.7.1: Setup opens on its home page */
  backBtn.addEventListener('click', function () { location.hash = 'briefing'; });  /* 0.21.0: #brief opens Talk it through */

  /* ---------- render: brief (tabs-v6) ----------
     Tabs Briefing · Replies · Agenda · Areas · Tasks (later). One-line rows open a detail panel (desktop: side
     panel, phone: full-screen sheet). The notepad menu notes or dismisses events on this device until tomorrow.
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
  /* 0.6.0 chat: Chat takes the place of Areas (area summaries are in Briefing, Replies has area chips).
     0.11.0: the Briefing tab is shown as "Today" (2026-11-06); its id, #briefing and saved tab stay "briefing". */
  var TABS = [['briefing', 'Today', 'Today', 'brief'], ['replies', 'Replies', 'Replies', 'mail'], ['agenda', 'Agenda', 'Agenda', 'cal'],
    ['chat', 'Chat', 'Chat', 'chat'], ['projects', 'Projects', 'Projects', 'tasks']];  /* 0.7.0: Projects replaces Tasks */
  var LIVE_TABS = ['briefing', 'replies', 'agenda', 'chat', 'projects'];
  var OLD_TABS = { areas: 'replies' };  /* 0.6.0 chat: a saved tab or #areas link from before opens Replies */
  function tabFromHash(hash) {          /* #briefing, #replies, #agenda, #chat (and the old #areas) open the brief there */
    var t = String(hash || '').replace(/^#/, '');
    t = OLD_TABS[t] || t;
    return LIVE_TABS.indexOf(t) >= 0 ? t : null;
  }
  function savedTab() { var t = load(KEY_TAB); t = OLD_TABS[t] || t; return LIVE_TABS.indexOf(t) >= 0 ? t : 'briefing'; }
  var phoneMq = window.matchMedia('(max-width: 700px)');
  function isPhone() { return phoneMq.matches; }

  var B = null;            /* the brief being shown */
  var U = {                /* brief UI state; survives re-renders and refreshes */
    tab: tabFromHash(location.hash) || savedTab(),
    open: null,            /* {kind: 'reply'|'event'|'item', id} ('item': an index item from a chat source chip, 0.6.0) */
    pop: null,             /* notepad menu {kind: 'reply'|'event'|'heads'|'para', id, anc, sub} */
    doneOpen: false, area: null, more: false, drafts: {}, toast: null, busy: false,
    legend: false,         /* 0.7.1: the reason legend unfolded behind its "?" (after first use) */
    keys: null,            /* 0.7.1: the key sheet is open: {from: the element to focus again} */
    kcur: null,            /* 0.7.1: the row J/K last moved to (a reply id) */
    sorted: null, sortAll: false, hzAll: {}   /* 0.8.0: the open Sorted for you list; Coming up's "See all" */
  };
  /* 0.7.1 quick wins, remembered on this device: the legend has been used (first tick), the day the All clear
     moment played, and which Setup help sections are unfolded */
  var KEY_HELP = 'callsheet.help';
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

  /* ----- data helpers ----- */
  function areaOf(id) {
    var a = ((B && B.areas) || []).filter(function (x) { return x.id === id; })[0];
    return a ? { id: a.id, name: a.name, cls: aColour(a).cls, style: aColour(a).style } : { id: id, name: String(id || ''), cls: 'an' };
  }
  /* 0.22.0 an item's area on Today, Replies and the Agenda (mockup areas-v1 "where it shows"): its dot and name, only
     when there is more than one area; the colour is the area's (palette tokens or its two checked shades) */
  function areaMark(id) {
    var all = (B && B.areas) || [];
    if (!id || all.length < 2) return null;
    return all.filter(function (x) { return x.id === id; })[0] || null;
  }
  function areaLead(id) { var a = areaMark(id); return a ? [adot(a), h('span', { class: 'arn' }, a.name), ' · '] : []; }
  function tagOf(id) { var a = areaOf(id); return a.name ? sty(h('span', { class: 'tag ' + a.cls }, a.name), a.style) : null; }
  function events() { return ((B && B.schedule) || []).filter(function (e) { return !isDismissed('ev:' + (e.key || e.id)); }); }
  function eventById(id) { return ((B && B.schedule) || []).filter(function (e) { return e.id === id; })[0] || null; }
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
  function hourLabel(hr) {
    if (B && B.time_format === '24h') return pad(hr % 24) + ':00';
    return (hr % 12 || 12) + (hr % 24 >= 12 ? ' pm' : ' am');
  }

  /* ----- shared pieces ----- */
  function npBtn(kind, id, anc, wide) {
    var open = !!(U.pop && U.pop.anc === anc), nt = noteOf(kind, id);
    return h('button', { type: 'button', class: 'np' + (wide ? ' wide' : '') + (nt ? ' has' : ''), 'data-np': kind + ':' + id, 'data-anc': anc,
      'aria-haspopup': 'true', 'aria-expanded': String(open), 'aria-label': wide ? null : (nt ? 'Note and dismiss (has a note)' : 'Note and dismiss'),
      title: nt ? 'Note saved: ' + nt.text : 'Note for Callsheet, or dismiss' },
    [ico('note'), wide ? (nt ? 'Note saved' : 'Note or dismiss') : null]);
  }
  /* 0.20.0: one quiet line (the approved Today mockup) */
  function foot() {
    return h('p', { class: 'foot' }, 'Nothing is sent from here except an RSVP you press · calendar events and Gmail drafts only when you press');
  }

  /* ----- tabs (0.21.0: the old Briefing view, T_briefing, and the old replies list, T_replies, with One at a time,
     are gone: Today, Replies and Talk it through replace them) ----- */
  /* ===== 0.21.0 Agenda (agenda.py, docs/agenda.md): Day, Week and Month from api/agenda. What is on the calendar,
     what is only in a message, what the owner said, clashes, free stretches and invites. The server makes every label; this
     only draws text (h(), textContent). Past days stay, dimmed; the month shows dots only. */
  var AG = { v: null, err: '', view: 'day', date: '', sel: '', busy: false, seq: 0, at: 0 };
  function agShow() { if (U.tab === 'agenda' && ui.view === 'brief' && B) renderBrief(); }
  function agLoad() {
    var q = 'api/agenda?view=' + AG.view + (AG.date ? '&date=' + AG.date : '') + (AG.view === 'month' && AG.sel ? '&sel=' + AG.sel : '');
    var n = ++AG.seq;
    AG.busy = true;
    return api('GET', q).then(function (d) { if (n !== AG.seq) return; AG.busy = false; AG.v = d; AG.err = ''; AG.at = Date.now(); agShow(); },
      function (e) { if (n !== AG.seq) return; AG.busy = false; AG.err = e.message; agShow(); });
  }
  function agGo(view, date, sel) { AG.view = view; AG.date = date || ''; AG.sel = sel || ''; TD.addc = null; agLoad(); renderBrief(); }
  function agAfter() { if (TD.v) tdRefresh(); if (AG.v) agLoad(); }
  function agPost(path, body, said) {
    return mutate(function () { return api('POST', 'api/today/' + path, body); })
      .then(function () { if (said) showToast(said, ''); agAfter(); }, failToast);
  }
  /* 0.21.0 Answer invites (rsvp.py; approved 2026-11-13). Yes, No or Maybe, only when the owner presses one: it shows their
     current answer, then the result or a plain error; without "Allow adding events" the keys are off and it says how
     to allow it. Google tells the organizer; nothing else is sent. */
  var RS = {}, RS_ANS = [['yes', 'Yes'], ['no', 'No'], ['maybe', 'Maybe']];
  function rsvpBox(rs, item, via) {
    var st = RS[rs.event] || (RS[rs.event] = {});
    var cur = st.answer != null ? st.answer : rs.answer, word = st.word || rs.word, wid = 'rs-why-' + rs.event;
    return h('div', { class: 'rsvp', role: 'group', 'aria-label': 'Answer this invite' }, [
      h('div', { class: 'keys' }, [h('span', { class: 'rsw' }, ['Your answer: ', h('b', null, word)]), RS_ANS.map(function (a) {
        var on = cur === a[0];
        return btn('k' + (on ? ' on' : ''), a[1], { 'aria-pressed': String(on), disabled: !rs.on || st.busy ? true : null,
          'aria-describedby': rs.on ? null : wid, 'aria-busy': st.busy ? 'true' : null }, function () {
          if (st.busy || on) return;
          st.busy = true; st.err = ''; st.said = ''; renderBrief();
          var b = item ? { item: item, answer: a[0], via: via } : { event: rs.event, answer: a[0], via: via };
          mutate(function () { return api('POST', 'api/agenda/rsvp', b); }).then(function (r) {
            st.busy = false; st.answer = r.answer; st.word = r.word; st.said = 'Answered ' + r.word + '. Google told the organizer.';
            showToast('Answered ' + r.word + '.', 'Google told the organizer. Nothing else was sent.');
            agAfter(); renderBrief();
          }, function (e) { st.busy = false; st.err = (e && e.message) || 'That didn’t work.'; renderBrief(); });
        });
      })]),
      !rs.on && rs.why ? h('p', { class: 'dim small', id: wid }, rs.why) : null,
      st.err ? h('p', { class: 'err', role: 'alert' }, st.err) : st.said ? h('p', { class: 'rsok', role: 'status' }, st.said) : null]);
  }
  var AG_TAG = { 'You said': 'yes', 'Asks you': 'ask', 'Invite': 'inv' };
  function agTag(t) { return t ? h('span', { class: 'agtag ' + (AG_TAG[t] || '') }, t) : null; }
  /* one box: solid = on the calendar, dashed = only in a message, teal edge = the owner said it; withTime for the week */
  function agBox(r, withTime) {
    var k = r.k || 'cal', kids = [withTime && r.time ? h('span', { class: 'etm' }, r.time) : null,
      h('span', { class: 'et' }, (r.likely ? 'Likely: ' : '') + r.title), r.meta || areaMark(r.area) ? h('span', { class: 'em' }, (areaMark(r.area) ? [areaMark(r.area).name, r.meta ? ' · ' : ''] : []).concat([r.meta || ''])) : null,
      r.from || r.tag ? h('span', { class: 'ef' }, [r.from ? h('span', { class: 'fm' }, [tdIco(r.icon), r.from]) : null, agTag(r.tag)]) : null];
    var am = areaMark(r.area), ac = am ? aColour(am) : null;   /* 0.22.0: the area's bar on the block's left */
    var cls = 'eb ' + k + (r.on ? ' on' : '') + (ac ? ' arb ' + ac.cls : '');
    if (r.item) return sty(btn(cls, kids, { 'aria-label': [r.time, r.title, am ? am.name : '', r.meta, r.from, r.tag, r.on ? 'now' : ''].filter(Boolean).join(', ') }, function () { tdGo('item', r.item); }), ac && ac.style);
    return sty(h('div', { class: cls }, kids), ac && ac.style);
  }
  function agInvited(v, ev) { return (v.invites || []).some(function (x) { return x.rsvp && x.rsvp.event === ev; }); }
  function agAddCard(r, v) { return r.add && r.add.on && TD.addc && TD.addc.item === r.item ? tdAddCard(r.add, v.cal_targets || []) : null; }
  function agItem(r, v, where) {
    var rs = r.rsvp && !agInvited(v, r.rsvp.event) ? rsvpBox(r.rsvp, r.item, 'agenda') : null;
    var add = tdCalKey(r, null, where), link = !r.item && r.link ? tdLink(r.link, 'k q') : null;
    return h('div', { class: 'ebw' }, [agBox(r, false), add || link ? h('div', { class: 'ebx' }, [add, link]) : null, rs, agAddCard(r, v)]);
  }
  function agClash(r, small) {
    var al = r.level === 'al';
    return h('div', { class: 'clb agcl' + (al ? ' al' : '') + (small ? ' sm' : ''), role: 'group', 'aria-label': 'Clash ' + r.when + (r.group ? ': ' + r.group : '') + ', ' + r.until }, [
      h('div', { class: 'clh' }, [h('p', { class: 'cl' }, [al ? h('span', { class: 'led al', 'aria-hidden': 'true' }) : '▲ ', 'Clash · ' + (small ? r.short : r.time)]),
        small ? null : h('p', { class: 'until' }, r.until)]),
      small ? h('p', { class: 'until' }, r.until) : null,
      h('div', { class: 'lanes' }, r.sides.map(function (s) { return agBox(s, false); })),
      r.away ? h('p', { class: 'fu dim' }, r.away) : null,
      h('div', { class: 'keys' }, [btn(small ? 'k q' : 'k', r.group ? 'Open' : 'Open both', null, function () { tdGo('clash', r.id); }),
        small ? null : (r.reply ? tdLink(r.reply, 'k') : null),
        small ? null : btn('k q', 'Dismiss', null, function () { agPost('dismiss', { clash: r.id }, 'Clash dismissed.'); })])]);
  }
  function agRows(rows, v, where) {
    return h('div', { class: 'drs', role: 'list' }, rows.map(function (r) {
      if (r.kind === 'free') return h('div', { class: 'dr free', role: 'listitem' }, [h('span', { class: 'dt' }), h('p', { class: 'agfree' }, segs(r.text))]);
      if (r.kind === 'clash') return h('div', { class: 'dr', role: 'listitem' }, [h('span', { class: 'dt' }, r.time), agClash(r, false)]);
      return h('div', { class: 'dr' + (r.k === 'past' ? ' old' : ''), role: 'listitem' }, [h('span', { class: 'dt' + (/^By /.test(r.time || '') ? ' by' : '') }, r.time), agItem(r, v, where)]);
    }));
  }
  function agSaid(v) {
    if (!v.said || !v.said.length) return null;
    return h('section', { class: 'card', 'aria-labelledby': 'ag-said' }, [h('h2', { class: 'lab', id: 'ag-said' }, 'You said · with times'),
      v.said.map(function (s) {
        return btn('agl', [h('span', { class: 'rel' }, [h('b', null, s.rel), h('span', null, s.date)]),
          h('span', { class: 'w' }, [h('span', { class: 'wt' }, s.what), h('span', { class: 'fx' }, [h('span', { class: 'fm' }, [tdIco(s.icon), s.from]),
            s.cal ? h('span', { class: 'fm' }, [ico('cal'), 'On your calendar']) : null, s.clash ? h('span', { class: 'cw' }, '▲ Clash') : null])])],
        { 'aria-label': s.rel + ', ' + s.date + ': ' + s.what + (s.clash ? ', clash' : '') }, function () { tdGo('item', s.item); });
      })]);
  }
  function agInvites(v) {
    var xs = v.invites || [];
    if (!xs.length) return null;
    return h('section', { class: 'card', 'aria-labelledby': 'ag-inv' }, [h('h2', { class: 'lab', id: 'ag-inv' }, 'Invites waiting · ' + xs.length),
      xs.map(function (r) {
        var add = tdCalKey(r, null, 'inv'), link = r.link ? tdLink(r.link, 'k q') : null;
        return h('div', { class: 'rv' + (r.calendar ? '' : ' dsh') }, [
          h('div', { class: 'rvh' }, [tdIco(r.icon), btn('rvt', r.title, { 'aria-label': 'Open ' + r.title }, function () { tdGo('item', r.item); }), h('span', { class: 'ag' }, r.rel)]),
          h('p', { class: 'dim small' }, r.sub),
          r.rsvp ? rsvpBox(r.rsvp, r.item, 'agenda') : null,
          add || link ? h('div', { class: 'ebx' }, [link, add]) : null, agAddCard(r, v)]);
      })]);
  }
  function agOnly(v) {
    var xs = v.only_msgs || [];
    if (!xs.length) return null;
    return h('section', { class: 'card', 'aria-labelledby': 'ag-om' }, [h('h2', { class: 'lab', id: 'ag-om' }, 'Only in messages · this week'),
      h('p', { class: 'dim small' }, 'Not on your calendar. Add puts one event on your calendar; nothing else changes.'),
      xs.map(function (r) {
        return [h('div', { class: 'up' }, [h('p', { class: 'rel' }, [h('b', null, r.rel || ''), h('span', null, r.date || r.time || '')]),
          btn('w agw', [(r.likely ? 'Likely: ' : '') + r.title, r.tag ? ' ' : null, agTag(r.tag)], { 'aria-label': 'Open ' + r.title }, function () { tdGo('item', r.item); }),
          h('div', { class: 'fx' }, [h('span', { class: 'fm' }, [tdIco(r.icon), r.from]), tdCalKey(r, null, 'om')])]), agAddCard(r, v)];
      })]);
  }
  function agDay(v) {
    var d = v.day;
    return h('div', { class: 'g75' }, [h('section', { class: 'card', 'aria-labelledby': 'ag-dl' }, [h('h2', { class: 'lab', id: 'ag-dl' }, d.label),
      d.rows.length ? agRows(d.rows, v, 'ag') : h('p', { class: 'dim' }, 'Nothing on this day.')]),
    h('div', { class: 'agside' }, [agSaid(v), agInvites(v), agOnly(v)])]);
  }
  function agWeek(v) {
    var w = v.week, s = w.summary;
    var sum = [plural(s.clashes, 'clash', 'clashes'), plural(s.only_msgs, 'thing only in messages', 'things only in messages'),
      plural(s.promises, 'promise', 'promises'), s.most_free ? 'Most free: ' + s.most_free : ''].filter(Boolean);
    var cols = h('div', { class: 'wk' }, w.days.map(function (d) {
      return h('div', { class: 'wdc' + (d.past ? ' past' : '') + (d.today ? ' today' : ''), role: 'group', 'aria-label': d.label + (d.past ? ', past' : d.rel ? ', ' + d.rel : '') }, [
        btn('dh', [h('span', { class: 'dw' }, d.dow), h('span', { class: 'dn' }, String(d.n)), h('span', { class: 'drl' }, d.past ? 'done' : d.rel)],
          { 'aria-label': 'Open ' + d.label + ' as a day' }, function () { agGo('day', d.date); }),
        d.rows.map(function (r) {
          if (r.kind === 'free') return h('p', { class: 'wfree' }, segs(r.text));
          if (r.kind === 'clash') return agClash(r, true);
          return agBox(r, true);
        }), d.rows.length ? null : h('p', { class: 'wfree' }, 'Nothing')]);
    }));
    return [h('section', { class: 'card', 'aria-labelledby': 'ag-wl' }, [h('h2', { class: 'lab', id: 'ag-wl' }, [w.label, h('span', { class: 'r' }, 'Mon to Sun')]),
      h('p', { class: 'wsum' }, sum.map(function (t) { return h('span', null, t); })), cols]),
    h('div', { class: 'g2' }, [agOnly(v), h('div', { class: 'agside' }, [agInvites(v), agSaid(v)])])];
  }
  var AG_DOTS = [['c', 'On your calendar'], ['m', 'Only in a message'], ['x', 'Clash'], ['xa', 'Clash, under 24 h']];
  function agDots(ds) { return h('span', { class: 'dots', 'aria-hidden': 'true' }, ds.map(function (d) { return h('span', { class: 'dot ' + d }); })); }
  function agMonth(v) {
    var m = v.month, f = m.figs;
    var grid = h('div', { class: 'mg', role: 'group', 'aria-label': m.name }, [h('span', { class: 'mwl' }), m.dows.map(function (d) { return h('span', { class: 'mdw', 'aria-hidden': 'true' }, d); }),
      m.weeks.map(function (wk) {
        return [h('span', { class: 'mwl' + (wk.cur ? ' cur' : '') }, wk.label), wk.cells.map(function (c) {
          return btn('md' + (c.out ? ' out' : '') + (c.past ? ' past' : '') + (c.today ? ' today' : '') + (c.sel ? ' sel' : ''), [h('span', { class: 'mn' }, c.label), agDots(c.dots)],
            { 'aria-label': c.aria, 'aria-pressed': String(!!c.sel) }, function () { agGo('month', v.date, c.date); });
        })];
      })]);
    var sel = m.sel ? h('section', { class: 'card', 'aria-labelledby': 'ag-sel' }, [h('h2', { class: 'lab', id: 'ag-sel' }, [m.sel.label, h('span', { class: 'r' }, m.sel.rel || '')]),
      m.sel.rows.length ? agRows(m.sel.rows, v, 'ms') : h('p', { class: 'dim' }, 'Nothing on this day.'),
      btn('k q', 'Open the day', null, function () { agGo('day', m.sel.date); })]) : null;
    var figs = [[f.on_cal, 'On calendar'], [f.only_msgs, 'Only in messages'], [f.promises, 'Promises'], [f.clashes, 'Clashes']];
    return [h('section', { class: 'card', 'aria-labelledby': 'ag-30' }, [h('h2', { class: 'lab', id: 'ag-30' }, ['Next 30 days', h('span', { class: 'r' }, m.strip.to)]), tdStrip(m.strip)]),
      h('section', { class: 'card', 'aria-labelledby': 'ag-mo' }, [h('h2', { class: 'lab', id: 'ag-mo' }, 'Month'), grid,
        h('p', { class: 'mlg' }, AG_DOTS.map(function (d) { return h('span', null, [agDots([d[0]]), d[1]]); }))]),
      h('div', { class: 'g75' }, [sel, h('section', { class: 'card', 'aria-labelledby': 'ag-gl' }, [h('h2', { class: 'lab', id: 'ag-gl' }, m.name + ' at a glance'),
        h('div', { class: 'figs' }, figs.map(function (x) { return h('p', null, [h('b', null, String(x[0])), h('span', null, x[1])]); }))])]),
      h('div', { class: 'g2' }, [agInvites(v), agSaid(v)])];
  }
  function agHead(v) {
    var hd = v.header, vw = v.view, unit = vw === 'day' ? 'day' : vw;
    var nowLab = vw === 'day' ? 'Today' : vw === 'week' ? 'This week' : 'This month';
    return h('div', { class: 'tdy-hd ag-hd' }, [h('div', null, [
      h('p', { class: 'dline' }, [hd.time, ' · ', h('span', { class: 'fresh-' + hd.fresh.state }, hd.fresh.word), hd.clashes ? ' · ' + plural(hd.clashes, 'clash', 'clashes') : '']),
      h('h1', { class: 'hl' }, isPhone() ? hd.short : hd.title)]),
    h('div', { class: 'ag-nav' }, [h('div', { class: 'ag-step', role: 'group', 'aria-label': 'Move' }, [
      btn('k arr', '‹', { 'aria-label': 'Previous ' + unit }, function () { agGo(vw, hd.prev); }),
      btn('k', nowLab, { disabled: hd.label === nowLab ? true : null }, function () { agGo(vw, ''); }),
      btn('k arr', '›', { 'aria-label': 'Next ' + unit }, function () { agGo(vw, hd.next); })]),
    h('div', { class: 'ag-seg', role: 'group', 'aria-label': 'View' }, [['day', 'Day'], ['week', 'Week'], ['month', 'Month']].map(function (x) {
      return btn('k' + (AG.view === x[0] ? ' on' : ''), x[1], { 'aria-pressed': String(AG.view === x[0]) }, function () { agGo(x[0], v.date); });
    }))])]);
  }
  function agLegend(v) {
    return h('p', { class: 'ag-lgd', 'aria-label': 'Key' }, (v.legend || []).map(function (l) {
      return h('span', null, [h('span', { class: 'agsw ' + l[0], 'aria-hidden': 'true' }, l[0] === 'clash' ? '▲' : ''), l[1]]);
    }));
  }
  function T_agenda() {
    if ((!AG.v || Date.now() - AG.at > 120000) && !AG.busy && !AG.err) agLoad();
    var v = AG.v;
    if (!v) return h('div', { class: 'tdy ag' }, h('p', { class: 'tdy-empty' }, AG.err ? 'The agenda isn’t ready: ' + AG.err : 'Loading the agenda…'));
    var body = v.view === 'week' && v.week ? agWeek(v) : v.view === 'month' && v.month ? agMonth(v) : v.day ? agDay(v) : null;
    return h('div', { class: 'tdy ag ag-' + v.view, 'aria-busy': AG.busy ? 'true' : null }, [agHead(v), v.view === 'month' ? null : agLegend(v),
      AG.err ? h('p', { class: 'err', role: 'alert' }, AG.err) : null, body,
      v.can_add ? null : h('p', { class: 'dim small', id: 'td-calw' }, '“Add to calendar” and answering invites need one more permission: Setup › Connections › Allow adding events.')]);
  }
  /* ---------- 0.8.0 shared pieces: Google links, outside links and marks (everything is text through h()) ---------- */
  /* a link from the server's Google URL builders: https on Google's own hosts. kind 'mail' = Gmail only,
     'cal' = a Calendar event page (never Gmail). Anything else: no link. */
  function gLink(u, kind) {
    var s = safeHttps(u), host = '';
    if (!s) return null;
    try { host = new URL(s).hostname.toLowerCase(); } catch (e) { return null; }
    if (kind === 'mail') return host === 'mail.google.com' ? s : null;
    return host !== 'mail.google.com' && /(^|\.)google\.com$/.test(host) ? s : null;
  }
  function extA(cls, href, label, aria) {
    return h('a', { class: cls, href: href, target: '_blank', rel: 'noopener noreferrer', 'aria-label': aria || null }, label);
  }
  /* a mark ([code, text] from the server, or a code and a text): dashed for unverified, alert for sender doubts */
  var MK_BAD = ['sender_fail', 'look_alike', 'uses_your_address', 'lure', 'disagree', 'uses_name'];
  function mk8(code, text) {
    var cls = code === 'unverified' || code === 'event_file' ? 'unv' : code === 'not_answered' ? 'inv'
      : MK_BAD.indexOf(code) >= 0 ? 'bad' : '';
    return h('span', { class: 'mk8' + (cls ? ' ' + cls : '') }, String(text || ''));
  }
  function mks(list, skip) {
    return (Array.isArray(list) ? list : []).filter(function (m) { return Array.isArray(m) && m[1] && (!skip || skip.indexOf(m[0]) < 0); })
      .map(function (m) { return mk8(m[0], m[1]); });
  }

  /* ----- Setup's "calendar isn't read" card: 'area' (the area's Calendar is off), 'off' (the account's Calendar
     switch), 'none' (no calendar ticked), or null ----- */
  function calGap(a, acc) {
    var reads = a.reads || {}, svc = acc.services || {};
    if (!(reads.mail && svc.mail)) return null;
    if (!reads.calendar) return 'area';
    if (!svc.calendar) return 'off';
    return (acc.calendars || []).some(function (c) { return c.selected; }) ? null : 'none';
  }
  function gapCard(a, acc, err) {
    var why = calGap(a, acc);
    if (!why) return null;
    var fk = 'calgap:' + acc.id, first = '#cal-' + cssq(acc.id) + '-0';
    var b = btn('btn', why === 'none' ? 'Choose calendars' : 'Read its calendar', { 'data-fk': fk }, function () {
      ui.openCals[acc.id] = true;
      if (why === 'none') { renderSetup((acc.calendars || []).length ? first : '[data-fk="' + cssq(fk) + '"]'); return; }
      run([b], err, function () {
        if (why === 'area') {
          var r = a.reads || {};
          return api('PATCH', 'api/areas/' + encodeURIComponent(a.id), { reads: { calendar: true, mail: !!r.mail, chat: !!r.chat, tasks: !!r.tasks, writer: r.writer !== false } });
        }
        return api('PATCH', 'api/accounts/' + encodeURIComponent(acc.id), { services: { calendar: true, mail: !!(acc.services || {}).mail } });
      }, function () { return reload().then(function () { var f = main.querySelector(first); if (f) f.focus(); }); });
    });
    return h('div', { class: 'gap8 acct-gap', role: 'note' }, [h('div', { class: 'g1' }, [
      h('p', null, why === 'area' ? 'Calendar is off for this area, so invites to this address show only from email.'
        : why === 'off' ? 'Its calendar isn’t read, so invites there show only from email.'
          : 'No calendar is ticked, so invites there show only from email.'), b])]);
  }

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
    pjMore();
  }
  /* 0.21.0 (suggest.py): the Suggested card (5+ messages and Find projects, Personal / Professional) and Drive files */
  function pjMore() {
    return api('GET', 'api/projects/more').then(function (m) { PJ.more = m; if (U.tab === 'projects') renderBrief(); }, function () { PJ.more = null; });
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
      pjPoll(); renderBrief(); pjMore();
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
    var views = [['list', 'List', 'Projects'], ['timeline', 'Timeline', null], ['slipped', 'Slipping', 'Slipping'], ['review', 'Weekly review', 'Review']];
    return [h('div', { class: 'pjhd' }, [h('h2', { class: 'sec-h' }, ['Projects ', h('span', { class: 'n' }, plural(act.length, 'active', 'active') + (d.today_label ? ' · ' + d.today_label : ''))]),
      h('span', { class: 'sp' }), btn('tbtn', 'Settings', { 'aria-pressed': String(PJ.view === 'setup') }, function () { pjGo('setup'); })]),
    h('div', { class: 'live' }, [h('span', { class: 'livedot', 'aria-hidden': 'true' }), 'From your notes, mail, calendar' + (d.github && d.github.repos.length ? ' and GitHub' : '') + '.',
      d.draft ? h('span', null, ['Found ', h('b', null, String(d.draft.found)), ' ' + (d.draft.found === 1 ? 'project' : 'projects') + ' last time.']) : null]),
    h('div', { class: 'vnav', role: 'group', 'aria-label': 'Views' }, views.map(function (v) {
      var on = PJ.view === v[0];
      return h('button', { type: 'button', class: 'chip' + (v[2] ? '' : ' d-only'), 'aria-pressed': String(on), 'data-pjv': v[0] }, [
        h('span', { class: 'd-only' }, v[1]), h('span', { class: 'p-only' }, v[2] || v[1]),
        v[0] === 'slipped' && c.slipped ? h('span', { class: 'badge', 'aria-label': c.slipped + ' slipping' }, String(c.slipped)) : null,
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
    var sg = pjSuggested(sug);
    var mainCol = [c.slipped && top ? h('p', { class: 'slipsum' }, [kdot(top.kind), h('b', null, plural(c.slipped, 'thing slipping', 'things slipping')), h('span', { class: 'sp' }, ' · ' + top.title),
      btn('tbtn', 'See them', null, function () { pjGo('slipped'); })]) : null,
    tools, aiNote,
    h('h3', { class: 'grp-h' }, ['Projects ', h('span', { class: 'n' }, act.length + ' active · late first')]),
    act.length ? h('ul', { class: 'list' }, act.map(pjRow)) : h('p', { class: 'empty' }, sg ? 'Add a suggested project, or make a new one.' : 'No projects yet. Find projects, or make a new one.'),
    paused.length ? [h('h3', { class: 'grp-h' }, ['Paused ', h('span', { class: 'n' }, String(paused.length))]), h('ul', { class: 'list' }, paused.map(pjRow))] : null,
    d.not.length ? h('p', { class: 'fhint' }, ['Not projects: ', d.not.map(function (n, i) { return [i ? ', ' : '', btn('tbtn', n.name, { title: 'Bring it back' }, function (e) { pjDo('POST', 'api/projects/' + n.id + '/undo', {}, e.currentTarget); })]; })]) : null];
    return sg ? h('div', { class: 'pjg' }, [h('div', { class: 'pjgm' }, mainCol), sg]) : mainCol;
  }
  /* one Suggested card: Personal and Professional. A Find projects one (pid) is added or refused through its project;
     a 5+-message one through api/projects/suggested/<key>. Nothing becomes a project until the owner presses Add. */
  function pjSuggested(sug) {
    var s = PJ.more && PJ.more.suggested, seen = {}, sides = { personal: [], professional: [] };
    if (s) ['personal', 'professional'].forEach(function (k) { (s[k] || []).forEach(function (x) { if (x.pid) seen[x.pid] = 1; sides[k].push(x); }); });
    sug.forEach(function (p) { if (!seen[p.id]) sides.professional.push({ key: p.id, pid: p.id, name: p.name, why: p.why[0] || 'Found by “Find projects”' }); });
    var n = sides.personal.length + sides.professional.length;
    if (!n) return null;
    return h('section', { class: 'sgc', 'aria-labelledby': 'pj-sg' }, [h('h3', { class: 'lab', id: 'pj-sg' }, 'Suggested · ' + n),
      h('p', { class: 'fhint' }, 'From your messages and Find projects. Nothing becomes a project until you add it.'),
      [['personal', 'Personal'], ['professional', 'Professional']].map(function (sd) {
        var xs = sides[sd[0]];
        return xs.length ? [h('h4', { class: 'sgside' }, sd[1] + ' · ' + xs.length), xs.map(pjSugRow)] : null;
      })]);
  }
  function pjSugRow(x) {
    var pid = x.pid, p = pid ? pjById(pid) : null;
    function add(e) {
      if (pid) pjDo('POST', 'api/projects/' + pid + '/accept', p && p.ai && !p.ai.warning ? { use_ai: true } : {}, e.currentTarget);
      else pjDo('POST', 'api/projects/suggested/' + x.key + '/add', {}, e.currentTarget, function () { PJ.loading = false; pjLoad(); });
    }
    function not(e) { pjDo('POST', pid ? 'api/projects/' + pid + '/not' : 'api/projects/suggested/' + x.key + '/not', {}, e.currentTarget, pid ? null : function () { PJ.loading = false; pjLoad(); }); }
    var meta = [(x.sources || []).join(', '), (x.people || []).join(', '), x.ago ? 'last ' + x.ago : ''].filter(Boolean).join(' · ');
    return h('div', { class: 'sgi' }, [h('p', { class: 'sgt' }, ['Looks like a project: ', h('b', null, x.name), x.count ? ' · ' + plural(x.count, 'message', 'messages') : '']),
      x.why ? h('p', { class: 'fhint' }, x.why) : null, meta ? h('p', { class: 'sgm' }, meta) : null,
      h('div', { class: 'dbtns' }, [btn('act ghost', 'Add', { 'aria-label': 'Add project: ' + x.name }, add),
        p ? btn('tbtn', 'See what’s in it', null, function () { pjOpen(pid); }) : null,
        btn('tbtn', 'Not a project', { 'aria-label': 'Not a project: ' + x.name }, not)])]);
  }
  function pjFiles(p) {
    var fs = PJ.more && PJ.more.files && PJ.more.files[p.id];
    if (!fs || !fs.length) return null;
    return [h('h3', { class: 'plab' }, 'Files · Drive'), h('ul', { class: 'pjfiles' }, fs.map(function (f) {
      var u = gLink(f.link, 'cal');
      return h('li', null, [ico('drive'), h('span', { class: 'fx1' }, [h('span', { class: 'ft' }, f.title), h('small', null, [f.kind, f.ago, f.why].filter(Boolean).join(' · '))]),
        u ? extA('tbtn', u, 'Open', 'Open ' + f.title + ' in Drive') : null]);
    }))];
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
    pjFiles(p),
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
    var steps = [['slipped', 'Slipping'], ['projects', 'Projects'], ['next', 'Next week'], ['done', 'Done']];
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
      body = [h('h3', { class: 'rvq' }, "That's every project."), h('p', null, 'Your answers are saved in the projects. Nothing is sent from here except an RSVP you press.'),
        h('div', { class: 'qnav' }, [btn('tbtn', 'Start again', null, function (e) { pjDo('POST', 'api/projects/review', { action: 'start' }, e.currentTarget, function () { PJ.rv = { step: 'slipped', i: 0 }; }); }), h('span', { class: 'sp' }),
          btn('act', 'Back to the list', null, function () { pjGo('list'); })])];
    }
    var cur = R.step === 'projects' && order.length ? order[Math.min(R.i, order.length - 1)] : null;
    var rail = h('aside', { class: 'pjrail', 'aria-label': 'This review' }, [h('h3', { class: 'plab' }, 'This review'),
      h('ul', null, order.map(function (p, i) {
        var a = rv.answers[p.id];
        return h('li', { class: cur && cur.id === p.id ? 'on' : null }, [sdot(p.status), h('span', { class: 'nm' }, p.name),
          cur && cur.id === p.id ? h('span', { class: 'ans' }, 'Reviewing now') : a ? [h('span', { class: 'ans' }, a), btn('tbtn', 'Change', { 'aria-label': 'Change ' + p.name }, function () { go('projects', i); })] : null]);
      })), h('p', { class: 'fhint' }, PJ_REVIEW_WHEN[rv.setting] + ', or start it any time. Answers update the projects. Nothing is sent from here except an RSVP you press.')]);
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
  /* 0.7.0: Chat about one project ("Ask about this project"): the server filters the existing read-only tools */
  function chatScope() { return CH.conv ? (CH.conv.scope || null) : (CH.scope || null); }
  function chatScopeRow() {
    var sc = chatScope();
    if (!sc) return null;
    return h('div', { class: 'cscope' }, [h('span', { class: 'cchip' }, ['Only about ', h('b', null, sc.name),
      btn('cx', '×', { 'aria-label': 'Stop: ask about everything' }, function () { chatNew(); })]),
    tdScopeOf(sc) ? btn('tbtn', 'Back to Today', null, function () { var x = tdScopeOf(sc); tdGo(x[0], x[1]); }) :
    btn('tbtn', 'Back to project', null, function () { PJ.view = 'list'; PJ.open = sc.project; PJ.edit = null; setTab('projects'); })]);
  }
  function tdScopeOf(sc) {  /* 0.18.0: a chat about a Today item or clash ("ledger:item:5" on the server) */
    var m = /^(?:ledger:)?(item|clash):(\d+)$/.exec(String(sc.ledger || sc.project || ''));
    return m ? [m[1], parseInt(m[2], 10)] : null;
  }
  /* ===== 0.18.0 Today (docs/today.md): the ledger's Now and next, Needs you, Coming up and Clean up; the item and
     clash view; the Replies tab. The server makes every label (api/today); this only draws text (h(), textContent). */
  var TD = { v: null, err: '', sub: 'today', id: null, d: null, derr: '', rp: null, rsel: null, rd: null, note: null, rem: null,
    tab: 0, more: {}, old: false, autodraft: null, undo: null, said: '', busy: false };
  var TD_ICO = { text: 'chat', news: 'brief' };
  function tdIco(k, label) { return ico(TD_ICO[k] || k, label); }
  function tdAge(x) { return x.age; }
  function segs(a) { return (a || []).map(function (s) { return s[1] ? h('b', null, s[0]) : s[0]; }); }
  function tdHref(x) { return x && typeof x.href === 'string' && /^(https:|sgnl:\/\/signal\.me\/|sms:\+?\d)/.test(x.href) ? x.href : null; }
  function tdLink(x, cls, label) {
    var u = tdHref(x);
    return u ? h('a', { class: cls, href: u, target: '_blank', rel: 'noopener noreferrer' }, label || x.label) : null;
  }
  function tdLoad() { return api('GET', 'api/today').then(function (d) { TD.v = d; TD.err = ''; }, function (e) { TD.err = e.message; }); }
  function tdDetail() {
    if (TD.sub === 'item' || TD.sub === 'clash') return api('GET', 'api/today/' + (TD.sub === 'clash' ? 'clashes/' : 'items/') + TD.id)
      .then(function (d) { TD.d = d; TD.derr = ''; }, function (e) { TD.derr = e.message; });
    if (TD.sub === 'cleanup') return api('GET', 'api/today/cleanup').then(function (d) { TD.cu = d; }, function (e) { TD.derr = e.message; });
    return Promise.resolve();
  }
  function tdRefresh() {
    var p = [tdLoad(), tdDetail()];
    if (TD.rp) p.push(tdReplies(true));
    return Promise.all(p).then(function () { if (ui.view === 'brief' && B) renderBrief(); });
  }
  function tdGo(sub, id) {
    TD.sub = sub; TD.id = id == null ? null : id; TD.d = null; TD.derr = ''; TD.note = null; TD.rem = null; TD.tab = 0; TD.said = ''; TD.addc = null;
    if (U.tab !== 'briefing') setTab('briefing'); else renderBrief();
    window.scrollTo(0, 0);
    tdDetail().then(function () {
      if (TD.autodraft != null && TD.autodraft === TD.id && TD.d && TD.d.kind === 'item') { TD.autodraft = null; tdDraftStart(TD.d); return; }  /* 0.21.0 */
      if (ui.view === 'brief' && B) renderBrief();
    });
  }
  function tdBackKey(label) { return btn('k q tdy-back', [ico('back'), label || 'Today'], null, function () { tdGo('today'); }); }
  function tdPost(path, body, said) {
    if (TD.busy) return Promise.resolve();
    TD.busy = true;
    return mutate(function () { return api('POST', 'api/today/' + path, body); }).then(function (r) {
      TD.busy = false; TD.note = null; TD.rem = null;
      if (said) showToast(said, '');
      return tdRefresh().then(function () { renderLayer(); return r; });
    }, function (e) { TD.busy = false; failToast(e); });
  }
  function tdRel(u, items) {   /* 0.22.0: open clashes that share an item with what was just finished or filed */
    return api('POST', 'api/today/related', { items: items }).then(function (r) { u.related = (r && r.related) || []; if (TD.undo === u) renderBrief(); }, function () {});
  }
  function tdDone(ids, label) {
    return tdPost('done', { items: ids }, null).then(function (r) {
      if (r && r.done && r.done.length) { TD.undo = { ids: r.done, label: 'Done: ' + label }; renderBrief(); tdRel(TD.undo, r.done); }
    });
  }
  function tdDismiss(cid) {   /* 0.22.0: Dismiss is journaled, with Undo, and offers the related clashes */
    return tdPost('dismiss', { clash: cid }, null).then(function (r) {
      if (r && r.log) { TD.undo = { log: r.log, label: r.label, related: r.related || [] }; renderBrief(); }
      return r;
    });
  }
  function tdUndoLog(id) {
    return mutate(function () { return api('POST', 'api/actions/undo', { id: id }); }).then(function () {
      showToast('Undone.', ''); return tdRefresh().then(function () { renderBrief(); });
    }, failToast);
  }
  function tdUndoBar() {
    if (!TD.undo) return null;
    var u = TD.undo, rel = u.related || [];
    return h('div', { class: 'tdy-undo', role: 'status' }, [h('span', null, u.label),
      btn('k q', 'Undo', null, function () { TD.undo = null; if (u.log) tdUndoLog(u.log); else tdPost('undone', { items: u.ids }, 'Back on your list.'); }),
      rel.length ? btn('k', 'Also dismiss ' + rel.length + ' related clash' + (rel.length === 1 ? '' : 'es'), { 'data-relx': String(rel.length) }, function () {
        tdPost('related/dismiss', { clashes: rel.map(function (c) { return c.id; }) }, null).then(function (r) {
          if (r && r.log) { TD.undo = { log: r.log, label: r.label, related: [] }; renderBrief(); }
        });
      }) : null,
      btn('k q', 'Close', { 'aria-label': 'Close this message' }, function () { TD.undo = null; renderBrief(); })]);
  }
  /* "Remind me at…": Noon, 5 pm, Tomorrow 9 am or a time the owner picks */
  function tdRemind(key, target, who) {
    if (TD.rem !== key) return null;
    var pick = h('input', { type: 'datetime-local', class: 'inp', id: 'td-rem-at', 'aria-label': 'Pick a time' });
    function go(at) { var b = { at: at }; b[target[0]] = target[1]; tdPost('remind', b, 'Reminder set.'); }
    return h('div', { class: 'rmd', role: 'group', 'aria-label': 'Remind me' }, [
      h('div', { class: 'aseg' }, [['noon', 'Noon'], ['5pm', '5 pm'], ['tomorrow', 'Tomorrow 9 am']].map(function (x) {
        return btn('k', x[1], null, function () { go(x[0]); });
      })),
      h('div', { class: 'rmd-pick' }, [pick, btn('k', 'Set', null, function () { if (pick.value) go(pick.value); })]),
      who ? h('p', { class: 'dim' }, 'Clears by itself when Callsheet sees a message to ' + who + '.') : null]);
  }
  /* the note to the assistant: quick chips and free text, on one item or a batch */
  var TD_CHIPS = [['not_for_me', 'Not for me'], ['junk', 'Junk'], ['remind_tomorrow', 'Remind me tomorrow']];
  function tdNoteBox(key) {
    var n = TD.note;
    if (!n || n.key !== key) return null;
    var ta = h('textarea', { class: 'inp', id: 'td-note-text', rows: '2', 'aria-label': 'Note to ' + aname(), placeholder: 'Tell ' + aname() + ' why (optional)' });
    ta.value = n.text || '';
    on(ta, 'input', function () { n.text = ta.value; });
    return h('div', { class: 'notebox', role: 'group', 'aria-label': 'Note to ' + aname() }, [
      h('p', { class: 'lab' }, 'Note to ' + aname() + ' · ' + n.label),
      h('div', { class: 'chips' }, TD_CHIPS.map(function (c) {
        var onp = n.chips.indexOf(c[0]) >= 0;
        return btn('chipk', c[1], { 'aria-pressed': String(onp) }, function () {
          if (onp) n.chips = n.chips.filter(function (x) { return x !== c[0]; }); else n.chips.push(c[0]);
          renderBrief('#td-note-text');
        });
      })), ta,
      h('div', { class: 'keys' }, [btn('k', 'Save note', null, function () {
        tdPost('notes', { items: n.ids, chips: n.chips, text: n.text || '' }, 'Noted for ' + aname() + '.').then(function (r) {
          if (r && r.filed && r.filed.length) { TD.undo = { ids: r.filed, label: 'Filed: ' + r.label }; renderBrief(); tdRel(TD.undo, r.filed); }   /* 0.22.0 */
        });
      }), btn('k q', 'Cancel', null, function () { TD.note = null; renderBrief(); })])]);
  }
  function tdNoteOpen(key, ids, label) { TD.note = { key: key, ids: ids, label: label, chips: [], text: '' }; renderBrief('#td-note-text'); }

  /* ----- Today ----- */
  function T_today() {
    if (TD.sub === 'item' || TD.sub === 'clash') return tdItemView();
    if (TD.sub === 'cleanup') return tdCleanPage();
    if (TD.sub === 'talk') return tkView();
    var v = TD.v;
    if (!v) return h('div', { class: 'tdy' }, h('p', { class: 'tdy-empty' }, TD.err ? 'Today isn’t ready: ' + TD.err : 'Loading Today…'));
    return h('div', { class: 'tdy' }, [tdHead(v), tdTidyBanner(v), tdTidyUndoBar(), tdUndoBar(), tdNow(v), tdNeeds(v), tdComing(v), tdFeeds(v), tdClean(v)]);
  }
  /* 0.21.0 Talk it through (brief2.py, docs/brief.md): one step at a time, each a spoken turn and its picture, with
     quick replies and a reply field (a Chat turn on that step's items, saved in the day's Morning brief chat) */
  var TK = { d: null, err: '', at: 0, busy: false, timer: null, tr: false, prev: {}, upd: {}, sending: false, serr: '', draft: '', cards: {} };
  function tkOpen() { TK.err = ''; tdGo('talk'); tkLoad(true); }
  function tkBack() { if (location.hash === '#brief') history.replaceState(null, '', '#briefing'); tdGo('today'); }
  function tkRender() { if (TD.sub === 'talk' && ui.view === 'brief' && B) renderBrief(); }
  function tkLoad(start) {
    if (TK.timer) { clearTimeout(TK.timer); TK.timer = null; }
    TK.busy = true;
    return api(start ? 'POST' : 'GET', start ? 'api/talk/start' : 'api/talk', start ? {} : undefined).then(function (d) {
      TK.err = ''; tkSet(d);
    }, function (e) { TK.err = (e && e.message) || 'The brief isn’t ready.'; }).then(function () {
      TK.busy = false; tkRender();
      var r = TK.d && TK.d.run, c = TK.d && TK.d.conversation;
      var pend = !!(c && c.pending), after = TK.pend && !pend;   /* an answer just came: once more, for what it changed */
      TK.pend = pend;
      if (TD.sub === 'talk' && ((r && r.state === 'writing') || pend || after)) TK.timer = setTimeout(function () { tkLoad(false); }, after ? 800 : 1500);
    });
  }
  function tkSet(d) {
    var r = d && d.run, said = tkSaid(d);
    if (r) r.steps.forEach(function (st) {
      var j = JSON.stringify(st.blocks || null);
      if (st.blocks && TK.prev[st.key] != null && TK.prev[st.key] !== j && said[st.key]) TK.upd[st.key] = true;
      if (st.blocks) TK.prev[st.key] = j;
    });
    TK.d = d;
    if (r && TK.at >= r.steps.length) TK.at = Math.max(0, r.steps.length - 1);
  }
  function tkSaid(d) {   /* the step's replies: the owner's message and the answer after it */
    var out = {}, ms = (d && d.conversation && d.conversation.messages) || [];
    ms.forEach(function (m, i) {
      var b = m.brief_step; if (!b || !b.key) return;
      if (m.role === 'user') (out[b.key] = out[b.key] || []).push({ q: m, a: null, i: i });
      else if (out[b.key] && out[b.key].length) { var last = out[b.key][out[b.key].length - 1]; if (!last.a) { last.a = m; last.ai = i; } }
    });
    return out;
  }
  function tkSend(text, st) {
    text = String(text || '').trim();
    var r = TK.d && TK.d.run;
    if (!text || !r || TK.sending) return;
    TK.sending = true; TK.serr = ''; TK.pend = true; tkRender();
    api('POST', 'api/talk/reply', { text: text, step: st.key, run: r.id }).then(function () { TK.draft = ''; }, function (e) { TK.serr = (e && e.message) || 'Couldn’t send that.'; })
      .then(function () { TK.sending = false; tkLoad(false); });
  }
  function tkCard(m, ix) {
    var k = m.card, cid = TK.d && TK.d.conversation && TK.d.conversation.id;
    if (!k || typeof k !== 'object' || k.status !== 'ready') return k && k.status === 'refused' ? h('p', { class: 'tk-cerr' }, String(k.reason || 'Nothing was changed.')) : null;
    var st = TK.cards[ix] = TK.cards[ix] || {};
    function press(undo) {
      if (!k.token || !cid || st.busy) return;
      st.busy = true; st.err = ''; tkRender();
      api('POST', undo ? 'api/chat/undo' : 'api/chat/apply', { chat: cid, turn: k.turn, token: k.token }).then(function (res) {
        if (undo) { k.applied = null; k.undone = true; delete k.token; } else k.applied = (res && res.applied) || { label: k.label };
        if (k.kind === 'assistant_name') return loadState().then(null, function () { });
      }, function (e) { st.err = (e && e.message) || 'That didn’t work.'; }).then(function () { st.busy = false; tkLoad(false); });
    }
    var lab = String(k.label || ''), err = st.err ? h('p', { class: 'tk-cerr', role: 'alert' }, st.err) : null;
    if (k.undone) return h('div', { class: 'tk-card done', role: 'status' }, 'Undone: ' + lab + '.');
    if (k.applied && k.kind === 'calendar') return h('div', { class: 'tk-card done', role: 'status' }, [h('span', null, 'On your calendar ✓'),   /* 0.21.0 */
      typeof k.applied === 'object' && k.applied.link ? tdLink(k.applied.link, 'k q') : null, h('span', { class: 'dim' }, 'Callsheet can’t delete events: remove it in Google Calendar.')]);
    if (k.type === 'gdraft' && k.applied) return h('div', { class: 'tk-card done', role: 'status' }, h('span', null, gdOk(k.applied.open_url)));  /* 0.21.0 */
    if (k.applied) return h('div', { class: 'tk-card done', role: 'status' }, [h('span', null, 'Done: ' + ((typeof k.applied === 'object' && k.applied.label) || lab) + '.'),
      k.token ? btn('k q', 'Undo', { 'aria-busy': st.busy ? 'true' : null }, function () { press(true); }) : null, err]);
    if (k.expired || !k.token) return h('p', { class: 'dim' }, 'Not changed. This card has expired; ask again.');
    return h('div', { class: 'tk-card', role: 'group' }, [h('p', { class: 'tk-ctitle' }, String(k.title || 'Action')), h('p', null, lab + '?'),
      k.why ? h('p', { class: 'dim' }, String(k.why)) : null, k.type === 'gdraft' ? rcRows(k) : null, err,
      h('div', { class: 'tk-cacts' }, [btn('k go', 'Confirm', { 'aria-busy': st.busy ? 'true' : null }, function () { press(false); }),
        h('span', { class: 'dim' }, k.type === 'gdraft' ? 'Nothing is saved until you press Confirm. Nothing is sent from here except an RSVP you press.' : 'Nothing changes until you press Confirm.')])]);
  }
  function tkWords(st) {   /* the assistant's turn: the mark beside the words (mockup rework-today-v4, brief frames) */
    return h('div', { class: 'tk-line' }, [assistantMark('speaking', true), h('p', { class: 'tk-say' }, (st.segs || [[st.text || '', null]]).map(function (sg) {
      return sg[1] ? h('mark', { class: 'tk-flag', title: aname() + '’s words: ' + String(sg[1].why || '') }, String(sg[0])) : String(sg[0]);
    }))]);
  }
  function tkStep(st, n, total) {
    var said = tkSaid(TK.d)[st.key] || [], phone = isPhone();
    if (st.state !== 'ready') return h('div', { class: 'tk-body' }, h('p', { class: 'tk-say dim' }, aname() + ' is writing this step…'));
    var flagged = (st.checks || []).map(function (c) { return c.what; });
    var vis = (st.blocks || []).map(function (b) { try { return window.CallsheetVisuals ? window.CallsheetVisuals.render(b) : null; } catch (e) { return null; } });
    var form = h('form', { class: 'tk-reply' }, [h('input', { type: 'text', class: 'tk-in', 'aria-label': 'Reply to ' + aname(), placeholder: 'Reply, or ask about this…', maxlength: '2000', value: TK.draft }),
      h('button', { type: 'submit', class: 'k', disabled: TK.sending ? true : null }, TK.sending ? 'Sending…' : 'Send')]);
    form.addEventListener('submit', function (e) { e.preventDefault(); var i = form.querySelector('input'); TK.draft = i.value; tkSend(i.value, st); });
    form.querySelector('input').addEventListener('input', function (e) { TK.draft = e.target.value; });
    return h('div', { class: 'tk-body' }, [tkWords(st),
      flagged.length ? h('p', { class: 'tk-attr' }, aname() + '’s words, not found in these messages: ' + flagged.join(', ') + '.') : null,
      st.warning ? h('p', { class: 'tk-warn', role: 'note' }, String(st.warning)) : null,
      st.by === 'template' ? h('p', { class: 'tk-by dim' }, 'Written by Callsheet from your messages (the local model wasn’t used).') : null,
      said.map(function (x) {
        return h('div', { class: 'tk-said' }, [h('p', { class: 'tk-me' }, String(x.q.text || '')),
          h('div', { class: 'tk-line' }, [assistantMark(x.a ? 'speaking' : 'listening', true), x.a ? h('p', { class: 'tk-ans' }, String(x.a.error || x.a.text || '')) : h('p', { class: 'tk-ans dim' }, aname() + ' is answering…')]),
          x.a ? tkCard(x.a, x.ai) : null]);
      }),
      vis.length ? h('div', { class: 'tk-vis' }, [h('div', { class: 'tk-vhd' }, [h('span', { class: 'tk-vlab' }, (st.title + (st.count ? ' · ' + st.count : '')).toUpperCase()),
        TK.upd[st.key] ? h('span', { class: 'tk-upd' }, 'UPDATED') : null])].concat(vis)) : null,
      h('div', { class: 'tk-chips' }, (st.chips || []).map(function (c) { return btn('k chip', c, { disabled: TK.sending ? true : null }, function () { tkSend(c, st); }); })),
      form, TK.serr ? h('p', { class: 'tk-cerr', role: 'alert' }, TK.serr) : null,
      h('div', { class: 'tk-nav' }, [h('div', { class: 'tk-navb' }, [btn('k', '‹ Back', { disabled: n === 0 ? true : null }, function () { TK.at = n - 1; tkRender(); window.scrollTo(0, 0); }),
        n + 1 < total ? btn('k ink', 'Next ›', null, function () { TK.at = n + 1; tkRender(); window.scrollTo(0, 0); }) : btn('k ink', 'Done', null, tkBack)]),
        phone ? null : h('span', { class: 'tk-of' }, 'STEP ' + (n + 1) + ' OF ' + total)])]);
  }
  function tkTranscript(r) {
    var said = tkSaid(TK.d), rows = [];
    r.steps.forEach(function (st) {
      if (st.state !== 'ready') return;
      rows.push(h('li', null, [h('span', { class: 'tk-who' }, aname().toUpperCase()), h('span', null, String(st.text || ''))]));
      (said[st.key] || []).forEach(function (x) {
        rows.push(h('li', { class: 'me' }, [h('span', { class: 'tk-who' }, 'YOU'), h('span', null, String(x.q.text || ''))]));
        if (x.a) rows.push(h('li', null, [h('span', { class: 'tk-who' }, aname().toUpperCase()), h('span', null, String(x.a.error || x.a.text || ''))]));
      });
    });
    return h('ol', { class: 'tk-tr' }, rows);
  }
  function tkView() {
    var r = TK.d && TK.d.run, phone = isPhone();
    if (!r) return h('div', { class: 'tdy tk' }, [btn('k q tdy-back', [ico('back'), 'Today'], null, tkBack),
      h('p', { class: 'tdy-empty' }, TK.err ? 'The brief isn’t ready: ' + TK.err : aname() + ' is getting your brief ready…')]);
    var total = r.steps.length, n = Math.min(TK.at, total - 1), st = r.steps[n];
    if (!total) return h('div', { class: 'tdy tk' }, [btn('k q tdy-back', [ico('back'), 'Today'], null, tkBack), h('p', { class: 'tdy-empty' }, 'Nothing to talk through right now.')]);
    var title = { morning: 'Morning brief', midday: 'Midday check-in', afternoon: 'Afternoon check-in' }[r.call] || 'Morning brief';
    var side = phone ? null : h('aside', { class: 'tk-side' }, [btn('k q tdy-back', [ico('back'), 'Today'], null, tkBack),
      h('p', { class: 'lab' }, 'THIS MORNING'),
      h('ol', { class: 'tk-steps' }, r.steps.map(function (x, i) {
        return h('li', null, btn('tk-st' + (i === n ? ' on' : i < n ? ' heard' : ''), [h('span', { class: 'tk-n' }, String(i + 1)),
          h('span', { class: 'tk-t' }, x.title + (x.count ? ' · ' + x.count : '')), h('span', { class: 'tk-s' }, i === n ? 'NOW' : i < n ? 'HEARD' : '')],
          { 'aria-current': i === n ? 'step' : null }, function () { TK.at = i; tkRender(); }));
      })),
      h('p', { class: 'dim tk-help' }, 'Answer at any step: tap a reply or type one. Your replies are saved in Chat as today’s Morning brief.')]);
    var bar = h('div', { class: 'tk-bar', role: 'list', 'aria-label': 'Steps' }, r.steps.map(function (x, i) {
      return h('span', { role: 'listitem', class: 'tk-seg' + (i === n ? ' on' : i < n ? ' heard' : ''), 'aria-label': x.title + (i === n ? ' (now)' : '') });
    }));
    var head = h('div', { class: 'tk-hd' }, [h('span', { class: 'tk-av' }, assistantMark('idle')), h('div', null, [
      h('p', { class: 'tk-date' }, (phone ? r.date_short + ' · ' + (n + 1) + ' OF ' + total : r.date_line).toUpperCase()),
      h('h2', null, title)])]);
    var main = h('section', { class: 'tk-main', 'aria-label': title + ', step ' + (n + 1) + ' of ' + total + ': ' + st.title }, [
      phone ? btn('k q tdy-back', [ico('back'), 'Today'], null, tkBack) : null, head, bar,
      phone && !(st.blocks || []).length ? h('h3', { class: 'tk-h3' }, st.title + (st.count ? ' · ' + st.count : '')) : null,
      tkStep(st, n, total),
      h('div', { class: 'tk-foot' }, [btn('k q tk-trk', TK.tr ? 'Hide transcript' : 'Show transcript', { 'aria-expanded': TK.tr ? 'true' : 'false' }, function () { TK.tr = !TK.tr; tkRender(); }),
        TK.tr ? tkTranscript(r) : null,
        h('div', { class: 'tk-voice', 'aria-disabled': 'true' }, [h('span', null, (TK.d.voice && TK.d.voice.label) || 'Start talking'), h('span', { class: 'tk-soon' }, 'COMING LATER')])])]);
    return h('div', { class: 'tdy tk' + (phone ? ' ph' : '') }, [side, main]);
  }
  function tdHead(v) {
    /* 0.21.4 Refresh: rebuilds Today and the day's brief from what is already read (no mail is read again) */
    var busy = !!TD.refreshing, upd = busy ? 'Updating…' : (v.header.updated || '');
    var rf = btn('k tdy-rf', [h('span', { class: 'rfi' + (busy ? ' spin' : ''), 'aria-hidden': 'true' }, ico('refresh')), 'Refresh'],
      { 'aria-busy': busy ? 'true' : null, disabled: busy ? true : null, 'aria-label': 'Refresh Today and the day’s brief' }, tdRefreshNow);
    return h('div', { class: 'tdy-hd' }, [h('div', null, [
      h('p', { class: 'dline' }, [v.header.time, ' · ', h('span', { class: 'fresh-' + v.header.fresh.state }, v.header.fresh.word),
        upd ? h('span', { class: 'upd', role: 'status' }, ' · ' + upd) : null]),
      h('h1', { class: 'hl' }, isPhone() ? v.header.date_short : v.header.date_long)]),
    h('div', { class: 'tdy-hk' }, [rf, btn('k', [ico('chat'), 'Talk it through'], null, tkOpen)])]);
  }
  function tdRefreshNow() {
    if (TD.refreshing) return;
    TD.refreshing = true; renderBrief();
    mutate(function () { return api('POST', 'api/today/refresh', {}); }).then(function (d) {
      TD.refreshing = false; TD.v = d; TD.err = ''; renderBrief();
    }, function (e) { TD.refreshing = false; renderBrief(); failToast(e); });
  }
  /* 0.21.4 the one-time Tidy up offer after the update (tidy.py): the owner presses it; never automatic. One Undo. */
  function tdTidyBanner(v) {
    var b = v.banner;
    if (!b || TD.tidyBusy === 'done') return null;
    return h('div', { class: 'tdy-tidy', role: 'region', 'aria-label': 'Tidy up' }, [h('p', null, b.text),
      h('div', { class: 'keys' }, [btn('k go', 'Tidy up', { 'aria-busy': TD.tidyBusy ? 'true' : null, disabled: TD.tidyBusy ? true : null }, function () {
        TD.tidyBusy = true; renderBrief();
        mutate(function () { return api('POST', 'api/today/tidy', {}); }).then(function (r) {
          TD.tidyBusy = false; TD.tidyUndo = r && r.moved ? r : null;
          return tdRefresh();
        }, function (e) { TD.tidyBusy = false; renderBrief(); failToast(e); });
      }), btn('k q', 'Not now', null, function () { tdPost('tidy/later', {}, 'You can ask Chat to tidy up any time.'); })])]);
  }
  function tdTidyUndoBar() {
    var u = TD.tidyUndo;
    if (!u) return null;
    return h('div', { class: 'tdy-undo', role: 'status' }, [h('span', null, 'Tidied ' + u.moved + (u.moved === 1 ? ' item' : ' items') + ' into Clean up'),
      btn('k q', 'Undo', null, function () { TD.tidyUndo = null; tdPost('tidy/undo', { run: u.run }, 'Back where they were.'); }),
      btn('k q', 'Close', { 'aria-label': 'Close this message' }, function () { TD.tidyUndo = null; renderBrief(); })]);
  }
  /* 0.21.0 security alerts (heads.py): always first, a red rule, the lock and the word, until the owner marks one Done.
     The red key goes on the newest; no orange key on a screen with a red one. */
  function tdSec(s, i) {
    var open = tdLink(s.link, 'k' + (i === 0 ? ' red' : ''));
    var k = mks(s.marks);
    return h('div', { class: 'clb al sec', role: 'group', 'aria-label': 'Security alert from ' + s.who }, [
      h('div', { class: 'clh' }, [h('p', { class: 'cl' }, [h('span', { class: 'led al', 'aria-hidden': 'true' }), ico('lock'), 'Security']),
        h('p', { class: 'ag' }, s.at)]),
      h('p', { class: 'ct' }, [h('b', null, s.who), ' · ' + s.what]),
      h('p', { class: 'fm' }, [tdIco(s.icon), s.source]),
      k.length ? h('p', { class: 'mkl' }, k) : null,
      h('div', { class: 'keys' }, [open,
        btn('k' + (!open && i === 0 ? ' red' : ''), 'See it', null, function () { tdGo('item', s.id); }),
        btn('k q', 'Done', null, function () { tdDone([s.id], 'security alert from ' + s.who); })])]);
  }
  function tdNow(v) {
    var nn = v.now_next, kids = [], sec = nn.security || [];
    sec.forEach(function (s, i) { kids.push(tdSec(s, i)); });
    nn.clashes.forEach(function (c) { kids.push(c.group ? tdClashGroup(c, sec.length > 0) : c.compact ? tdClashLine(c) : tdClash(c, sec.length > 0)); });
    nn.followups.forEach(function (f) {
      kids.push(h('div', { class: 'clb' }, [h('p', { class: 'cl' }, '▲ Follow-up'), h('p', { class: 'ct' }, f.line ? [h('b', null, f.what), ' · ' + f.line] : [h('b', null, f.who), ' hasn’t heard from you: ' + f.what]),
        f.reminded ? h('p', { class: 'fu dim' }, 'Reminded ' + f.reminded) : f.remind_at ? h('p', { class: 'fu dim' }, 'Reminder ' + f.remind_at) : null,
        h('div', { class: 'keys' }, [btn('k', 'Open', null, function () { tdGo('item', f.item); }),
          btn('k q', 'Remind me at…', { 'aria-expanded': String(TD.rem === 'lf' + f.id) }, function () { TD.rem = TD.rem === 'lf' + f.id ? null : 'lf' + f.id; renderBrief(); })]),
        tdRemind('lf' + f.id, ['followup', f.id], f.who)]));
    });
    nn.cleared.forEach(function (x) { kids.push(h('p', { class: 'clrd' }, [h('span', { class: 'led ok', 'aria-hidden': 'true' }), 'Cleared ' + x.at + ' · ' + x.why])); });
    if (!nn.clashes.length && !nn.followups.length) kids.push(h('p', { class: 'dim' }, 'No clashes.'));
    if (nn.clashes_left_out) kids.push(h('p', { class: 'dim' }, plural(nn.clashes_left_out, 'clash', 'clashes') + ' with promotions, finished or started items not shown.'));
    var tl = nn.timeline;
    if (tl.length) kids.push(h('div', { class: 'tdl', role: 'list', 'aria-label': 'Today’s calendar' }, tl.map(function (r) {
      if (r.kind === 'gap') return h('div', { class: 'gapr', role: 'listitem' }, [r.done ? h('span', { class: 'done' }, r.done + ' done') : null, r.text ? h('span', null, r.text) : null]);
      return h('div', { class: 'tlr' + (r.on ? ' on' : ''), role: 'listitem' }, [h('span', { class: 'tt' }, r.time), h('span', { class: 'tw' }, r.title), h('span', { class: 'tm' }, r.meta)]);
    })));
    return h('section', { class: 'card', 'aria-labelledby': 'td-nn' }, [h('h2', { class: 'lab', id: 'td-nn' }, 'Now and next'), kids]);
  }
  function tdAlertLine(c) {
    var a = c.alert || {};
    if (a.state === 'sent') return 'Sent to your phone' + (a.at ? ' ' + a.at : '');
    if (a.state === 'held') return 'Phone alert waiting (quiet hours)';
    if (a.state === 'failed') return 'The phone alert didn’t go out';
    return '';
  }
  function tdClash(c, calm) {
    var red = c.level === 'red', fu = c.followup, key = 'c' + c.id;
    var lead = c.lead ? (red ? ' red' : calm ? '' : ' go') : '';   /* calm: a security alert holds the red key */
    var main1 = c.reply ? tdLink(c.reply, 'k' + lead) : null;
    var keys = [main1];
    if (!main1) keys.push(btn('k' + lead, 'Open both', null, function () { tdGo('clash', c.id); }));
    else keys.push(btn('k', 'Open both', null, function () { tdGo('clash', c.id); }));
    if (fu) keys.push(btn('k', fu.remind_at ? 'Reminder ' + fu.remind_at : 'Remind me at…', { 'aria-expanded': String(TD.rem === key) }, function () { TD.rem = TD.rem === key ? null : key; renderBrief(); }));
    keys.push(btn('k q', 'Dismiss', null, function () { tdDismiss(c.id); }));
    var al = tdAlertLine(c);
    return h('div', { class: 'clb' + (red ? ' al' : ''), role: 'group', 'aria-label': 'Clash ' + c.when }, [
      h('div', { class: 'clh' }, [h('p', { class: 'cl' }, [red ? h('span', { class: 'led al', 'aria-hidden': 'true' }) : '▲ ', 'Clash · ' + c.when]),
        h('p', { class: 'until' }, c.until)]),
      c.rows.map(function (r) {
        return h('div', { class: 'clr' + (r.yes ? ' yes' : '') }, [h('span', { class: 'srcc' }, [tdIco(r.icon), r.chip]), h('p', { class: 'ct' }, segs(r.text)), h('span', { class: 'ag' }, tdAge(r))]);
      }),
      c.away ? h('p', { class: 'fu dim' }, c.away) : null,
      fu && fu.note ? h('p', { class: 'fu' }, fu.note) : null,
      fu && fu.reminded ? h('p', { class: 'fu dim' }, 'Reminded ' + fu.reminded + ' · clears by itself when Callsheet sees a message to ' + fu.who + '.') : null,
      al ? h('p', { class: 'fu dim' }, al) : null,
      h('div', { class: 'keys' }, keys), fu ? tdRemind(key, ['followup', fu.id], fu.who) : null]);
  }
  /* 0.22.x: the owner's trip's one grouped clash: one row ("Your trip to Tromsø overlaps 3 entries") that opens to list the
     calendar entries during it (open at first when it is near, like a clash's two sides). The server makes every label. */
  function tdClashGroup(c, calm) {
    var g = c.group, k = 'cg' + c.id, open = TD.more[k] === undefined ? !c.compact : !!TD.more[k], red = c.level === 'red';
    var lead = c.lead ? (red ? ' red' : calm ? '' : ' go') : '';
    var row = btn('cll', [h('span', { class: 'cl' }, '▲ ' + c.when), h('span', { class: 'ct' }, segs(g.title)), h('span', { class: 'until' }, c.until)],
      { 'aria-expanded': String(open), 'aria-label': 'Clash ' + c.when + ': ' + g.plain + ', ' + c.until }, function () { TD.more[k] = !open; renderBrief(); });
    if (!open) return row;
    var al = tdAlertLine(c);
    return h('div', { class: 'clb' + (red ? ' al' : ''), role: 'group', 'aria-label': 'Clash ' + c.when }, [row,
      h('div', { role: 'list', 'aria-label': g.plain }, g.entries.map(function (r) {
        return h('div', { class: 'clr', role: 'listitem' }, [h('span', { class: 'srcc' }, [tdIco(r.icon), r.chip]), h('p', { class: 'ct' }, segs(r.text)), h('span', { class: 'ag' }, r.age)]);
      })),
      al ? h('p', { class: 'fu dim' }, al) : null,
      c.more ? null : h('div', { class: 'keys' }, [btn('k' + lead, 'Open', null, function () { tdGo('clash', c.id); }),
        btn('k q', 'Dismiss', null, function () { tdDismiss(c.id); })])]);
  }
  function tdClashLine(c) {
    return btn('cll', [h('span', { class: 'cl' }, '▲ ' + c.when), h('span', { class: 'ct' }, segs(c.line)), h('span', { class: 'until' }, c.until)],
      { 'aria-label': 'Clash ' + c.when + ': ' + c.names.join(' and ') + ', ' + c.until }, function () { tdGo('clash', c.id); });
  }
  function tdNeeds(v) {
    var n = v.needs_you, lim = TD.more.needs ? n.rows.length : (isPhone() ? 3 : 4);
    var rows = n.rows.slice(0, lim).map(tdNeedRow);
    return h('section', { class: 'card', 'aria-labelledby': 'td-ny' }, [h('h2', { class: 'lab', id: 'td-ny' }, ['Needs you', h('span', { class: 'r' }, n.total ? 'oldest first' : '')]),
      n.total ? rows : h('p', { class: 'dim' }, 'Nothing waiting on you.'),
      n.rows.length > lim ? btn('k q more', (n.rows.length - lim) + ' more, ' + n.total + ' in all', null, function () { TD.more.needs = true; renderBrief(); }) : null,
      tdOlder(n.older)]);
  }
  /* 0.21.4 "Older, still open?" (tidy.older_of): collapsed, with its count; Clear all marks them done (with Undo).
     They stay open in search until then. */
  function tdOlder(o) {
    if (!o || !o.total) return null;
    var open = !!TD.more.older;
    var head = btn('k q tdy-old', [o.label + ' · ' + o.total, h('span', { class: 'chev', 'aria-hidden': 'true' }, open ? '▴' : '▾')],
      { 'aria-expanded': String(open), 'aria-controls': 'td-old' }, function () { TD.more.older = !open; renderBrief(); });
    return h('div', { class: 'tdy-oldg' }, [head, open ? h('div', { id: 'td-old', role: 'group', 'aria-label': o.label }, [
      h('p', { class: 'dim' }, o.note), o.rows.map(tdNeedRow),
      h('div', { class: 'keys' }, [btn('k', 'Clear all ' + o.total, null, function () { tdDone(o.ids, o.total + ' older items'); })])]) : null]);
  }
  function tdNeedRow(r) {
      var b = btn('nr', [tdIco(r.icon, r.source), h('span', { class: 'who' }, [r.who, r.mine ? h('span', { class: 'mine' }, 'You said') : null]),
        h('span', { class: 'ag' }, tdAge(r)), h('span', { class: 'why' }, areaLead(r.area).concat([r.late ? h('b', { class: 'late' }, r.late + ' · ') : null,   /* 0.22.0: past its deadline; its area */
          r.back ? r.back + '. ' : null,   /* 0.22.1: back from Clean up, saying why */
          (r.likely ? 'Likely: ' : '') + r.why + (r.amount ? ' · ' + r.amount : '')]))], null, function () { tdGo('item', r.id); });
      var l = r.invite ? tdLink(r.invite.link, 'k nrl') : null;   /* 0.21.0: an invite, answered where it lives */
      var rs = r.invite && r.invite.rsvp ? rsvpBox(r.invite.rsvp, r.id, 'today') : null;   /* or here: Yes, No, Maybe */
      return l || rs ? h('div', { class: 'nrw' + (rs ? ' hasrs' : '') }, [b, l, rs]) : b;
  }
  function tdStrip(cu) {
    var s = h('div', { class: 'strip', 'aria-hidden': 'true' });
    cu.marks.forEach(function (m) { add(s, h('span', { class: 'dc' + (m ? ' ' + m : '') })); });
    cu.weeks.forEach(function (w) { var e = h('span', { class: 'swl' }, w.label); e.style.gridColumn = w.col + ' / span ' + w.span; add(s, e); });
    return s;
  }
  function tdComing(v) {
    var cu = v.coming_up, lim = TD.more.coming ? 1e9 : (isPhone() ? 4 : 6), shown = 0, out = [];
    cu.groups.forEach(function (g) {
      if (shown >= lim) return;
      var rows = g.rows.slice(0, lim - shown); shown += rows.length;
      out.push(h('h3', { class: 'cgh' }, [g.label, h('span', null, g.range)]));
      rows.forEach(function (r) {
        out.push(h('div', { class: 'up' }, [h('p', { class: 'rel' }, [h('b', null, r.rel), h('span', null, r.date)]),
          h('p', { class: 'w' }, [(r.likely ? 'Likely: ' : '') + r.what]),
          h('div', { class: 'fx' }, [r.from ? h('span', { class: 'fm' }, [tdIco(r.icon), r.from]) : h('span', { class: 'fm' }, [ico('cal'), 'On your calendar']),
            tdCalKey(r, cu, 'cu')])]));
        if (r.add && r.add.on && TD.addc && TD.addc.item === r.item) out.push(tdAddCard(r.add, cu.cal_targets || []));
      });
    });
    return h('section', { class: 'card', 'aria-labelledby': 'td-cu' }, [h('h2', { class: 'lab', id: 'td-cu' }, ['Coming up', h('span', { class: 'r' }, 'next 30 days')]),
      tdStrip(cu), h('p', { class: 'sum' }, cu.summary), out,
      cu.total > shown ? btn('k q more', (cu.total - shown) + ' more, to ' + cu.last, null, function () { TD.more.coming = true; renderBrief(); }) : null,
      cu.can_add ? null : h('p', { class: 'dim small', id: 'td-calw' }, '“Add to calendar” needs one more permission: Setup › Connections › Allow adding events.')]);
  }
  /* 0.21.0 Add to calendar (calwrite.py; approved 2026-11-11). Off (with the reason) until an account allows adding
     events; the press opens a small confirm card (title, date, time, calendar, Add); only Add writes, one event per
     item, ever. Afterwards the row says "On your calendar ✓" with Open (the event in Google Calendar). */
  function tdCalKey(r, cu, where) {
    if (r.added) return h('span', { class: 'addok' }, ['On your calendar ✓', r.added.link ? ' · ' : '', r.added.link ? tdLink(r.added.link, 'tbtn') : null]);
    if (!r.add) return null;
    if (!r.add.on) return h('button', { type: 'button', class: 'addc', disabled: true, 'aria-describedby': 'td-calw' }, 'Add to calendar?');
    var open = !!(TD.addc && TD.addc.item === r.item);
    return btn('addc on', 'Add to calendar', { 'aria-expanded': String(open), 'data-fk': 'addc:' + where + ':' + r.item }, function () {
      TD.addc = open ? null : { item: r.item, title: r.add.title, date: r.add.date, time: r.add.time, minutes: r.add.minutes, end: r.add.end || '',
        ask: !!r.add.ask_end, target: 0, err: '', busy: false };
      renderBrief();
      if (TD.addc) { var f = main.querySelector('#addc-title'); if (f) f.focus(); }
    });
  }
  function addMins(from, to) {   /* "HH:MM" to "HH:MM", minutes; an end at or before the start is the next day */
    var a = String(from).split(':'), b = String(to).split(':'), m = (+b[0] * 60 + +b[1]) - (+a[0] * 60 + +a[1]);
    return m > 0 ? m : m + 1440;
  }
  function tdAddCard(pre, targets) {
    var a = TD.addc;
    function keep() { a.title = t.value; a.date = d.value; a.time = tm.value; if (en) a.end = en.value; a.target = Number(sel.value) || 0; }
    var t = h('input', { class: 'inp', id: 'addc-title', type: 'text', maxlength: '200', value: a.title || '', autocomplete: 'off' });
    var d = h('input', { class: 'inp', id: 'addc-date', type: 'date', value: a.date || '' });
    var tm = h('input', { class: 'inp', id: 'addc-time', type: 'time', value: a.time || '' });
    /* 0.21.0: the message gave a start and no end (ask_end): the owner says when it ends */
    var en = a.ask ? h('input', { class: 'inp', id: 'addc-end', type: 'time', value: a.end || '' }) : null;
    var sel = h('select', { class: 'inp', id: 'addc-cal' }, targets.map(function (x, i) {
      return h('option', { value: String(i), selected: i === (a.target || 0) ? true : null }, x.name + ' · ' + x.email);
    }));
    [t, d, tm, en, sel].forEach(function (e) { if (e) { on(e, 'change', keep); on(e, 'input', keep); } });
    var go = btn('k go', 'Add', { 'aria-busy': a.busy ? 'true' : null, disabled: a.busy ? true : null, 'data-fk': 'addc-go' }, function () {
      keep();
      var x = targets[a.target] || targets[0];
      if (!x) return;
      var mins = a.minutes || 60;
      if (a.ask && a.time) {
        if (!a.end) { a.err = 'Say when it ends.'; renderBrief('#addc-end'); return; }
        mins = addMins(a.time, a.end);
      }
      a.busy = true; a.err = ''; renderBrief();
      mutate(function () {
        return api('POST', 'api/calendar/add', { item: a.item, title: a.title, date: a.date, time: a.time || '', minutes: mins, account_id: x.account_id, calendar: x.calendar });
      }).then(function (r) {
        TD.addc = null; showToast(r && r.already ? 'It was on your calendar already.' : 'On your calendar.', '');
        if (AG.v) agLoad();
        return tdRefresh();
      }, function (e) { a.busy = false; a.err = e.message; renderBrief(); });
    });
    return h('div', { class: 'addcard', role: 'group', 'aria-label': 'Add to calendar' }, [
      h('label', { class: 'label', for: 'addc-title' }, 'Title'), t,
      h('div', { class: 'addrow' + (en ? ' three' : '') }, [h('span', null, [h('label', { class: 'label', for: 'addc-date' }, 'Date'), d]),
        h('span', null, [h('label', { class: 'label', for: 'addc-time' }, 'Time (empty: all day)'), tm]),
        en ? h('span', null, [h('label', { class: 'label', for: 'addc-end' }, 'Ends'), en]) : null]),
      h('label', { class: 'label', for: 'addc-cal' }, 'Calendar'), sel,
      a.err ? h('p', { class: 'err', role: 'alert' }, a.err) : null,
      h('div', { class: 'keys' }, [go, btn('k q', 'Cancel', null, function () { TD.addc = null; renderBrief(); })]),
      h('p', { class: 'fine' }, 'Only you are on it: nobody is invited or emailed.')]);
  }
  /* 0.21.0 "From your channels, feeds and reports" (connectors.py, conn_reports.py): quiet, newest first, at most
     TD_FEEDS with "n more". Report text is someone else's words: text only, never a link; a feed links only
     through cnLink. */
  var TD_FEEDS = 5;
  function tdFeeds(v) {
    var f = v.feeds;
    if (!f || !Array.isArray(f.rows) || !f.rows.length) return null;
    var lim = TD.more.feeds ? f.rows.length : TD_FEEDS;
    var rows = f.rows.slice(0, lim).map(function (x) {
      var u = x.kind === 'feed' && !x.flagged ? cnLink(x.url) : null;
      var k = (Array.isArray(x.marks) ? x.marks : []).filter(function (m) { return Array.isArray(m) && m[1]; }).map(cnFlag);
      return h('div', { class: 'fdr' + (x.flagged ? ' flagged' : ''), role: 'listitem' }, [
        u ? extA('w', u, x.title, x.title + ' (opens ' + (x.noun === 'YouTube channel' ? 'YouTube' : 'the site') + ')') : h('p', { class: 'w' }, x.title),
        h('p', { class: 'fm' }, [tdIco(x.kind === 'report' ? 'note' : 'brief'), [x.source || x.noun, x.when].filter(Boolean).join(' · ')]),
        k.length ? h('p', { class: 'mkl' }, k) : null]);
    });
    var hidden = f.rows.length - lim;
    return h('section', { class: 'card quiet', 'aria-labelledby': 'td-fd' }, [
      h('h2', { class: 'lab', id: 'td-fd' }, ['From your channels, feeds and reports', h('span', { class: 'r' }, 'newest first')]),
      h('div', { class: 'fdl', role: 'list' }, rows),
      hidden > 0 ? btn('k q more', hidden + ' more', null, function () { TD.more.feeds = true; renderBrief(); }) : null,
      f.note && (TD.more.feeds || hidden <= 0) ? h('p', { class: 'dim small' }, f.note) : null]);
  }
  function tdClean(v) {
    var c = v.clean_up;
    return h('section', { class: 'card', 'aria-labelledby': 'td-cl' }, [h('h2', { class: 'lab', id: 'td-cl' }, 'Clean up'),
      c.total ? [h('div', { class: 'cu' }, [h('span', { class: 'big' }, String(c.total)), h('span', null, ['to clear', h('br'), h('span', { class: 'dim' }, c.since)])]),
        h('ul', { class: 'smp' }, c.samples.map(function (s) { return h('li', null, [tdIco(s.icon), h('span', null, s.what), h('span', { class: 'ag' }, s.day)]); })),
        h('div', { class: 'keys' }, [btn('k', 'Done with all ' + c.total, null, function () { tdDone(c.ids, c.total + ' to clear'); }),
          btn('k q', 'Note…', null, function () { tdNoteOpen('clean', c.ids, c.total + ' items'); }),
          btn('k q', 'See all', null, function () { tdGo('cleanup'); })]), tdNoteBox('clean')]
        : h('p', { class: 'dim' }, 'Nothing to clear.')]);
  }
  function tdCleanPage() {
    var c = TD.cu;
    if (!c) return h('div', { class: 'tdy' }, [tdBackKey(), h('p', { class: 'tdy-empty' }, TD.derr || 'Loading…')]);
    return h('div', { class: 'tdy' }, [tdBackKey(), tdUndoBar(), h('h1', { class: 'hl sm' }, 'Clean up'),
      c.groups.map(function (g) {
        var ids = g.rows.map(function (r) { return r.id; });
        return h('section', { class: 'card', 'aria-label': g.label }, [h('h2', { class: 'lab' }, [g.label, h('span', { class: 'r' }, String(g.rows.length))]),
          h('ul', { class: 'smp' }, g.rows.map(function (r) {
            return h('li', null, [tdIco(r.icon), h('span', null, r.what), h('span', { class: 'ag' }, r.day),
              btn('k q', 'Done', { 'aria-label': 'Done: ' + r.what }, function () { tdDone([r.id], r.what); }),
              btn('k q', 'Note…', { 'aria-label': 'Note on ' + r.what }, function () { tdNoteOpen('i' + r.id, [r.id], r.what); })]);
          })), tdNoteBox(g.rows.map(function (r) { return 'i' + r.id; }).filter(function (k) { return TD.note && TD.note.key === k; })[0] || 'g' + g.key),
          h('div', { class: 'keys' }, [btn('k', 'Done with these ' + ids.length, null, function () { tdDone(ids, g.label.toLowerCase()); }),
            btn('k q', 'Note…', null, function () { tdNoteOpen('g' + g.key, ids, g.label.toLowerCase()); })])]);
      }),
      c.total ? null : h('p', { class: 'dim' }, 'Nothing to clear.'),
      c.notes && c.notes.length ? h('section', { class: 'card', 'aria-labelledby': 'td-notes' }, [h('h2', { class: 'lab', id: 'td-notes' }, 'Your notes to ' + aname()),
        h('ul', { class: 'smp' }, c.notes.map(function (n) {
          return h('li', null, [ico('note'), h('span', null, [h('b', null, n.about), ' · ' + n.said]), h('span', { class: 'ag' }, n.at)]);
        }))]) : null]);
  }

  /* ----- the item and clash view ----- */
  function tdStand(list) {
    return list.map(function (s) {
      return h('div', { class: 'ws' }, [h('span', { class: 'wsb' + (s.mark ? ' ' + s.mark : '') }, (s.mark === 'ok' ? '✓ ' : s.mark === 'wn' ? '▲ ' : '') + s.badge),
        h('p', null, segs(s.text)), h('span', { class: 'ag' }, s.age)]);
    });
  }
  function tdThread(t) {
    var all = !!TD.more['t' + t.item], msgs = all ? t.messages : t.messages.slice(t.earlier), day = null, out = [];
    out.push(h('div', { class: 'thh' }, [tdIco(t.icon), h('b', null, t.who), h('span', { class: 'dim' }, t.chip + (t.subject ? ' · ' + t.subject : ''))]));
    if (t.earlier && !all) out.push(btn('k q more', t.earlier + ' earlier', null, function () { TD.more['t' + t.item] = true; renderBrief(); }));
    msgs.forEach(function (m) {
      if (m.day !== day) { day = m.day; out.push(h('p', { class: 'daysep' }, m.day)); }
      var txt = m.hl ? [m.text.slice(0, m.hl[0]), h('u', null, m.text.slice(m.hl[0], m.hl[1])), m.text.slice(m.hl[1])] : m.text;
      out.push(h('div', { class: 'bm' + (m.me ? ' me' : '') + (m.mark ? ' ' + m.mark : '') }, [
        m.mark ? h('span', { class: 'tag' }, m.mark === 'ask' ? 'The ask' : 'Your yes') : null,
        h('p', null, txt), h('span', { class: 'meta' }, m.who + ' · ' + m.time)]));
    });
    if (t.end) out.push(h('p', { class: 'thend' }, '▲ ' + t.end));
    return h('div', { class: 'thread', role: 'group', 'aria-label': 'Conversation with ' + t.who }, out);
  }
  function tdChat(d, q) {
    clearTimeout(CH.timer); CH.timer = null;
    CH.conv = null; CH.cid = null; save(KEY_CHAT, ''); CH.input = ''; CH.err = ''; CH.earlier = false; CH.scroll = true;
    CH.scope = { name: d.title, ledger: d.kind + ':' + d.id, item_id: d.kind === 'item' ? d.id : undefined, clash_id: d.kind === 'clash' ? d.id : undefined };
    setTab('chat');
    if (q) chatAsk(q);
  }
  /* ----- 0.21.0 Draft a reply (replydraft.py): an on-demand suggested reply to one open ask, written by the local model
     on Chat's path, kept on the item. The text is shown in an editable box (textContent/value only) with Copy; Gmail
     asks get Put in Gmail Drafts (owner-pressed; reply-all only when ticked), other apps Copy and the app's own link.
     Nothing is sent from here except an RSVP you press. States: none · writing (polled) · ready · held · failed. */
  var DR = { ed: {}, gd: {}, all: {}, busy: {}, err: {}, timer: null };
  function tdDraftOf(d) { return d && d.kind === 'item' && d.draft && d.draft.can !== undefined ? d.draft : null; }
  function tdDraftText(d) { return DR.ed[d.id] != null ? DR.ed[d.id] : (d.draft && d.draft.text) || ''; }
  function tdDraftSet(id, res) {
    if (TD.d && TD.d.kind === 'item' && TD.d.id === id) TD.d = res;
    if (TD.rd && TD.rd.id === id) TD.rd = res;
  }
  function tdDraftPoll(id) {
    clearTimeout(DR.timer);
    DR.timer = setTimeout(function () {
      DR.timer = null;
      if ((!TD.d || TD.d.id !== id) && (!TD.rd || TD.rd.id !== id)) return;
      api('GET', 'api/today/items/' + id).then(function (res) {
        tdDraftSet(id, res);
        if (res.draft && res.draft.state === 'writing') tdDraftPoll(id);
        else renderBrief(res.draft && res.draft.state === 'ready' ? '#td-dr-' + id : '#td-drh-' + id);
      }, function () { tdDraftPoll(id); });
    }, 1500);
  }
  function tdDraftStart(d) {
    if (DR.busy[d.id]) return;
    DR.busy[d.id] = true; DR.err[d.id] = ''; delete DR.ed[d.id]; delete DR.gd[d.id];
    if (d.draft) d.draft = Object.assign({}, d.draft, { state: 'writing' });
    renderBrief('#td-drh-' + d.id);
    api('POST', 'api/today/items/' + d.id + '/draft', {}).then(function (r) {
      DR.busy[d.id] = false; d.draft = r.draft;
      if (r.draft.state === 'writing') tdDraftPoll(d.id);
      renderBrief('#td-drh-' + d.id);
    }, function (e) {
      DR.busy[d.id] = false; DR.err[d.id] = e.message;
      if (d.draft) d.draft = Object.assign({}, d.draft, { state: 'none' });
      renderBrief('#td-drh-' + d.id);
    });
  }
  /* the one orange key: Put in Gmail Drafts while a Gmail draft is shown, else the Reply in … link */
  function tdGoCls(d) { var x = tdDraftOf(d); return x && x.state === 'ready' && x.kind === 'gmail' ? 'k' : 'k go'; }
  function tdDraftKey(d) {
    var x = tdDraftOf(d);
    if (!x || !x.can || x.state !== 'none') return null;
    return btn('k', 'Draft a reply', { 'data-draft-start': d.id }, function () { tdDraftStart(d); });
  }
  function tdCopyDraft(ta, b) {
    function said(t) { b.textContent = t; setTimeout(function () { b.textContent = 'Copy'; }, 2000); }
    function sel() {
      var ok = false;
      try { ta.focus(); ta.select(); ok = document.execCommand && document.execCommand('copy'); } catch (e) { ok = false; }
      said(ok ? 'Copied' : 'Selected: press Ctrl+C');
    }
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(ta.value).then(function () { said('Copied'); }, sel);
    else sel();
  }
  function tdDraftBox(d) {
    var x = tdDraftOf(d);
    if (!x || (x.state === 'none' && !DR.err[d.id])) return null;
    var id = d.id, hid = 'td-drh-' + id, kids = [h('h2', { class: 'lab', id: hid, tabindex: '-1' }, 'Suggested reply')];
    var again = x.can ? btn('k q', x.state === 'failed' ? 'Try again' : 'Write again', { 'data-draft-again': id }, function () { tdDraftStart(d); }) : null;
    if (DR.err[id] && x.state === 'none') kids.push(h('p', { class: 'err', role: 'alert' }, DR.err[id]));
    else if (x.state === 'writing') kids.push(h('p', { class: 'drw', role: 'status', 'aria-live': 'polite' }, [h('span', { class: 'drdot', 'aria-hidden': 'true' }), 'Writing a reply…']));
    else if (x.state === 'failed') kids.push(h('p', { class: 'err', role: 'alert' }, x.why || 'The reply couldn’t be written.'), h('div', { class: 'keys' }, again));
    else if (x.state === 'held') kids.push(h('p', { class: 'pflag sughide', role: 'status' }, x.why), h('div', { class: 'keys' }, again));
    else if (x.state === 'ready') {
      var ta = h('textarea', { class: 'inp draft', id: 'td-dr-' + id, 'aria-labelledby': hid, rows: phoneMq.matches ? '6' : '5', spellcheck: 'true' });
      ta.value = tdDraftText(d);
      var g = DR.gd[id] || (DR.gd[id] = {});
      var saved = function () { return !!(g.url && g.text === ta.value && g.all === !!DR.all[id]); };
      var ok = h('p', { class: 'gdok', role: 'status', hidden: saved() ? null : true }, g.url ? gdOk(g.url) : null);
      var gerr = h('p', { class: 'err', role: 'alert', hidden: g.err ? null : true }, g.err || '');
      var cp = btn('k', 'Copy', { 'data-draft-copy': id }, function () { tdCopyDraft(ta, cp); });
      var acts = [cp], gb = null;
      if (x.kind === 'gmail') {
        gb = btn('k go', g.busy ? 'Saving…' : 'Put in Gmail Drafts', { 'data-draft-gd': id, hidden: saved() ? true : null, 'aria-busy': g.busy ? 'true' : null, disabled: g.busy ? true : null }, function () {
          var t = ta.value, all = !!DR.all[id];
          g.busy = true; g.err = ''; gb.disabled = true; gb.textContent = 'Saving…';
          api('POST', 'api/today/items/' + id + '/gmail', { text: t, reply_all: all }).then(function (res) { g.url = res.open_url; g.text = t; g.all = all; g.to = res.to; g.cc = res.cc; }, function (e) { g.err = e.message; })
            .then(function () { g.busy = false; renderBrief(g.err ? '[data-draft-gd="' + id + '"]' : '#td-drh-' + id); });
        });
        acts.push(gb);
        if (x.reply_all) {
          var cb = h('input', { type: 'checkbox', id: 'td-dra-' + id, checked: DR.all[id] ? true : null });
          on(cb, 'change', function () { DR.all[id] = cb.checked; gb.hidden = saved(); ok.hidden = !saved(); });
          acts.push(h('label', { class: 'drall', for: 'td-dra-' + id }, [cb, 'Reply all']));
        }
      } else if (d.reply && tdHref(d.reply)) acts.push(tdLink(d.reply, 'k', 'Open in ' + (x.app || d.reply.app)));
      on(ta, 'input', function () { DR.ed[id] = ta.value; if (gb) { gb.hidden = saved(); ok.hidden = !saved(); } });
      kids.push(x.warning ? h('p', { class: 'err sugwarn' }, x.warning) : null,
        x.claim ? h('p', { class: 'pflag claim' }, CLAIM_NOTE) : null, ta,
        h('div', { class: 'dbtns' }, acts), ok, gerr,
        h('div', { class: 'drfoot' }, [h('span', { class: 'hint' }, 'Edit it here. Nothing is sent from here except an RSVP you press.'), again]),
        g.url && saved() && (g.to || g.cc) ? h('p', { class: 'dim' }, 'To ' + g.to + (g.cc ? ' · Cc ' + g.cc : '')) : null);
    }
    return h('section', { class: 'card tdy-draft', 'aria-labelledby': hid, 'aria-busy': x.state === 'writing' ? 'true' : null, 'data-draft': id }, kids);
  }
  function tdChatCard(d) {
    var inp = h('input', { class: 'inp', id: 'td-ask', type: 'text', 'aria-label': 'Ask about this', placeholder: 'Ask about this…' });
    return h('section', { class: 'card', 'aria-labelledby': 'td-chat' }, [h('h2', { class: 'lab', id: 'td-chat' }, 'Chat about this'),
      h('div', { class: 'sugs' }, (d.chat || []).map(function (q) {
        /* 0.21.0: "Draft a reply to <name>" drafts the reply here (or opens that ask and drafts it), as Draft a reply does */
        var ds = d.draft_sug;
        if (ds && d.kind === 'item' && q === ds) return btn('sug', q, null, function () { tdDraftStart(d); });
        if (ds && d.kind === 'clash' && typeof ds === 'object' && q === ds.q) return btn('sug', q, null, function () { TD.autodraft = ds.item; tdGo('item', ds.item); });
        return btn('sug', q, null, function () { tdChat(d, q); });
      })),
      h('div', { class: 'composer' }, [inp, btn('k', [ico('send'), 'Ask'], null, function () { tdChat(d, inp.value.trim()); })])]);
  }
  function tdKeys(d, phone) {
    var keys = [];
    if (d.reply) keys.push(tdLink(d.reply, tdGoCls(d)));
    if (d.kind === 'item' && d.open) {
      keys.push(tdDraftKey(d));  /* 0.21.0 Draft a reply (asks only) */
      keys.push(btn('k', 'Done', null, function () { tdDone([d.id], d.title); }));
      keys.push(btn('k', 'Note…', null, function () { tdNoteOpen('d' + d.id, [d.id], d.title); }));
    }
    if (d.kind === 'item' && !d.open) keys.push(btn('k', 'Put back on my list', null, function () { tdPost('undone', { items: [d.id] }, 'Back on your list.'); }));
    if (d.open) keys.push(btn('k q', d.remind_at ? 'Reminder ' + d.remind_at : 'Remind me', { 'aria-expanded': String(TD.rem === 'd') }, function () { TD.rem = TD.rem === 'd' ? null : 'd'; renderBrief(); }));
    if (d.kind === 'clash' && d.open) keys.push(btn('k q', 'Dismiss', null, function () { tdDismiss(d.id).then(function () { tdGo('today'); }); }));
    var target = d.followup ? ['followup', d.followup.id] : [d.kind === 'clash' ? 'clash' : 'item', d.id];
    return [h('div', { class: 'keys' }, keys), tdRemind('d', target, d.followup ? d.followup.who : ''), tdNoteBox('d' + d.id)];
  }
  function tdItemView() {
    var d = TD.d, phone = isPhone();
    if (!d) return h('div', { class: 'tdy' }, [tdBackKey(), h('p', { class: 'tdy-empty' }, TD.derr || 'Loading…')]);
    var threads = d.threads || [];
    var th = null;
    if (threads.length > 1 && phone) {
      var t = Math.min(TD.tab, threads.length - 1);
      th = [h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Conversations' }, threads.map(function (x, i) {
        return h('button', { type: 'button', role: 'tab', 'aria-selected': String(i === t), class: 'tab' }, [tdIco(x.icon), x.first]);
      })), tdThread(threads[t])];
      th[0].querySelectorAll('button').forEach(function (b, i) { on(b, 'click', function () { TD.tab = i; renderBrief(); }); });
    } else th = h('div', { class: threads.length > 1 ? 'g2' : '' }, threads.map(tdThread));
    return h('div', { class: 'tdy tdy-item' }, [tdBackKey(), tdUndoBar(),
      h('p', { class: 'dline' }, d.kicker.filter(Boolean).join(' · ')),
      h('div', { class: 'tdy-ttl' }, [h('h1', { class: 'hl sm' }, d.title), d.until ? h('p', { class: 'until' + (d.level === 'red' ? ' red' : '') }, d.until) : null]),
      d.likely_from ? h('p', { class: 'dim' }, d.likely_from) : null,
      h('div', { class: 'g75' }, [h('section', { class: 'card', 'aria-labelledby': 'td-ws' }, [h('h2', { class: 'lab', id: 'td-ws' }, 'Where you stand'), tdStand(d.stand),
        d.when ? h('div', { class: 'when' }, [h('p', { class: 'lab' }, 'When'), h('p', { class: 'wlab' }, d.when.label), h('p', { class: 'dim' }, d.when.sub),
          tdCalKey({ item: d.id, add: d.add, added: d.added }, null, 'item')]) : null,
        d.rsvp ? h('div', { class: 'when' }, [h('p', { class: 'lab' }, 'Answer the invite'), rsvpBox(d.rsvp, d.id, 'item')]) : null,  /* 0.21.0 */
        d.add && d.add.on && TD.addc && TD.addc.item === d.id ? tdAddCard(d.add, d.cal_targets || []) : null,
        tdKeys(d, phone)]), tdChatCard(d)]),
      tdDraftBox(d),  /* 0.21.0 */
      threads.length ? h('section', { class: 'card', 'aria-label': threads.length > 1 ? 'Both conversations' : 'The conversation' }, [
        h('h2', { class: 'lab' }, threads.length > 1 ? 'Both conversations' : 'The conversation'), th]) : null]);
  }

  /* ----- Replies: the same asks, with more context ----- */
  function tdReplies(quiet) {
    return api('GET', 'api/today/replies').then(function (r) {
      TD.rp = r;
      if (TD.rsel == null && r.rows.length && !isPhone()) TD.rsel = r.rows[0].id;
      return tdReplyDetail(quiet);
    }, function (e) { TD.rp = { rows: [], total: 0, err: e.message }; });
  }
  function tdReplyDetail() {
    if (TD.rsel == null) { TD.rd = null; return Promise.resolve(); }
    return api('GET', 'api/today/items/' + TD.rsel).then(function (d) { TD.rd = d; }, function () { TD.rd = null; });
  }
  function T_replies_new() {
    var r = TD.rp, phone = isPhone();
    if (!r) { tdReplies().then(function () { if (U.tab === 'replies') renderBrief(); }); return h('div', { class: 'tdy' }, h('p', { class: 'tdy-empty' }, 'Loading…')); }
    var list = h('section', { class: 'card rpl', 'aria-labelledby': 'td-rp' }, [h('h2', { class: 'lab', id: 'td-rp' }, ['Waiting on you', h('span', { class: 'r' }, r.total + ' · oldest first')]),
      r.err ? h('p', { class: 'dim' }, r.err) : r.total ? r.rows.map(function (x) {
        var b = btn('rpr' + (x.id === TD.rsel ? ' sel' : ''), [tdIco(x.icon, x.source), h('span', { class: 'who' }, [x.who, x.mine ? h('span', { class: 'mine' }, 'You said') : null]),
          h('span', { class: 'ag' }, x.age), h('span', { class: 'why' }, areaLead(x.area).concat([x.why])), h('span', { class: 'bar', 'aria-hidden': 'true' })],
          { 'aria-current': x.id === TD.rsel ? 'true' : null }, function () { TD.rsel = x.id; TD.rd = null; renderBrief(); tdReplyDetail().then(function () { renderBrief(); window.scrollTo(0, 0); }); });
        b.querySelector('.bar').style.setProperty('--w', String(x.bar));
        return b;
      }) : h('p', { class: 'dim' }, 'Nobody is waiting on you.')]);
    var d = TD.rd, det = null;
    if (TD.rsel != null) {
      det = !d ? h('section', { class: 'card' }, h('p', { class: 'dim' }, 'Loading…')) : h('section', { class: 'card rpd', 'aria-labelledby': 'td-rd' }, [
        phone ? btn('k q tdy-back', [ico('back'), 'All replies'], null, function () { TD.rsel = null; TD.rd = null; renderBrief(); }) : null,
        h('p', { class: 'dline' }, d.kicker.filter(Boolean).join(' · ')), h('h2', { class: 'hl xs', id: 'td-rd' }, d.title),
        tdStand(d.stand),
        (d.context || []).map(function (c) {
          if (c.kind === 'days') return h('div', { class: 'ctx' }, [h('p', { class: 'lab' }, 'Your calendar · ' + c.label), h('div', { class: 'days' }, c.days.map(function (x) {
            return h('div', { class: 'day' + (x.busy === 'Free' ? '' : ' busy') }, [h('span', null, x.wd), h('b', null, x.d), h('span', null, x.busy)]);
          }))]);
          return h('div', { class: 'ctx' + (c.kind === 'heads' ? ' heads' : '') }, [h('p', { class: 'lab' }, (c.kind === 'heads' ? '▲ ' : '') + c.label), h('p', null, h('b', null, c.title)), h('p', { class: 'dim' }, c.sub)]);
        }),
        (d.threads || []).slice(0, 1).map(tdThread),
        h('div', { class: 'keys' }, [d.reply ? tdLink(d.reply, tdGoCls(d), 'Reply in ' + d.reply.app) : null,
          tdDraftKey(d),  /* 0.21.0 Draft a reply (it replaced Draft with Chat) */
          btn('k', 'Done', null, function () { tdDone([d.id], d.title).then(function () { TD.rsel = null; TD.rd = null; tdReplies().then(function () { renderBrief(); }); }); }),
          btn('k q', 'Note…', null, function () { tdNoteOpen('r' + d.id, [d.id], d.title); })]), tdNoteBox('r' + d.id),
        tdDraftBox(d)]);
    }
    if (phone) return h('div', { class: 'tdy' }, det || list);
    return h('div', { class: 'tdy rp2' }, [list, det]);
  }
  var TFN = { briefing: T_today, replies: T_replies_new, agenda: T_agenda, chat: T_chat, projects: T_projects };  /* 0.6.0 chat, 0.7.0 projects; 0.18.0 Today and Replies from the ledger (the old ones under Old brief) */

  /* ----- detail panel ----- */
  /* 0.21.0 Put in Gmail Drafts (approved 2026-11-12): the suggested reply as a draft in the owner's Gmail, only when
     the owner presses it (POST api/gdrafts/reply {row_id, text}; the server finds the account and thread). One draft per text: the
     same text again gives the draft already made; edited text shows the button again. Nothing is sent from here except an RSVP you press. Data is text. */
  var GD = { rows: {}, v: null, loading: false, err: '', grant: null, tests: {} };
  function gdOk(url) {
    var u = safeHttps(url);
    return ['In your Gmail Drafts ✓', u ? [' · ', h('a', { href: u, target: '_blank', rel: 'noopener noreferrer' }, 'Open in Gmail')] : null];
  }
  /* 0.8.2: a likely match for this sender: the Same person? card in the panel; the answer shows in its place */
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
    var r = eventById(U.open.id);
    if (!r) { U.open = null; return null; }
    var phone = isPhone(), isEv = U.open.kind === 'event';
    var closeB = h('button', { type: 'button', class: 'xbtn', 'data-close': '1' }, phone ? [ico('back'), 'Back'] : [ico('close'), 'Close']);
    var attrs = { class: 'panel', 'aria-labelledby': 'bv-ptitle' };
    if (phone) { attrs.role = 'dialog'; attrs['aria-modal'] = 'true'; }
    return h('aside', attrs, h('div', { class: 'panel-in' }, [
      h('div', { class: 'ptop' }, [phone ? closeB : h('span', { class: 'label lbl' }, isEv ? 'Event' : 'Message'), h('span', { class: 'sp' }),
        phone ? null : closeB]),
      h('div', { class: 'pbody' }, panelEvent(r))]));
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
    items.push(h('button', { type: 'button', 'data-dismiss': '1' }, ['Dismiss', h('span', { class: 's' }, 'On this device, until tomorrow')]));
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
  function showToast(b, d, undo, err, ms) {  /* 0.8.1: ms, how long it stays (a chat note's Undo: 30 s) */
    U.toast = { b: b, d: d, undo: undo || null, err: !!err, id: Date.now() };
    var id = U.toast.id;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { if (U.toast && U.toast.id === id) { U.toast = null; renderLayer(); } }, ms || (undo ? 8000 : 6000));
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
  function noteRef(kind, id) {
    var x;
    if (kind === 'event') { x = eventById(id); return x && x.key ? x.key : null; }
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
      P.warn ? h('p', { class: 'pwarn', role: 'alert' }, P.warn) : null,  /* 0.8.2: looks pasted from a message */
      h('div', { class: 'btns' }, [h('button', { type: 'button', class: 'act', 'data-nsave': '1', disabled: !!P.busy }, 'Save'),
        P.warn ? h('button', { type: 'button', class: 'tbtn', 'data-nsaveany': '1', disabled: !!P.busy }, 'Save anyway') : null,
        h('button', { type: 'button', class: 'tbtn', 'data-ncancel': '1' }, 'Cancel'),
        nt ? h('button', { type: 'button', class: 'tbtn', 'data-ndel': '1', disabled: !!P.busy }, 'Delete note') : null]),
      h('div', { class: 'hint' }, 'Callsheet reads it before each brief when it fits the day. All notes are in Setup → Delivery → Memory.')]));
  }
  function focusLayer(sel) { var f = layer.querySelector(sel); if (f) f.focus(); }
  function noteAction(d, P) {
    if (d.pnote) { P.mode = 'note'; P.text = null; P.err = null; renderLayer(); focusLayer('[data-ntext]'); return; }
    if (d.ncancel) { P.mode = null; P.err = null; P.warn = null; renderLayer(); focusLayer('[data-pnote]'); return; }
    if (d.nsaveany) d = { nsave: '1', confirm: true };  /* 0.8.2: the owner saves past the paste warning */
    var nt = noteOf(P.kind, P.id), ref = noteRef(P.kind, P.id), anc = P.anc, req;
    if (!ref || P.busy) return;
    if (d.nsave) {
      var txt = String(P.text != null ? P.text : (nt ? nt.text : '')).trim();
      if (!txt) { P.err = 'Write the note first.'; renderLayer(); focusLayer('[data-ntext]'); return; }
      var body = { kind: P.kind, id: P.id, ref: ref, text: txt };
      if (d.confirm) body.confirm = true;
      req = nt ? function () { return api('PATCH', 'api/memory/notes/' + encodeURIComponent(nt.id), d.confirm ? { text: txt, confirm: true } : { text: txt }); }
        : function () { return api('POST', 'api/memory/notes', body); };
    } else if (nt) req = function () { return api('DELETE', 'api/memory/notes/' + encodeURIComponent(nt.id)); };
    else return;
    P.busy = true; P.err = null; P.warn = null; renderLayer();
    mutate(req).then(function (res) {
      if (res && res.saved === false && res.warning) {  /* 0.8.2: a warning, not a refusal */
        if (U.pop !== P) return;
        P.busy = false; P.warn = String(res.warning); renderLayer(); focusLayer('[data-nsaveany]'); return;
      }
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

  /* ----- the whole view ----- */
  function render(d) { setBrief(d); if (CH && CH.briefNote && !B.writing) CH.briefNote = false; renderBrief(); pollWriting(); }
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
    (B.schedule || []).forEach(function (e, i) { if (!e.id) e.id = 'xs' + (i + 1); });
    if (U.open && U.open.kind !== 'item' && !eventById(U.open.id)) U.open = null;
  }
  function renderBrief(focusSel) {
    if (ui.view !== 'brief' || !B) return;
    var phone = isPhone();
    var panelScroll = main.querySelector('.panel-in'), ps = panelScroll ? panelScroll.scrollTop : 0, sy = window.scrollY;
    document.title = 'Daily Brief · ' + (B.date_short || '');
    document.getElementById('sample-tag').hidden = !B.sample;

    var banner = S && !S.setup_done && noAccounts() ? h('div', { class: 'banner', role: 'region', 'aria-label': 'Setup' }, [
      h('p', null, 'This is sample data. Set up Callsheet to see your own day.'),
      btn('btn primary', 'Start setup', null, function () { wizGo(0); })]) : null;
    var nOpen = TD.v ? (TD.rp && TD.rp.rows ? TD.rp.total : TD.v.needs_you.total) : 0;  /* 0.18.0: the ledger's asks */
    if (U.tab === 'chat' && chatOff()) U.tab = 'briefing';  /* 0.9.0 */
    var tabs = h('nav', { class: 'tabbar', role: 'tablist', 'aria-label': 'Sections' }, TABS.filter(function (t) { return liveTabs().indexOf(t[0]) >= 0 || t[0] === 'tasks'; }).map(function (t) {
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
  function dismiss(P) {
    var key = 'ev:' + ((eventById(P.id) || {}).key || P.id), what = (eventById(P.id) || {}).title;
    setDismissed(key, true);
    if (U.open && U.open.kind === 'event' && P.kind === 'event' && U.open.id === P.id) U.open = null;
    U.pop = null;
    showToast('Dismissed.', (what || '') + ' · until tomorrow', { kind: 'dismiss', key: key });
    renderBrief('[data-undo]');
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
    if (liveTabs().indexOf(t) < 0) return;
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
    else if (d.open) { U.kcur = null; openItem(d.open); }
    else if (d.close) closePanel();
    else if (d.np) {
      var k = d.np.indexOf(':');
      U.pop = P && P.anc === d.anc ? null : { kind: d.np.slice(0, k), id: d.np.slice(k + 1), anc: d.anc, sub: false };
      renderLayer();
      var fb = U.pop ? layer.querySelector('[data-pop] button') : null;
      if (fb) fb.focus(); else t.setAttribute('aria-expanded', 'false');
      main.querySelectorAll('[data-np]').forEach(function (b) { b.setAttribute('aria-expanded', String(!!(U.pop && b.dataset.anc === U.pop.anc))); });
    }
    else if ((d.pnote || d.ncancel || d.nsave || d.nsaveany || d.ndel) && P) noteAction(d, P);
    else if (d.dismiss && P) dismiss(P);
    else if (d.undo) {
      var u = U.toast && U.toast.undo;
      if (!u) return;
      if (u.kind === 'dismiss') { setDismissed(u.key, false); U.toast = null; renderBrief(); }
      else if (u.kind === 'memory') chatUndoNote(u);  /* 0.8.1: a note saved from a chat Save card */
    }
    else if (d.toastx) { U.toast = null; renderLayer(); }
  });
  document.addEventListener('input', function (e) {
    var t = e.target;
    if (ui.view === 'brief' && t.dataset && t.dataset.draft) {
      U.drafts[t.dataset.draft] = t.value;
      var gs = GD.rows[t.dataset.draft];   /* 0.21.0: edited after Put in Gmail Drafts: the button again (a new draft) */
      if (gs && gs.url) {
        var same = gs.text === t.value;
        document.querySelectorAll('[data-gd="' + cssq(t.dataset.draft) + '"]').forEach(function (x) { x.hidden = same; });
        document.querySelectorAll('[data-gdok="' + cssq(t.dataset.draft) + '"]').forEach(function (x) { x.hidden = !same; });
      }
    }
    if (ui.view === 'brief' && t.dataset && t.dataset.ntext && U.pop) U.pop.text = t.value;
  });
  /* never steal keys while typing (inputs, text areas, selects, editable text) */
  function typing(t) {
    if (!t || !t.tagName) return false;
    return t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || !!t.isContentEditable;
  }
  document.addEventListener('keydown', function (e) {
    if (ui.view !== 'brief' || !B || FA.open) return;
    if (e.key === 'Escape') {
      if (U.pop) { var anc = U.pop.anc; U.pop = null; renderLayer(); var a = main.querySelector('[data-anc="' + cssq(anc) + '"]'); if (a) { a.setAttribute('aria-expanded', 'false'); a.focus(); } e.preventDefault(); }
      else if (U.open && lookPanel.hidden) { closePanel(); e.preventDefault(); }
      return;
    }
    var t = e.target;
    if (t.dataset && t.dataset.tab && (e.key === 'ArrowRight' || e.key === 'ArrowLeft' || e.key === 'Home' || e.key === 'End')) {
      var lt = liveTabs(), i = lt.indexOf(t.dataset.tab), n = lt.length;
      i = e.key === 'Home' ? 0 : e.key === 'End' ? n - 1 : (i + (e.key === 'ArrowRight' ? 1 : -1) + n) % n;
      setTab(lt[i], true); e.preventDefault();
      return;
    }
  });

  /* ---------- state ---------- */
  var ui = {
    view: null, place: 'home', wiz: null,  /* 0.9.0: the Setup place; the first-run step (0..2) or null */
    adv: {},             /* Advanced section id -> open (all closed by default) */
    acctOpen: {},        /* account id -> its switches shown (one line otherwise) */
    areaSel: null, tzOpen: false,
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
    focusNext: null      /* focus target for the next Setup render (the brief's "Manage" link) */
  };
  function applyServerLook() {
    var st = S && S.settings;
    if (!st) return;
    if (lookPending) { st.look = PREFS.look; st.mode = PREFS.mode; return; }
    if (MODES.indexOf(st.mode) >= 0 && st.mode !== PREFS.mode) setMode(st.mode, true);
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
      S.google_client = S.google_client || {};
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
    var td = tdLoad();   /* 0.18.0: Today from the ledger (the brief still draws when it fails) */
    TD.rp = null;
    return Promise.all([br, st, td]).then(function (r) { if (seq === briefSeq && ui.view === 'brief') render(r[0]); },
      function (e) { if (seq === briefSeq && ui.view === 'brief') status('Could not load the brief (' + e.message + ')'); });
  }
  function showSetup(focusSel) {
    var p = S ? Promise.resolve(S) : loadState();
    return p.then(function () { if (ui.view === 'setup') renderSetup(focusSel || '#wiz-title'); },
      function (e) { if (ui.view === 'setup') status('Could not load the setup (' + e.message + ')'); });
  }
  function route() {
    var sp = placeFromHash(location.hash);  /* 0.9.0: #setup, #setup/<place>, old #setup/<step> links */
    var v = sp ? 'setup' : location.hash === '#knowme' ? 'knowme' : location.hash === '#profile' ? 'profile' : location.hash === '#profile-questions' ? 'profileq' : location.hash === '#people' ? 'people' : 'brief';  /* 0.6.0 index: #knowme; 0.6.0 profile: #profile, #profile-questions */
    var changed = v !== ui.view;
    ui.view = v;
    setupBtn.hidden = v !== 'brief'; backBtn.hidden = v === 'brief';
    if (v !== 'brief') {   /* leaving the brief: no menu, toast, sheet lock or inert header */
      clearLayer(); document.documentElement.classList.remove('bv-lock'); document.querySelector('header.top').removeAttribute('inert');
    }
    if (v !== 'knowme') stopKnowPoll();  /* 0.6.0 index */
    if (v !== 'profile' && v !== 'profileq') stopProfilePoll();  /* 0.6.0 profile */
    var ht = v === 'brief' ? tabFromHash(location.hash) : null;
    if (v === 'brief' && location.hash === '#brief' && TD.sub !== 'talk') { ht = 'briefing'; TD.sub = 'talk'; tkLoad(true); }  /* 0.21.0: the push "Your brief is ready" */  /* 0.6.0 chat: #replies, #chat, … (and #areas) */
    if (ht && ht !== U.tab) { U.tab = ht; save(KEY_TAB, ht); if (ht !== 'replies') U.area = null; }
    if (v === 'setup') ui.place = sp;
    if (v === 'setup' && !changed && S && main.querySelector('.sheet')) { var fs0 = ui.focusNext; ui.focusNext = null; ui.confirm = null; renderSetup(fs0 || '#wiz-title'); if (!fs0) window.scrollTo(0, 0); return; }
    if (!changed && main.querySelector('.sheet, .bv')) { if (ht && B) renderBrief(); return; }
    window.scrollTo(0, 0);
    if (v === 'setup') {
      var fs = ui.focusNext; ui.focusNext = null; ui.confirm = null;
      loadState().then(function () { return showSetup(fs); }, function () { return showSetup(fs); });
    }
    else if (v === 'knowme') showKnow('#kn-title');  /* 0.6.0 index */
    else if (v === 'profile' || v === 'profileq') showProfile(v);  /* 0.6.0 profile */
    else if (v === 'people') { PP.said = ''; showPeople('#pp-title'); }  /* 0.8.2 people */
    else showBrief();
  }
  window.addEventListener('hashchange', route);

  /* ---------- render: setup ---------- */
  function go(p) { ui.confirm = null; if (p === 'home') ui.wiz = null; openSetup(p); window.scrollTo(0, 0); }
  function reload(focusKey) {
    return loadState().then(function () { renderSetup(focusKey ? '[data-fk="' + cssq(focusKey) + '"]' : null); },
      function (e) { var w = document.getElementById('wiz-err'); if (w) w.textContent = 'Could not reload (' + e.message + ')'; });
  }
  /* 0.9.0: the rail is Home plus the four places (a row that scrolls sideways on phones); the first run shows its
     three steps instead, with Back and Next */
  function renderSetup(focusSel) {
    /* 0.17.0: a re-render without a focus target (a loader finishing) keeps the focus on the same control, by data-fk */
    var ae0 = document.activeElement, keepFk = !focusSel && ae0 && ae0 !== document.body && main.contains(ae0) && ae0.getAttribute('data-fk');
    var p = PLACE_FN[ui.place] ? ui.place : 'home', home = p === 'home', wz = ui.wiz;
    if (wz != null && !(wz >= 0 && wz <= 2)) wz = ui.wiz = null;
    var rail = wz != null ? h('nav', { class: 'rail', 'aria-label': 'First-run steps' }, WIZ.map(function (x, i) {
      return btn(i < wz ? 'done' : i === wz ? 'on' : '', [h('span', { class: 'n' }, String(i + 1)), x[1]], { 'aria-current': i === wz ? 'step' : null }, function () { wizGo(i); });
    })) : h('nav', { class: 'rail', 'aria-label': 'Setup' }, [btn('home' + (home ? ' on' : ''), 'Home', { 'aria-current': home ? 'page' : null }, function () { go('home'); })]
      .concat(PLACES.map(function (x) {
        return btn(p === x[0] ? 'on' : '', x[1], { 'aria-current': p === x[0] ? 'page' : null, 'data-place': x[0] }, function () { go(x[0]); });
      })));
    var body = wz === 2 ? wizKnow() : home ? setupHome() : PLACE_FN[p]();
    var title = wz != null ? WIZ[wz][1] : home ? 'Your setup' : placeName(p);
    var wnav = wz == null ? null : h('div', { class: 'wnav' }, [
      btn('btn', wz ? 'Back: ' + WIZ[wz - 1][1] : 'Back: Setup', null, function () { if (wz) wizGo(wz - 1); else go('home'); }),
      wz < 2 ? btn('btn primary', 'Next: ' + WIZ[wz + 1][1], { 'data-fk': 'wiz-next' }, function () { wizGo(wz + 1); }) : null]);
    var sheet = h('div', { class: 'sheet wiz' }, [
      h('div', { class: 'mast two' }, [
        h('div', { class: 't' }, [h('div', { class: 'label' }, 'Setup'), h('h1', { id: 'wiz-title', tabindex: '-1' }, title)]),
        wz != null ? h('div', { class: 'box call' }, [h('div', { class: 'label' }, 'Step'), h('div', { class: 'disp' }, (wz + 1) + ' / 3')])
          : null]),
      rail,
      h('section', { class: 'step place-' + (wz === 2 ? 'knowme' : p), 'aria-labelledby': 'wiz-title' }, [h('p', { class: 'err', id: 'wiz-err', role: 'alert' }), body]),
      wnav,
      h('p', { class: 'ver' }, 'Callsheet ' + (S.version || PAGE_VERSION))]);
    main.textContent = '';
    add(main, sheet);
    foldHelp(sheet.querySelector('.step'), wz === 2 ? 'knowme' : p);  /* 0.7.1: long help behind "?" */
    var cur = rail.querySelector('[aria-current]');
    if (cur && rail.scrollWidth > rail.clientWidth) rail.scrollLeft = Math.max(0, cur.offsetLeft - 16);
    pressMode();
    syncBg();
    main.querySelectorAll('.wi textarea').forEach(fitTa);
    if (focusSel) { var f = main.querySelector(focusSel); if (f) { f.focus({ preventScroll: focusSel === '#wiz-title' }); if (focusSel !== '#wiz-title' && f.scrollIntoView) f.scrollIntoView({ block: 'center' }); } }
    else if (keepFk) { var g = main.querySelector('[data-fk="' + cssq(keepFk) + '"]'); if (g) g.focus({ preventScroll: true }); }
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
    yesB = btn('btn danger critical', yes, null, function () { onYes(yesB, noB, err); });
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
      'America/New_York', 'America/Chicago', 'America/Halifax', 'America/Los_Angeles', 'Asia/Singapore', 'Asia/Tokyo', 'Australia/Sydney'];
    if (list.indexOf('UTC') < 0) list.push('UTC');
    if (cur && list.indexOf(cur) < 0) list.unshift(cur);
    return list;
  }
  /* 0.9.0 Your day › You: name, time zone (one line with Change), time format. Working days moved into the brief
     times (the urgent checks' own days); the Look chooser is the header's Look panel (a row here opens it). */
  function dayAbout() {
    var st = S.settings, nameErr = errBox(), tzErr = errBox(), anErr = errBox();
    var name = on(h('input', { class: 'inp', type: 'text', id: 'f-name', autocomplete: 'name', value: st.name || '' }), 'change', function () {
      patchSettings({ name: name.value.trim() }, nameErr);
    });
    /* the assistant's name: what Callsheet calls itself in Chat, Today and the brief (empty puts the default back) */
    var an = on(h('input', { class: 'inp', type: 'text', id: 'f-aname', autocomplete: 'off', maxlength: '40', value: aname(),
      'aria-describedby': 'f-aname-n' }), 'change', function () {
      var v = an.value.trim() || ASSISTANT_DEFAULT;
      patchSettings({ assistant_name: v }, anErr, [an], function () { an.value = v; });
    });
    var cur = st.timezone || (Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
    var tzEl;
    if (ui.tzOpen) {
      var tz = on(h('select', { class: 'inp', id: 'f-tz', 'data-fk': 'tz-sel' }, tzList(cur).map(function (z) {
        return h('option', { value: z, selected: z === cur }, z.replace(/_/g, ' '));
      })), 'change', function () { patchSettings({ timezone: tz.value }, tzErr, [tz], function () { ui.tzOpen = false; renderSetup('[data-fk="tz"]'); }); });
      tzEl = h('span', { class: 'selw' }, tz);
    } else {
      tzEl = h('div', { class: 'lrow' }, [h('span', { id: 'f-tzv' }, cur.replace(/_/g, ' ')),
        btn('btn sm', 'Change', { 'data-fk': 'tz', 'aria-describedby': 'f-tzv', 'aria-label': 'Change time zone' }, function () { ui.tzOpen = true; renderSetup('#f-tz'); })]);
    }
    /* 12-hour or 24-hour times: the server writes the brief's display strings in this format */
    var tfErr = errBox(), tfCur = st.time_format === '24h' ? '24h' : '12h';
    var tf = h('div', { class: 'chips', role: 'group', 'aria-labelledby': 'lbl-tf' }, [['12h', '12-hour (2:30 pm)'], ['24h', '24-hour (14:30)']].map(function (x) {
      var b = btn('chip', x[1], { 'aria-pressed': String(tfCur === x[0]), 'data-tf': x[0], 'data-fk': 'tf:' + x[0] }, function () {
        if ((S.settings.time_format === '24h' ? '24h' : '12h') === x[0]) return;
        patchSettings({ time_format: x[0] }, tfErr, [].slice.call(tf.querySelectorAll('button')), function () {
          tf.querySelectorAll('button').forEach(function (y) { y.setAttribute('aria-pressed', String(y.getAttribute('data-tf') === x[0])); });
        });
      });
      return b;
    }));
    return h('section', { class: 'where you', 'aria-labelledby': 'you-h' }, [h('h2', { class: 'h3', id: 'you-h' }, 'You'),
      h('div', { class: 'cols' }, [
        h('div', { class: 'field' }, [h('label', { class: 'label', for: 'f-name' }, 'Your name'), name, nameErr]),
        h('div', { class: 'field' }, [h('label', { class: 'label', for: 'f-aname' }, 'Assistant’s name'), an,
          h('p', { class: 'note', id: 'f-aname-n' }, 'What Callsheet calls itself. You can also ask in Chat: “call yourself Max”.'), anErr]),
        h('div', { class: 'field' }, [ui.tzOpen ? h('label', { class: 'label', for: 'f-tz' }, 'Time zone') : h('span', { class: 'label' }, 'Time zone'), tzEl, tzErr]),
        h('div', { class: 'field' }, [h('span', { class: 'label', id: 'lbl-tf' }, 'Time format'), tf, tfErr])])]);
  }
  /* Your day › Appearance and sound: the theme (Light, Dark, Auto; the same choice as the header's Theme panel) and the
     All clear tick, kept on this device (0.21.0: the Replies density went: the Replies rows never read it) */
  function dayLook() {
    var snd = sw(load(KEY_SOUND) === '1', 'A soft tick at All clear', function () {
      var now = load(KEY_SOUND) !== '1'; save(KEY_SOUND, now ? '1' : ''); snd.setAttribute('aria-pressed', String(now)); if (now) tick(true);
    });
    snd.setAttribute('data-fk', 'snd');
    var theme = h('div', { class: 'grp themegrp', role: 'group', 'aria-labelledby': 'theme-lbl', 'data-errhost': '' }, [
      h('span', { class: 'label', id: 'theme-lbl' }, 'Theme')].concat(MODES.map(function (m) {
        return h('button', { type: 'button', class: 'chip', 'data-set-mode': m, 'data-fk': 'theme:' + m, 'aria-pressed': String(PREFS.mode === m) }, MODENAME[m]);
      }), [h('span', { class: 'err', role: 'alert' })]));
    return h('section', { class: 'where looks2', 'aria-labelledby': 'look-h' }, [h('h2', { class: 'h3', id: 'look-h' }, 'Appearance and sound'),
      theme, h('p', { class: 'note' }, 'Auto follows your device’s light or dark setting.'),
      h('div', { class: 'swrow' }, [snd, h('span', { 'aria-hidden': 'true' }, 'A soft tick at All clear')]),
      h('p', { class: 'note' }, 'Kept on this device. Sound stays off unless you turn it on.')]);
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
      /* 0.8.0: the box sits inside its label, so the whole row is the tap target ("Choose calendars" leads here) */
      return h('li', null, h('label', { for: id, class: 'calrow' }, [box, h('span', null, [c.summary || c.id, c.primary ? h('span', { class: 'muted' }, ' · main') : null])]));
    }));
    var key = 'acct:' + acc.id, shown = !!ui.acctOpen[acc.id] || ui.confirm === key;
    /* 0.9.0: one line per account (what it reads) with Change; the switches, calendars and Disconnect behind it */
    var reads = [svc.mail ? 'mail' : null, svc.calendar ? 'calendar' : null].filter(Boolean);
    var chg = btn('btn sm', shown ? 'Close' : 'Change', { 'aria-expanded': String(shown), 'aria-controls': 'acb-' + acc.id, 'data-fk': 'acc:' + acc.id, 'aria-label': (shown ? 'Close ' : 'Change ') + acc.email }, function () {
      ui.acctOpen[acc.id] = !shown; renderSetup('[data-fk="' + cssq('acc:' + acc.id) + '"]');
    });
    return h('div', { class: 'acct' }, [
      h('div', { class: 'acct-h' }, [h('span', { class: 'em' }, acc.email), statusTag(acc), h('span', { class: 'rd muted' }, reads.length ? 'Reads ' + reads.join(' and ') : 'Reads nothing'), chg]),
      acc.status !== 'connected' && acc.error ? h('p', { class: 'acct-err' }, acc.error) : null,
      acc.status === 'needs_reconnect' ? h('p', { class: 'note' }, 'Use "Connect Google account" below and sign in as this account again.') : null,
      acc.status === 'connected' ? gapCard(a, acc, err) : null,  /* 0.8.0: "calendar isn't read" */
      acc.status === 'connected' && acc.cal_add ? calAddBlock(a, acc) : null,  /* 0.21.0 Add to calendar */
      acc.status === 'connected' && svc.mail ? gdAcct(acc) : null,  /* 0.21.0 Allow drafts in Gmail */
      acc.status === 'connected' ? ctAcct(acc) : null,  /* 0.21.0 Allow contacts (read-only) */
      h('div', { id: 'acb-' + acc.id, class: 'acb', hidden: !shown }, [
      h('div', { class: 'svcs' }, [svcToggle('calendar', 'Calendar'), svcToggle('mail', 'Mail')]),
      err,
      h('div', { class: 'acct-f' }, [disc, ui.confirm === key ? null : askBtn(key, 'Disconnect')]),
      list, calErr,
      ui.confirm === key ? confirmRow('Disconnect ' + acc.email + '? Callsheet forgets its data.', 'Disconnect', function (y, c, e) {
        run([y, c], e, function () { return api('DELETE', 'api/accounts/' + encodeURIComponent(acc.id)); }, function () {
          ui.confirm = null; delete ui.openCals[acc.id]; return reload('connect:' + a.id);
        });
      }) : null])]);
  }
  /* 0.21.0 "Allow adding events" (calwrite.py; approved 2026-11-11): the incremental grant of calendar.events
     only, like Drive's (Google's page asks just for that; the access given before stays). Then "Can add events · to
     <calendar>" and the calendar new events go to (primary unless the owner picks another; the list is read-only). */
  function calAddBlock(a, acc) {
    var c = acc.cal_add, err = errBox(), g = ui.calGrant && ui.calGrant.account === acc.id ? ui.calGrant : null;
    var check = btn('tbtn', 'Check again', { 'data-fk': 'calg-check:' + acc.id, 'aria-label': 'Check again: adding events for ' + acc.email }, function () {
      run([check], err, function () { return api('POST', 'api/calendar/access/check', { account_id: acc.id }); }, function () { return reload('calg-check:' + acc.id); });
    });
    if (c.granted) {
      var sel = h('select', { class: 'inp sm', id: 'calg-def-' + acc.id }, (c.calendars || []).map(function (x) {
        return h('option', { value: x.id, selected: x.id === c.calendar ? true : null }, x.name + (x.primary ? ' (main)' : ''));
      }));
      on(sel, 'change', function () {
        run([sel], err, function () { return api('POST', 'api/calendar/access/default', { account_id: acc.id, calendar: sel.value }); }, function (r) {
          acc.cal_add = r.account; renderSetup('#calg-def-' + cssq(acc.id));
        });
      });
      return h('div', { class: 'calg' }, [h('p', { class: 'note' }, c.status || 'Can add events'),
        (c.calendars || []).length > 1 ? h('div', { class: 'calg-row' }, [h('label', { class: 'label', for: 'calg-def-' + acc.id }, 'New events go to'), sel]) : null, err]);
    }
    var start = btn('tbtn', 'Allow adding events', { 'data-fk': 'calg:' + acc.id, 'aria-label': 'Allow adding events for ' + acc.email }, function () {
      run([start], err, function () { return api('POST', 'api/calendar/access/start', { account_id: acc.id }); }, function (r) {
        ui.calGrant = { account: acc.id, flow: r.flow_id, url: r.auth_url }; renderSetup('#calg-paste');
      });
    });
    var box = [h('div', { class: 'calg-row' }, [start, c.granted === false ? check : null]), err];
    if (g) {
      var inp = h('input', { class: 'inp', id: 'calg-paste', type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: 'http://127.0.0.1:8765/?state=…&code=…' });
      var ferr = errBox();
      var fin = btn('act', 'Finish', { 'data-fk': 'calg-finish' }, function () {
        run([fin], ferr, function () { return api('POST', 'api/calendar/access/finish', { flow_id: g.flow, pasted: inp.value }); }, function (r) {
          ui.calGrant = null;
          if (!r.granted) ui.calSaid = 'Google didn’t give that permission. Tick “See, edit, share and delete… events” on Google’s page and try again.';
          return reload('calg:' + acc.id);
        });
      });
      var link = safeHttps(g.url);
      box.push(h('ol', { class: 'kngsteps' }, [
        h('li', null, ['Open ', link ? h('a', { href: link, target: '_blank', rel: 'noopener noreferrer' }, 'Google’s page') : 'Google’s page', ' and allow calendar events for ' + acc.email + '.']),
        h('li', null, 'Google then opens an address starting with http://127.0.0.1:8765 that doesn’t load. Copy that whole address.'),
        h('li', null, [h('label', { class: 'label', for: 'calg-paste' }, 'Paste it here'), h('div', { class: 'knrow' }, [inp, fin])])]), ferr);
    }
    if (ui.calSaid) { box.push(h('p', { class: 'err', role: 'status' }, ui.calSaid)); ui.calSaid = ''; }
    box.push(h('p', { class: 'fine muted' }, 'Callsheet adds an event only when you press Add, invites nobody, and can’t change or delete events.'));
    return h('div', { class: 'calg' }, box);
  }
  /* 0.21.0 Setup › Connections, per account: "Allow drafts in Gmail" (the incremental grant: POST api/gdrafts/grant/start →
     Google's page → paste the address → POST api/gdrafts/grant/finish), its status, and "Test drafts" (POST api/gdrafts/test:
     one draft "Callsheet test draft — safe to delete" to the account itself; says ok or refused). */
  function gdAcct(acc) {
    if (!GD.v && !GD.loading) {
      GD.loading = true;
      api('GET', 'api/gdrafts').then(function (r) { GD.v = r; GD.err = ''; }, function (e) { GD.err = e.message; }).then(function () { GD.loading = false; if (GD.v) renderSetup(); });
    }
    var s = GD.v && (GD.v.accounts || []).filter(function (x) { return x.account_id === acc.id; })[0];
    if (!s) return null;
    var err = errBox(), g = GD.grant && GD.grant.account === acc.id ? GD.grant : null, t = s.test || null;
    var said = s.granted === true ? 'Allowed' : s.granted === false ? 'Not allowed' : 'Not allowed yet';
    if (t && t.result === 'ok' && s.granted !== false) said += ' · test draft saved';
    var kids = [h('div', { class: 'gdrow' }, [h('span', { class: 'label' }, 'Gmail drafts'), h('span', { class: 'muted' }, said),
      s.granted !== true ? btn('tbtn', 'Allow drafts in Gmail', { 'data-fk': 'gd-allow:' + acc.id, 'aria-label': 'Allow drafts in Gmail for ' + acc.email }, function (e) {
        run([e.currentTarget], err, function () { return api('POST', 'api/gdrafts/grant/start', { account_id: acc.id }); }, function (r) {
          if (!r || !safeHttps(r.auth_url)) { err.textContent = 'Callsheet could not start the Google sign-in.'; return; }
          GD.grant = { account: acc.id, email: acc.email, flow: r.flow_id, url: r.auth_url }; renderSetup('#gd-paste');
        });
      }) : null,
      s.granted !== false ? btn('tbtn', 'Test drafts', { 'data-fk': 'gd-test:' + acc.id, 'aria-label': 'Test drafts for ' + acc.email }, function (e) {
        run([e.currentTarget], err, function () { return api('POST', 'api/gdrafts/test', { account_id: acc.id }); }, function (r) {
          GD.tests[acc.id] = r; GD.v = null; renderSetup('[data-fk="' + cssq('gd-test:' + acc.id) + '"]');
        });
      }) : null])];
    var tr = GD.tests[acc.id];
    if (tr) kids.push(h('p', { class: tr.result === 'ok' ? 'gdok' : 'err', role: 'status' }, tr.result === 'ok' ? gdOk(tr.open_url) : String(tr.said || '')));
    else if (t && t.result === 'refused') kids.push(h('p', { class: 'err' }, String(t.said || '')));
    if (g) {
      var inp = h('input', { class: 'inp', id: 'gd-paste', type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: 'http://127.0.0.1:8765/?state=…&code=…' });
      var ferr = errBox(), link = safeHttps(g.url);
      var fin = btn('act', 'Finish', { 'data-fk': 'gd-finish' }, function () {
        run([fin], ferr, function () { return api('POST', 'api/gdrafts/grant/finish', { flow_id: g.flow, pasted: inp.value }); }, function (r) {
          GD.grant = null; GD.v = null; GD.tests[acc.id] = r.granted ? null : { result: 'refused', said: 'Google didn’t allow drafts. Tick “Compose new draft emails” on Google’s page and try again.' };
          renderSetup('[data-fk="' + cssq('gd-test:' + acc.id) + '"]');
        });
      });
      kids.push(h('ol', { class: 'kngsteps' }, [
        h('li', null, ['Open ', link ? h('a', { href: link, target: '_blank', rel: 'noopener noreferrer' }, 'Google’s page') : 'Google’s page', ' and allow “Compose new draft emails” for ' + g.email + '. Nothing is sent from here except an RSVP you press.']),
        h('li', null, 'Google then opens an address starting with http://127.0.0.1:8765 that doesn’t load. Copy that whole address.'),
        h('li', null, [h('label', { class: 'label', for: 'gd-paste' }, 'Paste it here'), h('div', { class: 'knrow' }, [inp, fin])])]), ferr);
    }
    kids.push(err);
    return h('div', { class: 'gdacct' }, kids);
  }
  /* 0.21.0 Setup › Connections, per account: "Allow contacts" (Google Contacts, read-only; the incremental grant:
     POST api/contacts/grant/start → Google's page → paste the address → POST api/contacts/grant/finish), its status
     ("412 contacts · synced 2 h ago") and Sync now (POST api/contacts/sync). Contacts stay in Callsheet. */
  var CT = { v: null, loading: false, err: '', grant: null, said: {} };
  function ctAcct(acc) {
    if (!CT.v && !CT.loading) {
      CT.loading = true;
      api('GET', 'api/contacts').then(function (r) { CT.v = r; CT.err = ''; }, function (e) { CT.err = e.message; }).then(function () { CT.loading = false; if (CT.v) renderSetup(); });
    }
    var s = CT.v && (CT.v.accounts || []).filter(function (x) { return x.account_id === acc.id; })[0];
    if (!s) return null;
    var err = errBox(), g = CT.grant && CT.grant.account === acc.id ? CT.grant : null;
    var said = s.granted === true ? (s.synced ? s.count.toLocaleString() + ' contacts · synced ' + s.synced : 'Allowed · not synced yet')
      : s.granted === false ? 'Not allowed' : 'Not allowed yet';
    var kids = [h('div', { class: 'gdrow' }, [h('span', { class: 'label' }, 'Contacts'), h('span', { class: 'muted', 'data-ct': acc.id }, said),
      s.granted !== true ? btn('tbtn', 'Allow contacts', { 'data-fk': 'ct-allow:' + acc.id, 'aria-label': 'Allow contacts for ' + acc.email }, function (e) {
        run([e.currentTarget], err, function () { return api('POST', 'api/contacts/grant/start', { account_id: acc.id }); }, function (r) {
          if (!r || !safeHttps(r.auth_url)) { err.textContent = 'Callsheet could not start the Google sign-in.'; return; }
          CT.grant = { account: acc.id, email: acc.email, flow: r.flow_id, url: r.auth_url }; renderSetup('#ct-paste');
        });
      }) : btn('tbtn', 'Sync now', { 'data-fk': 'ct-sync:' + acc.id, 'aria-label': 'Sync contacts now for ' + acc.email }, function (e) {
        run([e.currentTarget], err, function () { return api('POST', 'api/contacts/sync', { account_id: acc.id }); }, function (r) {
          CT.v = r; CT.said[acc.id] = r.said || ''; SRCF.at = 0; renderSetup('[data-fk="' + cssq('ct-sync:' + acc.id) + '"]');
        });
      })])];
    var bad = CT.said[acc.id] || (s.granted === true && s.error) || '';
    if (bad) kids.push(h('p', { class: 'err', role: 'status' }, String(bad)));
    if (g) {
      var inp = h('input', { class: 'inp', id: 'ct-paste', type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: 'http://127.0.0.1:8765/?state=…&code=…' });
      var ferr = errBox(), link = safeHttps(g.url);
      var fin = btn('act', 'Finish', { 'data-fk': 'ct-finish' }, function () {
        run([fin], ferr, function () { return api('POST', 'api/contacts/grant/finish', { flow_id: g.flow, pasted: inp.value }); }, function (r) {
          CT.grant = null; CT.v = r.status || null; SRCF.at = 0;
          CT.said[acc.id] = r.granted ? '' : 'Google didn’t allow contacts. Tick “See and download your contacts” on Google’s page and try again.';
          renderSetup('[data-fk="' + cssq((r.granted ? 'ct-sync:' : 'ct-allow:') + acc.id) + '"]');
        });
      });
      kids.push(h('ol', { class: 'kngsteps' }, [
        h('li', null, ['Open ', link ? h('a', { href: link, target: '_blank', rel: 'noopener noreferrer' }, 'Google’s page') : 'Google’s page', ' and allow “See and download your contacts” for ' + g.email + '. Callsheet only reads them, to know who is who.']),
        h('li', null, 'Google then opens an address starting with http://127.0.0.1:8765 that doesn’t load. Copy that whole address.'),
        h('li', null, [h('label', { class: 'label', for: 'ct-paste' }, 'Paste it here'), h('div', { class: 'knrow' }, [inp, fin])])]), ferr);
    }
    kids.push(err);
    return h('div', { class: 'gdacct ctacct' }, kids);
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
      if (inPlace('connections')) renderSetup('[data-fk="' + cssq('ign:' + ws.id) + '"]');
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
            .then(function () { if (inPlace('connections')) renderSetup('[data-fk="' + cssq('slack:' + a.id) + '"]'); });
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
    var c = aColour(a), reads = a.reads || {}, err = errBox();
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
    function later(label, why) { return h('button', { type: 'button', class: 'chip later', disabled: true, 'aria-pressed': 'false' }, [label, h('span', { class: 'cl' }, ' · ' + why)]); }
    var accts = a.accounts || [], key = 'area:' + a.id;
    return sty(h('div', { class: 'acard ' + c.cls }, [
      h('h3', { class: 'areahead', tabindex: '-1', 'data-fk': 'areah:' + a.id }, a.name),
      accts.length ? h('div', { class: 'accts' }, accts.map(function (x) { return accountRow(a, x); })) : h('p', { class: 'note none' }, 'No Google account yet.'),
      connectBlock(a),
      slackBlock(a),
      h('div', { class: 'reads' }, [h('div', { class: 'q', id: 'q-' + a.id }, 'What may the brief read?'),
        h('div', { class: 'chips', role: 'group', 'aria-labelledby': 'q-' + a.id }, [readChip('calendar', 'Calendar'), readChip('mail', 'Mail'),
          a.chat_available ? readChip('chat', 'Chat') : later('Chat', 'connect Slack or link Signal first'), readChip('writer', 'Your model')]),  /* 0.7.1: the disabled Tasks chip is gone (Projects replaced Tasks) */
        err, a.chat_available ? h('p', { class: 'note' }, 'Chat is ' + chatNames(a) + '.') : null]),
      a.fixed ? null : ui.confirm === key ? confirmRow('Delete ' + a.name + '? Its accounts are disconnected and Callsheet forgets their data.', 'Delete', function (y, cn, e) {
        run([y, cn], e, function () { return api('DELETE', 'api/areas/' + encodeURIComponent(a.id)); }, function () { ui.confirm = null; return reload('add-name'); });
      }) : h('div', { class: 'acct-f' }, askBtn(key, 'Delete area'))]), c.style);
  }
  /* step 3: VIPs */
  function areaById(id) { return S.areas.filter(function (a) { return a.id === id; })[0] || { name: String(id || ''), colour: 4 }; }
  function stepVips() {
    var err = errBox(), vips = S.vips;
    var rows = vips.map(function (v) {
      var a = areaById(v.area_id), rmErr = errBox();
      var rm = btn('rm', '×', { 'aria-label': 'Remove ' + (v.name || v.email) }, function () {
        run([rm], rmErr, function () { return api('DELETE', 'api/vips/' + encodeURIComponent(v.id)); }, function () { return reload('vip-name'); });
      });
      /* 0.22.0 VIPs in one store: a VIP Chat added by name has no address; what Chat set for them shows under the name */
      return h('tr', null, [h('td', { class: 'who' }, v.how ? [h('span', null, v.name), h('span', { class: 'sub muted' }, String(v.how))] : v.name),
        h('td', { class: 'em' }, v.email || h('span', { class: 'muted' }, 'By name')),
        h('td', null, sty(h('span', { class: 'tag ' + aColour(a).cls }, a.name), aColour(a).style)), h('td', { class: 'x' }, [rm, rmErr])]);
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
    return [
      h('p', { class: 'intro' }, 'People whose mail always counts as needing a reply.'),
      h('p', { class: 'vipppl' }, [h('span', null, 'A VIP is a person: every address, Slack and Signal of theirs counts.'), peopleEntry('People')]),  /* 0.8.2 */
      h('div', null, [
        vips.length ? h('table', { class: 'vip' }, [
          h('thead', null, h('tr', null, [h('th', null, 'Name'), h('th', null, 'Email'), h('th', null, 'Area'), h('th', null, h('span', { class: 'sr' }, 'Remove'))])),
          h('tbody', null, rows)]) : h('p', { class: 'note' }, 'No VIPs yet.'),
        h('h2', { class: 'h3 addh' }, 'Add a VIP'), form]),
  ];
  }
  function fmtDay(iso) {
    var d = new Date(iso);
    if (!iso || isNaN(d.getTime())) return '';
    try { return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: (S.settings && S.settings.timezone) || undefined }); }
    catch (e) { return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }); }
  }

  /* step 4: call times */
  var CT_DEFAULT = { morning: 'Morning brief', midday: 'Midday check-in', afternoon: 'Afternoon check-in' };
  /* 0.9.0 Your day › Brief times: one row per time with its days; the urgent checks are one more row with their
     own days (the old working days and "No urgent checks on days off", merged: see urgentDays) */
  function urgentDays(st) { return st.skip_checkins_days_off ? DAYS.filter(function (d) { return (st.workdays || []).indexOf(d) >= 0; }) : DAYS.slice(); }
  function dayBriefs() {
    var st = S.settings, cts = (st.call_times || []).slice(), uErr = errBox();
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
      /* 0.6.2: the call time's own days (at least one; to stop it, turn it off) */
      var days = h('div', { class: 'chips ctdays', role: 'group', 'aria-label': label + ' days' }, DAYS.map(function (d) {
        var b = btn('chip', DAYNAME[d], { 'aria-pressed': String((ct.days || DAYS).indexOf(d) >= 0), 'data-ctday': ct.id + ':' + d, 'data-fk': 'ctday:' + ct.id + ':' + d }, function () {
          var cur = (cts.filter(function (x) { return x.id === ct.id; })[0] || ct).days || DAYS;
          var want = cur.indexOf(d) >= 0 ? cur.filter(function (x) { return x !== d; }) : cur.concat([d]);
          if (!want.length) { err.textContent = 'Pick at least one day. To stop the ' + label + ', turn it off.'; return; }
          save(ct, { days: DAYS.filter(function (x) { return want.indexOf(x) >= 0; }) }, err, [b], function (now) {
            days.querySelectorAll('button').forEach(function (y) { y.setAttribute('aria-pressed', String((now.days || []).indexOf(y.getAttribute('data-ctday').split(':')[1]) >= 0)); });
          });
        });
        return b;
      }));
      box = h('div', { class: 'tb' + (ct.id === 'morning' ? ' lead' : '') + (ct.enabled ? '' : ' off') }, [
        h('div', { class: 'label' }, label), disp,
        h('div', { class: 'row' }, [h('label', { class: 'sr', for: id }, label + ' time'), inp, t]), days, err]);
      return box;
    });
    var ug = urgentDays(st), pushOnNow = !!(S.push && S.push.urgent);
    var ugDays = h('div', { class: 'chips ctdays', role: 'group', 'aria-label': 'Urgent checks days' }, DAYS.map(function (d) {
      var b = btn('chip', DAYNAME[d], { 'aria-pressed': String(ug.indexOf(d) >= 0), 'data-ugday': d, 'data-fk': 'ugday:' + d }, function () {
        var cur = urgentDays(S.settings), want = cur.indexOf(d) >= 0 ? cur.filter(function (x) { return x !== d; }) : cur.concat([d]);
        if (!want.length) { uErr.textContent = 'Pick at least one day. To stop them, turn urgent alerts off under Phone.'; return; }
        patchSettings({ workdays: DAYS.filter(function (x) { return want.indexOf(x) >= 0; }), skip_checkins_days_off: true }, uErr, [b], function (sent) {
          ugDays.querySelectorAll('button').forEach(function (y) { y.setAttribute('aria-pressed', String(sent.workdays.indexOf(y.getAttribute('data-ugday')) >= 0)); });
        });
      });
      return b;
    }));
    blocks.push(h('div', { class: 'tb ug' + (pushOnNow ? '' : ' off') }, [h('div', { class: 'label' }, 'Urgent checks'),
      h('div', { class: 'disp sm' }, 'Between briefs'), h('p', { class: 'muted ugst' }, pushOnNow ? 'Every 10 minutes, on these days' : 'Off: turn on urgent alerts under Phone'), ugDays, uErr]));
    return h('section', { class: 'where briefs', 'aria-labelledby': 'bt-h' }, [h('h2', { class: 'h3', id: 'bt-h' }, 'Brief times'),
      h('p', { class: 'intro' }, 'Times are ' + (st.timezone || 'your time zone').replace(/_/g, ' ') + '.'),
      h('div', { class: 'times' }, blocks),
      h('p', { class: 'note' }, 'Callsheet refreshes at these times, on the days picked under each. Refresh always works.'),
      /* 0.17.0: the brief times can be changed by asking, too (a brief_schedule rule in Your rules, with Undo) */
      h('div', { class: 'rlask' }, [h('p', { class: 'rlhint' }, 'Or ask in Chat: “' + RL_ASK + '”.'),
        btn('btn sm', [ico('chat'), 'Ask in Chat'], { 'data-fk': 'bt-ask' }, function () { chatStartWith(RL_ASK); })])]);
  }

  /* step 5: writing instructions. Profiles (Default is built in and read-only); five texts per profile, each saved
     on its own (PATCH api/writer/profiles/<id>); Reset = PATCH "" (the text becomes Default's). The safety rules
     and the style examples are fixed in the server (writer.SAFETY, writer.EXAMPLES) and always come last. */
  var WSECT = [
    ['rules', 'Global rules', 'Applies to everything Callsheet writes.'],
    ['summary', 'Day summary', 'The short paragraph at the top of Today.'],
    ['areas', 'Area summaries', 'The few sentences for each area.'],
    ['emails', 'Email summaries', 'The one line in each row and “What they want”.'],
    ['drafts', 'Reply drafts', 'Suggested replies you copy, or put in your Gmail Drafts, and send yourself.']];
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

  /* ---------- Setup › AI (approved 2026-11-12, the five suggestions): one model server, a model per job picked
     from the server's own list, "Use one model for everything", web search, writing, and one closed Advanced drawer.
     GET/PATCH api/ai and GET api/ai/models (aisettings.py: the saved server's GET /models only, on Check or when the
     list is over five minutes old). Claude is a fallback in Advanced (api/writer): off unless a key is saved and the
     switch is on. Every value is text (h()). ---------- */
  var CLAUDE_MODELS = [
    ['claude-opus-5-5', 'Claude Opus 5.5', '$4 in / $20 out'],
    ['claude-sonnet-5-5', 'Claude Sonnet 5.5', '$2 in / $10 out'],
    ['claude-haiku-5-5', 'Claude Haiku 5.5', '$0.10 in / $0.50 out']];
  var LOCAL_URL_EG = 'http://192.168.1.50:8080/v1';
  function writerOf() {
    var w = S.writer || {};
    w.claude = w.claude || {}; w.local = w.local || {};
    return w;
  }
  function fmtNum(n) { n = Number(n) || 0; return n.toLocaleString('en-US'); }
  var AIS = { v: null, at: 0, loading: false, err: null, seq: 0, listing: false, listAt: 0, show: false, adv: false, draft: null, hsaid: '' };
  function aiLoad() {
    var seq = ++AIS.seq;
    AIS.loading = true;
    return api('GET', 'api/ai').then(function (r) { if (seq === AIS.seq) { AIS.v = r; AIS.err = null; } },
      function (e) { if (seq === AIS.seq) AIS.err = e && e.message ? e.message : 'error'; })
      .then(function () { var f = AIS.want; AIS.want = null; AIS.loading = false; AIS.at = Date.now(); aiRedraw(f || undefined); });
  }
  function aiRedraw(focus) {
    if (!inPlace('ai')) return;
    var ae = document.activeElement, fk = ae && ae.getAttribute && ae.getAttribute('data-fk');
    renderSetup(focus || (fk ? '[data-fk="' + cssq(fk) + '"]' : null));
  }
  /* ask the saved server for its models (only it: the endpoint takes no address) */
  function aiList(focus) {
    AIS.listing = true; AIS.listAt = Date.now();
    return api('GET', 'api/ai/models').then(function (r) { if (AIS.v) AIS.v.models = r; },
      function (e) { if (AIS.v) AIS.v.models = { ok: false, models: [], message: e.message, at: Date.now() / 1000 }; })
      .then(function () { AIS.listing = false; aiRedraw(focus); });
  }
  function aiPatch(body, btns, err, focus) {
    return run(btns, err, function () { return api('PATCH', 'api/ai', body); }, function (r) {
      var m = AIS.v && AIS.v.models;
      if (!r.models && m) r.models = m;
      AIS.v = r; CH.ov = null;   /* the Chat tab reloads its model label */
      aiRedraw(focus);
    });
  }
  function aiModels() { var l = AIS.v && AIS.v.models; return l && l.ok ? l.models || [] : null; }
  function aiFind(id) { return (aiModels() || []).filter(function (m) { return m.id === id; })[0] || null; }
  function ctxWord(n) { return n >= 1024 ? Math.round(n / 1024) + 'K' : String(n); }
  /* a model's state, as colour + shape + word: Loaded, Not loaded, Loading, Can't check, Not on the server */
  function aiState(id) {
    var l = AIS.v && AIS.v.models;
    if (!l || !l.ok) return h('span', { class: 'aist na' }, l ? '– Can’t check' : '');
    if (!id) return h('span', { class: 'aist wn' }, '▲ Pick a model');
    var m = aiFind(id);
    if (!m) return h('span', { class: 'aist wn' }, '▲ Not on the server');
    if (m.state === 'loaded') return h('span', { class: 'aist on' }, '■ Loaded');
    if (m.state === 'loading') return h('span', { class: 'aist' }, '□ Loading');
    if (m.state === 'unloaded') return h('span', { class: 'aist' }, '□ Not loaded');
    return null;
  }
  function placeAI() {
    if (!AIS.loading && (!AIS.at || Date.now() - AIS.at > 60000)) aiLoad();
    var v = AIS.v;
    if (v && v.server.base_url && !AIS.listing && Date.now() - AIS.listAt > 300000 &&
        (!v.models || Date.now() / 1000 - (v.models.at || 0) > 300)) setTimeout(function () { aiList(); }, 0);
    if (!v) {
      return [h('section', { class: 'where aisrv', 'aria-labelledby': 'ai-srv-h' }, [h('h2', { class: 'h3', id: 'ai-srv-h' }, 'Your model server'),
        AIS.err ? h('p', { class: 'err', role: 'alert' }, ['Couldn’t load the AI settings (' + AIS.err + '). ',
          btn('tbtn', 'Try again', { 'data-fk': 'ai-retry' }, function () { AIS.err = null; AIS.at = 0; renderSetup('[data-fk="ai-retry"]'); })])
          : h('p', { class: 'muted' }, 'Loading…')])];
    }
    return [aiNotes(v), aiServer(v), aiJobs(v), aiWeb(), memoryBlock('ai'), aiStyle(), aiAdvanced(v)];
  }
  /* what the upgrade moved (once): one quiet panel with OK */
  function aiNotes(v) {
    if (!v.notes || !v.notes.length) return null;
    var err = errBox();
    var ok = btn('btn sm', 'OK', { 'data-fk': 'ai-notes-ok' }, function () { aiPatch({ notes_seen: true }, [ok], err, '#ai-srv-h'); });
    return h('section', { class: 'where ainotes', 'aria-labelledby': 'ai-notes-h' }, [h('h2', { class: 'h3', id: 'ai-notes-h' }, 'What changed here'),
      h('ul', { class: 'ln' }, v.notes.map(function (t) { return h('li', null, t); })), h('div', { class: 'btns' }, ok), err]);
  }
  /* ----- your model server: the address once, an optional key, Check, and what the server lists ----- */
  function aiServer(v) {
    var sv = v.server, err = errBox(), l = v.models;
    var url = h('input', { class: 'inp', type: 'url', id: 'ai-url', value: AIS.draft != null ? AIS.draft : (sv.base_url || ''), placeholder: LOCAL_URL_EG,
      autocomplete: 'off', spellcheck: 'false', 'data-fk': 'ai-url' });
    on(url, 'input', function () { AIS.draft = url.value; });
    function check(b) {
      var val = url.value.trim();
      if (!val) { err.textContent = 'Type your model server’s address first.'; url.focus(); return; }
      run([b], err, function () { return val.replace(/\/+$/, '') !== (sv.base_url || '') ? api('PATCH', 'api/ai', { base_url: val }) : Promise.resolve(null); }, function (r) {
        if (r) { AIS.v = r; } AIS.draft = null; AIS.show = true;
        return aiList('[data-fk="ai-check"]');
      });
    }
    var chk = btn('btn sm', 'Check', { 'data-fk': 'ai-check' }, function () { check(chk); });
    on(url, 'keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); check(chk); } });
    var key = h('input', { class: 'inp', type: 'password', id: 'ai-key', autocomplete: 'off', spellcheck: 'false', 'data-fk': 'ai-key',
      placeholder: sv.key_hint ? 'Saved key ' + sv.key_hint : 'No key' });
    on(key, 'change', function () { var k = key.value.trim(); if (k) aiPatch({ api_key: k }, [key], err, '#ai-key'); });
    var kDel = sv.key_hint ? btn('tbtn', 'Remove key', { 'data-fk': 'ai-kdel' }, function () { aiPatch({ api_key: '' }, [kDel], err, '#ai-key'); }) : null;
    var state = null;
    if (AIS.listing && !l) state = h('p', { class: 'aistate', role: 'status' }, 'Checking…');
    else if (l && !l.ok) {
      var again = btn('btn primary sm', 'Try again', { 'data-fk': 'ai-again' }, function () { again.disabled = true; aiList('[data-fk="ai-again"]'); });
      state = h('div', { class: 'aidown', role: 'status' }, [h('p', { class: 'aist wn' }, '▲ Can’t reach it'), h('p', null, l.message),
        h('p', { class: 'muted' }, 'Reading and the brief wait for it; Callsheet’s own sentences fill in meanwhile.'), h('div', { class: 'btns' }, again)]);
    } else if (l) {
      var ms = l.models || [], loaded = ms.filter(function (m) { return m.state === 'loaded'; }).length, said = ms.some(function (m) { return m.state; });
      var tog = ms.length ? btn('tbtn', AIS.show ? 'Hide models' : 'Show models', { 'aria-expanded': String(!!AIS.show), 'aria-controls': 'ai-mlist', 'data-fk': 'ai-show' },
        function () { AIS.show = !AIS.show; renderSetup('[data-fk="ai-show"]'); }) : null;
      state = h('div', { class: 'aistate', role: 'status' }, [h('p', null, [h('span', { class: 'aist on' }, '■ Connected'), ' ',
        h('span', { class: 'muted' }, plural(ms.length, 'model', 'models') + (said ? ', ' + loaded + ' loaded' : '') + ' · checked ' + fmtWhen(new Date(l.at * 1000).toISOString()))]), tog]);
    } else if (sv.base_url) state = h('p', { class: 'aistate muted', role: 'status' }, 'Press Check to list its models.');
    var list = l && l.ok && AIS.show && (l.models || []).length ? h('ul', { class: 'aimlist', id: 'ai-mlist' }, l.models.map(function (m) {
      return h('li', null, [h('span', { class: 'aimid' }, m.id), h('span', { class: 'muted' }, m.ctx ? ctxWord(m.ctx) + ' context' : 'context not said'), aiState(m.id)]);
    })) : null;
    return h('section', { class: 'where aisrv', 'aria-labelledby': 'ai-srv-h' }, [h('h2', { class: 'h3', id: 'ai-srv-h', tabindex: '-1' }, 'Your model server'),
      h('p', { class: 'note' }, 'Every job below runs here, on your network. Your messages go nowhere else.'),
      h('div', { class: 'field' }, [h('label', { class: 'label', for: 'ai-url' }, 'Address'), h('div', { class: 'inrow airow' }, [url, chk])]),
      h('div', { class: 'field' }, [h('label', { class: 'label', for: 'ai-key' }, 'Key (only if your server asks for one)'), h('div', { class: 'inrow airow' }, [key, kDel])]),
      err, state, list]);
  }
  /* ----- which model does what: Chat and Background, or one model for both ----- */
  function aiPicker(id, label, cur, claude, onPick) {
    var ms = aiModels(), opts = [], seen = {};
    function opt(val, text, sel) { seen[val] = 1; opts.push(h('option', { value: val, selected: sel ? true : null }, text)); }
    var local = (ms || []).map(function (m) { return m.id; });
    if (cur.kind === 'local' && !cur.id && local.length) opts.push(h('option', { value: '', disabled: true, selected: true }, 'Pick a model'));
    else if (cur.kind === 'local' && cur.id && local.indexOf(cur.id) < 0) opt('l:' + cur.id, cur.id + (ms ? ' (not on the server)' : ''), true);
    var grp = h('optgroup', { label: 'On your model server' });
    local.forEach(function (x) { var m = aiFind(x); grp.appendChild(h('option', { value: 'l:' + x, selected: cur.kind === 'local' && cur.id === x ? true : null },
      x + (m && m.ctx ? ' · ' + ctxWord(m.ctx) : '') + (m && m.state === 'loaded' ? ' · loaded' : ''))); });
    if (local.length) opts.push(grp);
    if (claude) {
      var cg = h('optgroup', { label: 'Claude · Anthropic’s cloud' });
      CLAUDE_MODELS.forEach(function (c) { cg.appendChild(h('option', { value: 'c:' + c[0], selected: cur.kind === 'claude' && cur.id === c[0] ? true : null }, c[1])); });
      opts.push(cg);
    }
    if (!opts.length) opt('', ms ? 'No models listed' : 'Check your server first', true);
    var sel = h('select', { class: 'inp aisel', id: id, 'data-fk': id, 'aria-label': label, disabled: !local.length && !claude && !cur.id }, opts);
    on(sel, 'change', function () { onPick(sel.value, sel); });
    return sel;
  }
  function aiJobs(v) {
    var err = errBox(), c = v.chat, bg = v.background, claudeOk = !!(v.claude.key_hint);
    var bgCur = { kind: 'local', id: bg.model }, chCur = c.provider === 'claude' ? { kind: 'claude', id: c.claude_model } : { kind: 'local', id: c.model };
    function pick(field) {
      return function (val, sel) {
        var body = {};
        if (val.indexOf('c:') === 0) body.chat_claude = val.slice(2); else body[field] = val.slice(2);
        aiPatch(body, [sel], err, '[data-fk="' + sel.id + '"]');
      };
    }
    var one = sw(v.one_model, 'Use one model for everything', function () { aiPatch({ one_model: !v.one_model }, [one], err, '[data-fk="ai-one"]'); });
    one.setAttribute('data-fk', 'ai-one');
    var noList = !aiModels() ? h('p', { class: 'muted aihint' }, v.server.base_url ? 'Check your model server above to pick from its models.' : 'Add your model server above, then pick a model for each job.') : null;
    /* Chat: its model, Show thinking (the Chat tab's switch), and the Chat tab on this device */
    var th = sw(c.thinking, 'Show thinking', function () {
      run([th], err, function () { return api('PATCH', 'api/chat/settings', { local: { thinking: !c.thinking }, claude: { thinking: !c.thinking } }); },
        function () { CH.ov = null; aiLoad().then(function () { aiRedraw('[data-fk="ai-think"]'); }); });
    });
    th.setAttribute('data-fk', 'ai-think');
    var tab = sw(!chatOff(), 'Chat tab on this device', function () { save(KEY_CHATOFF, chatOff() ? '' : '1'); renderSetup('[data-fk="ai-chat"]'); });
    tab.setAttribute('data-fk', 'ai-chat');
    var chatExtras = h('div', { class: 'aiess' }, [h('div', { class: 'swrow' }, [th, h('span', { 'aria-hidden': 'true' }, 'Show thinking')]),
      h('div', { class: 'swrow' }, [tab, h('span', { 'aria-hidden': 'true' }, 'Chat tab on this device')])]);
    var bgLine = h('p', { class: 'muted aihint' }, bg.per_hour ? 'About ' + fmtNum(bg.per_hour) + ' messages an hour on this model.' : 'Reads new messages as they arrive.');
    function row(key, name, sub, picker, state, extras) {
      return h('div', { class: 'aijob', role: 'group', 'aria-labelledby': 'ai-j-' + key }, [
        h('div', { class: 'aijh' }, [h('h3', { class: 'aijn', id: 'ai-j-' + key }, name), h('p', { class: 'muted' }, sub)]),
        h('div', { class: 'aijp' }, [picker, state]), extras]);
    }
    var rows;
    if (v.one_model) {
      rows = [row('all', 'Every job', 'Chat, reading each message, the brief, Get to know me and project ideas',
        aiPicker('ai-m-all', 'Model for every job', bgCur, false, pick('background_model')), aiState(bg.model), bgLine),
        h('div', { class: 'aijob sub' }, [h('h3', { class: 'aijn' }, 'Chat'), chatExtras])];
    } else {
      var warn = null, bothLoaded = function () { var a = aiFind(c.model), b = aiFind(bg.model); return a && b && a.state === 'loaded' && b.state === 'loaded'; };
      if (c.provider === 'local' && bg.model && c.model !== bg.model && !bothLoaded()) {
        var fix = btn('btn sm', 'Use ' + bg.model + ' for both', { 'data-fk': 'ai-fix' }, function () { aiPatch({ one_model: true }, [fix, keep], err, '[data-fk="ai-one"]'); });
        var keep = btn('tbtn', 'Keep as is', { 'data-fk': 'ai-keep' }, function () { AIS.keep = c.model + '|' + bg.model; renderSetup('[data-fk="ai-m-chat"]'); });
        if (AIS.keep !== c.model + '|' + bg.model) {
          warn = h('div', { class: 'aiwarn', role: 'note' }, [h('p', null, [h('b', null, '▲ Two models. '), 'Chat uses ' + c.model + '; reading and the brief use ' + bg.model +
            '. Your server may hold one at a time, so it swaps between them and each side waits.']), h('div', { class: 'btns' }, [fix, keep])]);
        }
      }
      rows = [warn,
        row('chat', 'Chat', 'Questions in the Chat tab', aiPicker('ai-m-chat', 'Model for Chat', chCur, claudeOk, pick('chat_model')),
          c.provider === 'claude' ? h('span', { class: 'aist' }, 'Anthropic’s cloud') : aiState(c.model), chatExtras),
        row('bg', 'Background', 'Reading each message, the brief, Get to know me and project ideas',
          aiPicker('ai-m-bg', 'Model for background work', bgCur, false, pick('background_model')), aiState(bg.model), bgLine)];
    }
    var hv = bg.health, health = null;
    if (hv && hv.healthy === false) {
      var hErr = errBox();
      var again = btn('btn sm', 'Check again', { 'data-fk': 'lm-check' }, function () {
        run([again], hErr, function () { return api('POST', 'api/background/check'); }, function (r) {
          var ok = r && r.local_health && r.local_health.healthy;
          AIS.hsaid = ok ? 'The model is working again.' : 'Still not working.';
          return aiLoad().then(function () { aiRedraw('[data-fk="lm-check"]'); });
        });
      });
      health = h('div', { class: 'aiwarn' }, [h('p', null, [h('b', null, '▲ Not working right. '), 'The model’s answers have been unusable since ' + (hv.since_text || '') + '.']),
        h('div', { class: 'btns' }, [again, AIS.hsaid ? h('span', { class: 'hint', role: 'status' }, AIS.hsaid) : null]), hErr]);
    } else if (AIS.hsaid) health = h('p', { class: 'hint', role: 'status' }, AIS.hsaid);
    return h('section', { class: 'where aijobs', 'aria-labelledby': 'ai-jobs-h' }, [h('h2', { class: 'h3', id: 'ai-jobs-h', tabindex: '-1' }, 'Which model does what'),
      h('div', { class: 'swrow' }, [one, h('span', { 'aria-hidden': 'true' }, 'Use one model for everything')]),
      h('p', { class: 'note' }, 'Keeps one model loaded, so nothing waits while the server swaps.'),
      noList, health, rows, err]);
  }
  /* ----- the one Advanced drawer: rare numbers by job, Claude as a fallback, web search limits ----- */
  /* Setup › AI › Advanced: quiet times for the model server (settings model_quiet_times, local time): background
     reading leaves the server alone then, and the night's reading fits around them. None on a new install. */
  var QUIET_MAX = 8;
  function aiQuiet() {
    var err = errBox(), list = ((S && S.settings && S.settings.model_quiet_times) || []).map(function (w) { return [w[0], w[1]]; });
    function save(next, focus) { patchSettings({ model_quiet_times: next }, err, [], function () { aiRedraw(focus); }); }
    function tin(id, val, label) { return h('input', { class: 'inp', type: 'time', id: id, 'data-fk': id, value: val || '', 'aria-label': label }); }
    var rows = list.map(function (w, i) {
      var n = 'Quiet time ' + (i + 1), a = tin('ai-qt-s' + i, w[0], n + ' starts'), b = tin('ai-qt-e' + i, w[1], n + ' ends');
      function ch(e) {
        if (!a.value || !b.value) return;
        var next = list.slice(); next[i] = [a.value, b.value]; save(next, '#' + e.target.id);
      }
      on(a, 'change', ch); on(b, 'change', ch);
      var rm = btn('tbtn', 'Remove', { 'data-fk': 'ai-qt-rm' + i, 'aria-label': 'Remove ' + n.toLowerCase() + ', ' + w[0] + ' to ' + w[1] }, function () {
        save(list.filter(function (_, j) { return j !== i; }), '[data-fk="ai-qt-add"]');
      });
      return h('div', { class: 'qtrow', role: 'group', 'aria-label': n }, [a, h('span', { class: 'muted', 'aria-hidden': 'true' }, 'to'), b, rm]);
    });
    var addEl = null;
    if (list.length < QUIET_MAX) {
      var na = tin('ai-qt-ns', '', 'New quiet time starts'), nb = tin('ai-qt-ne', '', 'New quiet time ends');
      var add = btn('tbtn', 'Add', { 'data-fk': 'ai-qt-add' }, function () {
        if (!na.value || !nb.value) { err.textContent = 'Give the quiet time a start and an end.'; (na.value ? nb : na).focus(); return; }
        if (na.value === nb.value) { err.textContent = 'The quiet time starts and ends at the same time.'; nb.focus(); return; }
        save(list.concat([[na.value, nb.value]]), '[data-fk="ai-qt-add"]');
      });
      addEl = h('div', { class: 'qtrow', role: 'group', 'aria-label': 'New quiet time' }, [na, h('span', { class: 'muted', 'aria-hidden': 'true' }, 'to'), nb, add]);
    }
    return h('div', { class: 'aigrp' }, [h('h3', { class: 'label', id: 'ai-qt-h' }, 'Quiet times for the model server'),
      h('p', { class: 'note' }, list.length ? 'Background reading leaves your model server alone at these times (your time zone). Chat still answers.'
        : 'None: background reading may use your model server at any time. Add a time if the server is busy with something else then.'),
      rows, addEl, err]);
  }
  /* Setup › AI › Advanced › Model server: extra request fields for the background model (PATCH api/background
     {extra_body}), a small JSON object some models need (e.g. {"thinking_budget_tokens": 0}); keys only from the
     server's list (extra_keys), numbers only, checked again on the server. Empty = none (the default). */
  function aiExtra(bg, err) {
    var cur = bg.extra_body && Object.keys(bg.extra_body).length ? JSON.stringify(bg.extra_body) : '';
    var inp = h('input', { class: 'inp', type: 'text', id: 'ai-extra', 'data-fk': 'ai-extra', spellcheck: 'false', autocomplete: 'off',
      value: cur, placeholder: '{"top_k": 20}' });
    on(inp, 'change', function () {
      var t = inp.value.trim(), obj = null;
      if (t) {
        try { obj = JSON.parse(t); } catch (e) { obj = undefined; }
        if (!obj || typeof obj !== 'object' || Array.isArray(obj)) { err.textContent = 'Type a JSON object, like {"top_k": 20}, or leave it empty.'; inp.focus(); return; }
      }
      run([inp], err, function () { return api('PATCH', 'api/background', { extra_body: obj }); }, function () { return aiLoad().then(function () { aiRedraw('#ai-extra'); }); });
    });
    return h('div', { class: 'field' }, [h('label', { class: 'label', for: 'ai-extra' }, 'Extra request fields for ' + bg.model + ' (JSON)'), inp,
      h('p', { class: 'muted aihint' }, 'Only if your model needs them. Allowed: ' + (bg.extra_keys || []).join(', ') + '.')]);
  }
  function aiAdvanced(v) {
    var tog = btn('tbtn disc', 'Advanced', { 'aria-expanded': String(!!AIS.adv), 'aria-controls': 'ai-adv', 'data-fk': 'ai-adv' }, function () {
      AIS.adv = !AIS.adv; renderSetup('[data-fk="ai-adv"]');
    });
    if (!AIS.adv) return h('section', { class: 'where aiadv' }, tog);
    var err = errBox(), c = v.chat, bg = v.background, cl = v.claude, lim = c.limits || {};
    function num(id, label, val, lo, hi, save, hint) {
      var inp = h('input', { class: 'inp num', type: 'number', id: id, min: String(lo), max: String(hi), step: '1', inputmode: 'numeric', 'data-fk': id,
        value: val != null ? String(val) : '', placeholder: hint || '' });
      on(inp, 'change', function () {
        var s = inp.value.trim();
        run([inp], err, function () { return save(s ? Number(s) : null); }, function () { return aiLoad().then(function () { aiRedraw('#' + id); }); });
      });
      return h('div', { class: 'field' }, [h('label', { class: 'label', for: id }, label), inp]);
    }
    function chatSet(k) { return function (n) { var b = { local: {} }; b.local[k] = n; return api('PATCH', 'api/chat/settings', b); }; }
    var cl0 = bg.ctx_limits || [4096, 1048576];
    var srv = h('div', { class: 'aigrp' }, [h('h3', { class: 'label' }, 'Model server'),
      num('ai-ctx', 'Context size when the server doesn’t say (tokens)', bg.ctx_size, cl0[0], cl0[1], function (n) { return api('PATCH', 'api/background', { ctx_size: n }); }, String(bg.ctx_default || 32768)),
      h('p', { class: 'muted aihint' }, !bg.model ? 'Used for the background model once you pick one.' : bg.ctx_from === 'server' ? 'Your server says ' + fmtNum(bg.ctx) + ' tokens for ' + bg.model + '; that counts.' : 'Used for ' + bg.model + ' when your server doesn’t say.'),
      bg.model ? aiExtra(bg, err) : null]);
    var tools = on(h('select', { class: 'inp', id: 'ai-tools', 'data-fk': 'ai-tools' }, [['auto', 'Automatic (recommended)'], ['native', 'Server tool calling'], ['json', 'Simple JSON (for models without tool calling)']].map(function (o) {
      return h('option', { value: o[0], selected: c.tools === o[0] ? true : null }, o[1]);
    })), 'change', function () { run([tools], err, function () { return api('PATCH', 'api/chat/settings', { local: { tools: tools.value } }); }, function () { return aiLoad(); }); });
    var chatG = h('div', { class: 'aigrp' }, [h('h3', { class: 'label' }, 'Chat'),
      num('ai-cmax', 'Longest answer (tokens)', c.max_tokens, (lim.max_tokens || [256])[0], (lim.max_tokens || [0, 16384])[1], chatSet('max_tokens')),
      num('ai-ctime', 'Give up after each step (seconds)', c.timeout, (lim.timeout || [30])[0], (lim.timeout || [0, 600])[1], chatSet('timeout')),
      num('ai-cslot', 'Server slot', c.id_slot, (lim.id_slot || [0])[0], (lim.id_slot || [0, 63])[1], chatSet('id_slot')),
      h('div', { class: 'field' }, [h('label', { class: 'label', for: 'ai-tools' }, 'Tool use'), tools])]);
    /* Claude: a key, a model, and the fallback switch (off unless the owner turns it on; never on without a key) */
    var cErr = errBox();
    var cKey = h('input', { class: 'inp', type: 'password', id: 'ai-ckey', autocomplete: 'off', spellcheck: 'false', 'data-fk': 'ai-ckey',
      placeholder: cl.key_hint ? 'Saved key ' + cl.key_hint : 'sk-ant-…' });
    function claudeDone(focus) { return function (r) { if (r && r.claude) S.writer = r; return aiLoad().then(function () { aiRedraw(focus); }); }; }
    var cSave = btn('btn sm', 'Save key', { 'data-fk': 'ai-csave' }, function () {
      var k = cKey.value.trim();
      if (!k) { cErr.textContent = 'Paste your Anthropic API key first.'; cKey.focus(); return; }
      run([cSave], cErr, function () { return api('PATCH', 'api/writer', { claude: { api_key: k } }); }, claudeDone('[data-fk="ai-csave"]'));
    });
    var cDel = cl.key_hint ? btn('tbtn', 'Remove key', { 'data-fk': 'ai-cdel' }, function () {
      run([cDel], cErr, function () { return api('DELETE', 'api/writer/claude-key'); }, claudeDone('#ai-ckey'));
    }) : null;
    var cModel = on(h('select', { class: 'inp', id: 'ai-cmodel', 'data-fk': 'ai-cmodel' }, CLAUDE_MODELS.map(function (m) {
      return h('option', { value: m[0], selected: m[0] === (cl.model || 'claude-opus-5-5') ? true : null }, m[1] + ' · ' + m[2]);
    })), 'change', function () { run([cModel], cErr, function () { return api('PATCH', 'api/writer', { claude: { model: cModel.value } }); }, claudeDone('#ai-cmodel')); });
    var fb = sw(cl.fallback, 'If your model server fails, use Claude', function () {
      if (!cl.key_hint && !cl.fallback) { cErr.textContent = 'Save a Claude key first.'; cKey.focus(); return; }
      run([fb], cErr, function () { return api('PATCH', 'api/writer', { fallback_to_claude: !cl.fallback }); }, claudeDone('[data-fk="ai-fb"]'));
    });
    fb.setAttribute('data-fk', 'ai-fb');
    var cTest = cl.key_hint ? btn('act', 'Test key', { 'data-fk': 'ai-ctest' }, function () { testWriter('claude', cTest, cRes, cErr); }) : null;
    var cRes = h('span', { class: 'testres', role: 'status' });
    var claudeG = h('div', { class: 'aigrp' }, [h('h3', { class: 'label' }, 'Claude · Anthropic’s cloud'),
      h('div', { class: 'swrow' }, [fb, h('span', { 'aria-hidden': 'true' }, 'If your model server fails, use Claude')]),
      h('p', { class: 'note' }, 'Off keeps your messages on your network. On, only what that job needs goes to Anthropic, and only when your server fails.'),
      h('div', { class: 'field' }, [h('label', { class: 'label', for: 'ai-ckey' }, cl.key_hint ? 'Anthropic API key (saved ' + cl.key_hint + ')' : 'Anthropic API key'),
        h('div', { class: 'inrow airow' }, [cKey, cSave]), cDel ? h('div', { class: 'btns' }, cDel) : null]),
      h('div', { class: 'field' }, [h('label', { class: 'label', for: 'ai-cmodel' }, 'Model · USD per million tokens'), cModel]),
      cTest ? h('div', { class: 'testrow' }, [cTest, cRes]) : null, cErr,
      h('p', { class: 'muted aihint' }, 'With a key saved, Chat’s list offers Claude too. Keys: console.anthropic.com; billed by Anthropic.')]);
    return h('section', { class: 'where aiadv' }, [tog, h('div', { id: 'ai-adv', class: 'advbody' }, [
      h('p', { class: 'note' }, 'Rarely needed: the defaults work.'), srv, aiQuiet(), chatG, claudeG, WS.v ? wsAdvanced(WS.v) : null, err])]);
  }
  function testWriter(provider, b, res, err) {
    res.textContent = 'Testing…';
    run([b], err, function () { return api('POST', 'api/writer/test', { provider: provider }); }, function (r) {
      res.textContent = '';
      add(res, [h('span', { class: 'st' + (r && r.ok ? '' : ' warn') }, r && r.ok ? 'Works' : 'Didn’t work'), ' ', (r && r.message) || '']);
    }).then(function () { if (res.textContent === 'Testing…') res.textContent = ''; });
  }
  /* Setup home's AI line: what each job uses */
  function aiHomeLine() {
    var v = AIS.v;
    if (!v) return 'Your model server, a model per job';
    if (!v.server.base_url) return 'No model server yet';
    var bm = v.background.model || 'pick a model';
    if (v.one_model) return v.background.model ? bm + ' for everything' : 'Pick a model';
    return 'Chat: ' + (v.chat.provider === 'claude' ? 'Claude' : v.chat.model || 'pick a model') + ' · Background: ' + bm;
  }

  /* step 6: review */
  function daysSummary(w) {
    w = DAYS.filter(function (d) { return (w || []).indexOf(d) >= 0; });
    if (!w.length) return 'no working days';
    if (w.join() === 'mon,tue,wed,thu,fri') return 'Mon–Fri';
    return w.map(function (d) { return DAYNAME[d]; }).join(', ');
  }

  /* ---------- 0.9.0: Setup in four places ----------
     Connections, Your day, AI and Advanced (closed sections). The same settings and the same API as before; only
     where they live and their wording changed. Each place is a function made of section functions, so a later
     release can add its own (AI › the model server and the jobs: aiServer, aiJobs). The first run keeps three steps: Connect, Your day, Get to
     know me. Old links (#setup/<step number or name>) open the place the step's settings moved to. */
  var PLACE_IDS = ['home', 'connections', 'day', 'ai', 'advanced'];
  var OLD_STEPS = { 1: 'day', 2: 'connections', 3: 'day', 4: 'day', 5: 'ai', 6: 'home', about: 'day', 'about-you': 'day',
    areas: 'connections', vips: 'day', 'call-times': 'day', times: 'day', delivery: 'ai', review: 'home' };
  var WIZ = [['connections', 'Connect'], ['day', 'Your day'], ['knowme', 'Get to know me']];
  var PLACE_FN = { connections: placeConnections, day: placeDay, ai: placeAI, advanced: placeAdvanced };
  function placeFromHash(hash) {
    var m = /^#setup(?:\/(.*))?$/.exec(hash || '');
    if (!m) return null;
    var k = String(m[1] || '').toLowerCase();
    return PLACE_IDS.indexOf(k) >= 0 ? k : Object.prototype.hasOwnProperty.call(OLD_STEPS, k) ? OLD_STEPS[k] : 'home';
  }
  function placeName(p) { var x = PLACES.filter(function (y) { return y[0] === p; })[0]; return x ? x[1] : 'Setup'; }
  function inPlace(p) { return ui.view === 'setup' && ui.place === p; }
  /* open Setup at a place; focus: what to focus after the render; adv: an Advanced section to open first */
  function openSetup(p, focus, adv) {
    p = PLACE_IDS.indexOf(p) >= 0 ? p : 'home';
    if (adv) ui.adv[adv] = true;
    ui.focusNext = focus || null;
    var hash = 'setup' + (p === 'home' ? '' : '/' + p);
    if (location.hash === '#' + hash) {
      ui.place = p; ui.confirm = null;
      if (ui.view === 'setup' && S && main.querySelector('.sheet')) { ui.focusNext = null; renderSetup(focus || '#wiz-title'); } else route();
    }
    else location.hash = hash;
  }
  function wizGo(i) {
    ui.wiz = i; ui.confirm = null;
    openSetup(i === 2 ? 'home' : WIZ[i][0], '#wiz-title');
    window.scrollTo(0, 0);
  }
  /* first run, step 3: Get to know me is optional; Finish marks the setup done */
  function wizKnow() {
    var err = errBox();
    var fin = btn('btn primary', 'Go to my brief', { 'data-fk': 'wiz-finish' }, function () {
      run([fin], err, function () { return api('PATCH', 'api/settings', { setup_done: true }); }, function () {
        S.setup_done = true; ui.wiz = null; location.hash = 'briefing';
      });
    });
    return [h('p', { class: 'intro' }, 'Optional. Callsheet reads your past mail and notes on this box to learn who matters.'),
      h('div', { class: 'go' }, [knowEntry(), fin]), err,
      h('p', { class: 'note' }, 'You can do this later from AI, Memory.')];
  }

  /* Setup home: a "Find a setting" box, then each place with what it is set to now */
  function setupHome() {
    var err = errBox(), first = !S.setup_done, lines = placeLines();
    return [
      setFind(),
      h('div', { class: 'shome' }, PLACES.map(function (x) {
        var adv = x[0] === 'advanced';
        return h('section', { class: 'scard' + (adv ? ' adv' : ''), 'aria-labelledby': 'sh-' + x[0] }, [
          h('h2', { class: 'h3', id: 'sh-' + x[0] }, x[1]),
          h('ul', { class: 'ln' }, lines[x[0]].map(function (t) { return h('li', null, t); })),
          btn('btn sm', adv ? 'Open' : 'Edit', { 'aria-label': (adv ? 'Open ' : 'Edit ') + x[1], 'data-fk': 'home:' + x[0] }, function () { go(x[0]); })]);
      })),
      h('div', { class: 'go' }, [first ? btn('btn primary', 'Start: Connect', { 'data-fk': 'wiz-start' }, function () { wizGo(0); }) : null]),
      err,
  ];
  }
  /* each place's summary lines (Setup home) */
  function placeLines() {
    var st = S.settings, nAcc = 0, nSlack = 0, sig = false;
    S.areas.forEach(function (a) { nAcc += (a.accounts || []).length; nSlack += (a.slack || []).length; sig = sig || !!a.signal; });
    var cts = (st.call_times || []).filter(function (c) { return c.enabled; });
    function onDays(c) { var d = DAYS.filter(function (x) { return (c.days || DAYS).indexOf(x) >= 0; }); return d.length === 7 ? '' : ' (' + daysSummary(d) + ')'; }  /* 0.6.2 */
    var timesSum = cts.length ? 'Briefs at ' + cts.map(function (c) { return c.time + onDays(c); }).join(', ') : 'No brief times on';
    var pp = S.push || {};
    return {
      connections: [S.areas.map(function (a) { return a.name; }).join(', '),
        S.google_client.configured ? plural(nAcc, 'Google account', 'Google accounts') : 'Google sign-in key not added yet',
        nSlack ? plural(nSlack, 'Slack workspace', 'Slack workspaces') : null, sig ? 'Signal linked' : null].filter(Boolean),
      day: [[st.name || 'No name yet', (st.timezone || '').replace(/_/g, ' ')].filter(Boolean).join(' · '), timesSum,
        (pp.at_call_times || pp.urgent) ? 'Phone alerts on' : 'Phone alerts off',
        plural(S.vips.length, 'VIP', 'VIPs')],
      ai: [aiHomeLine(), 'Chat ' + (chatOff() ? 'off' : 'on') + ' on this device', 'Web search, Memory, Get to know me, People'],
      advanced: ['Google key, alert limits, writing rules, Start over']};
  }

  /* ----- Your day ----- */
  function placeDay() { return [dayAbout(), dayBriefs(), pushBlock('day'), dayPeople(), dayRules(), dayLook()]; }
  /* ---------- 0.17.0 Setup › Your day › Your rules (GET api/preferences): what the owner set by asking in Chat (and
     the ledger actions), grouped Alerts / VIPs / Quiet hours / Clean up / Digests / Brief. Each rule: Undo (turns it
     off and puts back what it changed; it stays listed as undone) and Delete (gone from the list), and a link to the
     chat it came from. Loaded when Your day is drawn and older than a minute, or after a change. Data is text. ---------- */
  var RL = { v: null, at: 0, loading: false, err: null, ask: null, said: '' };
  var RL_ASK = 'Move my morning brief to 7 am on weekdays';
  var RL_GROUPS = [['Alerts', ['alert_rule', 'alerts']], ['VIPs', ['vip', 'vip_through_quiet']], ['Quiet hours', ['quiet_hours']],
    ['Clean up', ['cleanup']], ['Digests', ['digest']], ['Brief', ['brief', 'brief_schedule']]];
  var RL_ACT = { done: 'Marked done', snooze: 'Snoozed', remind: 'Reminder', note: 'Note' };
  function rlChange(c) {   /* 0.22.0: one journal row (actions.py): what, where from, when; Undo once (refused when it changed since) */
    var via = { chat: 'From Chat', today: 'From Today', setup: 'From Setup' }[c.via] || '';
    return h('div', { class: 'alr' + (c.undone_at ? ' off' : ''), 'data-rlc': String(c.id) }, [
      h('b', null, String(c.label)), c.undone_at ? h('span', { class: 'tag' }, 'Undone') : h('span', { class: 'ag' }, fmtWhen(c.at * 1000, true)),
      h('span', { class: 'sub' }, [via, c.note].filter(Boolean).join(' · ')),
      h('div', { class: 'rlacts' }, c.can_undo ? [btn('tbtn', 'Undo', { 'data-fk': 'rlc:' + c.id + ':undo', 'aria-label': 'Undo ' + c.label }, function () {
        mutate(function () { return api('POST', 'api/actions/undo', { id: c.id }); }).then(function () { RL.said = 'Undone: ' + c.label + '.'; rlLoad(); },
          function (e) { RL.said = e.message; rlLoad(); });
      })] : [])]);
  }
  function rlLoad(focus) {
    RL.loading = true;
    api('GET', 'api/preferences').then(function (v) { RL.v = v; RL.err = null; }, function (e) { RL.err = e.message; })
      .then(function () { return api('GET', 'api/actions/recent').then(function (r) { RL.recent = (r && r.changes) || []; }, function () { RL.recent = []; }); })   /* 0.22.0 */
      .then(function () {
        RL.loading = false; RL.at = Date.now();
        if (!inPlace('day')) return;
        var ae = document.activeElement, fk = ae && ae.getAttribute && ae.getAttribute('data-fk');
        renderSetup(focus || (fk ? '[data-fk="' + cssq(fk) + '"]' : ae && ae.id && main.contains(ae) ? '#' + cssq(ae.id) : null));
      });
  }
  /* a new chat with the question box filled in (the owner presses Ask) */
  function chatStartWith(text) {
    clearTimeout(CH.timer); CH.timer = null;
    CH.conv = null; CH.cid = null; save(KEY_CHAT, ''); CH.err = ''; CH.earlier = false; CH.scroll = true; CH.scope = null;
    CH.input = text; location.hash = 'chat';
  }
  function rlOpenChat(cid) {
    clearTimeout(CH.timer); CH.timer = null;
    CH.cid = cid; save(KEY_CHAT, cid); CH.conv = null; CH.ov = null; CH.scope = null; CH.err = ''; CH.scroll = true;
    location.hash = 'chat';
  }
  function rlDo(method, path, b, fk) {
    RL.said = '';
    run([b], RL.errEl, function () { return api(method, path); }, function (r) {
      RL.ask = null;
      var x = r && (r.rule || r.action);
      RL.said = x ? (method === 'DELETE' ? 'Deleted: ' : 'Undone: ') + x.label + '.' : '';
      /* Undo can put back a brief time or a phone setting: reload the page's settings too */
      return loadState().then(null, function () { }).then(function () { RL.at = 0; rlLoad(fk ? '[data-fk="' + cssq(fk) + '"]' : '#rules-h'); });
    });
  }
  function rlChatLink(r) {
    return r.chat && r.chat_title ? btn('tbtn rlchat', 'From chat: “' + r.chat_title + '”', { 'data-fk': 'rl-chat:' + (r.item != null ? 'a' : '') + r.id }, function () { rlOpenChat(r.chat); })
      : h('span', { class: 'muted' }, r.from_chat || r.turn != null ? 'From a chat that’s gone' : 'Set in Setup');
  }
  function rlRule(r) {
    var fk = 'rl:' + r.id, asking = RL.ask === r.id, when = r.created_at ? fmtWhen(r.created_at * 1000, true) : '';
    var subs = (Array.isArray(r.rows) ? r.rows : []).slice(1).map(function (x) { return String(x[1]); }).join(' · ');
    var acts = asking ? [h('span', { class: 'rlq' }, 'Delete this rule for good?'),
      btn('tbtn', 'Delete', { 'data-fk': fk + ':del2', 'aria-label': 'Delete ' + r.label }, function (e) { rlDo('DELETE', 'api/preferences/' + r.id, e.currentTarget, null); }),
      btn('tbtn', 'Keep', { 'data-fk': fk + ':keep' }, function () { RL.ask = null; renderSetup('[data-fk="' + cssq(fk + ':del') + '"]'); })]
      : (r.undone ? [] : [btn('tbtn', 'Undo', { 'data-fk': fk + ':undo', 'aria-label': 'Undo ' + r.label }, function (e) { rlDo('POST', 'api/preferences/' + r.id + '/undo', e.currentTarget, fk + ':del'); }),
        h('span', { 'aria-hidden': 'true' }, '·')]).concat([
        btn('tbtn', 'Delete', { 'data-fk': fk + ':del', 'aria-label': 'Delete ' + r.label }, function () { RL.ask = r.id; renderSetup('[data-fk="' + cssq(fk + ':del2') + '"]'); })]);
    return h('div', { class: 'alr' + (r.undone ? ' off' : ''), 'data-rl': String(r.id) }, [
      h('b', null, String(r.label)), r.undone ? h('span', { class: 'tag' }, 'Undone') : h('span', { class: 'ag' }, when),
      subs ? h('span', { class: 'sub' }, subs) : null,
      h('div', { class: 'rlacts' }, acts.concat([rlChatLink(r)]))]);
  }
  function rlAction(a) {
    var fk = 'rla:' + a.id, until = a.until_ts ? fmtWhen(a.until_ts * 1000, true) : '';
    var sub = [RL_ACT[a.action] || 'Action', until ? (a.action === 'remind' ? (a.fired ? 'reminded ' : 'remind ') : 'back ') + until : ''].filter(Boolean).join(' · ');
    return h('div', { class: 'alr' + (a.undone ? ' off' : ''), 'data-rla': String(a.id) }, [
      h('b', null, String(a.label)), a.undone ? h('span', { class: 'tag' }, 'Undone') : h('span', { class: 'ag' }, a.at ? fmtWhen(a.at * 1000, true) : ''),
      h('span', { class: 'sub' }, sub),
      h('div', { class: 'rlacts' }, (a.undone ? [] : [btn('tbtn', 'Undo', { 'data-fk': fk + ':undo', 'aria-label': 'Undo ' + a.label },
        function (e) { rlDo('POST', 'api/ledger/actions/' + a.id + '/undo', e.currentTarget, null); })]).concat([rlChatLink(a)]))]);
  }
  function dayRules() {
    if (!RL.loading && (!RL.at || Date.now() - RL.at > 60000)) rlLoad();
    var v = RL.v, head = h('h2', { class: 'h3', id: 'rules-h', tabindex: '-1', 'data-fk': 'rules-h' }, 'Your rules');   /* data-fk: other loaders keep the focus here */
    RL.errEl = errBox();
    if (!v) return h('section', { class: 'where rules', 'aria-labelledby': 'rules-h' }, [head,
      RL.err ? h('p', { class: 'err', role: 'alert' }, 'Couldn’t load your rules (' + RL.err + ').') : h('p', { class: 'muted', role: 'status' }, 'Loading…')]);
    var rules = (v.rules || []).concat(v.undone || []), kids = [head,
      h('p', { class: 'intro' }, 'What you’ve set by asking in Chat. Undo turns a rule off and puts back what it changed.')];
    var any = false;
    RL_GROUPS.forEach(function (g) {
      var rs = rules.filter(function (r) { return g[1].indexOf(r.kind) >= 0; });
      if (!rs.length) return;
      any = true;
      kids.push(h('div', { class: 'rlgrp', 'data-rlg': g[0] }, [h('h3', { class: 'label' }, g[0] + ' · ' + rs.filter(function (r) { return !r.undone; }).length)].concat(rs.map(rlRule))));
    });
    var acts = (v.actions || []).slice(0, 10);
    if (acts.length) { any = true; kids.push(h('div', { class: 'rlgrp', 'data-rlg': 'Done to items' }, [h('h3', { class: 'label' }, 'Done to items')].concat(acts.map(rlAction)))); }
    if (!any) kids.push(h('p', { class: 'muted' }, 'No rules yet. Ask in Chat, like “Treat Lena’s Slack DMs as VIP” or “' + RL_ASK + '”.'));
    var rc = (RL.recent || []).slice(0, 12);   /* 0.22.0 Recent changes (actions.py's journal): labels only, Undo once */
    if (rc.length) kids.push(h('div', { class: 'rlgrp rcent', 'data-rlg': 'Recent changes' }, [h('h3', { class: 'label' }, 'Recent changes')].concat(rc.map(rlChange))));
    kids.push(RL.errEl, RL.said ? h('p', { class: 'muted', role: 'status' }, RL.said) : null);
    return h('section', { class: 'where rules', 'aria-labelledby': 'rules-h' }, kids);
  }
  function dayPeople() { return h('section', { class: 'where ppl', 'aria-labelledby': 'vip-h' }, [h('h2', { class: 'h3', id: 'vip-h' }, 'VIPs and replies')].concat(stepVips())); }

  /* ----- Connections: one policy line, the areas as chips (one area's accounts at a time), Signal, notes and code ----- */
  /* ---------- 0.22.0 Setup › Connections › Areas (mockup areas-v1, owner approved 2026-11-16): every area as a card
     (colour, name, connections), inline rename (Enter saves, Esc cancels), the palette of 10 and More colours, Move to…
     and desktop drag and drop ending in the same box, delete with where its connections go, Add an area, the line with
     Undo. Phone: menus and pickers as bottom sheets. textContent only (h()); every change is the server's (api/areas). */
  var AR = { b: null, loading: false, err: '', menu: null, cal: null, mv: null, pop: null, hex: null, edit: null, editV: '',
    more: null, del: null, add: null, done: false, flash: null, drag: null, tab: 'areas' };
  function aColour(a) {   /* {cls, style} for an area: a palette key (.ak-*) or a free colour (.ax with its two shades) */
    var sw = a && a.swatch, k = sw ? sw.key : (a && typeof a === 'object' ? a.colour : a);
    if (/^[1-4]$/.test(String(k))) return { cls: 'a' + clampColour(k), style: null };
    if (sw && sw.custom && /^#[0-9a-f]{6}$/.test(sw.light) && /^#[0-9a-f]{6}$/.test(sw.dark)) return { cls: 'ax', style: '--al:' + sw.light + ';--ad:' + sw.dark };
    return { cls: /^[a-z]{2,12}$/.test(String(k)) ? 'ak-' + k : 'a4', style: null };
  }
  function sty(el, s) {   /* inline custom properties through CSSOM (h() never writes a style attribute) */
    if (s) s.split(';').forEach(function (p) { var i = p.indexOf(':'); if (i > 0) el.style.setProperty(p.slice(0, i).trim(), p.slice(i + 1).trim()); });
    return el;
  }
  function adot(a, cls) { var c = aColour(a); return sty(h('span', { class: 'adot ' + c.cls + (cls ? ' ' + cls : ''), 'aria-hidden': 'true' }), c.style); }
  function arFlat(x) { return x.reduce(function (o, y) { return o.concat(y); }, []); }
  function arLoad(focus) {
    return api('GET', 'api/areas').then(function (b) { AR.b = b; AR.err = ''; }, function (e) { AR.err = e.message; })
      .then(function () { if (inPlace('connections')) renderSetup(focus ? '[data-fk="' + cssq(focus) + '"]' : null); });
  }
  function arAfter(focus, flash) {   /* after a change: the state (Today, the chips) and the board, then the Undo line */
    AR.menu = AR.cal = AR.mv = AR.more = AR.del = AR.add = AR.hex = null; AR.edit = null; AR.done = true; AR.flash = flash || null;
    return Promise.resolve(loadState()).then(function () { return arLoad(focus); });
  }
  function arClose() { AR.menu = AR.cal = AR.mv = AR.pop = AR.hex = AR.more = AR.del = null; }
  function arAll() { return AR.b ? AR.b.areas.reduce(function (o, a) { return o.concat(a.conns); }, []).concat(AR.b.loose) : []; }
  function arConn(id) {
    var all = arAll(), i, c;
    for (i = 0; i < all.length; i++) { c = all[i]; if (c.conn === id) return c; var k = (c.calendars || []).filter(function (x) { return x.conn === id; })[0]; if (k) return k; }
    return null;
  }
  function arArea(id) { return (AR.b ? AR.b.areas : []).filter(function (a) { return a.id === id; })[0]; }
  function arKey(label, cls, attrs, fn) { return btn('k' + (cls ? ' ' + cls : ''), label, attrs, fn); }
  function arSeg(opts, cur, fn) {
    return h('div', { class: 'arseg', role: 'group' }, opts.map(function (o) {
      return btn('', o[1], { 'aria-pressed': String(o[0] === cur), 'data-fk': 'ar-seg:' + o[0] }, function () { fn(o[0]); });
    }));
  }
  function arDoneLine() {
    var u = AR.done && AR.b && AR.b.last_undo, err = errBox();
    if (!u) return null;
    var undo = arKey('Undo', 'q', { 'data-fk': 'ar-undo' }, function () {
      run([undo], err, function () { return api('POST', 'api/areas/undo', { token: u.token }); }, function () { AR.done = false; AR.flash = null; return Promise.resolve(loadState()).then(function () { return arLoad('ar-add'); }); });
    });
    return h('div', { class: 'ardone', role: 'status' }, [h('span', { class: 'st' }, [h('i', null, '✓'), ' Done']), h('span', { class: 'what' }, u.label), undo, err]);
  }
  function arMoveBox(c, to, key) {
    var a = arArea(to), err = errBox(), past = AR.mv.past;
    var go = arKey(key || 'Move', 'go', { 'data-fk': 'ar-go' }, function () {
      run([go], err, function () { return api('POST', 'api/areas/move', { conn: c.conn, to: to, past: past }); }, function () { return arAfter('ar-undo', c.conn); });
    });
    return h('div', { class: 'armv', role: 'group', 'aria-label': 'Move' }, [
      h('div', { class: 'to' }, [h('span', { class: 'ar' }, 'Move to'), adot(a), a ? a.name : '']),
      h('ul', null, [h('li', null, [h('i', null, '✓'), h('span', null, c.name + ' belongs to ' + (a ? a.name : '') + '.')]),
        h('li', null, [h('i', null, '✓'), h('span', null, 'Its new ' + (c.kind === 'google' ? 'mail and events are' : c.kind === 'calendar' ? 'events are' : 'items are') + ' tagged ' + (a ? a.name : '') + ' from now on.')])]),
      c.past && c.past.n ? h('div', { class: 'past' }, [h('span', { class: 'fl' }, 'What it already read'),
        arSeg([['leave', 'Leave as it is'], ['move', 'Move it too']], past ? 'move' : 'leave', function (v) { AR.mv.past = v === 'move'; renderSetup('[data-fk="ar-seg:' + v + '"]'); }),
        h('span', { class: 'sub' }, c.past.text + '. People and projects keep their own area.')]) : null,
      h('div', { class: 'keys' }, [go, arKey('Cancel', 'q', { 'data-fk': 'ar-cancel' }, function () { AR.mv = null; renderSetup('[data-fk="' + cssq('ar-m:' + c.conn) + '"]'); })]), err]);
  }
  function arPick(c, to) { AR.menu = AR.cal = null; AR.mv = { conn: c.conn, to: to, past: true }; renderSetup('[data-fk="ar-go"]'); }
  function arMenuItems(c, phone) {   /* Move to: the areas (here now greyed), one calendar of a Google account, + New area… */
    var tgt = AR.cal ? arConn(AR.cal) : c, cls = phone ? 'opt' : 'mi';
    var items = AR.b.areas.map(function (a) {
      var here = a.id === tgt.area_id;
      return btn(cls + (here ? ' cur' : ''), [adot(a), a.name, here ? h('span', { class: 'r' }, 'here now') : null],
        { role: 'menuitem', 'aria-disabled': here ? 'true' : null, 'data-fk': 'ar-to:' + a.id }, function () { if (!here) arPick(tgt, a.id); });
    });
    var cals = !AR.cal && c.kind === 'google' && (c.calendars || []).length > 1 ? [h('hr'), h('div', { class: 'mh' }, 'Or one calendar')].concat(c.calendars.map(function (k) {
      return btn(cls, [h('span', { class: 'aric sm', 'aria-hidden': 'true' }, '▦'), k.name], { role: 'menuitem', 'data-fk': 'ar-cal:' + k.conn }, function () { AR.cal = k.conn; renderSetup('[data-fk^="ar-to:"]'); });
    })) : [];
    return [h('div', { class: 'mh' }, AR.cal ? 'Move ' + tgt.name + ' to' : 'Move to')].concat(items, cals, [h('hr'),
      btn(cls, '+ New area…', { role: 'menuitem', 'data-fk': 'ar-new' }, function () { arClose(); AR.add = { name: '', colour: arFree(), conn: tgt.conn }; renderSetup('#ar-add-name'); })]);
  }
  function arFree() { var used = AR.b.areas.map(function (a) { return a.colour; }); var f = AR.b.palette.filter(function (p) { return used.indexOf(p.key) < 0; })[0]; return (f || AR.b.palette[0]).key; }
  function arRow(c, a) {
    var phone = phoneMq.matches, open = AR.menu === c.conn, fl = AR.flash === c.conn;
    var mvk = arKey(phone ? 'Move' : 'Move to…', 'q mvk', { 'aria-haspopup': 'menu', 'aria-expanded': String(open), 'data-fk': 'ar-m:' + c.conn }, function () {
      var was = open; arClose(); AR.menu = was ? null : c.conn; renderSetup(was ? '[data-fk="' + cssq('ar-m:' + c.conn) + '"]' : '[data-fk^="ar-to:"]');
    });
    var row = h('div', { class: 'arcn' + (fl ? ' flash' : ''), draggable: phone ? null : 'true', 'data-conn': c.conn }, [
      h('span', { class: 'hd', 'aria-hidden': 'true' }, '⋮⋮'), h('span', { class: 'aric', 'aria-hidden': 'true' }, c.icon),
      h('span', { class: 'cm' }, [h('b', null, c.name), h('span', { class: 'sub' }, [fl ? h('span', { class: 'new' }, 'Just moved') : null, fl ? ' · ' : null, c.line])]),
      mvk, open && !phone ? h('div', { class: 'arpop armenu', role: 'menu', 'aria-label': 'Move ' + c.name }, arMenuItems(c, false)) : null]);
    if (!phone) {
      on(row, 'dragstart', function (e) { AR.drag = c.conn; arClose(); try { e.dataTransfer.setData('text/plain', c.conn); e.dataTransfer.effectAllowed = 'move'; } catch (x) { } setTimeout(function () { row.classList.add('ph'); }, 0); });
      on(row, 'dragend', function () { AR.drag = null; row.classList.remove('ph'); document.querySelectorAll('.arcard.drop').forEach(function (x) { x.classList.remove('drop'); }); });
    }
    var mv = AR.mv && (AR.mv.conn === c.conn) && !phone ? arMoveBox(c, AR.mv.to) : null;
    return [row, mv];
  }
  function arDropTarget(card, a) {
    function ok() { var c = AR.drag && arConn(AR.drag); return c && c.area_id !== a.id; }
    on(card, 'dragover', function (e) { if (!ok()) return; e.preventDefault(); if (!card.classList.contains('drop')) { document.querySelectorAll('.arcard.drop').forEach(function (x) { x.classList.remove('drop'); }); card.classList.add('drop'); } });
    on(card, 'dragleave', function (e) { if (!card.contains(e.relatedTarget)) card.classList.remove('drop'); });
    on(card, 'drop', function (e) { if (!ok()) return; e.preventDefault(); var c = AR.drag; AR.drag = null; arClose(); AR.mv = { conn: c, to: a.id, past: true }; renderSetup('[data-fk="ar-go"]'); });
  }
  function arSwatches(sel, fn, self) {
    return h('div', { class: 'arsws', role: 'radiogroup', 'aria-label': 'Colour' }, AR.b.palette.map(function (p) {
      var u = AR.b.areas.filter(function (x) { return x.colour === p.key && x.id !== self; })[0];
      return btn('arsw ak-' + p.key, [h('i', null, p.key === sel ? '✓' : ''), u ? h('span', { class: 'u' }) : null],
        { role: 'radio', 'aria-checked': String(p.key === sel), 'aria-pressed': String(p.key === sel), 'aria-label': p.name + (u ? ', used by ' + u.name : ''), 'data-fk': 'ar-sw:' + p.key }, function () { fn(p.key); });
    }));
  }
  function arColourBody(a, err) {
    var p = AR.b.palette.filter(function (x) { return x.key === a.colour; })[0], used = AR.b.areas.filter(function (x) { return x.id !== a.id; });
    var c = aColour(a);
    return [h('div', { class: 'cpick' }, [h('div', { class: 'nm' }, [p ? p.name : a.swatch.name, h('span', { class: 'ratio' }, p ? p.light_ratio.toFixed(1) + ':1 Light · ' + p.dark_ratio.toFixed(1) + ':1 Dark' : 'Your colour')]),
        h('div', { class: 'inuse' }, '○ In use: ' + used.map(function (x) { return x.swatch.name + ' · ' + x.name; }).join(', '))]),
      sty(h('div', { class: 'arprev ' + c.cls }, [h('span', { class: 'chipx' }, [adot(a), a.name]), h('span', { class: 'blk' }, [h('b', null, 'Design review'), ' 10:00 am']), h('span', { class: 'ink' }, a.name)]), c.style),
      h('div', { class: 'pfoot' }, [arKey('More colours…', 'q q0', { 'data-fk': 'ar-more-col' }, function () { AR.pop = null; AR.hex = { area: a.id, v: a.swatch.custom ? a.colour : '', r: null }; renderSetup('#ar-hex'); }), h('span', { class: 'sub' }, 'Saved as you pick')]), err];
  }
  function arSetColour(a, col, keepOpen) {
    var err = document.querySelector('.arpop .err, .arsheet .err');
    run([], err, function () { return api('PATCH', 'api/areas/' + encodeURIComponent(a.id), { colour: col }); }, function () {
      var pop = keepOpen ? a.id : null; return arAfter('ar-sw:' + col).then(function () { AR.pop = pop; renderSetup('[data-fk="' + cssq('ar-sw:' + col) + '"]'); });
    });
  }
  function arColourPop(a, phone) {
    var err = errBox(), sws = arSwatches(a.colour, function (k) { arSetColour(a, k, true); }, a.id);
    if (phone) return [h('div', { class: 'sh' }, ['Colour for ' + a.name, arKey('Done', 'q x', { 'data-fk': 'ar-pop-done' }, function () { AR.pop = null; renderSetup('[data-fk="' + cssq('ar-c:' + a.id) + '"]'); })]), sws].concat(arColourBody(a, err));
    return h('div', { class: 'arpop cpop', role: 'dialog', 'aria-label': 'Colour for ' + a.name }, [h('div', { class: 'lab' }, 'Colour'), sws].concat(arColourBody(a, err)));
  }
  var arHexT = null;
  function arHexPop(a, phone) {
    var x = AR.hex, r = x.r, err = errBox();
    var inp = on(h('input', { class: 'inp', id: 'ar-hex', type: 'text', maxlength: '7', value: x.v, autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Any colour, as a hex code', placeholder: '#2c6d7d', 'data-fk': 'ar-hex' }), 'input', function () {
      x.v = inp.value; clearTimeout(arHexT);
      arHexT = setTimeout(function () {
        if (!/^#?[0-9a-fA-F]{6}$/.test(x.v.trim())) { x.r = null; renderSetup(); return; }
        api('POST', 'api/areas/colour-check', { hex: x.v.trim() }).then(function (res) { x.r = res; x.e = ''; }, function (e) { x.r = null; x.e = e.message; }).then(function () { if (AR.hex === x) renderSetup(); });
      }, 250);
    });
    function side(cls, label, hex, ratio, rawRatio, raw) {
      var okr = raw === hex;
      /* 0.22.0: the shade on its own ground (Light on the light ground, Dark on the dark one), as the area's name */
      return h('div', { class: cls }, [h('span', { class: 'm' }, label + ' · ' + hex), h('span', { class: 's' }, [sty(h('span', { class: 'adot', 'aria-hidden': 'true' }), 'background:' + hex), sty(h('span', null, a.name), 'color:' + hex)]),
        h('span', { class: 'r' }, okr ? ratio.toFixed(1) + ':1 · ✓ reads well' : rawRatio.toFixed(1) + ':1 · ▲ uses ' + hex)]);
    }
    var body = [h('div', { class: 'hexrow' }, inp)];
    if (r && /^#[0-9a-f]{6}$/.test(r.light) && /^#[0-9a-f]{6}$/.test(r.dark)) {
      body.push(h('div', { class: 'hexprev' }, [side('lt', 'Light', r.light, r.light_ratio, r.raw_light, r.hex), side('dk', 'Dark', r.dark, r.dark_ratio, r.raw_dark, r.hex)]));
      r.warnings.forEach(function (w) { body.push(h('p', { class: 'arwarn' }, [h('i', { 'aria-hidden': 'true' }, '▲'), h('span', null, w)])); });
      var use = arKey(r.light === r.dark ? 'Use ' + r.light : 'Use ' + r.light + ' and ' + r.dark, 'go', { 'data-fk': 'ar-hex-use' }, function () {
        run([use], err, function () { return api('PATCH', 'api/areas/' + encodeURIComponent(a.id), { colour: r.hex }); }, function () { return arAfter('ar-c:' + a.id); });
      });
      body.push(h('div', { class: 'keys' }, [use, arKey('Back to the palette', 'q', { 'data-fk': 'ar-hex-back' }, function () { AR.hex = null; AR.pop = a.id; renderSetup('[data-fk="' + cssq('ar-sw:' + a.colour) + '"]'); })]));
    } else {
      body.push(h('p', { class: 'sub' }, x.e || 'Type a hex code. Callsheet checks it in Light and Dark and offers a shade that reads well.'));
      body.push(h('div', { class: 'keys' }, arKey('Back to the palette', 'q', { 'data-fk': 'ar-hex-back' }, function () { AR.hex = null; AR.pop = a.id; renderSetup(); })));
    }
    body.push(err);
    if (phone) return [h('div', { class: 'sh' }, 'More colours')].concat(body);
    return h('div', { class: 'arpop cpop hexp', role: 'dialog', 'aria-label': 'More colours for ' + a.name }, [h('div', { class: 'lab' }, 'More colours')].concat(body));
  }
  function arRename(a, inp, err) {
    var v = inp.value.trim();
    if (!AR.edit) return;
    if (v === a.name || !v) { AR.edit = null; renderSetup('[data-fk="' + cssq('ar-n:' + a.id) + '"]'); return; }
    AR.edit = null;
    run([], err, function () { return api('PATCH', 'api/areas/' + encodeURIComponent(a.id), { name: v }); }, function () { return arAfter('ar-n:' + a.id); })
      .then(function () { if (err.textContent) { AR.edit = a.id; AR.editV = v; renderSetup('#ar-name'); } });
  }
  function arDelete(a) {
    var others = AR.b.areas.filter(function (x) { return x.id !== a.id; }), d = AR.del, err = errBox(), n = a.conns.length;
    var hasAcc = a.conns.some(function (c) { return c.kind === 'google' || c.kind === 'slack'; });
    var go = arKey('Delete area', 'dark', { 'data-fk': 'ar-del-go' }, function () {
      run([go], err, function () { return api('DELETE', 'api/areas/' + encodeURIComponent(a.id), d.how === 'disconnect' ? { disconnect: true } : { to: d.to }); },
        function () { return d.how === 'disconnect' ? (AR.del = null, AR.done = false, Promise.resolve(loadState()).then(function () { return arLoad('ar-add'); })) : arAfter('ar-undo'); });
    });
    function rad(id, label, kids) {
      return h('div', { class: 'arrad' }, [btn('o' + (d.how === id ? ' on' : ''), null, { role: 'radio', 'aria-checked': String(d.how === id), 'aria-label': label, 'data-fk': 'ar-del:' + id }, function () { d.how = id; renderSetup('[data-fk="ar-del:' + id + '"]'); }), h('b', null, label)].concat(kids));
    }
    return h('div', { class: 'arconfirm', role: 'group', 'aria-label': 'Delete ' + a.name }, [h('span', { class: 'q' }, 'Delete ' + a.name + '?'),
      h('span', { class: 'sub' }, n ? 'Where should ' + (n === 1 ? a.conns[0].name : 'its ' + n + ' connections') + ' go?' : 'Nothing is in it.'),
      n ? rad('move', 'Move ' + (n === 1 ? 'it' : 'them') + ' to', [h('div', { class: 'achips' }, others.map(function (o) {
        return btn('achip', [adot(o), o.name], { 'aria-pressed': String(d.to === o.id), 'data-fk': 'ar-delto:' + o.id }, function () { d.to = o.id; d.how = 'move'; renderSetup('[data-fk="' + cssq('ar-delto:' + o.id) + '"]'); });
      })), h('span', { class: 'sub' }, 'With ' + (n === 1 ? 'its' : 'their') + ' past items.')]) : null,
      n && hasAcc ? rad('disconnect', 'Disconnect ' + (n === 1 ? 'it' : 'them'), [h('span', { class: 'sub' }, 'Callsheet signs out of its accounts and forgets their messages. This can’t be undone.')]) : null,
      h('div', { class: 'keys' }, [go, arKey('Keep it', 'q', { 'data-fk': 'ar-del-keep' }, function () { AR.del = null; renderSetup('[data-fk="' + cssq('ar-x:' + a.id) + '"]'); })]), err]);
  }
  function arMoreMenu(a) {
    function item(label, fk, fn) { return btn('mi', label, { role: 'menuitem', 'data-fk': fk }, fn); }
    return h('div', { class: 'arpop armenu right', role: 'menu', 'aria-label': 'More for ' + a.name }, [
      item('Rename', 'ar-ren:' + a.id, function () { arClose(); AR.edit = a.id; AR.editV = a.name; renderSetup('#ar-name'); }),
      item('Colour', 'ar-col:' + a.id, function () { arClose(); AR.pop = a.id; renderSetup('[data-fk="' + cssq('ar-sw:' + a.colour) + '"]'); }),
      a.fixed ? null : h('hr'),
      a.fixed ? null : item('Delete area', 'ar-delm:' + a.id, function () { arClose(); AR.del = { area: a.id, how: 'move', to: 'personal' }; renderSetup('[data-fk="ar-del-go"]'); })]);
  }
  function arCard(a) {
    var c = aColour(a), phone = phoneMq.matches, err = errBox(), head;
    var cb = btn('arcb', adot(a, 'big'), { 'aria-label': 'Colour: ' + a.swatch.name + '. Change', 'aria-expanded': String(AR.pop === a.id || (AR.hex && AR.hex.area === a.id) || false), 'data-fk': 'ar-c:' + a.id }, function () {
      var was = AR.pop === a.id; arClose(); AR.pop = was ? null : a.id; renderSetup(was ? '[data-fk="' + cssq('ar-c:' + a.id) + '"]' : '[data-fk="' + cssq('ar-sw:' + a.colour) + '"]');
    });
    var pop = phone ? null : AR.pop === a.id ? arColourPop(a, false) : AR.hex && AR.hex.area === a.id ? arHexPop(a, false) : AR.more === a.id ? arMoreMenu(a) : null;
    if (AR.edit === a.id) {
      var inp = h('input', { class: 'inp', id: 'ar-name', type: 'text', maxlength: '40', value: AR.editV, autocomplete: 'off', 'aria-label': 'Name of ' + a.name, 'data-fk': 'ar-name' });
      on(inp, 'input', function () { AR.editV = inp.value; });
      on(inp, 'keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); arRename(a, inp, err); }
        else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); AR.edit = null; renderSetup('[data-fk="' + cssq('ar-n:' + a.id) + '"]'); }
      });
      on(inp, 'blur', function () { if (AR.edit === a.id) setTimeout(function () { if (AR.edit === a.id) arRename(a, inp, err); }, 0); });
      head = [cb, h('span', { class: 'edit' }, [inp, h('span', { class: 'hint' }, [h('span', { class: 'kh' }, '↵'), ' saves · ', h('span', { class: 'kh' }, 'esc'), ' cancels'])]), pop];
    } else {
      head = [cb, btn('arnm', [a.name, h('span', { class: 'pen', 'aria-hidden': 'true' }, '✎')], { 'aria-label': 'Rename ' + a.name, 'data-fk': 'ar-n:' + a.id }, function () { arClose(); AR.edit = a.id; AR.editV = a.name; renderSetup('#ar-name'); }),
        h('span', { class: 'cnt' }, String(a.conns.length)),
        btn('armore', '⋯', { 'aria-label': 'More for ' + a.name, 'aria-haspopup': 'menu', 'aria-expanded': String(AR.more === a.id), 'data-fk': 'ar-x:' + a.id }, function () { var was = AR.more === a.id; arClose(); AR.more = was ? null : a.id; renderSetup(was ? '[data-fk="' + cssq('ar-x:' + a.id) + '"]' : '[data-fk="' + cssq('ar-ren:' + a.id) + '"]'); }), pop];
    }
    var rows = a.conns.length ? arFlat(a.conns.map(function (x) { return arRow(x, a); })) : [h('p', { class: 'none' }, 'Nothing here yet. Move a connection in, or connect one in Accounts.')];
    var card = h('section', { class: 'arcard ' + c.cls, 'aria-label': a.name, 'data-area': a.id }, [h('div', { class: 'arh' }, head), err].concat(rows,
      [AR.del && AR.del.area === a.id ? arDelete(a) : null, h('div', { class: 'ardrop' }, 'Drop to move to ' + a.name)]));
    sty(card, c.style);
    if (!phone) arDropTarget(card, a);
    return card;
  }
  function arAddForm() {
    if (!AR.add.colour) AR.add.colour = arFree();
    var f = AR.add, err = errBox(), p = AR.b.palette.filter(function (x) { return x.key === f.colour; })[0];
    var inp = on(h('input', { class: 'inp', id: 'ar-add-name', type: 'text', maxlength: '40', value: f.name, autocomplete: 'off', 'data-fk': 'ar-add-name' }), 'input', function () { f.name = inp.value; });
    var go = arKey('Add area', 'go', { 'data-fk': 'ar-add-go' }, function () {
      if (!f.name.trim()) { err.textContent = 'Give the area a name.'; inp.focus(); return; }
      run([go], err, function () {
        return api('POST', 'api/areas', { name: f.name.trim(), colour: f.colour }).then(function (r) {
          return f.conn ? api('POST', 'api/areas/move', { conn: f.conn, to: r.id, past: true }) : r;
        });
      }, function () { return arAfter('ar-undo', f.conn); });
    });
    on(inp, 'keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); go.click(); } else if (e.key === 'Escape') { AR.add = null; renderSetup('[data-fk="ar-add"]'); } });
    var used = AR.b.areas.some(function (a) { return a.colour === f.colour; });
    return h('section', { class: 'arcard addf ak-' + f.colour, 'aria-label': 'New area' }, [
      h('div', { class: 'arh' }, [h('span', { class: 'arcb' }, adot(f.colour, 'big')), h('span', { class: 'arnm' }, 'New area')]),
      h('label', { class: 'fl', for: 'ar-add-name' }, 'Name'), inp, h('span', { class: 'fl' }, 'Colour'),
      arSwatches(f.colour, function (k) { f.colour = k; renderSetup('[data-fk="ar-sw:' + k + '"]'); }, null),
      h('span', { class: 'sub' }, (p ? p.name : '') + (used ? ' is in use too.' : ' is free.') + ' ○ marks colours already in use.'),
      f.conn && arConn(f.conn) ? h('span', { class: 'sub' }, arConn(f.conn).name + ' moves into it.') : null,
      h('div', { class: 'keys' }, [go, arKey('Cancel', 'q', { 'data-fk': 'ar-add-cancel' }, function () { AR.add = null; renderSetup('[data-fk="ar-add"]'); })]), err]);
  }
  function arSheet() {   /* phone: Move to…, the box, the colour picker and More colours as a bottom sheet */
    var c = AR.menu && arConn(AR.menu), m = AR.mv && arConn(AR.mv.conn), a = arArea(AR.pop || (AR.hex && AR.hex.area)), kids = null, label = '';
    if (c) { label = 'Move ' + c.name; kids = [h('div', { class: 'sh' }, label), h('p', { class: 'sub' }, c.line)].concat(arMenuItems(c, true), [h('div', { class: 'keys' }, arKey('Cancel', 'q q0', { 'data-fk': 'ar-sh-x' }, function () { arClose(); renderSetup('[data-fk="' + cssq('ar-m:' + c.conn) + '"]'); }))]); }
    else if (m) { label = 'Move ' + m.name; kids = [h('div', { class: 'sh' }, label), arMoveBox(m, AR.mv.to)]; }
    else if (a && AR.pop) { label = 'Colour for ' + a.name; kids = arColourPop(a, true); }
    else if (a && AR.hex) { label = 'More colours'; kids = arHexPop(a, true); }
    else if (AR.more && arArea(AR.more)) { label = 'More'; kids = [arMoreMenu(arArea(AR.more))]; }
    if (!kids) return null;
    return h('div', { class: 'arsheetw' }, [btn('arscrim', null, { 'aria-label': 'Close', tabindex: '-1' }, function () { arClose(); renderSetup(); }),
      h('div', { class: 'arsheet', role: 'dialog', 'aria-modal': 'true', 'aria-label': label }, [h('div', { class: 'grab', 'aria-hidden': 'true' })].concat(kids))]);
  }
  function areasTab() {
    if (!AR.b) {
      if (!AR.loading) { AR.loading = true; arLoad().then(function () { AR.loading = false; }); }
      return [h('p', { class: 'muted', role: 'status' }, AR.err || 'Loading…')];
    }
    var loose = AR.b.loose.length ? h('section', { class: 'arcard loose' }, [h('div', { class: 'lab' }, ['Not in an area', h('span', { class: 'r sub' }, 'Shown under every area')]),
      h('div', { class: 'cns' }, arFlat(AR.b.loose.map(function (x) { return arRow(x, null); })))]) : null;
    var grid = h('div', { class: 'agrid' }, AR.b.areas.map(arCard).concat([AR.add ? arAddForm() : btn('araddc', '+ Add an area', { 'data-fk': 'ar-add' }, function () { arClose(); AR.add = { name: '', colour: arFree(), conn: null }; renderSetup('#ar-add-name'); }), loose]));
    return h('div', { class: 'arz' }, [h('p', { class: 'intro' }, 'Areas sort Today, Agenda and Replies. Each connection belongs to one.'), arDoneLine(), grid, AR.err ? h('p', { class: 'err', role: 'alert' }, AR.err) : null, phoneMq.matches ? arSheet() : null]);
  }
  document.addEventListener('keydown', function (e) {   /* Esc closes an open menu, picker or box on the Areas tab */
    if (e.key !== 'Escape' || !inPlace('connections') || AR.tab !== 'areas' || AR.edit) return;
    if (AR.menu || AR.mv || AR.pop || AR.hex || AR.more || AR.del) { arClose(); renderSetup(); }
  });
  function connTabs() {
    return h('div', { class: 'arsub', role: 'tablist', 'aria-label': 'Connections' }, [['areas', 'Areas'], ['accounts', 'Accounts'], ['connectors', 'Connectors']].map(function (t) {
      return btn('', t[1], { role: 'tab', 'aria-selected': String(AR.tab === t[0]), 'aria-current': AR.tab === t[0] ? 'page' : null, 'data-fk': 'ctab:' + t[0] }, function () {
        AR.tab = t[0]; arClose(); AR.done = false; if (t[0] === 'areas') AR.b = null; renderSetup('[data-fk="ctab:' + t[0] + '"]');
      });
    }));
  }
  function placeConnections() {
    var areas = S.areas, sel = areas.some(function (a) { return a.id === ui.areaSel; }) ? ui.areaSel : (areas[0] || {}).id;
    var chips = h('div', { class: 'chips areachips', role: 'group', 'aria-label': 'Areas' }, areas.map(function (a) {
      var n = (a.accounts || []).length + (a.slack || []).length + (a.signal ? 1 : 0);
      return btn('chip', [adot(a), a.name, h('span', { class: 'cl' }, ' · ' + n)],
        { 'aria-pressed': String(a.id === sel), 'data-fk': 'achip:' + a.id }, function () {
          ui.areaSel = a.id; renderSetup('[data-fk="' + cssq('achip:' + a.id) + '"]');
        });
    }).concat([btn('chip', '+ Add area', { 'data-fk': 'achip:new' }, function () {   /* 0.22.0: in Areas, with the palette */
      AR.tab = 'areas'; arClose(); AR.done = false; AR.add = { name: '', colour: null, conn: null }; AR.b = null; renderSetup('[data-fk="ctab:areas"]');
    })]));
    var cur = areas.filter(function (a) { return a.id === sel; })[0], tabs = connTabs();
    /* 0.22.0: Areas first (the area cards), then Accounts (as before) and Connectors */
    if (AR.tab === 'areas') return [tabs].concat(areasTab());
    if (AR.tab === 'connectors') return [tabs,
      connectorsSlot(),  /* 0.10.0 connectors */
      voiceSection()];  /* 0.21.0 the voice assistant's read-only API */
    return [tabs,
      sourcesPanel(),  /* 0.15.0: each source's freshness, first */
      h('p', { class: 'intro' }, 'What Callsheet may read, by area. Check your employer’s policy before adding work accounts.'),
      S.google_client.configured ? null : keyPanel(),
      chips,
      cur ? areaCard(cur) : null,
      signalCard(),
      textsSection(),  /* 0.16.1 texts */
      otherSources(),
      h('div', { class: 'callout' }, [h('div', { class: 'h' }, 'Always'),
        h('p', null, 'The brief reads and drafts. Nothing is sent from here except an RSVP you press. It never accepts or deletes anything.')])];
  }
  /* ---------- 0.15.0 Setup › Connections › Sources: how fresh each source is (GET api/sources). One row per source,
     status as shape + word; a source that stops advancing or keeps failing says so plainly, in a dashed box. Loaded when
     Connections is drawn and older than a minute (like Web search), so nothing re-renders under the owner's hands. ---------- */
  var SRCF = { v: null, at: 0, loading: false, err: null };
  var SRCF_ICON = { signal: 'signal', gmail: 'mail', calendar: 'cal', slack: 'slack', sms: 'chat', sms_live: 'chat', contacts: 'areas' };
  function srcfLoad() {
    SRCF.loading = true;
    api('GET', 'api/sources').then(function (v) { SRCF.v = v; SRCF.err = null; }, function (e) { SRCF.err = e.message; })
      .then(function () {
        SRCF.loading = false; SRCF.at = Date.now();
        if (!inPlace('connections') && !(inPlace('advanced') && ui.adv.messages)) return;
        var ae = document.activeElement, fk = ae && ae.getAttribute && ae.getAttribute('data-fk');
        renderSetup(fk ? '[data-fk="' + cssq(fk) + '"]' : null);
      });
  }
  function sourcesPanel() {
    if (!SRCF.loading && (!SRCF.at || Date.now() - SRCF.at > 60000)) srcfLoad();
    var v = SRCF.v, head = h('h2', { class: 'h3', id: 'srcf-h' }, 'Sources');
    if (!v) return h('section', { class: 'where srcf', 'aria-labelledby': 'srcf-h' }, [head,
      SRCF.err ? h('p', { class: 'err', role: 'alert' }, 'Couldn’t load the sources (' + SRCF.err + ').') : h('p', { class: 'muted' }, 'Loading…')]);
    var rows = v.sources || [];
    return h('section', { class: 'where srcf', 'aria-labelledby': 'srcf-h' }, [head,
      h('p', { class: 'note' }, '“Checked” is when Callsheet last looked. “Read up to” is the newest message it has taken in.'),
      h('div', { class: 'srows' }, rows.map(srcRow))]);
  }
  function srcRow(r) {
    var bad = r.state === 'behind' || r.state === 'error';
    return h('div', { class: 'srow' + (bad ? ' bad' : ''), 'data-src': r.key }, [
      h('span', { class: 'si' }, ico(SRCF_ICON[r.key] || 'note')),
      h('b', { class: 'sn' }, r.name),
      h('span', { class: 'sst ' + r.state }, r.word),
      h('span', { class: 'sub' }, r.line),
      bad && r.warn ? h('p', { class: 'swarn' }, r.warn) : null]);
  }
  /* Advanced › Message history: how far back mail is read the first time (PATCH api/sources/settings) */
  function advMessages() {
    if (!SRCF.v) { if (!SRCF.loading) srcfLoad(); return h('p', { class: 'note', role: 'status' }, SRCF.err || 'Loading…'); }
    var st = SRCF.v.settings, kept = SRCF.v.store || {};
    var sel = on(h('select', { class: 'inp', id: 'msg-days', 'data-fk': 'msg-days' }, st.choices.map(function (d) {
      return h('option', { value: String(d), selected: d === st.backfill_days ? true : null }, d + ' days');
    })), 'change', function () {
      api('PATCH', 'api/sources/settings', { backfill_days: +sel.value }).then(function (v) { SRCF.v = v; SRCF.err = null; SRCF.at = Date.now(); },
        function (e) { SRCF.err = e.message; }).then(function () { renderSetup('[data-fk="msg-days"]'); });
    });
    return [h('p', { class: 'note' }, 'How far back Callsheet reads mail the first time. Everything it reads is kept, sent and received.'),
      h('label', { class: 'fl', for: 'msg-days' }, 'Read back'), sel,
      h('p', { class: 'muted' }, (kept.messages || 0).toLocaleString() + ' messages kept · ' + Math.max(1, Math.round((kept.bytes || 0) / 1048576)).toLocaleString() + ' MB')];
  }
  /* ---------- 0.16.1 Setup › Connections › Texts (GET/PATCH api/texts, GET api/texts/sensors): the backup folder, the
     phone's Last notification sensor (asked from Home Assistant once when the section is drawn) and the messaging apps
     to keep. Read-only; nothing here sends. ---------- */
  var TX = { v: null, at: 0, loading: false, err: null, sens: null, sensLoading: false, sensErr: null, draft: null };
  function txRedraw(fk) {
    if (!inPlace('connections')) return;
    var ae = document.activeElement, k = fk || (ae && ae.getAttribute && ae.getAttribute('data-fk'));
    renderSetup(k ? '[data-fk="' + cssq(k) + '"]' : null);
  }
  function txLoad() {
    TX.loading = true;
    api('GET', 'api/texts').then(function (v) { TX.v = v; TX.err = null; }, function (e) { TX.err = e.message; })
      .then(function () { TX.loading = false; TX.at = Date.now(); txRedraw(); });
  }
  function txSensors() {
    TX.sensLoading = true;
    api('GET', 'api/texts/sensors').then(function (r) { TX.sens = r.sensors || []; TX.sensErr = r.error || null; },
      function (e) { TX.sens = []; TX.sensErr = e.message; }).then(function () { TX.sensLoading = false; txRedraw(); });
  }
  function textsSection() {
    if (!TX.loading && (!TX.at || Date.now() - TX.at > 60000)) txLoad();
    var v = TX.v, head = h('h2', { class: 'h3', id: 'tx-h' }, 'Texts');
    if (!v) return h('section', { class: 'where texts', 'aria-labelledby': 'tx-h' }, [head,
      TX.err ? h('p', { class: 'err', role: 'alert' }, 'Couldn’t load texts (' + TX.err + ').') : h('p', { class: 'muted' }, 'Loading…')]);
    if (v.available && TX.sens === null && !TX.sensLoading) txSensors();
    var st = v.settings, err = errBox();
    function patch(body, fk, els) {
      run(els || [], err, function () { return api('PATCH', 'api/texts', body); }, function (r) {
        TX.v = r; TX.draft = null; SRCF.at = 0; txRedraw(fk);
      });
    }
    var folder = h('input', { class: 'inp', id: 'tx-folder', value: TX.draft != null ? TX.draft : st.folder, placeholder: v.folder_hint,
      autocomplete: 'off', spellcheck: 'false', 'data-fk': 'tx-folder' });
    on(folder, 'input', function () { TX.draft = folder.value; });
    var save = btn('btn sm', 'Save', { 'data-fk': 'tx-save' }, function () { patch({ folder: folder.value.trim() }, 'tx-save', [save]); });
    var opts = [h('option', { value: '' }, 'Off')], ids = {};
    (TX.sens || []).forEach(function (x) { ids[x.id] = 1; opts.push(h('option', { value: x.id, selected: x.id === st.sensor ? true : null }, x.name + (x.available ? '' : ' (unavailable)'))); });
    if (st.sensor && !ids[st.sensor]) opts.push(h('option', { value: st.sensor, selected: true }, st.sensor));
    var sel = on(h('select', { class: 'inp', id: 'tx-sensor', 'data-fk': 'tx-sensor' }, opts), 'change', function () {
      patch({ sensor: sel.value }, 'tx-sensor', [sel]);
    });
    var apps = h('ul', { class: 'cals txapps', 'aria-label': 'Apps to keep' }, v.apps.map(function (a) {
      var id = 'tx-app-' + a.id.replace(/\W/g, '-');
      var box = on(h('input', { type: 'checkbox', id: id, checked: st.apps.indexOf(a.id) >= 0 ? true : null, 'data-fk': id }), 'change', function () {
        var want = v.apps.filter(function (x) { var b = apps.querySelector('#tx-app-' + x.id.replace(/\W/g, '-')); return b && b.checked; })
          .map(function (x) { return x.id; });
        patch({ apps: want }, id, []);
      });
      return h('li', null, h('label', { for: id, class: 'calrow' }, [box, h('span', null, a.name)]));
    }));
    var live = !v.available ? h('p', { class: 'note' }, 'Live texts work when Callsheet runs in Home Assistant.')
      : [h('label', { class: 'fl', for: 'tx-sensor' }, 'Live from'), sel,
        TX.sensLoading ? h('p', { class: 'muted', role: 'status' }, 'Looking for phones…') : null,
        TX.sensErr ? h('p', { class: 'err', role: 'alert' }, TX.sensErr) : null,
        TX.sens && !TX.sens.length && !TX.sensErr ? h('p', { class: 'note' }, 'No phone has its Last notification sensor on. Turn it on in the Home Assistant app: Settings › Companion app › Manage sensors.') : null,
        h('p', { class: 'fl', id: 'tx-apps-h' }, 'Keep notifications from'), apps];
    return h('section', { class: 'where texts', 'aria-labelledby': 'tx-h' }, [head,
      h('p', { class: 'note' }, 'Reads your phone’s texts: the nightly SMS Backup & Restore file, and new ones as they arrive. Nothing is sent from here except an RSVP you press.'),
      h('label', { class: 'fl', for: 'tx-folder' }, 'Backup folder'),
      h('div', { class: 'txrow' }, [folder, save]),
      live, err]);
  }
  /* ---------- 0.21.0 Setup › Connections › Voice assistant (GET/PATCH api/voice, POST api/voice/tokens, DELETE
     api/voice/tokens/<id>; voiceapi.py, docs/voice-api.md): the read-only API for the phone voice assistant. Off by
     default. A device token is shown once (text and QR code) and kept only as a hash; revoke any time. ---------- */
  var VA = { v: null, at: 0, loading: false, err: null, made: null, name: '', scopes: null, hosts: null };
  var VA_SCOPE = { 'messages:read': 'Messages', 'ledger:read': 'Needs you and coming up', 'calendar:read': 'Calendar',
    'people:read': 'People', 'brief:read': 'Morning brief', 'status:read': 'Sources status' };
  function vaRedraw(fk) {
    if (!inPlace('connections')) return;
    var ae = document.activeElement, k = fk || (ae && ae.getAttribute && ae.getAttribute('data-fk'));
    renderSetup(k ? '[data-fk="' + cssq(k) + '"]' : null);
  }
  function vaLoad() {
    VA.loading = true;
    api('GET', 'api/voice').then(function (v) { VA.v = v; VA.err = null; }, function (e) { VA.err = e.message; })
      .then(function () { VA.loading = false; VA.at = Date.now(); vaRedraw(); });
  }
  function vaWhen(ts) { return ts ? fmtWhen(ts * 1000, true) : ''; }
  function voiceSection() {
    if (!VA.loading && (!VA.at || Date.now() - VA.at > 60000) && !VA.made) vaLoad();
    var v = VA.v, head = h('h2', { class: 'h3', id: 'va-h' }, 'Voice assistant');
    /* the one plain line stays whole (class vaplain, not note: notes fold behind "?") */
    var line = h('p', { class: 'vaplain' }, 'Read-only. Reached over your home network or Tailscale; never put it behind Cloudflare.');
    if (!v) return h('section', { class: 'where vapi', 'aria-labelledby': 'va-h' }, [head, line,
      VA.err ? h('p', { class: 'err', role: 'alert' }, 'Couldn’t load the voice assistant settings (' + VA.err + ').') : h('p', { class: 'muted' }, 'Loading…')]);
    var enabled = v.settings.enabled, err = errBox();
    var s = sw(enabled, 'Voice assistant API', function () {
      run([s], err, function () { return api('PATCH', 'api/voice', { enabled: !enabled }); }, function (r) { VA.v = r; vaRedraw('va-on'); });
    });
    s.setAttribute('data-fk', 'va-on');
    var state = !enabled ? 'API for your voice assistant' : v.running ? 'Listening on port ' + v.port : 'Not listening';
    var kids = [head, line,
      h('div', { class: 'swrow' }, [s, h('span', { 'aria-hidden': 'true' }, state)]),
      enabled && v.error ? h('p', { class: 'err', role: 'alert' }, v.error) : null, err];
    if (VA.made) kids.push(vaMade());
    var live = v.tokens.filter(function (t) { return !t.revoked; });
    kids.push(h('h3', { class: 'label', id: 'va-dev-h' }, 'Devices'));
    kids.push(live.length ? h('ul', { class: 'vadevs', 'aria-labelledby': 'va-dev-h' }, live.map(vaDevice))
      : h('p', { class: 'muted' }, 'No devices yet.'));
    kids.push(vaAdd(v));
    kids.push(vaMore(v));
    return h('section', { class: 'where vapi', 'aria-labelledby': 'va-h' }, kids);
  }
  function vaDevice(t) {
    var key = 'va:' + t.id, err = errBox();
    var scopes = t.scopes.length === Object.keys(VA_SCOPE).length ? 'Everything, read-only' : t.scopes.map(function (x) { return VA_SCOPE[x] || x; }).join(', ');
    return h('li', { class: 'vadev' }, [
      h('div', { class: 'vadev-t' }, [h('b', null, t.name),
        h('span', { class: 'muted' }, scopes + ' · ' + (t.last_used ? 'Last used ' + vaWhen(t.last_used) : 'Not used yet'))]),
      ui.confirm === key ? confirmRow('Revoke “' + t.name + '”? That device stops working at once.', 'Revoke', function (y, c, e) {
        run([y, c], e, function () { return api('DELETE', 'api/voice/tokens/' + t.id); }, function (r) { ui.confirm = null; VA.v = r; vaRedraw('va-add'); });
      }) : askBtn(key, 'Revoke'), err]);
  }
  function vaAdd(v) {
    if (!VA.scopes) VA.scopes = v.scopes.slice();
    var err = errBox();
    var name = h('input', { class: 'inp', id: 'va-name', value: VA.name, placeholder: 'Phone assistant', maxlength: '40', autocomplete: 'off', 'data-fk': 'va-name' });
    on(name, 'input', function () { VA.name = name.value; });
    var boxes = h('ul', { class: 'cals vascopes', 'aria-labelledby': 'va-sc-h' }, v.scopes.map(function (sc) {
      var id = 'va-sc-' + sc.replace(/\W/g, '-');
      var box = on(h('input', { type: 'checkbox', id: id, checked: VA.scopes.indexOf(sc) >= 0 ? true : null, 'data-fk': id }), 'change', function () {
        VA.scopes = v.scopes.filter(function (x) { var b = boxes.querySelector('#va-sc-' + x.replace(/\W/g, '-')); return b && b.checked; });
      });
      return h('li', null, h('label', { for: id, class: 'calrow' }, [box, h('span', null, VA_SCOPE[sc] || sc)]));
    }));
    var make = btn('btn sm', 'Make a token', { 'data-fk': 'va-add' }, function () {
      run([make], err, function () { return api('POST', 'api/voice/tokens', { name: VA.name.trim(), scopes: VA.scopes }); }, function (r) {
        VA.made = r.new; r.new = undefined; VA.v = r; VA.name = ''; VA.scopes = null; vaRedraw('va-done');
      });
    });
    return h('div', { class: 'vaadd' }, [
      h('label', { class: 'fl', for: 'va-name' }, 'Add a device'), name,
      h('p', { class: 'fl', id: 'va-sc-h' }, 'It may read'), boxes,
      h('div', { class: 'btns' }, [make]), err]);
  }
  function vaMade() {
    var m = VA.made, out = h('div', { class: 'vamsg' });
    var img = h('img', { class: 'sigqr', alt: 'QR code of the token for ' + m.token.name + '.', width: '232', height: '232' });
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(String(m.qr_svg || ''));
    var code = h('code', { class: 'vatok', 'data-fk': 'va-tok', tabindex: '0' }, m.secret);
    var copy = btn('btn sm', 'Copy', { 'data-fk': 'va-copy' }, function () { copyText(m.secret, copy, 'Copy', out); });
    var done = btn('tbtn', 'Done', { 'data-fk': 'va-done' }, function () { VA.made = null; vaRedraw('va-add'); });
    return h('div', { class: 'vamade', role: 'group', 'aria-label': 'New token for ' + m.token.name }, [
      h('p', { class: 'vawarn' }, 'Shown once. Copy it into the assistant now: Callsheet keeps only a hash.'),
      h('div', { class: 'sigqrw' }, [h('div', { class: 'sigqrf' }, img),
        h('div', null, [h('p', { class: 'fl' }, m.token.name), code, h('div', { class: 'btns' }, [copy, done]), out])])]);
  }
  function vaMore(v) {
    var err = errBox();
    var hosts = h('input', { class: 'inp', id: 'va-hosts', value: VA.hosts != null ? VA.hosts : v.settings.hosts.join(', '),
      placeholder: 'nas.example.home', autocomplete: 'off', spellcheck: 'false', 'data-fk': 'va-hosts' });
    on(hosts, 'input', function () { VA.hosts = hosts.value; });
    var save = btn('btn sm', 'Save', { 'data-fk': 'va-hsave' }, function () {
      run([save], err, function () { return api('PATCH', 'api/voice', { hosts: hosts.value }); }, function (r) { VA.v = r; VA.hosts = null; vaRedraw('va-hsave'); });
    });
    var rows = (v.audit || []).slice(0, 20);
    return h('details', { class: 'vamore' }, [h('summary', null, 'Host names and recent calls'),
      h('p', { class: 'note' }, 'IP addresses, Tailscale names and one-word names always work. Add other names here.'),
      h('label', { class: 'fl', for: 'va-hosts' }, 'Also answer to'),
      h('div', { class: 'txrow' }, [hosts, save]), err,
      h('p', { class: 'fl' }, 'Recent calls (counts only, kept ' + v.audit_days + ' days)'),
      rows.length ? h('ul', { class: 'vaaudit' }, rows.map(function (a) {
        return h('li', null, [h('span', { class: 'muted' }, vaWhen(a.at)), h('span', null, (a.name || 'No token') + ' · ' + a.endpoint),
          h('span', { class: 'muted' }, a.status + (a.status === 200 ? ' · ' + a.count + ' result' + (a.count === 1 ? '' : 's') : ''))]);
      })) : h('p', { class: 'muted' }, 'No calls yet.')]);
  }
  function otherSources() {
    function row(name, line, fk, fn) {
      return h('div', { class: 'lrow src' }, [h('span', null, [h('b', null, name), h('span', { class: 'muted' }, ' · ' + line)]),
        btn('btn sm', 'Open', { 'data-fk': fk, 'aria-label': 'Open ' + name }, fn)]);
    }
    return h('section', { class: 'where srcs', 'aria-labelledby': 'src-h' }, [h('h2', { class: 'h3', id: 'src-h' }, 'Notes and code'),
      row('Notes folders', 'Obsidian and Google Drive, read in Get to know me', 'src-notes', function () { location.hash = 'knowme'; }),
      row('GitHub', 'repos for Projects, read-only', 'src-gh', function () { PJ.view = 'setup'; PJ.open = null; location.hash = 'projects'; })]);
  }

  /* ----- AI: placeAI (above, with the model server and the jobs), the Chat tab switch on this device ----- */
  var KEY_CHATOFF = 'callsheet.chatoff', KEY_SOUND = 'callsheet.sound';
  function chatOff() { return load(KEY_CHATOFF) === '1'; }
  /* ---------- 0.14.0 Setup › AI › Web search: the owner's SearXNG (GET/PATCH api/web-search, POST api/web-search/check).
     Check saves a new address (with today's date, the approval record) and asks SearXNG once for "test". ---------- */
  var WS = { v: null, at: 0, loading: false, err: null, res: null, draft: null, want: false, say: '' };
  var WS_EG = 'http://192.168.1.50:8888';
  function wsLoad() {
    WS.loading = true;
    api('GET', 'api/web-search').then(function (v) { WS.v = v; WS.err = null; }, function (e) { WS.err = e.message; })
      .then(function () {
        WS.loading = false; WS.at = Date.now();
        if (!inPlace('ai')) return;
        var ae = document.activeElement, fk = ae && ae.getAttribute && ae.getAttribute('data-fk');
        renderSetup(fk ? '[data-fk="' + cssq(fk) + '"]' : null);
      });
  }
  function fmtDay(d) {
    if (!d) return '';
    var x = new Date(d + 'T12:00:00Z');
    return isNaN(x.getTime()) ? d : x.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  }
  function aiWeb() {
    if (!WS.loading && (!WS.at || Date.now() - WS.at > 60000)) wsLoad();
    var v = WS.v, head = h('h2', { class: 'h3', id: 'ai-web-h', tabindex: '-1' }, 'Web search');
    if (!v) return h('section', { class: 'where aiweb', 'aria-labelledby': 'ai-web-h' }, [head,
      WS.err ? h('p', { class: 'err', role: 'alert' }, 'Couldn’t load web search (' + WS.err + ').') : h('p', { class: 'muted' }, 'Loading…')]);
    var err = errBox();
    if (WS.say) err.textContent = WS.say;
    /* 0.21.0: one switch. The address shows while web search is on, after a press with no address saved, or while a
       Check failed or a typed address isn't saved yet; off with a passed Check, the row is the switch alone. */
    var shown = v.on || WS.want || WS.draft != null || (!!v.base_url && !v.check_ok) || !!(WS.res && !WS.res.ok);
    var url = h('input', { class: 'inp', type: 'url', id: 'ws-url', value: WS.draft != null ? WS.draft : (v.base_url || ''), placeholder: WS_EG,
      autocomplete: 'off', spellcheck: 'false', 'data-fk': 'ws-url' });
    on(url, 'input', function () { WS.draft = url.value; });
    var check = btn('btn sm', 'Check', { 'data-fk': 'ws-check' }, function () {
      var val = url.value.trim();
      run([check, s], err, function () {
        var first = val !== (v.base_url || '') ? api('PATCH', 'api/web-search', val ? { base_url: val, on: true } : { base_url: '' }) : Promise.resolve(null);
        return first.then(function () { return val ? api('POST', 'api/web-search/check') : api('GET', 'api/web-search'); });
      }, function (r) { WS.v = r; WS.res = r.result || null; WS.draft = null; WS.say = ''; renderSetup('[data-fk="ws-check"]'); });
    });
    /* 0.14.5: the switch is never disabled (disabled, it said nothing and did nothing) and its row names the setting,
       not the state (the switch already reads Off / On). Off: one PATCH. On: on at once when an address is saved and
       its last Check passed; else the address (the saved one, or the one typed in the field, which is saved) is
       checked first and web search turns on only when the Check passes. An empty field gets the focus and
       "Add your SearXNG address first." */
    var s = sw(v.on, 'Web search', function () {
      var val = url.value.trim();
      if (!v.on && !v.base_url && !val) {
        WS.want = true; WS.say = 'Add your SearXNG address first.';
        if (!shown) { renderSetup('#ws-url'); return; }
        err.textContent = WS.say;
        url.focus();
        return;
      }
      function checkThenOn() {
        return api('POST', 'api/web-search/check').then(function (r) {
          return r.result && r.result.ok ? api('PATCH', 'api/web-search', { on: true }).then(function (x) { return Object.assign(x, { result: r.result }); }) : r;
        });
      }
      run([check, s], err, function () {
        if (v.on) return api('PATCH', 'api/web-search', { on: false });
        if (v.base_url && (!val || val === v.base_url)) return v.check_ok ? api('PATCH', 'api/web-search', { on: true }) : checkThenOn();
        return api('PATCH', 'api/web-search', { base_url: val }).then(checkThenOn);
      }, function (r) { WS.v = r; WS.res = r.result || null; WS.draft = null; WS.say = ''; if (r.on) WS.want = false; renderSetup('[data-fk="ws-on"]'); });
    });
    s.setAttribute('data-fk', 'ws-on');
    var res = WS.res, meta = [];
    if (v.added) meta.push('Added ' + fmtDay(v.added));
    if (res) meta.push(res.text);
    else if (v.checked) meta.push(v.check_ok ? 'Checked ' + fmtDay(v.checked) : 'The last Check didn’t work');
    return h('section', { class: 'where aiweb', 'aria-labelledby': 'ai-web-h' }, [head,
      h('div', { class: 'swrow' }, [s, h('span', { 'aria-hidden': 'true' }, 'Chat can search the web')]),
      shown ? h('div', { class: 'field' }, [h('label', { class: 'label', for: 'ws-url' }, 'Your SearXNG address'), h('div', { class: 'inrow wsrow' }, [url, check])])
        : v.base_url ? null : h('p', { class: 'muted' }, 'Needs your own SearXNG. Turning it on asks for its address.'),
      shown && meta.length ? h('p', { class: 'muted wsmeta' + (res && !res.ok ? ' bad' : ''), role: 'status' }, meta.join(' · ')) : null, err,
      h('p', { class: 'note' }, 'Chat searches only with words you wrote, and asks first when a search would include anything else.')]);
  }
  /* 0.14.0: the daily limits (default 100 searches and 100 pages); 0.21.0: inside Setup › AI › Advanced */
  function wsAdvanced(v) {
    var caps = v.caps || {}, lim = caps.day_limits || [1, 1000], err = errBox();
    function num(key, label, val) {
      var id = 'ws-' + key, inp = h('input', { class: 'inp num', type: 'number', id: id, min: String(lim[0]), max: String(lim[1]), step: '1', value: String(val), 'data-fk': id });
      on(inp, 'change', function () {
        var n = Number(inp.value), body = {};
        body[key] = n;
        run([inp], err, function () { return api('PATCH', 'api/web-search', body); }, function (r) { WS.v = r; renderSetup('[data-fk="' + id + '"]'); });
      });
      return h('div', { class: 'field' }, [h('label', { class: 'label', for: id }, label), inp]);
    }
    return h('div', { class: 'aigrp' }, [h('h3', { class: 'label' }, 'Web search limits'),
      h('div', { class: 'aipair' }, [num('day_searches', 'Searches a day', caps.day_searches || 100), num('day_reads', 'Pages a day', caps.day_reads || 100)]), err,
      h('p', { class: 'muted aihint' }, 'Today: ' + ((v.today && v.today.searches) || 0) + ' searches, ' + ((v.today && v.today.reads) || 0) + ' pages. At most '
        + (caps.searches || 5) + ' searches and ' + (caps.reads || 5) + ' pages for each question.')]);
  }
  function aiStyle() {
    var w = writerOf(), p = (w.prompt_profiles || []).filter(function (x) { return x.id === (w.active_profile || 'default'); })[0];
    return h('section', { class: 'where', 'aria-labelledby': 'ai-style-h' }, [h('h2', { class: 'h3', id: 'ai-style-h' }, 'Writing style'),
      h('div', { class: 'lrow' }, [h('span', null, p && p.id !== 'default' ? 'Your own: ' + p.name : 'Default'),
        btn('btn sm', 'Change', { 'data-fk': 'ai-style', 'aria-label': 'Change the writing style' }, function () { openSetup('advanced', '[data-fk="adv:writing"]', 'writing'); })])]);
  }

  /* ----- Advanced: one closed section each (open state kept while the page is open) ----- */
  var ADV = [['key', 'Google sign-in key', function () { return keyPanel(); }],
    ['alerts', 'Alert limits and privacy', function () { return pushBlock('adv'); }],
    ['writing', 'Writing instructions and safety rules', function () { return writingBlock(); }],
    ['memory', 'Memory file', function () { return memoryBlock('adv'); }],
    ['messages', 'Message history', advMessages],  /* 0.15.0 */
    ['signal', 'Signal history import', advSignal],
    ['visuals', 'Visuals', function () { return window.CallsheetVisuals ? window.CallsheetVisuals.gallery() : null; }],  /* 0.19.0 */
    ['old', 'Old data from earlier versions', advOld],  /* 0.21.0 (oldata.py) */
    ['startover', 'Start over', advStartOver]];
  function advName(id) { var x = ADV.filter(function (y) { return y[0] === id; })[0]; return x ? x[1] : ''; }
  function placeAdvanced() {
    return [h('p', { class: 'intro' }, 'For people who want the details. Each part opens on its own.'),
      h('div', { class: 'advs' }, ADV.map(function (x) {
        var open = !!ui.adv[x[0]], id = 'adv-' + x[0];
        var b = btn('advh', [h('span', null, x[1]), h('span', { class: 'chev', 'aria-hidden': 'true' }, open ? '−' : '+')],
          { 'aria-expanded': String(open), 'aria-controls': id, 'data-adv': x[0], 'data-fk': 'adv:' + x[0] }, function () {
            ui.adv[x[0]] = !ui.adv[x[0]]; renderSetup('[data-fk="adv:' + x[0] + '"]');
          });
        return h('section', { class: 'adv' + (open ? ' open' : '') + (x[0] === 'startover' ? ' danger' : '') }, [h('h2', { class: 'advt' }, b),
          h('div', { class: 'advb', id: id, hidden: !open }, open ? x[2]() : null)]);
      }))];
  }
  function advSignal() {
    if (!SIG.st) { if (!SIG.loading && !SIG.err) sigLoad(); return h('p', { class: 'note', role: 'status' }, SIG.err || 'Loading…'); }
    return sigImport(SIG.st);
  }
  /* ----- 0.21.0 Setup › Advanced › Old data from earlier versions (GET api/old-data, POST api/old-data/clear; oldata.py):
     what removed features left, each group with its size and a tick, nothing ticked at first, and one Clear with a
     confirm step (the list is fetched again first, so the numbers and the one-time token are fresh). Nothing clears
     without the press. This browser's own leftovers (odDeviceKeys: any callsheet.* key that isn't one of the keys
     this page uses, odKept) are listed and removed here; the server never sees them. ----- */
  function odKept() { return [KEY_PREFS, KEY_MODE, KEY_TAB, KEY_DISMISS, KEY_HELP, KEY_CHATOFF, KEY_SOUND, KEY_CHAT]; }
  function odDeviceKeys() {
    var out = [], kept = odKept();
    try {
      for (var i = 0; i < window.localStorage.length; i++) {
        var k = window.localStorage.key(i);
        if (k && k.indexOf('callsheet.') === 0 && kept.indexOf(k) < 0) out.push(k);
      }
    } catch (e) { /* storage blocked: nothing to list */ }
    return out.sort();
  }
  var OD = { v: null, loading: false, err: null, pick: {}, ask: false, done: null };
  function odRedraw(fk) { if (inPlace('advanced')) renderSetup(fk ? '[data-fk="' + cssq(fk) + '"]' : null); }
  function odLoad() {
    OD.loading = true;
    api('GET', 'api/old-data').then(function (v) { OD.v = v; OD.err = null; }, function (e) { OD.err = e.message; })
      .then(function () { OD.loading = false; odRedraw(); });
  }
  function odMB(b) { var m = (b || 0) / 1048576; return m < 0.1 ? 'under 0.1 MB' : m.toFixed(m < 10 ? 1 : 0) + ' MB'; }
  function odDevice() {
    var items = [];
    odDeviceKeys().forEach(function (k) { var v = load(k); if (v != null) items.push({ name: k, kind: 'device', count: 1, unit: 'setting', bytes: 2 * (k.length + v.length) }); });
    if (!items.length) return null;
    return { id: 'device', label: 'On this device', why: 'This browser’s choices for looks and screens that are gone.',
      items: items, count: items.length, rows: 0, bytes: items.reduce(function (n, x) { return n + x.bytes; }, 0) };
  }
  function odGroups() { var g = OD.v ? OD.v.groups.slice() : [], d = odDevice(); if (d) g.push(d); return g; }
  function odItems(g) {
    return g.items.map(function (x) {
      return x.name + (x.unit === 'row' ? ' (' + plural(x.count, 'row', 'rows') + ')' : x.kind === 'folder' ? ' (' + plural(x.count, 'file', 'files') + ')' : '');
    }).join(', ');
  }
  function odImported(m) {
    return 'Copied into the message store: ' + plural(m.documents, 'document', 'documents') + ', ' + plural(m.settings, 'setting', 'settings') +
      ', ' + plural(m.messages, 'older message', 'older messages') + ' (' + m.already_stored.toLocaleString() + ' already there).';
  }
  function advOld() {
    if (!OD.v) {
      if (!OD.loading && !OD.err) odLoad();
      return OD.err ? h('p', { class: 'err', role: 'alert' }, 'Couldn’t load it (' + OD.err + ').') : h('p', { class: 'note', role: 'status' }, 'Loading…');
    }
    var groups = odGroups(), done = OD.done ? h('p', { class: 'note', role: 'status', 'data-fk': 'od-done', tabindex: '-1' }, OD.done) : null;
    if (!groups.length) return [done, h('p', { class: 'note', 'data-fk': 'od-none', tabindex: '-1' }, 'Nothing left from earlier versions.')];
    var picked = groups.filter(function (g) { return OD.pick[g.id]; }), err = errBox();
    var n = picked.reduce(function (s, g) { return s + g.count; }, 0), b = picked.reduce(function (s, g) { return s + g.bytes; }, 0);
    var list = h('ul', { class: 'cals odl', 'aria-label': 'Old data' }, groups.map(function (g) {
      var id = 'od-' + g.id, size = plural(g.count, 'item', 'items') + (g.rows ? ' · ' + plural(g.rows, 'row', 'rows') : '') + ' · ' + odMB(g.bytes);
      var box = on(h('input', { type: 'checkbox', id: id, checked: OD.pick[g.id] ? true : null, 'data-fk': id }), 'change', function () {
        OD.pick[g.id] = box.checked; OD.ask = false; OD.done = null; odRedraw(id);
      });
      return h('li', null, [h('label', { for: id, class: 'calrow' }, [box, h('span', null, [h('b', null, g.label), h('span', { class: 'muted' }, ' · ' + size)])]),
        h('p', { class: 'odw' }, g.why), h('p', { class: 'muted odi' }, odItems(g)), g.imported ? h('p', { class: 'muted odi' }, odImported(g.imported)) : null]);
    }));
    var act;
    if (OD.ask && picked.length) {
      var yes = btn('btn danger critical', 'Clear', { 'data-fk': 'od-yes' }, function () {
        run([yes], err, function () {
          var ids = picked.filter(function (g) { return g.id !== 'device'; }).map(function (g) { return g.id; });
          return ids.length ? api('POST', 'api/old-data/clear', { token: OD.v.token, groups: ids }) : Promise.resolve(null);
        }, function (r) {
          if (OD.pick.device) odDeviceKeys().forEach(function (k) { try { window.localStorage.removeItem(k); } catch (e) { /* storage blocked */ } });
          if (r) OD.v = r;
          OD.done = 'Cleared ' + plural(n, 'item', 'items') + (r && r.freed ? '; ' + odMB(r.freed) + ' freed' : '') + '.' + (r && r.errors && r.errors.length ? ' ' + r.errors.join(' ') : '');
          OD.pick = {}; OD.ask = false; odRedraw('od-done');
        });
      });
      act = h('div', { class: 'confirm', role: 'group', 'aria-label': 'Clear old data' }, [h('p', null, 'Clear ' + plural(n, 'item', 'items') + ', ' + odMB(b) + '? This can’t be undone.'),
        h('div', { class: 'btns' }, [yes, btn('btn', 'Cancel', { 'data-fk': 'od-no' }, function () { OD.ask = false; odRedraw('od-clear'); })]), err]);
    } else {
      var clr = btn('btn', 'Clear', { 'data-fk': 'od-clear', disabled: picked.length ? null : true, 'aria-describedby': 'od-hint' }, function () {
        run([clr], err, function () { return api('GET', 'api/old-data'); }, function (v) { OD.v = v; OD.ask = true; odRedraw('od-yes'); });
      });
      act = h('div', { class: 'testrow' }, [clr, h('span', { class: 'muted', id: 'od-hint' }, picked.length ? plural(n, 'item', 'items') + ' · ' + odMB(b) : 'Tick what to clear.'), err]);
    }
    return [done, h('p', { class: 'note' }, 'Left behind by features that are gone. Nothing uses it any more; your messages, ledger, people, chats, notes and settings stay.'), list, act];
  }
  function advStartOver() {
    return [h('p', { class: 'note' }, 'These delete for good. Each asks first.'), chatClear(), memoryBlock('startover')];
  }

  /* ----- the settings index: every setting, its place (and Advanced section), what to focus, search words and
     the API fields it covers. Find a setting (Setup home) and Find or ask both use it. ----- */
  var SETTINGS = [
    ['connections', null, 'Areas', '[data-fk="ctab:areas"]', 'area work personal employer add delete', ['areas']],
    ['connections', null, 'Google accounts', '[data-fk="ctab:accounts"]', 'gmail calendar mail connect sign in disconnect calendars', ['accounts', 'services', 'calendars']],
    ['connections', null, 'What an area may read', '[data-fk="ctab:accounts"]', 'calendar mail chat ai writer reads', ['reads']],
    ['connections', null, 'Slack', '[data-fk="ctab:accounts"]', 'workspace ignore channels', ['slack']],
    ['connections', null, 'Signal', '[data-fk="sig-h"]', 'link phone ignore chats unlink', ['signal']],
    ['connections', null, 'Notes folders', '[data-fk="src-notes"]', 'obsidian drive vault know me', ['index']],
    ['connections', null, 'GitHub', '[data-fk="src-gh"]', 'repos token projects code', ['github']],
    ['connections', null, 'Connectors', '[data-fk="cn-add-yt"]', 'youtube channel news feed rss atom add pause remove', ['connectors']],  /* 0.10.0 */
    ['connections', null, 'Skip Shorts, how often', '.cnadvb', 'youtube shorts every hour daily advanced connector', ['skip_shorts', 'every_s']],  /* 0.10.0 */
    ['connections', null, 'What to watch for', '#cn-int', 'interests topics watch always big never rundown', ['interests']],  /* 0.10.0 */
    ['connections', null, 'Reports from other agents', '[data-fk="cn-add-rp"]', 'reports agent folder share media markdown digest audit', ['reports', 'tell']],  /* 0.10.2a */
    ['connections', null, 'Voice assistant', '[data-fk="va-on"]', 'voice assistant phone api token tokens device siri', []],  /* 0.21.0; 0.22.0 Open in Setup */
    ['connections', null, 'Places Callsheet contacts', '#cn-h', 'hosts sites outbound places contacts network', ['hosts']],  /* 0.10.0 */
    ['day', null, 'Your name', '#f-name', 'name', ['name']],
    ['day', null, 'Assistant’s name', '#f-aname', 'assistant name bruce call rename', ['assistant_name']],
    ['day', null, 'Time zone', '[data-fk="tz"]', 'timezone zone clock', ['timezone']],
    ['day', null, 'Time format', '[data-fk="tf:12h"]', '12-hour 24-hour clock', ['time_format']],
    ['day', null, 'Brief times', '#ct-morning', 'call times morning midday afternoon check-ins days schedule', ['call_times']],
    ['day', null, 'Urgent checks: days', '[data-fk="ugday:mon"]', 'working days off weekend workdays', ['workdays', 'skip_checkins_days_off']],
    ['day', null, 'Phones to notify', '#push-h', 'phone notifications send to', ['targets']],
    ['day', null, 'Phone note after each brief', '[data-fk="p-calls"]', 'notification summary call times', ['at_call_times']],
    ['day', null, 'Urgent alerts between briefs', '[data-fk="p-urgent"]', 'notification urgent vip', ['urgent']],
    ['day', null, 'Quiet hours', '#p-qs', 'night sleep do not disturb', ['quiet_start', 'quiet_end']],
    ['day', null, 'Send a test notification', '[data-fk="p-test"]', 'phone test', []],
    ['day', null, 'VIPs', '#f-vname', 'important people always', ['vips']],
    ['day', null, 'Theme', '[data-fk="theme:light"]', 'theme appearance look mode dark light auto colours', ['mode']],
    ['day', null, 'Sound', '[data-fk="snd"]', 'tick all clear audio', []],
    ['ai', null, 'Model server', '#ai-url', 'local model server url address openai llama ollama lm studio api key connect check list models', ['base_url', 'api_key', 'provider', 'local.base_url', 'local.api_key']],
    ['ai', null, 'Use one model for everything', '[data-fk="ai-one"]', 'one model same model swap all jobs', ['one_model']],
    ['ai', null, 'Chat model', '#ai-jobs-h', 'chat model pick list local claude', ['chat_model', 'chat_claude', 'chat.provider', 'chat.local.model', 'chat.claude.model']],
    ['ai', null, 'Background model', '#ai-jobs-h', 'model pick local reading ledger brief get to know me projects', ['background_model', 'background.model', 'local.model']],
    ['ai', null, 'Local model health', '#ai-jobs-h', 'unhealthy check again server broken garbage', []],
    ['ai', null, 'Show thinking', '[data-fk="ai-think"]', 'chat thinking reasoning', ['chat.local.thinking', 'chat.claude.thinking']],
    ['ai', null, 'Chat tab on this device', '[data-fk="ai-chat"]', 'chat tab ask', []],
    ['ai', null, 'Web search', '#ai-web-h', 'web search searxng internet online look up', ['web_search.on', 'web_search.base_url']],
    ['ai', null, 'Memory', '#mem-h', 'notes remember', ['memory']],
    ['ai', null, 'Get to know me', '[data-fk="kn-open"]', 'know me profile documents drive notes', []],
    ['ai', null, 'People', '[data-fk="pp-open"]', 'same person merge', ['people']],
    ['ai', null, 'Writing style', '[data-fk="ai-style"]', 'profile tone', ['active_profile']],
    ['ai', null, 'Context size', '#ai-ctx', 'advanced context size tokens ctx', ['ctx_size']],
    ['ai', null, 'Quiet times for the model server', '#ai-qt-h', 'advanced quiet times model server busy gpu pause audit night', ['model_quiet_times']],
    ['ai', null, 'Chat limits and slot', '#ai-cmax', 'advanced tool use timeout max tokens server slot', ['chat.local.tools', 'chat.local.timeout', 'chat.local.max_tokens', 'chat.local.id_slot']],
    ['ai', null, 'Claude key and model', '#ai-ckey', 'advanced anthropic api key cost usd cloud', ['claude.api_key', 'claude.model']],
    ['ai', null, 'If your model server fails, use Claude', '#ai-ckey', 'advanced fallback claude anthropic', ['fallback_to_claude']],
    ['ai', null, 'Web search limits', '#ws-day_searches', 'advanced searches pages a day limit', ['day_searches', 'day_reads']],
    ['advanced', 'key', 'Google sign-in key', '[data-fk="adv:key"]', 'client id secret oauth cloud console', ['google_client']],
    ['advanced', 'alerts', 'Urgent alerts per hour', '#p-max', 'rate limit', ['max_per_hour']],
    ['advanced', 'alerts', 'Alert privacy', '[data-fk="p-subj"]', 'subject show privacy push', ['show_subject']],
    ['advanced', 'writing', 'Writing instructions', '[data-fk="adv:writing"]', 'rules summary drafts style examples prompt profile', ['prompt_profiles']],
    ['advanced', 'writing', 'Safety rules (read-only)', '[data-fk="adv:writing"]', 'safety fixed locked', []],
    ['advanced', 'memory', 'Memory file', '#mem-text', 'memory.md markdown edit', []],
    ['advanced', 'signal', 'Signal history import', '[data-fk="adv:signal"]', 'history export desktop import', []],
    ['advanced', 'old', 'Old data from earlier versions', '[data-fk="adv:old"]', 'clear old data leftovers earlier versions cleanup tidy space index.db', []],
    ['advanced', 'startover', 'Clear all chats', '[data-fk="ch-clr"]', 'delete chats start over', []],
    ['advanced', 'startover', 'Clear memory', '[data-fk="ask:mem-reset"]', 'delete notes reset start over', []]];
  function setWhere(x) { return placeName(x[0]) + (x[1] ? ' › ' + advName(x[1]) : ''); }
  function findSettings(q) {
    var ws = String(q || '').toLowerCase().split(/\s+/).filter(Boolean);
    if (!ws.length) return [];
    return SETTINGS.filter(function (x) {
      var hay = (x[2] + ' ' + x[4] + ' ' + setWhere(x)).toLowerCase();
      return ws.every(function (w) { return hay.indexOf(w) >= 0; });
    });
  }
  function openSetting(x) {
    ui.wiz = null; if (x[0] === 'ai' && /^advanced /.test(x[4])) AIS.adv = true;
    if (x[0] === 'ai' && (!AIS.at || Date.now() - AIS.at > 60000)) AIS.want = x[3];   /* 0.22.0: AI loads first; focus the setting once it has */
    openSetup(x[0], x[3], x[1]);
  }
  /* Setup home's "Find a setting" (the results update as you type; Enter opens the first) */
  function setFind() {
    var list = h('ul', { class: 'sfind-l', id: 'sfind-l', 'aria-live': 'polite' });
    var inp = h('input', { class: 'inp', type: 'search', id: 'sfind', autocomplete: 'off', spellcheck: 'false', placeholder: 'quiet hours, slot, VIPs…', 'aria-describedby': 'sfind-l' });
    function draw() {
      list.textContent = '';
      var hits = findSettings(inp.value);
      if (inp.value.trim() && !hits.length) add(list, h('li', { class: 'muted' }, 'No setting matches.'));
      hits.slice(0, 8).forEach(function (x) {
        add(list, h('li', null, btn('tbtn sres', [h('span', null, x[2]), h('span', { class: 'muted' }, ' · ' + setWhere(x))], null, function () { openSetting(x); })));
      });
    }
    on(inp, 'input', draw);
    on(inp, 'keydown', function (e) { if (e.key === 'Enter') { var x = findSettings(inp.value)[0]; if (x) { e.preventDefault(); openSetting(x); } } });
    return h('div', { class: 'sfind', role: 'search' }, [h('label', { class: 'label', for: 'sfind' }, 'Find a setting'), inp, list]);
  }

  /* ----- per device: the All clear tick (opt-in, a short tone, no file) ----- */
  function tick(force) {
    if (!force && load(KEY_SOUND) !== '1') return;
    try {
      var C = window.AudioContext || window.webkitAudioContext; if (!C) return;
      var c = new C(), o = c.createOscillator(), g = c.createGain(), t = c.currentTime;
      o.type = 'sine'; o.frequency.setValueAtTime(880, t);
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.06, t + 0.01); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);
      o.connect(g); g.connect(c.destination); o.start(t); o.stop(t + 0.25); o.onended = function () { c.close(); };
    } catch (e) { /* no sound */ }
  }
  /* 0.7.1: help paragraphs in a Setup section fold behind one "?": the first sentence stays, the rest opens on
     demand. Open or closed is remembered per section on this device. State lines (status, errors, "No … yet",
     "Loading…") and paragraphs with controls in them always show. */
  var HELP_SEC = '.where, .keyp, .acard, .sigcard, .wpanel, .callout, .ign, .step';
  function helpOpen() { var o = null; try { o = JSON.parse(load(KEY_HELP) || '{}'); } catch (e) { o = null; } return o && typeof o === 'object' ? o : {}; }
  function firstSentence(s) {
    var m = /^([\s\S]*?[.!?:])\s+(?=[A-Z“"(‘])/.exec(s);
    return m ? [m[1], s.slice(m[0].length)] : [s, ''];
  }
  function foldHelp(root, step) {
    if (!root) return;
    var secs = [], groups = [], open = helpOpen();
    root.querySelectorAll('p.note, p.help').forEach(function (p) {
      if (p.hasAttribute('role') || /\b(err|warn|none|sigdone|sigwait|sigprog)\b/.test(p.className)) return;
      if (p.closest('.confirm, [role=status], [role=alert]') || p.querySelector('button, a, input, select, textarea')) return;
      if (/^(No |Nothing|Loading)/.test(p.textContent)) return;
      var sec = p.closest(HELP_SEC) || root, i = secs.indexOf(sec);
      if (i < 0) { secs.push(sec); groups.push([]); i = secs.length - 1; }
      groups[i].push(p);
    });
    groups.forEach(function (ps, gi) {
      var sec = secs[gi], hd = sec.querySelector('h2, h3, .wh, .h, .areahead, .sighead');
      var name = hd ? hd.textContent.trim() : '', key = step + ':' + (name || gi), first = ps[0], hide = ps.slice(1);
      if (!first.children.length) {
        var fs = firstSentence(first.textContent);
        if (fs[1]) { first.textContent = fs[0] + ' '; var more = h('span', { class: 'hmore' }, fs[1]); first.appendChild(more); hide.unshift(more); }
      }
      if (!hide.length) return;
      var isOpen = !!open[key], ids = [];
      hide.forEach(function (x, k) { x.id = x.id || 'hx-' + step + '-' + gi + '-' + k; ids.push(x.id); x.hidden = !isOpen; x.classList.add('hfold'); });
      var q = btn('helpq', '?', { 'aria-expanded': String(isOpen), 'aria-controls': ids.join(' '), 'aria-label': (isOpen ? 'Less help' : 'More help') + (name ? ': ' + name : ''),
        title: isOpen ? 'Less help' : 'More help' }, function () {
        isOpen = !isOpen;
        hide.forEach(function (x) { x.hidden = !isOpen; });
        q.setAttribute('aria-expanded', String(isOpen)); q.title = isOpen ? 'Less help' : 'More help';
        q.setAttribute('aria-label', (isOpen ? 'Less help' : 'More help') + (name ? ': ' + name : ''));
        var o = helpOpen(); if (isOpen) o[key] = 1; else delete o[key];
        save(KEY_HELP, JSON.stringify(o));
      });
      first.appendChild(q);
    });
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
    if (ui.view !== 'setup') return;
    var old = main.querySelector('.sigcard');
    if (!old) return;
    var a = document.activeElement, fk = focusKey || (a && old.contains(a) ? a.getAttribute('data-fk') : null);
    var nu = signalCard();
    old.parentNode.replaceChild(nu, old);
    foldHelp(nu, 2);  /* 0.7.1 */
    if (fk) { var f = nu.querySelector('[data-fk="' + cssq(fk) + '"]'); if (f) f.focus(); }
  }
  /* while a link code waits for the phone: ask every 2 s whether it was scanned */
  function sigPollSoon() {
    if (SIG.timer) return;
    SIG.timer = setTimeout(function () {
      SIG.timer = null;
      if (ui.view !== 'setup' || !SIG.st || !SIG.st.link || SIG.st.link.state !== 'waiting') return;
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
      kids.push(h('p', { class: 'note' }, 'Callsheet links to Signal as an extra device, like Signal Desktop, and only reads. Nothing is sent from here except an RSVP you press.'));
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
    /* 0.9.0: the history import is in Advanced */
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
  var MEM = { data: null, draft: null, err: null, seq: 0, loading: false, editing: null };  /* 0.8.1: editing, a chat note's id */
  function loadMemory() {
    var seq = ++MEM.seq;
    MEM.loading = true;
    function done() { MEM.loading = false; if (seq === MEM.seq && ui.view === 'setup') renderSetup(); }
    return api('GET', 'api/memory').then(function (m) { if (seq !== MEM.seq) return; MEM.data = m; MEM.err = null; done(); },
      function (e) { if (seq !== MEM.seq) return; MEM.err = e && e.message ? e.message : 'error'; done(); });
  }
  function memoryBlock(part) {  /* 0.9.0: 'ai' (notes, Get to know me, People), 'adv' (the file), 'startover' (Clear) */
    if (!MEM.data && !MEM.err && !MEM.loading) loadMemory();
    var m = MEM.data, cap = m && Number(m.cap) || 1500;
    var head = h('div', { class: 'shd' }, [h('h2', { class: 'h3', id: 'mem-h', tabindex: '-1' }, 'Memory'), knowEntry(), peopleEntry('People'),
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
    var warnB = h('div', { class: 'pwbox' });
    function saveMem(confirm) {
      var text = ta.value;
      warnB.textContent = '';
      run([saveB, undoB], err, function () { return api('PATCH', 'api/memory', confirm ? { text: text, confirm: true } : { text: text }); }, function (r) {
        if (r && r.saved === false && r.warning) { pasteWarn(warnB, r.warning, 'mem-any', function () { saveMem(true); }); return; }
        MEM.data = r; MEM.draft = null; renderSetup('[data-fk="mem-text"]');
        var sd = main.querySelector('[data-said="mem"]'); if (sd) sd.textContent = 'Saved';
      });
    }
    var saveB = btn('act', 'Save', { 'data-fk': 'mem-save', disabled: cur === saved }, function () { saveMem(false); });
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
    if (part === 'startover') return clear;
    if (part === 'ai') {
      return h('section', { class: 'where mem', 'aria-labelledby': 'mem-h' }, [head,
        h('p', { class: 'note' }, 'Your notes to Callsheet. Before each brief the writer reads the ones that fit the day.'),
        m.error ? h('p', { class: 'err', role: 'alert' }, 'The memory file has a problem, so no notes are read until it’s fixed (Advanced, Memory file).') : null,
        memChatList(m, cur !== saved),
        h('div', { class: 'memfoot' }, [h('span', { class: 'muted' }, m.error ? 'Notes aren’t read right now.' : plural(n, 'note', 'notes') + ' in the memory.'),
          btn('tbtn', 'Edit the memory file', { 'data-fk': 'mem-file' }, function () { openSetup('advanced', '#mem-text', 'memory'); })])]);
    }
    if (part === 'adv') {
      return h('div', { class: 'where mem', role: 'group', 'aria-label': 'Memory file' }, [intro,
        m.error ? h('p', { class: 'err', role: 'alert' }, 'The file has a problem, so no notes are read until it’s fixed: ' + m.error) : null,
        h('label', { class: 'label', for: 'mem-text' }, 'memory.md'),
        h('p', { class: 'help', id: 'mem-help' }, 'Each note starts with “### ” and a title, then “- key: value” lines (people, emails, domains, topics, area), a blank line and the note. ' +
          'A note without people, emails, domains or topics applies to its whole area. Profile and Answers hold what you saved in Get to know me.'),
        ta,
        h('div', { class: 'wfoot' }, [cnt, h('span', { class: 'btns' }, [said, undoB, saveB])]), err, warnB,
        h('div', { class: 'memfoot' }, [h('span', { class: 'muted' }, m.error ? 'Notes aren’t read right now.' : plural(n, 'note', 'notes') + ' in the memory.')]),
        h('div', { class: 'locked', role: 'group', 'aria-labelledby': 'mem-lock' }, [
          h('div', { class: 'lh', id: 'mem-lock' }, [ico('lock'), 'Sent with your notes (can’t be changed)']),
          h('p', { class: 'fixtext' }, m.rule || ''),
          h('p', { class: 'muted' }, 'Your notes go to the writer in their own block, apart from mail and chat. The safety rules still come first.')])]);
    }
    return h('div', { class: 'where mem', role: 'region', 'aria-labelledby': 'mem-h' }, [head, intro,
      m.error ? h('p', { class: 'err', role: 'alert' }, 'The file has a problem, so no notes are read until it’s fixed: ' + m.error) : null,
      memChatList(m, cur !== saved),
      h('label', { class: 'label', for: 'mem-text' }, 'memory.md'),
      h('p', { class: 'help', id: 'mem-help' }, 'Each note starts with “### ” and a title, then “- key: value” lines (people, emails, domains, topics, area), a blank line and the note. ' +
        'A note without people, emails, domains or topics applies to its whole area. Profile and Answers hold what you saved in Get to know me.'),
      ta,
      h('div', { class: 'wfoot' }, [cnt, h('span', { class: 'btns' }, [said, undoB, saveB])]), err, warnB,
      h('div', { class: 'memfoot' }, [h('span', { class: 'muted' }, m.error ? 'Notes aren’t read right now.' : plural(n, 'note', 'notes') + ' in the memory.'), clear]),
      h('div', { class: 'locked', role: 'group', 'aria-labelledby': 'mem-lock' }, [
        h('div', { class: 'lh', id: 'mem-lock' }, [ico('lock'), 'Sent with your notes (can’t be changed)']),
        h('p', { class: 'fixtext' }, m.rule || ''),
        h('p', { class: 'muted' }, 'Your notes go to the writer in their own block, apart from mail and chat. The safety rules still come first.')])]);
  }

  /* 0.8.1 Remember: the notes saved from chat Save cards (from: chat), each with Edit (PATCH api/memory/notes/<id>) and
     Remove (DELETE, after a confirmation). Hidden while the file has unsaved changes above (one source of truth). */
  function memChatList(m, dirty) {
    var notes = (m.notes || []).filter(function (x) { return x.from === 'chat' && x.id; });
    if (!notes.length) return null;
    return h('div', { class: 'memchat', role: 'group', 'aria-labelledby': 'mem-chat-h' }, [
      h('h3', { class: 'label', id: 'mem-chat-h' }, 'Saved from chat'),
      dirty ? h('p', { class: 'muted' }, 'Save or undo your changes to the file first.') : h('ul', null, notes.map(function (n) {
        var key = 'memrm:' + n.id, meta = [].concat(n.people || [], n.topics || [], n.area ? [n.area] : []).join(' · ');
        if (MEM.editing === n.id) {
          var err = errBox(), ta = h('textarea', { class: 'inp', id: 'mem-e-' + n.id, rows: '3', maxlength: String(m.note_max || 500), 'data-fk': 'mem-e:' + n.id });
          ta.value = n.text;
          var wb = h('div', { class: 'pwbox' });
          var saveN = function (confirm) {
            wb.textContent = '';
            run([sv, cn], err, function () { return api('PATCH', 'api/memory/notes/' + encodeURIComponent(n.id), confirm ? { text: ta.value, confirm: true } : { text: ta.value }); }, function (r) {
              if (r && r.saved === false && r.warning) { pasteWarn(wb, r.warning, 'mem-eany:' + n.id, function () { saveN(true); }); return; }
              MEM.editing = null; loadMemory();
            });
          };
          var sv = btn('act', 'Save', { 'data-fk': 'mem-es:' + n.id }, function () { saveN(false); });
          var cn = btn('tbtn', 'Cancel', null, function () { MEM.editing = null; renderSetup('[data-fk="' + cssq('mem-ed:' + n.id) + '"]'); });
          return h('li', null, [h('label', { class: 'sr', for: 'mem-e-' + n.id }, 'Edit the note'), ta, wb, h('div', { class: 'btns' }, [sv, cn]), err]);
        }
        return h('li', null, [h('p', { class: 'mt' }, n.text), meta || n.created ? h('p', { class: 'muted' }, (meta ? meta + ' · ' : '') + 'from chat' + (n.created ? ' · ' + n.created : '')) : null,
          ui.confirm === key ? confirmRow('Remove this note from memory?', 'Remove', function (y, c, e) {
            run([y, c], e, function () { return api('DELETE', 'api/memory/notes/' + encodeURIComponent(n.id)); }, function () { ui.confirm = null; loadMemory(); });
          }) : h('div', { class: 'btns' }, [btn('tbtn', 'Edit', { 'data-fk': 'mem-ed:' + n.id }, function () { MEM.editing = n.id; renderSetup('[data-fk="' + cssq('mem-e:' + n.id) + '"]'); }),
            askBtn(key, 'Remove')])]);
      }))]);
  }

  /* ---------- 0.5.1: Setup → Delivery → Phone notifications (through Home Assistant's notify.mobile_app_*) ----------
     GET api/push (settings, available, last sent/error) and GET api/push/targets (the phones Home Assistant lists)
     when the step is shown; each change is PATCH api/push; "Send a test notification" = POST api/push/test.
     Errors from Home Assistant come back as plain sentences and are shown as they are (textContent). */
  var PUSH = { data: null, targets: null, tErr: null, err: null, seq: 0, loading: false };
  function loadPush() {
    var seq = ++PUSH.seq;
    PUSH.loading = true;
    function done() { PUSH.loading = false; if (seq === PUSH.seq && ui.view === 'setup') renderSetup(); }
    var a = api('GET', 'api/push').then(function (r) { if (seq === PUSH.seq) { PUSH.data = r; PUSH.err = null; } },
      function (e) { if (seq === PUSH.seq) PUSH.err = e && e.message ? e.message : 'error'; });
    var b = api('GET', 'api/push/targets').then(function (r) {
      if (seq !== PUSH.seq) return;
      PUSH.targets = (r && r.targets) || []; PUSH.tErr = r && r.available === false ? null : (r && r.error) || null;
    }, function (e) { if (seq === PUSH.seq) { PUSH.targets = null; PUSH.tErr = e && e.message ? e.message : 'error'; } });
    return Promise.all([a, b]).then(done, done);
  }
  function pushBlock(part) {  /* 0.9.0: 'day' (phones, switches, quiet hours, test) or 'adv' (per hour, privacy) */
    if (!PUSH.data && !PUSH.err && !PUSH.loading) loadPush();
    var head = h('div', { class: 'shd' }, [h('h2', { class: 'h3', id: 'push-h', tabindex: '-1' }, 'Phone')]);
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
    if (part === 'adv') {
      return h('div', { class: 'where push', role: 'group', 'aria-label': 'Alert limits and privacy' }, [
        h('div', { class: 'swrow' }, [subj, h('span', { 'aria-hidden': 'true' }, 'Show the subject in notifications')]),
        h('p', { class: 'note' }, 'Notification text passes through Apple’s or Google’s push service. Off: the sender’s name only (“New urgent message from Priya”).'),
        h('div', { class: 'pfields' }, [h('div', { class: 'field' }, [h('label', { class: 'label', for: 'p-max' }, 'Urgent alerts per hour'), perHour])]),
        qErr, err,
        h('p', { class: 'note' }, 'More than that in an hour are grouped into one “more urgent messages” alert.')]);
    }
    if (part === 'day') {
      return h('section', { class: 'where push', 'aria-labelledby': 'push-h' }, [head, intro,
        d.available ? null : h('p', { class: 'syncst bad' }, 'Phone notifications work when Callsheet runs as a Home Assistant app.'),
        phones, err,
        h('div', { class: 'swrow' }, [calls, h('span', { 'aria-hidden': 'true' }, 'After each brief')]),
        h('p', { class: 'note' }, 'How many replies and events, and the top item. Tap it to open Callsheet.'),
        h('div', { class: 'swrow' }, [urg, h('span', { 'aria-hidden': 'true' }, 'Urgent alerts between briefs')]),
        h('p', { class: 'note' }, 'Every ' + every + ' minutes, without AI: a VIP, mail that says urgent, ASAP or emergency (or today with a deadline), or a sender saying something is approved, paid or signed. ' +
          'Each message once. Only on the urgent checks’ days (Brief times), never in quiet hours.'),
        h('div', { class: 'pfields' }, [
          h('div', { class: 'field' }, [h('label', { class: 'label', for: 'p-qs' }, 'Quiet hours from'), timeIn('p-qs', 'quiet_start')]),
          h('div', { class: 'field' }, [h('label', { class: 'label', for: 'p-qe' }, 'Quiet hours until'), timeIn('p-qe', 'quiet_end')])]),
        qErr,
        h('div', { class: 'testrow' }, [test, res]), tstErr,
        le && le.text ? h('p', { class: 'syncst bad', role: 'status' }, 'Last problem ' + fmtWhen(le.at, true) + ': ' + le.text)
          : h('p', { class: 'syncst', role: 'status' }, ls && ls.at ? 'Last sent ' + fmtWhen(ls.at, true) + ' (' + (KIND[ls.kind] || 'notification') + ').' : 'Nothing sent yet.')]);
    }
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

  /* ---------- Setup → Memory → Get to know me (#knowme): the documents Callsheet reads (0.21.0) ----------
     GET api/index/status (the library's stats, the document sources Drive and notes, their settings); PATCH
     api/index/settings (sources on/off, the notes folder); the Drive grant (POST api/index/drive/start → Google's page →
     paste the address → POST api/index/drive/finish). 0.21.0: messages.db is the one library and every source is read
     live, so the 0.6.0 scan (windows, estimate, scan, pause, resume, cancel) and Delete the index are gone. The Check,
     Questions and Done steps are the profile's (#profile). Everything is inserted as text. */
  var KN = { st: null, seq: 0, grant: null };
  var KNSTEPS = ['Sources', 'Check', 'Questions', 'Done'];   /* 0.21.0: the Scan step went with the scan */
  var KNICON = { gmail: 'mail', calendar: 'cal', slack: 'slack', drive: 'drive', notes: 'note', signal: 'signal' };
  function stopKnowPoll() { KN.seq++; }   /* a load still on its way is dropped */
  function knowEntry() { return btn('act', 'Get to know me', { 'data-fk': 'kn-open' }, function () { location.hash = 'knowme'; }); }
  function showKnow(focusSel) {
    var seq = ++KN.seq;
    var st = S ? Promise.resolve(S) : loadState();
    return st.then(function () { return api('GET', 'api/index/status'); }).then(function (r) {
      if (seq !== KN.seq || ui.view !== 'knowme') return;
      KN.st = r;
      renderKnow(focusSel);
    }, function (e) { if (ui.view === 'knowme') status('Could not load Get to know me (' + e.message + ')'); });
  }
  function knBytes(b) {
    b = Number(b) || 0;
    if (b < 1e6) return 'under 1 MB';
    if (b < 1e9) return Math.round(b / 1e6) + ' MB';
    return (b / 1e9).toFixed(1) + ' GB';
  }
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
      run([b], err, function () { return api('PATCH', 'api/index/settings', patch); }, function () { showKnow('[data-fk="kn-src-' + s.key + '"]'); });
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
      run([check], err, function () { return api('POST', 'api/index/drive/check', {}); }, function () { showKnow('[data-fk="kn-drive-check"]'); });
    });
    var box = [h('p', { class: 'note' }, 'Drive needs one more read-only permission for each account. Google’s page asks only for that; the access you gave before stays as it is.'),
      h('ul', { class: 'kngrant' }, rows), h('div', null, check), err];
    if (g) {
      var inp = h('input', { class: 'inp', id: 'kn-paste', type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: 'http://127.0.0.1:8765/?state=…&code=…' });
      var ferr = errBox();
      var fin = btn('act', 'Finish', { 'data-fk': 'kn-finish' }, function () {
        run([fin], ferr, function () { return api('POST', 'api/index/drive/finish', { flow_id: g.flow, pasted: inp.value }); }, function (r) {
          KN.grant = null;
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
      run([save], err, function () { return api('PATCH', 'api/index/settings', { vault: inp.value.trim() }); }, function () { showKnow('[data-fk="kn-vault"]'); });
    });
    return h('div', { class: 'knsub' }, [h('label', { class: 'label', for: 'kn-vault' }, 'Obsidian vault folder'),
      h('div', { class: 'knrow' }, [inp, save]),
      h('p', { class: 'help' }, 'Read-only. Sync the vault into Home Assistant’s share or media folder (for example with Syncthing or a network share), then type its path. Settings and attachments are skipped.'),
      v.error ? h('p', { class: 'err', role: 'alert' }, v.error) : null, err]);
  }
  function knStat(l, v) { return h('div', { class: 'stat' }, [h('span', { class: 'label' }, l), h('span', { class: 'disp' }, v)]); }
  function knSources(st) {
    var srcs = st.sources || [], stats = st.stats || {}, err = h('p', { class: 'err', id: 'kn-err', role: 'alert' });
    var drive = srcs.filter(function (s) { return s.key === 'drive'; })[0], notes = srcs.filter(function (s) { return s.key === 'notes'; })[0];
    return [
      h('p', { class: 'intro' }, 'Callsheet reads your mail, calendars, Slack, Drive and notes here on this box and keeps them, so it can get to know your people and projects.'),
      h('h3', { class: 'fl' }, 'What to read'),
      h('div', { class: 'cks', role: 'group', 'aria-label': 'What to read' }, srcs.map(knChip)),
      h('p', { class: 'help' }, 'Turning a source off removes what Callsheet read from it.'),
      drive && drive.on ? knDrive(drive) : null,
      notes && notes.on ? knVault(notes) : null,
      h('div', { class: 'locked', role: 'note' }, [h('div', { class: 'lh' }, [ico('lock'), 'What stays where']),
        h('p', { class: 'wsw' }, 'Everything is stored on this Home Assistant box, in its message store, and is included in Home Assistant backups.'),
        h('p', { class: 'muted' }, 'Nothing is sent from here except an RSVP you press. Callsheet creates only two things, and only when you press: a calendar event (Add to calendar) and a Gmail draft (Put in Gmail Drafts); and it answers an invite only when you press Yes, No or Maybe on it (Google tells the organizer). Everything else in Gmail, Calendar and Drive is read-only.')]),
      stats.total ? h('div', { class: 'go' }, [pfEntry()]) : null,
      err];
  }
  function renderKnow(focusSel) {
    var st = KN.st || {};
    var body = st.available ? knSources(st) : [h('p', { class: 'err', role: 'alert' }, st.error || 'The message store isn’t available.')];
    var stats = st.stats || {};
    var sheet = h('div', { class: 'sheet wiz kn' }, [
      h('div', { class: 'mast two' }, [
        h('div', { class: 't' }, [h('div', { class: 'label' }, 'Setup › Memory'), h('h1', { id: 'kn-title', tabindex: '-1' }, 'Get to know me')]),
        h('div', { class: 'box call' }, [h('div', { class: 'label' }, 'Stored on this box'), h('div', { class: 'disp' }, knBytes(stats.bytes)),
          h('div', { class: 's' }, plural(stats.total || 0, 'item', 'items'))])]),
      h('div', { class: 'knhead' }, knSteps(0)),
      h('section', { class: 'step', 'aria-labelledby': 'kn-title' }, body),
      h('div', { class: 'wnav' }, [btn('btn', ui.wiz === 2 ? 'Back: First-run steps' : 'Back: AI', null, function () { if (ui.wiz === 2) openSetup('home'); else openSetup('ai', '#mem-h'); })])]);
    var keep = focusSel ? null : document.activeElement && document.activeElement.getAttribute('data-fk');
    main.textContent = '';
    add(main, sheet);
    pressMode();
    syncBg();
    var f = focusSel ? main.querySelector(focusSel) : keep ? main.querySelector('[data-fk="' + cssq(keep) + '"]') : null;
    if (f) f.focus({ preventScroll: !!keep });
  }


  /* ---------- 0.8.2 People (#people): Same person? cards, You, Everyone, History ----------
     GET api/people; answers POST api/people/proposals/<id> {answer: same|different|later} (into a VIP: needs_confirm
     with the evidence first, then {answer: same, confirm: token}); Split POST
     api/people/merges/<id>/split; VIP PATCH api/people/<id>; names people use for you PATCH api/people/me.
     Only the owner merges; nothing ever merges with "You". Data is text (h()). */
  var PP = { v: null, seq: 0, q: '', said: '', err: null, timer: null };
  var PP_ICO = { email: 'mail', slack: 'slack', signal: 'signal' };
  function ppCard(p) {
    return h('div', { class: 'ppc' }, [h('h4', null, p.name), p.sub ? h('div', { class: 'ppsub' }, p.sub) : null,
      h('ul', { class: 'pph' }, (p.handles || []).map(function (x) {
        return h('li', null, [ico(PP_ICO[x.kind] || 'mail'), h('span', { class: 'v' }, x.label), x.count ? h('span', { class: 'c' }, x.count) : null]);
      })),
      p.more ? h('p', { class: 'ppsub' }, '+ ' + p.more + ' more') : null]);
  }
  /* one "Same person?" card: both people, why, and the owner's three answers. o.compact: the reply panel's version */
  function sigList(list) {
    return h('ul', { class: 'sigs' }, (list || []).map(function (s) { return h('li', null, [h('b', null, s.strength), h('span', null, s.why)]); }));
  }
  function sameCard(c, o) {
    var err = errBox(), yes, no, later, row, conf = null;
    function answer(a, token) {
      var body = token ? { answer: a, confirm: token } : { answer: a };
      run(conf ? [].slice.call(conf.querySelectorAll('button')) : [yes, no, later], err, function () { return api('POST', 'api/people/proposals/' + encodeURIComponent(c.id), body); },
        function (r) { if (r && r.needs_confirm) { askConfirm(r); return; } if (o.done) o.done(a, r); });
    }
    /* 0.8.2: a merge into a VIP takes a second step that shows the evidence again; only "Yes, merge" sends the token */
    function askConfirm(r) {
      var go = btn(o.compact ? 'act ghost' : 'btn primary', 'Yes, merge', { 'data-fk': 'sameok:' + c.id }, function () { answer('same', r.confirm); });
      var back = btn('tbtn', 'Cancel', { 'data-fk': 'sameno:' + c.id }, function () { conf.replaceWith(row); conf = null; yes.focus(); });
      var hd = h('h4', { class: 'label', tabindex: '-1' }, 'Merge into a VIP?');
      var el = h('div', { class: 'sconfirm', role: 'group', 'aria-label': 'Merge into a VIP? ' + (r.names || '') }, [hd,
        h('p', { class: 'note' }, (r.vip || 'One of them') + ' is a VIP: every address of the merged person will count as VIP.'),
        h('h4', { class: 'label' }, 'Evidence'), sigList(r.evidence),
        r.caution ? h('p', { class: 'scaution' }, r.caution) : null,
        h('div', { class: 'btns' }, [go, back])]);
      (conf || row).replaceWith(el); conf = el; hd.focus();
    }
    yes = btn(o.compact ? 'act ghost' : 'btn primary', 'Same person',  /* in a reply's panel, Open in Gmail stays the loud one */ { 'data-fk': 'same:' + c.id }, function () { answer('same'); });
    no = btn(o.compact ? 'act ghost' : 'btn', 'Different people', { 'data-fk': 'diff:' + c.id }, function () { answer('different'); });
    later = btn('tbtn', 'Later', { 'data-fk': 'later:' + c.id }, function () { answer('later'); });
    row = h('div', { class: 'btns' }, [yes, no, later]);
    return h('div', { class: 'same' + (o.compact ? ' compact' : ''), role: 'group', 'aria-label': 'Same person? ' + c.a.name + ' and ' + c.b.name }, [
      o.compact ? h('h3', { class: 'plab' }, 'Same person?') : null,
      h('div', { class: 'pair' }, [ppCard(c.a), h('span', { class: 'or' }, 'and'), ppCard(c.b)]),
      h('div', { class: 'swhy' }, [h('h4', { class: 'label' }, 'Why Callsheet thinks so'), sigList(c.signals),
        c.caution ? h('p', { class: 'scaution' }, c.caution) : null]),  /* a signature the sender wrote is the only evidence */
      row, err]);
  }
  function ppAnswered(a, r) {
    return a === 'same' ? ((r.person && r.person.name) || 'They') + ' is one person now.' : a === 'different' ? 'Kept apart. Callsheet won’t ask about these two again.' : 'Callsheet will ask again next week.';
  }
  function showPeople(focusSel) {
    var seq = ++PP.seq;
    return api('GET', 'api/people' + (PP.q ? '?q=' + encodeURIComponent(PP.q) : '')).then(function (r) {
      if (seq !== PP.seq || ui.view !== 'people') return;
      PP.v = r; PP.err = null; renderPeople(focusSel);
    }, function (e) { if (seq !== PP.seq || ui.view !== 'people') return; PP.err = e.message; renderPeople(focusSel); });
  }
  function ppSame(v) {
    var ps = v.proposals || [], c = ps[0];
    return h('div', { class: 'ppsec', role: 'region', 'aria-labelledby': 'pp-same' }, [
      h('h2', { class: 'h3', id: 'pp-same', tabindex: '-1', 'data-fk': 'pp-same' }, ['Same person?', ps.length ? h('span', { class: 'n' }, '1 of ' + ps.length) : null]),
      PP.said ? h('p', { class: 'ppsaid', role: 'status' }, PP.said) : null,
      c ? [h('p', { class: 'note' }, 'If they are one person, Callsheet treats them as one everywhere: replies, VIPs, notes and Chat.'),
        sameCard(c, { done: function (a, r) { PP.said = ppAnswered(a, r); showPeople('[data-fk="pp-same"]'); } })]
        : h('p', { class: 'muted' }, 'No suggestions right now.'),
      h('p', { class: 'muted ppnever' }, 'Never suggested: shared addresses like info@ or team@, first names on their own, or you.')]);
  }
  function ppYou(v) {
    var me = v.me || {}, err = errBox(), names = me.names || [];
    var inp = h('input', { class: 'inp', type: 'text', id: 'pp-name', maxlength: '40', autocomplete: 'off', 'data-fk': 'pp-name' });
    function saveNames(list, fk) {
      run([], err, function () { return api('PATCH', 'api/people/me', { names: list }); }, function (r) { PP.v = r; renderPeople('[data-fk="' + fk + '"]'); });
    }
    var addB = h('button', { type: 'submit', class: 'btn' }, 'Add');
    var chips = names.length ? h('ul', { class: 'ppchips', 'aria-label': 'Names people use for you' }, names.map(function (n, i) {
      return h('li', { class: 'pchip' }, [n, btn('cx', '×', { 'aria-label': 'Remove ' + n, 'data-fk': 'pp-n' + i }, function () {
        saveNames(names.filter(function (x) { return x !== n; }), 'pp-name');
      })]);
    })) : null;
    var form = on(h('form', { class: 'ppadd', novalidate: '', 'aria-label': 'Add a name people use for you' }, [
      h('label', { class: 'label', for: 'pp-name' }, 'Names people use for you'), chips, h('div', { class: 'row' }, [inp, addB])]), 'submit', function (e) {
      e.preventDefault();
      var n = inp.value.trim();
      if (!n) { err.textContent = 'Type a name first.'; return; }
      saveNames(names.concat([n]), 'pp-name');
    });
    var sa = btn('btn', me.send_as_read ? 'Read “send as” again' : 'Read my Gmail “send as” addresses', { 'data-fk': 'pp-sendas' }, function () {
      run([sa], err, function () { return api('POST', 'api/people/me/send-as', {}); }, function (r) {
        PP.v = r; PP.said = (r.errors && r.errors.length) ? 'Some accounts couldn’t be read: ' + r.errors.join(' ') : 'Your addresses are up to date.';
        renderPeople('[data-fk="pp-sendas"]');
      });
    });
    return h('div', { class: 'ppsec', role: 'region', 'aria-labelledby': 'pp-you' }, [
      h('h2', { class: 'h3', id: 'pp-you' }, ['You', h('span', { class: 'n' }, 'from your accounts only')]),
      h('div', { class: 'ppc you' }, [
        (me.handles || []).length ? h('ul', { class: 'pph' }, me.handles.map(function (x) {
          return h('li', null, [ico(PP_ICO[x.kind] || 'mail'), h('span', { class: 'v' }, x.label), x.count ? h('span', { class: 'c' }, x.count) : null]);
        })) : h('p', { class: 'muted' }, 'No accounts connected yet.'),
        form,
        h('div', { class: 'btns' }, [sa]),
        h('p', { class: 'note' }, 'Only your connected accounts add to this. A message that says “I’m your other email” never changes it.'), err])]);
  }
  function ppRow(p) {
    var err = errBox(), vipB = sw(p.vip, 'VIP: ' + p.name, function () {
      run([vipB], err, function () { return api('PATCH', 'api/people/' + encodeURIComponent(p.id), { vip: !p.vip }); }, function () { showPeople('[data-fk="' + cssq('pp-vip:' + p.id) + '"]'); });
    });
    vipB.setAttribute('data-fk', 'pp-vip:' + p.id);
    var hs = (p.handles || []).slice(0, 3).map(function (x) { return x.label; }).join(' · ') + (p.more || (p.handles || []).length > 3 ? ' · …' : '');
    return h('li', { class: 'pprow' }, [
      h('div', { class: 'nm' }, [h('b', null, p.name), p.sub ? h('span', { class: 'ppsub' }, p.sub) : null, h('span', { class: 'hs' }, hs)]),
      h('div', { class: 'acts' }, [h('span', { class: 'label vl', 'aria-hidden': 'true' }, 'VIP'), vipB].concat((p.merges || []).map(function (m) {
        return btn('tbtn', 'Split', { 'aria-label': 'Split: ' + m.names, 'data-fk': 'pp-split:' + m.id }, function () { ppSplit(m.id, [], err); });
      }))), err]);
  }
  function ppSplit(id, btns, err) {
    run(btns, err, function () { return api('POST', 'api/people/merges/' + encodeURIComponent(id) + '/split', {}); }, function () {
      PP.said = 'Split. They are two people again, and Callsheet won’t suggest them again.'; showPeople('[data-fk="pp-same"]');
    });
  }
  function ppEveryone(v) {
    var q = h('input', { class: 'inp', type: 'search', id: 'pp-q', autocomplete: 'off', spellcheck: 'false', value: PP.q, 'data-fk': 'pp-q', placeholder: 'Name or address' });
    on(q, 'input', function () {
      clearTimeout(PP.timer);
      PP.timer = setTimeout(function () { PP.q = q.value.trim(); showPeople('[data-fk="pp-q"]'); }, 300);
    });
    var list = v.people || [];
    return h('div', { class: 'ppsec', role: 'region', 'aria-labelledby': 'pp-all' }, [
      h('h2', { class: 'h3', id: 'pp-all' }, ['Everyone', h('span', { class: 'n' }, fmtNum(v.total || 0))]),
      h('div', { class: 'ppsearch' }, [h('label', { class: 'label', for: 'pp-q' }, 'Search people'), q]),
      list.length ? h('ul', { class: 'pplist' }, list.map(ppRow)) : h('p', { class: 'muted' }, PP.q ? 'No one matches.' : 'No people yet. They come from your VIPs, Get to know me and your mail.'),
      v.shown < (PP.q ? v.shown : v.total) ? h('p', { class: 'muted' }, 'Showing ' + fmtNum(v.shown) + '. Search to find the others.') : null]);
  }
  function ppHistory(v) {
    var a = v.audit || [];
    if (!a.length) return null;
    return h('div', { class: 'ppsec', role: 'region', 'aria-labelledby': 'pp-hist' }, [
      h('h2', { class: 'h3', id: 'pp-hist' }, 'History'),
      h('ul', { class: 'pphist' }, a.map(function (e) {
        var err = errBox(), b = e.can_split ? btn('tbtn', 'Split', { 'aria-label': 'Split: ' + e.text, 'data-fk': 'pp-hsplit:' + e.id }, function () { ppSplit(e.id, [b], err); }) : null;
        return h('li', null, [h('span', null, e.text), h('span', { class: 'muted' }, fmtWhen(new Date(e.at * 1000).toISOString(), true)), b, err]);
      }))]);
  }
  function renderPeople(focusSel) {
    var v = PP.v, body;
    if (!v) body = [PP.err ? h('p', { class: 'err', role: 'alert' }, 'Couldn’t load people (' + PP.err + ').') : h('p', { class: 'muted' }, 'Loading…')];
    else body = [ppSame(v), ppYou(v), ppEveryone(v), ppHistory(v)];
    var sheet = h('div', { class: 'sheet wiz pp' }, [
      h('div', { class: 'mast two' }, [
        h('div', { class: 't' }, [h('div', { class: 'label' }, 'Setup'), h('h1', { id: 'pp-title', tabindex: '-1' }, 'People')]),
        h('div', { class: 'box call' }, [h('div', { class: 'label' }, 'Suggestions'), h('div', { class: 'disp' }, v ? String((v.proposals || []).length) : '–')])]),
      h('section', { class: 'step', 'aria-labelledby': 'pp-title' }, body),
      h('div', { class: 'wnav' }, [btn('btn', 'Back: AI', null, function () { openSetup('ai', '#mem-h'); })])]);
    var keep = focusSel ? null : document.activeElement && document.activeElement.getAttribute('data-fk');
    main.textContent = '';
    add(main, sheet);
    pressMode();
    syncBg();
    var f = focusSel ? main.querySelector(focusSel) : keep ? main.querySelector('[data-fk="' + cssq(keep) + '"]') : null;
    if (f) f.focus({ preventScroll: focusSel === '#pp-title' });
    if (f && f.id === 'pp-q') { var n = f.value.length; f.setSelectionRange(n, n); }
  }
  /* 0.8.2: a hand-written note that reads like an instruction or a claim pasted from a message: a warning, not a refusal */
  function pasteWarn(box, text, fk, again) {
    box.textContent = '';
    var b = btn('tbtn', 'Save anyway', { 'data-fk': fk }, function () { box.textContent = ''; again(); });
    add(box, [h('p', { class: 'pwarn', role: 'alert' }, text), h('div', { class: 'btns' }, [b])]);
    b.focus();
  }
  function peopleEntry(label) { return btn('btn', label || 'People', { 'data-fk': 'pp-open' }, function () { location.hash = 'people'; }); }

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
          h('div', { class: 's' }, 'stored items')])]),
      h('div', { class: 'knhead' }, h('ol', { class: 'knsteps', 'aria-label': 'Steps' }, KNSTEPS.map(function (s, i) {
        var n = PF.done ? 3 : q ? 2 : 1;
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
      pfSame(), pfPeople(), pfProjects(), pfItems('Recurring', 'pf-rec', v.recurring, function (r) { return [h('b', null, r.title), h('span', { class: 'sub' }, r.when)]; },
        function (r) { return r.title + ', ' + r.when; }),
      pfItems('Reply habits', 'pf-hab', v.habits, function (r) { return [h('b', null, r.text), h('span', { class: 'sub' }, r.sub)]; },
        function (r) { return r.text; }),
      h('div', { class: 'go pfnav' }, [
        btn('act', 'Continue to questions' + (v.interview.questions.length ? ' (' + v.interview.questions.length + ')' : ''), { 'data-fk': 'pf-next' }, function () { PF.done = false; PF.enough = false; location.hash = 'profile-questions'; }),
        btn('act ghost', 'Save and finish later', { 'data-fk': 'pf-later' }, function () { location.hash = 'knowme'; }),
        h('span', { class: 'hint' }, 'What you saved is kept. Come back any time from Setup, Memory.')]), err];
  }
  function pfSame() {  /* 0.8.2: the first Same person? card; the rest in Setup, People */
    var v = PF.v, c = (v.same || [])[0];
    if (PF.same_said) return h('p', { class: 'ppsaid', role: 'status' }, [PF.same_said, ' ', peopleEntry('People')]);
    if (!c) return null;
    return pfCard('Same person?', v.same_open > 1 ? '1 of ' + v.same_open : '', [sameCard(c, { done: function (a, r) {
      PF.same_said = ppAnswered(a, r); showProfile(ui.view, '.ppsaid');
    } }), v.same_open > 1 ? h('div', { class: 'go' }, [peopleEntry('See all in People')]) : null]);
  }
  function pfAi() {
    var v = PF.v, err = errBox(), j = v.job && v.job.kind === 'suggest' ? v.job : null;
    if (j && j.state === 'running') return h('div', { class: 'calm', role: 'status' }, [h('b', null, 'Asking the AI for suggestions… '), 'This can take a minute with a local model.']);
    if (v.can_ask && v.can_ask.code === 'off') return h('p', { class: 'help' }, 'Suggested roles and project names need your model server. ' + v.can_ask.text);
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
      h('div', { class: 'go' }, [btn('act', 'See it in Memory (Setup)', { 'data-fk': 'pf-mem' }, function () { PF.done = false; openSetup('ai', '#mem-h'); }),
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
    earlier: false, clearAsk: false, scroll: true, keepTop: null,
    thinkOpen: {},       /* 0.14.0: Thinking panel open state by message key (closed by default after the answer) */
    cards: {}            /* 0.8.1: Save card state by proposal id: {edit, text, people, topics, area, confirm, ok, warning, err, busy, dismissed} */
  };
  var CHAT_SUGG = ['What’s waiting on me?', 'When did I last talk to…', 'Summarise this week'];
  var SRC_ICON = { gmail: 'mail', slack: 'slack', calendar: 'cal', drive: 'drive', notes: 'note', signal: 'signal', github: 'tasks' };
  var SRC_NAME = { gmail: 'Gmail', slack: 'Slack', calendar: 'Calendar', drive: 'Google Drive', notes: 'Notes', signal: 'Signal', github: 'GitHub' };
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
  /* 0.14.0 Show thinking: the chat's thinking setting (both engines), kept on this box */
  function chatThinking(v) {
    var ov = CH.ov;
    if (!ov) return;
    ov.thinking = v; chatRender('[data-cthink]');
    api('PATCH', 'api/chat/settings', { local: { thinking: v }, claude: { thinking: v } }).then(function () { return chatOverview(); },
      function (e) { ov.thinking = !v; CH.err = e.message; chatRender('[data-cthink]'); });
  }
  /* a Chat card that renames the assistant (applied at once, confirmed or undone): read the name again, then redraw */
  function nameSync(k) {
    if (!k || k.kind !== 'assistant_name') return;
    loadState().then(function () { if (CH.conv) chatRender(); }, function () { });
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
        else {
          var lm = c.messages && c.messages[c.messages.length - 1];
          if (lm && lm.card && lm.card.applied) nameSync(lm.card);   /* "call yourself …" applied at once */
          if (!chatSwap()) { CH.scroll = true; chatRender(); chatOverview(); }
        }
      }, function (e) {
        if (!CH.conv || CH.conv.id !== cid) return;
        if (++CH.fails < 5) { chatPoll(); return; }
        CH.fails = 0; CH.conv.pending = null; CH.err = e.message; chatRender();
      });
    }, chatFast(CH.conv.pending) ? 300 : 1000);   /* 0.14.0: 300 ms while the answer streams */
  }
  function chatFast(p) {
    return !!(p && (p.state === 'writing' || p.state === 'checking' || (p.live && p.live.length) || (p.thinking && p.thinking.sents && p.thinking.sents.length)));
  }
  /* 0.14.0: the answer is done: its final version replaces the streamed one in place (same spot, no re-render flash) */
  function chatSwap() {
    var box = main.querySelector('.chatcol .msgs'), old = box && box.querySelector('[data-live]');
    var msgs = (CH.conv && CH.conv.messages) || [], i = msgs.length - 1;
    if (!old || i < 0 || msgs[i].role !== 'assistant' || ui.view !== 'brief' || U.tab !== 'chat') return false;
    var atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    old.replaceWith(chatAnswer(msgs[i], i));
    if (atEnd) box.scrollTop = box.scrollHeight;
    var sb = main.querySelector('[data-csend]');
    if (sb) sb.disabled = chatPending();
    chatOverview();
    return true;
  }
  function chatAsk(text) {
    text = String(text || '').trim();
    if (!text || chatPending()) return;
    CH.sending = text; CH.err = ''; CH.input = ''; CH.scroll = true;
    chatRender('[data-ask-in]');
    api('POST', 'api/chat/ask', { conversation_id: CH.conv ? CH.conv.id : null, text: text, project_id: !CH.conv && CH.scope && !CH.scope.ledger ? CH.scope.project : undefined,
      item_id: !CH.conv && CH.scope ? CH.scope.item_id : undefined, clash_id: !CH.conv && CH.scope ? CH.scope.clash_id : undefined }).then(function (c) {
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
    var think = ov && ov.ready ? chsw(ov.thinking, 'Show thinking', function () { chatThinking(!ov.thinking); }) : null;
    var head = h('div', { class: 'dtop' }, [h('h2', null, 'Chat'), ov && ov.label ? h('span', { class: 'cmodel' }, ov.label) : null, h('span', { class: 'sp' }), think,
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
        h('button', { type: 'button', class: 'act critical', 'data-cclearyes': '1' }, 'Delete all'),
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
    if (chatBriefNote()) out.push(h('p', { class: 'cbrief', role: 'status' }, CHAT_BRIEF_NOTE));
    return out;
  }
  /* 0.14.2: "Writing the brief…" in Chat: after Refresh with Chat open (until the brief is written), or while a
     question waits and Callsheet says a brief is being written (one shared model: answers may be slow) */
  var CHAT_BRIEF_NOTE = 'Writing the brief — answers may be slow for a minute.';
  function chatBriefNote() {
    if (CH.briefNote) return true;
    var conv = CH.conv, waiting = !!(CH.sending || (conv && conv.pending));
    return waiting && !!((conv && conv.brief_note) || (CH.ov && CH.ov.brief_note) || (B && B.writing));
  }
  function chatIntro() {
    var ov = CH.ov || {};
    var kids = [h('p', null, 'Ask about your own mail, chat, calendar and notes. Callsheet searches them here on this box and shows where each answer comes from.')];
    if (ov.index_available === false) kids.push(h('p', { class: 'err' }, 'The index isn’t available, so Callsheet can’t search. Check the app log.'));
    else if (!ov.index_total) kids.push(h('p', null, 'Callsheet hasn’t read your history yet.'),
      h('button', { type: 'button', class: 'act', 'data-cknow': '1' }, 'Get to know me'));
    return h('div', { class: 'cintro' }, kids);
  }
  /* 0.14.0: a web source's address, only when it is a plain http(s) address without a user name or password (the
     result's own address from SearXNG; links inside answers stay defanged text). Anything else: no link. */
  function webHref(u) {
    if (typeof u !== 'string') return null;
    try { var x = new URL(u); return (x.protocol === 'https:' || x.protocol === 'http:') && !x.username && !x.password ? x.href : null; }
    catch (e) { return null; }
  }
  /* 0.14.0: a web source: its title and host, a link to the page (a new tab, no referrer). A result that reads like
     instructions stays text, never a link. */
  function webChip(s) {
    var instr = s.instructions === true, href = instr ? null : webHref(s.url);
    var attrs = { class: 'schip wchip' + (instr ? ' instr' : '') + (href ? ' wlink' : ''), title: (s.title || '') + ' · ' + (s.host || ''),
      'aria-label': 'Web page: ' + (s.title || '') + ', ' + (s.host || '') + (instr ? ', contains instructions' : href ? ' (opens in a new tab)' : '') };
    var kids = [h('span', { class: 'wt' }, s.title || s.host || 'Web page'), h('small', { class: 'wh' }, s.host || ''),
      instr ? h('b', { class: 'imark', 'aria-hidden': 'true' }, 'contains instructions') : null];
    if (!href) return h('span', attrs, kids);
    attrs.href = href; attrs.target = '_blank'; attrs.rel = 'noopener noreferrer'; attrs.referrerpolicy = 'no-referrer';
    return h('a', attrs, kids);
  }
  function chatChip(s) {
    var sel = U.open && U.open.kind === 'item' && String(U.open.id) === String(s.id);
    var name = SRC_NAME[s.source] || s.source || 'Item';
    var instr = s.instructions === true;  /* 0.6.0: the item holds an instruction someone planted (inject.py) */
    return h('button', { type: 'button', class: 'schip' + (sel ? ' sel' : '') + (instr ? ' instr' : ''), 'data-open': 'item:' + s.id, title: 'Open: ' + (s.title || name),
      'aria-label': 'Open ' + name + ': ' + (s.title || '') + (s.who ? ', ' + s.who : '') + (s.date ? ', ' + s.date : '') + (instr ? ', contains instructions' : '') }, [
      ico(SRC_ICON[s.source] || 'note'), h('span', null, (s.who || s.title || name) + (s.date ? ' · ' + s.date : '')),
      instr ? h('b', { class: 'imark', 'aria-hidden': 'true' }, 'contains instructions') : null,
      /* 0.8.4: the mail's HTML had text a reader wouldn't see (dropped before indexing) */
      s.hidden_text === true ? h('b', { class: 'imark', title: 'Had hidden text' }, 'had hidden text') : null]);
  }
  /* ---------- 0.14.0: the streamed answer and Show thinking. Sentences arrive whole, defanged and with their marks
     (chat.py Live): all text, never a link. The Thinking panel is the model's working, not an answer. ---------- */
  var LIVE_MARK = { claim: 'the sender’s claim, unverified', instruction: 'reads like an instruction', lure: 'link lure' };
  function liveParas(sents) {
    var paras = [], cur = null;
    (sents || []).forEach(function (r) {
      if (!cur || r.p) { cur = []; paras.push(cur); }
      if (cur.length) cur.push(' ');
      cur.push(h('span', { class: 'ls' + (r.k && r.k.length ? ' lflag' : '') }, String(r.t || '')));
      (r.k || []).forEach(function (k) { cur.push(' ', h('b', { class: 'imark lmark' }, LIVE_MARK[k] || k)); });
    });
    return paras.map(function (kids) { return h('p', null, kids); });
  }
  function thinkPanel(t, key, live) {
    if (!t) return null;
    var openNow = CH.thinkOpen[key] != null ? CH.thinkOpen[key] : !!live;   /* open while it streams, closed after */
    var body = (t.sents && t.sents.length) ? liveParas(t.sents) : [h('p', { class: 'muted' }, live ? 'Thinking…' : 'This model didn’t share its thinking.')];
    if (t.cut) body.push(h('p', { class: 'muted' }, 'Thinking was cut short.'));
    var sm = h('summary', null, [h('span', { class: 'tkl' }, 'Thinking'), h('span', { class: 'tkn' }, 'the model’s working, not an answer')]);
    var d = h('details', { class: 'cthink', open: openNow }, [sm, h('div', { class: 'tkb' }, body)]);
    on(sm, 'click', function () { CH.thinkOpen[key] = !d.open; });   /* the owner's own toggle only (not the default) */
    return d;
  }
  /* the Calm look's channel-reveal switch (settings steel): role=switch; the label is visible text next to it */
  function chsw(onNow, label, fn) {
    var b = h('button', { type: 'button', class: 'chsw', role: 'switch', 'aria-checked': String(!!onNow), 'data-cthink': '1' }, [
      h('span', { class: 'ch', 'aria-hidden': 'true' }, h('span', { class: 'th' })), h('span', { class: 'cl' }, label)]);
    on(b, 'click', fn);
    return b;
  }
  function chatAnswer(m, i) {
    var kids = [wlLabel('idle', m.by)];   /* 0.11.1: the assistant's name, with its mark */
    if (m.thinking && !m.stopped) kids.push(thinkPanel(m.thinking, (CH.conv ? CH.conv.id : '') + ':' + i, false));
    if (m.error) {
      kids.push(h('p', { class: 'err' }, m.error));
      /* 0.6.2: a busy or loading server: one click asks the same question again (only on the latest answer) */
      var msgs = (CH.conv && CH.conv.messages) || [], q = msgs[i - 1];
      if (m.retry && i === msgs.length - 1 && q && q.role === 'user' && !chatPending())
        kids.push(h('button', { type: 'button', class: 'act ghost cretry', 'data-cretry': String(i - 1) }, 'Try again'));
    }
    else if (m.stopped) kids.push(h('p', { class: 'muted' }, 'Stopped.'));
    else {
      if (m.warning) kids.push(h('p', { class: 'err sugwarn' }, m.warning));
      if (m.instr_warning) kids.push(h('p', { class: 'err sugwarn instrwarn' }, m.instr_warning));  /* 0.6.0: repeats a planted instruction */
      String(m.text || '').split(/\n{2,}/).forEach(function (p) { if (p.trim()) kids.push(h('p', null, p.trim())); });
      /* 0.14.0: the ask-first question is a plain message; the reason is one quiet line (the owner just replies) */
      if (m.web_ask && m.web_ask.why) kids.push(h('p', { class: 'cwhy' }, m.web_ask.why + ' Reply yes to search, or write other words.'));
      if (m.sources && m.sources.length) kids.push(h('div', { class: 'from' }, [h('span', { class: 'label' }, 'From'), m.sources.map(chatChip)]));
      var wq = (m.web && m.web.queries) || [];
      if (wq.length) kids.push(h('p', { class: 'cweb' }, [ico('search'), h('span', null, 'Searched the web for ' + wq.map(function (q) { return '‘' + q + '’'; }).join(', '))]));
      if (m.web_sources && m.web_sources.length) kids.push(h('div', { class: 'from wfrom' }, [h('span', { class: 'label' }, 'Web'), m.web_sources.map(webChip)]));
    }
    if (m.proposal) kids.push(chatCard(m, i));  /* 0.8.1 Remember: the Save card (also after a failed answer) */
    if (m.setup) kids.push(scCard(m, i));       /* 0.11.1: the setup card */
    if (m.card) kids.push(ruleCard(m, i));      /* 0.17.0: a setting or action card */
    if (m.open_setup) kids.push(osCard(m, i));  /* 0.22.0: Open in Setup (keys, sign-ins, voice tokens) */
    var steps = (m.steps || []).length ? h('p', { class: 'csteps' }, m.steps.join(' · ')) : null;
    kids.push(h('div', { class: 'macts' }, [steps, m.text ? h('button', { type: 'button', class: 'tbtn', 'data-ccopy': String(i) }, 'Copy') : null]));
    return h('div', { class: 'ma' }, kids);
  }
  /* ---------- 0.8.1 Remember: the Save card. The owner's own "remember that…" gets a card with the note (chat.py
     build_proposal); nothing is saved until Save, which sends the card's one-time token with the (maybe edited) note to
     POST api/memory/notes {kind: "chat"}. The server checks it again: an instruction is refused (its plain reason is
     shown), a decision or claim asks for "I know this is true". Undo (30 s, in the toast) deletes the note again. ---------- */
  function cardState(p) {
    var st = CH.cards[p.pid];
    if (!st) st = CH.cards[p.pid] = { edit: false, text: p.text || '', people: (p.people || []).slice(), topics: (p.topics || []).slice(),
      area: p.area || '', confirm: !!p.confirm, ok: false, warning: p.warning || '', err: '', busy: false, dismissed: false };
    return st;
  }
  function cardOpen(m) {
    var p = m && m.proposal;
    return !!(p && p.status !== 'refused' && p.token && !p.saved && !p.undone && !p.expired && !cardState(p).dismissed);
  }
  function listText(a) { return (a || []).join(', '); }
  function listOf(v) { return String(v || '').split(',').map(function (x) { return x.trim(); }).filter(Boolean).slice(0, 12); }
  function chatCard(m, i) {
    var p = m.proposal;
    if (!p || typeof p !== 'object') return null;
    if (p.status === 'refused') return h('div', { class: 'csave refused', role: 'note' }, h('p', { class: 'err' }, p.reason || 'This can’t be saved.'));
    if (p.saved) return h('div', { class: 'csaved', role: 'status' }, [ico('note'), h('span', null, 'Saved to memory'),
      h('button', { type: 'button', class: 'tbtn', 'data-pmem': '1' }, 'Open in Setup → Memory')]);
    if (p.undone) return h('p', { class: 'muted csaved' }, 'Removed from memory.');
    var st = cardState(p);
    if (st.dismissed) return h('p', { class: 'muted csaved' }, 'Not saved.');
    if (p.expired || !p.token) return h('p', { class: 'muted csaved' }, 'Not saved. This note has expired; ask again to save it.');
    var ix = String(i), id = 'cs' + ix, kids = [h('div', { class: 'hd', id: id + '-h' }, [ico('note'), h('span', null, 'Save to memory?')])];
    if (p.by === 'ai') kids.push(h('p', { class: 'aiw' }, p.note || 'Written by the AI from this chat: check it.'));
    if (st.edit) {
      var ta = h('textarea', { class: 'inp', id: id + '-t', rows: '3', maxlength: '500', 'data-ptext': ix });
      ta.value = st.text;
      var pe = h('input', { class: 'inp', id: id + '-p', type: 'text', maxlength: '300', autocomplete: 'off', 'data-ppeople': ix });
      pe.value = listText(st.people);
      var to = h('input', { class: 'inp', id: id + '-o', type: 'text', maxlength: '300', autocomplete: 'off', 'data-ptopics': ix });
      to.value = listText(st.topics);
      var ar = h('select', { class: 'inp', id: id + '-a', 'data-parea': ix }, [h('option', { value: '' }, 'No area')].concat(((B && B.areas) || []).map(function (a) {
        return h('option', { value: a.name, selected: a.name === st.area ? true : null }, a.name);
      })));
      kids.push(h('div', { class: 'cform' }, [h('label', { class: 'label', for: id + '-t' }, 'Note'), ta,
        h('label', { class: 'label', for: id + '-p' }, 'People (commas between)'), pe,
        h('label', { class: 'label', for: id + '-o' }, 'Topics (commas between)'), to,
        h('label', { class: 'label', for: id + '-a' }, 'Area'), ar]));
    } else {
      kids.push(h('p', { class: 'ctext' }, st.text));
      var keys = [];
      if (st.people.length) keys.push(h('span', { class: 'label' }, 'People'), st.people.map(function (x) { return h('span', { class: 'pchip' }, x); }));
      if (st.topics.length) keys.push(h('span', { class: 'label' }, 'Topics'), st.topics.map(function (x) { return h('span', { class: 'pchip' }, x); }));
      if (st.area) keys.push(h('span', { class: 'label' }, 'Area'), h('span', { class: 'pchip' }, st.area));
      if (keys.length) kids.push(h('div', { class: 'ckeys' }, keys));
    }
    if (st.confirm) kids.push(h('p', { class: 'err sugwarn' }, st.warning || 'This reads like a decision; save anyway?'),
      h('label', { class: 'cok' }, [h('input', { type: 'checkbox', 'data-pok': ix, checked: st.ok ? true : null }), 'I know this is true']));
    if (st.err) kids.push(h('p', { class: 'err', role: 'alert' }, st.err));
    kids.push(h('div', { class: 'acts' }, [
      h('button', { type: 'button', class: 'act', 'data-psave': ix, 'aria-busy': st.busy ? 'true' : null,
        disabled: st.busy || (st.confirm && !st.ok) || !st.text.trim() }, 'Save to memory'),
      h('button', { type: 'button', class: 'act ghost', 'data-pedit': ix, 'aria-pressed': String(st.edit) }, st.edit ? 'Done editing' : 'Edit'),
      h('button', { type: 'button', class: 'tbtn', 'data-pdismiss': ix }, 'Dismiss')]));
    kids.push(h('p', { class: 'fine' }, 'Nothing is saved until you press Save. Edit or remove it later in Setup → Memory.'));
    return h('div', { class: 'csave', role: 'group', 'aria-labelledby': id + '-h' }, kids);
  }
  function chatCardMsg(i) {
    var m = CH.conv && CH.conv.messages && CH.conv.messages[Number(i)];
    return m && m.proposal && m.proposal.pid ? m : null;
  }
  function chatSaveNote(i, b) {
    var m = chatCardMsg(i), p = m && m.proposal, cid = CH.conv && CH.conv.id;
    if (!p || !p.token || !cid) return;
    var st = cardState(p);
    st.busy = true; st.err = ''; b.disabled = true;
    api('POST', 'api/memory/notes', { kind: 'chat', chat: cid, turn: p.turn, token: p.token, text: st.text, people: st.people,
      topics: st.topics, area: st.area, confirm: !!(st.confirm && st.ok) }).then(function (r) {
      st.busy = false;
      if (r && r.confirm) { st.confirm = true; st.ok = false; st.warning = r.warning || ''; chatRender('[data-pok="' + i + '"]'); return; }
      p.saved = r && r.note ? r.note.id : true; delete p.token; MEM.data = null;
      showToast('Saved to memory.', 'Briefs use it from the next one.', r && r.note ? { kind: 'memory', id: r.note.id, pid: p.pid, i: Number(i) } : null, false, 30000);
      chatRender('[data-pmem]');
    }, function (e) { st.busy = false; st.err = e.message; chatRender('[data-psave="' + i + '"]'); });
  }
  function chatUndoNote(u) {
    U.toast = null; renderLayer();
    api('DELETE', 'api/memory/notes/' + encodeURIComponent(u.id)).then(function () {
      MEM.data = null;
      var m = chatCardMsg(u.i);
      if (m && m.proposal.pid === u.pid) { m.proposal.saved = null; m.proposal.undone = true; }
      showToast('Removed from memory.', 'The note is gone.');
      chatRender();
    }, failToast);
  }
  function cardInput(t) {
    var d = t.dataset, k = d.ptext || d.ppeople || d.ptopics || d.parea || d.pok, m = k != null ? chatCardMsg(k) : null;
    if (!m) return false;
    var st = cardState(m.proposal);
    if (d.ptext != null) st.text = t.value;
    else if (d.ppeople != null) st.people = listOf(t.value);
    else if (d.ptopics != null) st.topics = listOf(t.value).map(function (x) { return x.toLowerCase(); });
    else if (d.parea != null) st.area = t.value;
    else if (d.pok != null) st.ok = !!t.checked;
    var sb = main.querySelector('[data-psave="' + k + '"]');
    if (sb) sb.disabled = st.busy || (st.confirm && !st.ok) || !st.text.trim();
    return true;
  }
  function chatBusy(p, canStop) {
    var steps = p.steps || [];
    if (p.live && p.live.length || p.thinking) {   /* 0.14.0: thinking and the answer so far, then a quiet state line */
      var n = ((CH.conv && CH.conv.messages) || []).length, live = p.live && p.live.length;
      return h('div', { class: 'ma mlive', 'data-live': '1' }, [wlLabel(live ? 'speaking' : 'listening'),
        p.thinking ? thinkPanel(p.thinking, (CH.conv ? CH.conv.id : '') + ':' + n, true) : null,
        live ? h('div', { class: 'lans' }, liveParas(p.live)) : null,
        h('p', { class: 'lstate', role: 'status' }, p.checking ? 'checking…' : live ? '' : (p.text || 'Thinking…')),
        steps.length ? h('div', { class: 'bsteps' }, steps.map(function (s) { return h('span', { class: 'bst done' }, s); })) : null,
        canStop ? h('button', { type: 'button', class: 'tbtn', 'data-cstop': '1' }, 'Stop') : null]);
    }
    return h('div', { class: 'ma', 'data-live': '1' }, [wlLabel('listening'),
      h('div', { class: 'busy', role: 'status' }, [h('span', { class: 'spin', 'aria-hidden': 'true' }), p.text || 'Thinking…']),
      steps.length ? h('div', { class: 'bsteps' }, steps.map(function (s) { return h('span', { class: 'bst done' }, s); })) : null,
      canStop ? h('button', { type: 'button', class: 'tbtn', 'data-cstop': '1' }, 'Stop') : null]);
  }
  /* ---------- 0.17.0 Configuring by chat: the setting and action card (chat.py _rule_card; docs/preferences.md). When
     the owner's own latest message says exactly this change, Callsheet applied it at once: "Done: …" with the change's
     rows, then Undo (the card's one-time token, POST api/chat/undo) · See all rules (Setup › Your day › Your rules).
     Anything filled in, taken from earlier messages, or broad waits for Confirm (POST api/chat/apply; the card's one
     loud button). After the token's 30 minutes, Undo lives in Your rules. Data is text. ---------- */
  function rcState(k) { var all = CH.rc || (CH.rc = {}); return all[k.pid] || (all[k.pid] = { busy: false, err: '', dismissed: false }); }
  function rcMsg(i) { var m = CH.conv && CH.conv.messages && CH.conv.messages[Number(i)]; return m && m.card && m.card.pid ? m : null; }
  function rcOpen(m) { var k = m && m.card; return !!(k && k.status === 'ready' && k.token && !k.applied && !k.undone && !k.expired && !rcState(k).dismissed); }
  function rcRows(k) {
    var rows = (Array.isArray(k.rows) ? k.rows : []).filter(function (r) { return Array.isArray(r) && r.length > 1; });
    return rows.length ? h('dl', { class: 'rrows' }, rows.map(function (r) {
      return h('div', { class: 'rr' + (r[0] === 'Guessed' ? ' guess' : '') }, [h('dt', { class: 'label' }, String(r[0])), h('dd', null, String(r[1]))]);
    })) : null;
  }
  /* 0.22.0 Open in Setup: Chat never takes keys, tokens, passwords or sign-ins; this card's one button opens the Setup
     section that does (SETTINGS names it). It writes nothing. */
  var OPEN_SETUP = { voice: 'Voice assistant', claude: 'Claude key and model', github: 'GitHub', slack: 'Slack', signal: 'Signal',
    google_key: 'Google sign-in key', google: 'Google accounts', model: 'Model server' };
  function osCard(m, i) {
    var o = m.open_setup;
    if (!o || typeof o !== 'object') return null;
    var id = 'os' + String(i);
    return h('div', { class: 'csave rcard ask osetup', role: 'group', 'aria-labelledby': id + '-h' }, [
      h('div', { class: 'label rchd', id: id + '-h' }, String(o.title || 'Keys and sign-ins')),
      h('p', { class: 'rcwhy' }, String(o.why || '')),
      h('div', { class: 'acts' }, [h('button', { type: 'button', class: 'act go', 'data-osopen': String(i) }, 'Open in Setup'),
        h('span', { class: 'muted' }, String(o.label || 'Setup'))])]);
  }
  function osOpen(i) {
    var m = CH.conv && CH.conv.messages && CH.conv.messages[Number(i)], o = m && m.open_setup;
    var name = o && OPEN_SETUP[o.section], x = name && SETTINGS.filter(function (y) { return y[2] === name; })[0];
    if (x) openSetting(x); else openSetup('home');
  }
  function ruleCard(m, i) {
    var k = m.card;
    if (!k || typeof k !== 'object') return null;
    if (k.status === 'refused') return h('div', { class: 'csave refused', role: 'note' }, h('p', { class: 'err' }, String(k.reason || 'Nothing was changed.')));
    if (k.status !== 'ready') return null;
    var ix = String(i), id = 'rc' + ix, st = rcState(k), lab = String(k.label || '');
    var see = h('button', { type: 'button', class: 'tbtn', 'data-rcsee': ix }, 'See all rules');
    var err = st.err ? h('p', { class: 'err', role: 'alert' }, st.err) : null;
    if (k.undone) return h('div', { class: 'csaved rcend', role: 'status' }, [h('span', null, 'Undone: ' + lab + '.'), see]);
    var head = h('div', { class: 'label rchd', id: id + '-h' }, String(k.title || 'New rule'));
    if (k.applied && k.kind === 'calendar') return rcCalDone(k, head, id, err);   /* 0.21.0: no Undo (no delete) */
    if (k.type === 'gdraft' && k.applied) return h('div', { class: 'csaved rcend', role: 'status' }, h('span', null, gdOk(k.applied.open_url)));  /* 0.21.0 */
    if (k.area && (k.kind === 'area' || k.kind === 'area_move')) { var ac = rcArea(k, ix, st, head, err); if (ac) return ac; }   /* 0.22.0 */
    if (k.applied) {
      var done = (typeof k.applied === 'object' && k.applied.label) || lab;
      return h('div', { class: 'rcwrap' }, [
        h('div', { class: 'csave rcard done', role: 'group', 'aria-labelledby': id + '-h' }, [head, h('p', { class: 'rcok' }, 'Done: ' + done + '.'), rcRows(k),
          k.related ? h('p', { class: 'rcwhy' }, k.related + ' related clash' + (k.related === 1 ? ' is' : 'es are') + ' still open. Say “and its clashes” to dismiss them too.') : null]),
        h('div', { class: 'cact' }, (k.token ? [h('button', { type: 'button', class: 'tbtn', 'data-rcundo': ix, 'aria-busy': st.busy ? 'true' : null, disabled: st.busy ? true : null }, 'Undo'),
          h('span', { 'aria-hidden': 'true' }, '·')] : []).concat([see])), err]);
    }
    if (st.dismissed) return h('p', { class: 'muted csaved' }, 'Not changed.');
    if (k.expired || !k.token) return h('p', { class: 'muted csaved' }, 'Not changed. This card has expired; ask again.');
    if (k.choices && k.choices.length) return h('div', { class: 'csave rcard ask', role: 'group', 'aria-labelledby': id + '-h' }, [head,   /* 0.22.0 Which one? */
      h('p', { class: 'rcq' }, 'Which one?'), err, h('div', { class: 'rcpick' }, k.choices.map(function (c) {
        return h('div', { class: 'rcrow' }, [h('span', null, String(c.label)), h('button', { type: 'button', class: 'k', 'data-rcpick': ix + ':' + c.pick, disabled: st.busy ? true : null, 'aria-label': 'Confirm: ' + c.label }, 'Confirm')]);
      })), h('div', { class: 'acts' }, [h('button', { type: 'button', class: 'tbtn', 'data-rcdismiss': ix }, 'None of these')]),
      h('p', { class: 'fine' }, 'Nothing changes until you press Confirm.')]);
    return h('div', { class: 'csave rcard ask' + (k.type === 'gdraft' ? ' gd' : ''), role: 'group', 'aria-labelledby': id + '-h' }, [head,
      h('p', { class: 'rcq' }, lab + '?'), k.why ? h('p', { class: 'rcwhy' }, String(k.why)) : null, rcRows(k), err,
      h('div', { class: 'acts' }, [h('button', { type: 'button', class: 'act go', 'data-rcok': ix, 'aria-busy': st.busy ? 'true' : null, disabled: st.busy ? true : null }, k.destructive ? 'Clear' : 'Confirm'),
        h('button', { type: 'button', class: 'tbtn', 'data-rcdismiss': ix }, 'Dismiss')]),
      h('p', { class: 'fine' }, k.type === 'gdraft' ? 'Nothing is saved until you press Confirm. Nothing is sent from here except an RSVP you press.' : k.destructive ? 'This can’t be undone. Nothing changes until you press Clear.' : 'Nothing changes until you press Confirm.')]);
  }
  /* 0.22.0 Chat's area cards (mockup areas-v1): applied, the Done line with the area's dots (Undo · See it in Setup);
     a move that waits, the Areas tab's move box (what moves, and "What it already read": Leave as it is / Move it too,
     sent with Confirm). Display only: names and colours come from Setup through the card (k.area). */
  function rcDot(x) { return x && x.swatch ? adot({ swatch: x.swatch }) : null; }
  function rcArea(k, ix, st, head, err) {
    var a = k.area, mv = k.kind === 'area_move', rows = Array.isArray(k.rows) ? k.rows : [];
    var conn = mv && rows[0] ? String(rows[0][1]) : '', to = a.to || {}, past = st.past == null ? a.past !== false : st.past;
    if (k.applied) {
      var what = mv ? ['Moved ' + conn + ' to ', rcDot(to), to.name || '', past && a.past_n ? ', with ' + a.past_text : '']
        : [rcDot(a.from), (a.from || {}).name || '', h('span', { 'aria-hidden': 'true' }, ' → '), rcDot(to), to.name || '',
          to.swatch && a.from && a.from.swatch && to.swatch.key !== a.from.swatch.key ? ', ' + String(to.swatch.name || '').toLowerCase() : ''];
      return h('div', { class: 'arz rcarz' }, h('div', { class: 'ardone', role: 'status' }, [h('span', { class: 'st' }, [h('i', null, '✓'), ' Done']),
        h('span', { class: 'what' }, what),
        k.token ? h('button', { type: 'button', class: 'k q', 'data-rcundo': ix, 'aria-busy': st.busy ? 'true' : null, disabled: st.busy ? true : null }, 'Undo') : null,
        h('button', { type: 'button', class: 'k q', 'data-rcareas': ix }, 'See it in Setup ›'), err]));
    }
    if (!mv || st.dismissed || k.expired || !k.token) return null;
    var verb = a.conn_kind === 'google' ? 'mail and events are' : a.conn_kind === 'calendar' ? 'events are' : 'items are';
    var seg = h('div', { class: 'arseg', role: 'group', 'aria-label': 'What it already read' }, [['leave', 'Leave as it is'], ['move', 'Move it too']].map(function (o) {
      return h('button', { type: 'button', 'aria-pressed': String(past === (o[0] === 'move')), 'data-rcpast': ix + ':' + o[0] }, o[1]);
    }));
    var box = h('div', { class: 'armv' }, [
      h('div', { class: 'to' }, [h('span', { class: 'ar' }, 'Move to'), rcDot(to), to.name || '']),
      h('ul', null, [h('li', null, [h('i', null, '✓'), h('span', null, conn + ' belongs to ' + (to.name || '') + '.')]),
        h('li', null, [h('i', null, '✓'), h('span', null, 'Its new ' + verb + ' tagged ' + (to.name || '') + ' from now on.')])]),
      a.past_n ? h('div', { class: 'past' }, [h('span', { class: 'fl' }, 'What it already read'), seg,
        h('span', { class: 'sub' }, a.past_text + '. People and projects keep their own area.')]) : null,
      h('div', { class: 'keys' }, [h('button', { type: 'button', class: 'k go', 'data-rcok': ix, 'aria-busy': st.busy ? 'true' : null, disabled: st.busy ? true : null }, 'Confirm'),
        h('button', { type: 'button', class: 'k q', 'data-rcdismiss': ix }, 'Cancel')]), err]);
    head.textContent = a.conn_kind === 'google' ? 'Move an account' : 'Move a connection';
    return h('div', { class: 'csave rcard ask rcmove', role: 'group', 'aria-labelledby': head.id }, [head,
      h('p', { class: 'rcwhy' }, String(k.why || 'Nothing changes until you press Confirm.')), h('div', { class: 'arz rcarz' }, box)]);
  }
  /* 0.21.0 Add to calendar from Chat (always the owner's Confirm): after it, "On your calendar ✓" with Open; there is
     no Undo, because Callsheet has no permission to delete events. */
  function rcCalDone(k, head, id, err) {
    var a = typeof k.applied === 'object' ? k.applied : {};
    return h('div', { class: 'csave rcard done', role: 'group', 'aria-labelledby': id + '-h' }, [head,
      h('p', { class: 'rcok' }, ['On your calendar ✓', a.where ? ' · ' + a.where : '', a.link ? ' · ' : '', a.link ? tdLink(a.link, 'tbtn') : null]),
      rcRows(k), h('p', { class: 'fine' }, 'Callsheet can’t delete events: remove it in Google Calendar.'), err]);
  }
  function rcPress(i, b, undo, pick) {
    var m = rcMsg(i), k = m && m.card, cid = CH.conv && CH.conv.id;
    if (!k || !k.token || !cid) return;
    var st = rcState(k);
    st.busy = true; st.err = ''; b.disabled = true; b.setAttribute('aria-busy', 'true');
    var body = { chat: cid, turn: k.turn, token: k.token };
    if (!undo && k.kind === 'area_move' && k.area && st.past != null) body.past = st.past;   /* 0.22.0: as the owner pressed it */
    if (!undo && pick != null) body.pick = pick;   /* 0.22.0: the Which one? card's choice */
    api('POST', undo ? 'api/chat/undo' : 'api/chat/apply', body).then(function (r) {
      st.busy = false; RL.at = 0; nameSync(k);
      if (undo) { k.applied = null; k.undone = true; delete k.token; chatRender('[data-rcsee="' + i + '"]'); }
      else { k.applied = (r && r.applied) || { label: k.label }; chatRender('[data-rcundo="' + i + '"]'); }
    }, function (e) { st.busy = false; st.err = e.message; chatRender(undo ? '[data-rcundo="' + i + '"]' : '[data-rcok="' + i + '"]'); });
  }
  function rcClick(d, t) {
    if (d.rcok) rcPress(d.rcok, t, false);
    else if (d.rcundo) rcPress(d.rcundo, t, true);
    else if (d.rcpick) { var pk = String(d.rcpick).split(':'); rcPress(pk[0], t, false, parseInt(pk[1], 10)); }
    else if (d.rcsee) { RL.at = 0; openSetup('day', '#rules-h'); }
    else if (d.osopen) osOpen(d.osopen);   /* 0.22.0 Open in Setup */
    else if (d.rcareas) { AR.tab = 'areas'; AR.b = null; arClose(); AR.done = false; openSetup('connections', '[data-fk="ctab:areas"]'); }   /* 0.22.0 */
    else if (d.rcpast) {   /* 0.22.0: the move card's "What it already read" */
      var pp = String(d.rcpast).split(':'), pm = rcMsg(pp[0]);
      if (pm) { rcState(pm.card).past = pp[1] === 'move'; chatRender('[data-rcpast="' + d.rcpast + '"]'); }
    }
    else if (d.rcdismiss) { var m = rcMsg(d.rcdismiss); if (m) { rcState(m.card).dismissed = true; chatRender('[data-ask-in]'); } }
    else return false;
    return true;
  }

  /* ---------- 0.11.1 The assistant's mark (a round one-eyed robot): the Lens (concept D, no glint) with eyelid shutters (concept A). Three still states
     (idle, listening, speaking); a change slides the lids or the pupil in 250 ms, nothing loops, reduced motion turns it
     off. Colours follow the look: the body is the text colour, the face the page. No SVG masks (the lids are clipped by a
     clipPath), so it draws in Home Assistant's app too. Master and small cut: the approved logo. ---------- */
  var WL_SEQ = 0, SVGNS = null;   /* the SVG namespace, taken from a bundled icon (no address in the page) */
  function svgEl(tag, attrs, kids) {
    if (!SVGNS) { var ic = ico('note'); SVGNS = ic ? ic.namespaceURI : document.documentElement.namespaceURI; }
    var el = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    (kids || []).forEach(function (c) { if (c) el.appendChild(c); });
    return el;
  }
  function assistantMark(state, small) {
    var id = 'wl-c' + (++WL_SEQ), face = small ? 18 : 16.5;
    var kids = [svgEl('circle', { class: 'b', cx: '32', cy: '32', r: small ? '26' : '25' })];
    if (!small) [[17.15, 17.15], [46.85, 17.15], [17.15, 46.85], [46.85, 46.85]].forEach(function (p) {
      kids.push(svgEl('circle', { class: 'f', cx: String(p[0]), cy: String(p[1]), r: '1.7' }));
    });
    kids.push(svgEl('circle', { class: 'f', cx: '32', cy: '32', r: String(face) }));
    if (!small) kids.push(svgEl('circle', { class: 'b', cx: '32', cy: '32', r: '13.6' }));
    kids.push(svgEl('g', { class: 'ey' }, [svgEl('circle', { class: 'i', cx: '32', cy: '32', r: small ? '12.5' : '10.5' }),
      svgEl('circle', { class: 'p', cx: '32', cy: '32', r: small ? '5' : '4.2' })]));
    kids.push(svgEl('clipPath', { id: id }, [svgEl('circle', { cx: '32', cy: '32', r: String(face) })]),
      svgEl('g', { 'clip-path': 'url(#' + id + ')' }, [
        svgEl('rect', { class: 'b lt', x: String(32 - face), y: String(32 - 3 * face), width: String(2 * face), height: String(2 * face) }),
        svgEl('rect', { class: 'b lb', x: String(32 - face), y: String(32 + face), width: String(2 * face), height: String(2 * face) })]));
    return svgEl('svg', { class: 'wl' + (small ? ' sm' : ''), viewBox: '0 0 64 64', 'data-s': state || 'idle', 'aria-hidden': 'true', focusable: 'false' }, kids);
  }
  function wlLabel(state, by) {
    return h('span', { class: 'label wlab' }, [assistantMark(state, true), aname(), by ? h('span', { class: 'by' }, ' · ' + by) : null]);
  }

  /* ---------- 0.11.1 Set up from Chat (chat.py build_card; docs/connectors.md "From Chat"). The owner's own "add /
     follow / hook up …" gets a card with Setup's fields. Callsheet builds it from the owner's words: the address is the
     one the owner wrote, the site is shown as Callsheet will contact it (with the xn-- form for an international name)
     and "new site" when Callsheet has never contacted it. Check and Add are Setup's own endpoints with the card's
     one-time token (chat + turn): nothing is read before Check, nothing is added before Add, one loud button at a time.
     After Add the card folds to one line with Undo (removes the connector) and See it in Setup. ---------- */
  var SC_TELLS = [['always', 'Always tell me'], ['important', 'If it’s important'], ['list', 'Only in the list']];
  function scState(c) {
    var all = CH.sc || (CH.sc = {}), st = all[c.pid];
    if (!st) st = all[c.pid] = { stage: c.status === 'refused' ? 'refused' : 'address', addr: c.address || '', name: c.name || '',
      tell: c.tell || 'important', refusal: c.status === 'refused' ? { word: c.word, text: c.text } : null, draft: null, other: null,
      err: '', seq: 0, host: c.host || '', dismissed: false, browse: null,
      where: c.where === 'drive' ? 'drive' : 'folder', dp: {} };   /* 0.12.1: a reports card's Drive folder */
    return st;
  }
  function scBind(c) { return { chat: CH.conv && CH.conv.id, turn: c.turn, token: c.token }; }
  function scOpen(m) { var c = m && m.setup; return !!(c && c.token && !c.added && !c.undone && !c.expired && !scState(c).dismissed); }
  function scMsg(i) { var m = CH.conv && CH.conv.messages && CH.conv.messages[Number(i)]; return m && m.setup && m.setup.pid ? m : null; }
  function scHostOf(a) { var m = /^(?:https:\/\/)?([^\/?#:@\s]+)/i.exec(String(a || '').trim()); return m ? m[1].toLowerCase() : ''; }
  function scSite(host, uni, idn, fresh) {
    if (!host) return null;
    return h('div', { class: 'scsite' }, [h('span', { class: 'label' }, 'Site'), h('b', { class: 'schost' }, host),
      idn && uni ? h('p', { class: 'scnote' }, 'Written as ' + uni + '. The name uses letters from another alphabet: check it’s the site you mean.') : null,
      fresh ? h('p', { class: 'scnote' }, 'New site: Callsheet has never contacted it.') : null]);
  }
  function scCheck(i, other) {
    var m = scMsg(i); if (!m) return;
    var c = m.setup, st = scState(c), addr = String(st.addr || '').trim();
    var body = other ? { kind: c.kind, address: st.other.feed_url, via: st.other.token, chat: scBind(c) } : { kind: c.kind, address: addr, chat: scBind(c) };
    var dpick = c.kind === 'reports' && st.where === 'drive' ? dpPicked(st.dp) : null;   /* 0.12.1 */
    if (c.kind === 'reports' && st.where === 'drive') {
      if (!dpick) { st.err = 'Open the Drive folder for this source first.'; chatRender('[data-sccheck="' + i + '"]'); return; }
      body = { kind: 'reports', where: 'drive', account: dpick.account, address: dpick.folder, chat: scBind(c) };
    }
    if (!body.address) { st.err = c.kind === 'reports' ? 'Pick a folder first.' : 'Paste an address first.'; chatRender('#sc' + i + '-a'); return; }
    var seq = ++st.seq;
    st.stage = 'checking'; st.err = ''; st.reading = other ? st.other.host : dpick ? (st.dp.data && st.dp.data.name) || 'the Drive folder' : (c.kind === 'reports' ? addr : scHostOf(addr));
    chatRender('[data-scstop="' + i + '"]');
    api('POST', 'api/connectors/check', body).then(function (r) {
      if (seq !== st.seq) return;
      if (r && r.stage === 'refused') { st.stage = 'refused'; st.refusal = { word: r.word, text: r.text }; chatRender('[data-scagain="' + i + '"]'); return; }
      if (r && r.stage === 'other_host') { st.stage = 'other'; st.other = r; chatRender('[data-scother="' + i + '"]'); return; }
      st.stage = 'found'; st.draft = r || {}; if (!st.name) st.name = st.draft.label || st.draft.title || '';
      chatRender('#sc' + i + '-n');
    }, function (e) {
      if (seq !== st.seq) return;
      st.stage = 'address'; st.err = e.message; chatRender('#sc' + i + '-a');
    });
  }
  function scAdd(i, b) {
    var m = scMsg(i); if (!m) return;
    var c = m.setup, st = scState(c), d = st.draft;
    if (!d || !d.token || !c.token) return;
    b.disabled = true; st.err = '';
    api('POST', 'api/connectors', { token: d.token, label: String(st.name || '').trim() || null, tell: st.tell, chat: scBind(c) }).then(function (r) {
      c.added = { id: r.id, at: Date.now() / 1000, label: r.label, noun: r.noun, kind: r.kind, every: r.every, tell_label: r.tell_label,
        folder: r.folder, instruction: r.instruction };
      delete c.token; CN.at = 0;
      chatRender('[data-scsee="' + i + '"]');
    }, function (e) { st.err = e.message; chatRender('[data-scadd="' + i + '"]'); });
  }
  function scUndo(i, b) {
    var m = scMsg(i), a = m && m.setup.added;
    if (!a || !a.id) return;
    b.disabled = true;
    api('DELETE', 'api/connectors/' + encodeURIComponent(a.id)).then(function () {
      m.setup.added = null; m.setup.undone = true; CN.at = 0;
      chatRender('[data-ask-in]');
    }, function (e) { b.disabled = false; scState(m.setup).err = e.message; chatRender('[data-scundo="' + i + '"]'); });
  }
  function scBrowse(i, path) {
    var m = scMsg(i); if (!m) return;
    var st = scState(m.setup);
    st.browse = { path: path, loading: true, data: st.browse && st.browse.data };
    api('POST', 'api/connectors/reports/folders', { path: path }).then(function (r) {
      st.browse = { path: path, data: r }; if (r && r.pickable) st.addr = r.path;
      chatRender('[data-scdir="' + i + '"]');
    }, function (e) { st.browse = null; st.err = e.message; chatRender('#sc' + i + '-a'); });
  }
  function scFolders(i, st) {
    var b = st.browse, d = b && b.data;
    if (!b) return h('button', { type: 'button', class: 'tbtn', 'data-scbrowse': i }, 'Pick a folder');
    if (!d) return h('p', { class: 'muted' }, 'Loading…');
    var fs = Array.isArray(d.folders) ? d.folders : [];
    var list = fs.slice(0, 30).map(function (f) {
      return h('li', null, h('button', { type: 'button', class: 'rpdir', 'data-scdir': i, 'data-path': f.path }, [h('span', { class: 'rpdn' }, f.name),
        typeof f.reports === 'number' && f.reports ? h('span', { class: 'muted' }, ' · ' + plural(f.reports, 'report', 'reports')) : null]));
    });
    if (d.path) list.unshift(h('li', null, h('button', { type: 'button', class: 'rpdir up', 'data-scdir': i, 'data-path': d.parent || '' }, '↑ Up')));
    return h('div', { class: 'scdirs' }, [h('p', { class: 'rppath' }, d.path ? ['In ', h('b', null, d.path)] : 'Home Assistant’s shared folders'),
      list.length ? h('ul', { class: 'rpdirs', 'aria-label': 'Folders' }, list) : h('p', { class: 'muted' }, 'No folders in here.')]);
  }
  /* 0.12.1: where a reports card's folder is (this box, or Google Drive for an agent in the cloud) */
  function scWhere(ix, st) {
    return rpTells(RP_WHERE, st.where, 'Where', 'sc-where-' + ix, function (w) {
      st.where = w; st.err = '';
      chatRender('[data-fk="' + cssq('sc-where-' + ix + ':' + w) + '"]');
    });
  }
  function scFolded(c, i) {
    var a = c.added, st = scState(c);
    var when = a.at ? fmtWhen(new Date(a.at * 1000).toISOString()) : '';
    var line = [a.label, a.noun, a.kind === 'news' ? a.every : a.tell_label].filter(Boolean).join(' · ');
    return h('div', { class: 'scdone' }, [h('div', { class: 'csaved', role: 'status' }, [h('span', { class: 'scok', 'aria-hidden': 'true' }, '✓'),
      h('span', { class: 'sct' }, 'Added ' + when + ' · ' + line),
      h('button', { type: 'button', class: 'tbtn', 'data-scundo': i, 'aria-label': 'Undo: remove ' + (a.label || 'it') }, 'Undo'),
      h('button', { type: 'button', class: 'tbtn', 'data-scsee': i }, 'See it in Setup ›')]),
      st.err ? h('p', { class: 'err', role: 'alert' }, st.err) : null,
      a.kind === 'reports' && a.instruction ? rpInstr(a.instruction, 'sc' + i + '-ins') : null]);
  }
  function scCard(m, i) {
    var c = m.setup;
    if (!c || typeof c !== 'object') return null;
    var ix = String(i), id = 'sc' + ix;
    if (c.added) return scFolded(c, ix);
    if (c.undone) return h('p', { class: 'muted csaved' }, 'Removed. Callsheet doesn’t read it any more.');
    var st = scState(c);
    if (st.dismissed) return h('p', { class: 'muted csaved' }, 'Not added.');
    if (c.expired || !c.token) return h('p', { class: 'muted csaved' }, 'Not added. This card has expired; ask again to set it up.');
    var kids = [h('div', { class: 'hd', id: id + '-h' }, h('span', null, c.title || 'Add a connector'))];
    var acts = [];
    if (st.stage === 'checking') {
      kids.push(h('div', { class: 'busy', role: 'status' }, [h('span', { class: 'spin', 'aria-hidden': 'true' }), 'Reading ' + (st.reading || 'it') + '…']));
      acts.push(h('button', { type: 'button', class: 'tbtn', 'data-scstop': ix }, 'Stop'));
    } else if (st.stage === 'refused') {
      kids.push(h('div', { class: 'screfuse', role: 'alert' }, [h('b', null, (st.refusal && st.refusal.word) || 'Can’t use this address'),
        h('p', null, (st.refusal && st.refusal.text) || 'Nothing was read.')]));
      acts.push(h('button', { type: 'button', class: 'act', 'data-scagain': ix }, 'Try again'),
        h('button', { type: 'button', class: 'tbtn', 'data-scdismiss': ix }, 'Back to the chat'));
    } else if (st.stage === 'other') {
      kids.push(h('p', null, ['This site’s feed is on another site, ', h('b', { class: 'schost' }, st.other.host), '. Check it there?']));
      acts.push(h('button', { type: 'button', class: 'act go', 'data-scother': ix }, 'Check ' + st.other.host),
        h('button', { type: 'button', class: 'tbtn', 'data-scback': ix }, 'Back'));
    } else if (st.stage === 'found') {
      var d = st.draft || {}, latest = Array.isArray(d.latest) ? d.latest.slice(0, 3) : [];
      var nm = h('input', { class: 'inp', id: id + '-n', type: 'text', maxlength: '60', autocomplete: 'off', 'data-scname': ix });
      nm.value = st.name;
      kids.push(h('div', { class: 'cnfound' }, [h('b', { class: 'cnfname' }, c.kind === 'reports' ? (d.folder || '') : (d.title || '')),
          latest.length ? h('span', { class: 'label' }, c.kind === 'youtube' ? 'Latest videos' : c.kind === 'reports' ? 'Latest reports' : 'Latest stories') : null,
          latest.length ? h('ol', { class: 'cnlatest' }, latest.map(function (x) {
            return h('li', null, [h('span', null, x.title || ''), x.flagged ? cnFlag(['instructions', 'Reads like instructions · shown as text']) : null]);
          })) : null,
          d.note ? h('p', { class: 'cnnote' }, d.note) : null]),
        d.duplicate ? h('p', { class: 'err' }, 'You’ve already added this as ' + d.duplicate + '.') : null,
        h('div', { class: 'cform' }, [h('label', { class: 'label', for: id + '-n' }, 'Name on Today'), nm]),
        rpTells(d.tells || SC_TELLS, st.tell, 'When to tell you', 'sc-tell-' + ix, function (t) { st.tell = t; chatRender('[data-fk="' + cssq('sc-tell-' + ix + ':' + t) + '"]'); }),
        c.kind === 'reports' ? h('p', { class: 'scnote' }, d.hostline || '') : scSite(d.host, d.host_unicode, d.idn, d.new_site),
        c.kind === 'reports' ? null : h('p', { class: 'fine' }, 'Callsheet will read it ' + (d.every || '') + '. Nothing about you is sent.'));
      acts.push(h('button', { type: 'button', class: 'act go', 'data-scadd': ix }, c.kind === 'youtube' ? 'Add channel' : c.kind === 'reports' ? 'Add folder' : 'Add feed'),
        h('button', { type: 'button', class: 'tbtn', 'data-scback': ix }, 'Back'));
    } else if (c.kind === 'reports' && st.where === 'drive') {   /* 0.12.1: a Google Drive folder, picked here */
      var redraw = function (f) { chatRender(f || '[data-sc="' + ix + '"] [data-fk^="sc-where-"]'); };
      if (!st.dp.data && !st.dp.loading && !st.dp.err) dpStart(st.dp, redraw);
      kids.push(scWhere(ix, st), dpView(st.dp, id + '-dp', redraw));
      acts.push(h('button', { type: 'button', class: 'act go', 'data-sccheck': ix }, 'Check'),
        h('button', { type: 'button', class: 'tbtn', 'data-scdismiss': ix }, 'Dismiss'));
      kids.push(h('p', { class: 'fine' }, 'Nothing is read until you press Check.'));
    } else {
      var inp = h('input', { class: 'inp', id: id + '-a', type: 'text', maxlength: '2000', autocomplete: 'off', spellcheck: 'false',
        inputmode: c.kind === 'reports' ? null : 'url', 'data-scaddr': ix,
        placeholder: c.kind === 'youtube' ? 'youtube.com/@channel' : c.kind === 'reports' ? '/share/reports/…' : 'A feed or a site address' });
      inp.value = st.addr;
      on(inp, 'keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); scCheck(ix, false); } });
      kids.push(h('div', { class: 'cform' }, [h('label', { class: 'label', for: id + '-a' },
        c.kind === 'youtube' ? 'Channel address or @handle' : c.kind === 'reports' ? 'Folder' : 'Feed or site address'), inp]));
      if (c.kind === 'reports') { kids.splice(1, 0, scWhere(ix, st)); kids.push(scFolders(ix, st)); }
      else if (st.addr === c.address) kids.push(scSite(c.host, c.host_unicode, c.idn, c.new_site));
      acts.push(h('button', { type: 'button', class: 'act go', 'data-sccheck': ix }, 'Check'),
        h('button', { type: 'button', class: 'tbtn', 'data-scdismiss': ix }, 'Dismiss'));
      kids.push(h('p', { class: 'fine' }, c.kind === 'reports' ? 'Nothing is read until you press Check.' : 'Nothing is contacted until you press Check.'));
    }
    if (st.err) kids.push(h('p', { class: 'err', role: 'alert' }, st.err));
    kids.push(h('div', { class: 'acts' }, acts));
    return h('div', { class: 'csave scard', role: 'group', 'aria-labelledby': id + '-h', 'data-sc': ix }, kids);
  }
  function scClick(d, t) {
    var k = d.sccheck || d.scother || d.scadd || d.scback || d.scagain || d.scdismiss || d.scstop || d.scundo || d.scsee || d.scbrowse || d.scdir;
    var m = k != null ? scMsg(k) : null;
    if (!m) return false;
    var c = m.setup, st = scState(c);
    if (d.sccheck) scCheck(k, false);
    else if (d.scother) scCheck(k, true);
    else if (d.scadd) scAdd(k, t);
    else if (d.scundo) scUndo(k, t);
    else if (d.scsee) { var aid = c.added && c.added.id; if (aid) { CN.open[aid] = true; CN.at = 0; openSetup('connections', '[data-cn="' + cssq(aid) + '"] [data-fk^="cn-pause"]'); } }
    else if (d.scbrowse) scBrowse(k, null);
    else if (d.scdir) scBrowse(k, d.path || null);
    else if (d.scstop) { st.seq++; st.stage = 'address'; chatRender('#sc' + k + '-a'); }
    else if (d.scback || d.scagain) { st.stage = 'address'; st.draft = null; st.other = null; st.err = ''; chatRender('#sc' + k + '-a'); }
    else if (d.scdismiss) { st.dismissed = true; chatRender('[data-ask-in]'); }
    return true;
  }
  function scInput(t) {
    var d = t.dataset, k = d.scaddr != null ? d.scaddr : d.scname, m = k != null ? scMsg(k) : null;
    if (!m) return false;
    var st = scState(m.setup);
    if (d.scaddr != null) st.addr = t.value; else st.name = t.value;
    return true;
  }
  /* ---------- end 0.11.1 Set up from Chat ---------- */
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
        h('button', { type: 'button', class: CH.conv && (CH.conv.messages || []).some(function (m) { return cardOpen(m) || scOpen(m) || rcOpen(m); }) ? 'act ghost' : 'act', 'data-csend': '1', 'aria-label': 'Ask',
          disabled: chatPending() }, [ico('send'), h('span', { class: 'd-only' }, 'Ask')])]),  /* 0.8.1: the Save card's button is the loud one */
      h('p', { class: 'fine' }, 'Answers come from your own mail, chat and calendar on this box. Callsheet can’t send anything; settings it changes for you can be undone.'
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
          : h('span', { class: 'muted' }, it.source === 'notes' ? 'In your notes folder' : 'No link for this item'))];
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
    else if (d.cretry) { var rq = CH.conv && CH.conv.messages && CH.conv.messages[Number(d.cretry)]; if (rq && rq.role === 'user') { t.disabled = true; chatAsk(rq.text); } }  /* 0.6.2 */
    else if (d.ccopy) chatCopy(t, parseInt(d.ccopy, 10));
    else if (d.cearlier) { CH.earlier = !CH.earlier; CH.clearAsk = false; chatRender('[data-cearlier]'); }
    else if (d.cconv) chatOpen(d.cconv);
    else if (d.cclear) { CH.clearAsk = true; chatRender('[data-cclearno]'); }
    else if (d.cclearno) { CH.clearAsk = false; chatRender('[data-cclear]'); }
    else if (d.cclearyes) chatClearAll(t);
    else if (d.csetup) openSetup('advanced', '#ch-h', 'chat');
    else if (d.cknow) location.hash = 'knowme';
    else if (d.cretry) { CH.err = ''; chatLoad(); }
    /* 0.8.1 Remember: the Save card */
    else if (d.psave) chatSaveNote(d.psave, t);
    else if (d.pedit) { var em = chatCardMsg(d.pedit); if (em) { var es = cardState(em.proposal); es.edit = !es.edit; chatRender(es.edit ? '[data-ptext="' + d.pedit + '"]' : '[data-pedit="' + d.pedit + '"]'); } }
    else if (d.pdismiss) { var dm = chatCardMsg(d.pdismiss); if (dm) { cardState(dm.proposal).dismissed = true; chatRender('[data-ask-in]'); } }
    else if (d.pmem) openSetup('ai', '#mem-h');
    else if (!rcClick(d, t)) scClick(d, t);   /* 0.17.0: the setting and action card; 0.11.1: the setup card */
  });
  document.addEventListener('input', function (e) {
    if (e.target.dataset && e.target.dataset.askIn) CH.input = e.target.value;
    else if (e.target.dataset && U.tab === 'chat' && main.contains(e.target)) { if (!scInput(e.target)) cardInput(e.target); }  /* 0.8.1, 0.11.1 */
  });
  document.addEventListener('change', function (e) {  /* 0.8.1: the Save card's checkbox and area */
    var d = e.target.dataset;
    if (d && (d.pok != null || d.parea != null) && U.tab === 'chat' && main.contains(e.target)) cardInput(e.target);
  });
  document.addEventListener('keydown', function (e) {
    var t = e.target;
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && t.dataset && t.dataset.askIn) { e.preventDefault(); chatAsk(t.value); }
  });

  /* Setup › Advanced › Start over: Clear all chats. (Chat's model is picked in Setup › AI with the other jobs.) */
  var CHS_CLEAR = false;  /* the clear confirm */
  function chatSetupRender(focusKey) {
    if (ui.view !== 'setup') return;
    var a = document.activeElement, keep = focusKey || (a && a.getAttribute && main.contains(a) ? a.getAttribute('data-fk') : null);
    renderSetup(keep ? '[data-fk="' + cssq(keep) + '"]' : null);
  }
  function chatClear() {
    var clrErr = errBox(), clr;
    if (CHS_CLEAR) {
      var yes = btn('btn danger critical', 'Delete all chats', { 'data-fk': 'ch-clryes' }, function () {
        run([yes], clrErr, function () { return api('DELETE', 'api/chat'); }, function () {
          CHS_CLEAR = false; CH.conv = null; CH.cid = null; save(KEY_CHAT, ''); CH.ov = null; CH.items = {};
          chatSetupRender('ch-clr');
        });
      });
      clr = h('div', { class: 'confirm', role: 'group', 'aria-label': 'Delete all chats' }, [h('p', null, 'Delete all chats? They can’t be brought back.'),
        h('div', { class: 'btns' }, [yes, btn('btn', 'Cancel', { 'data-fk': 'ch-clrno' }, function () { CHS_CLEAR = false; chatSetupRender('ch-clr'); })]), clrErr]);
    } else clr = h('div', { class: 'testrow' }, btn('tbtn', 'Clear all chats', { 'data-fk': 'ch-clr' }, function () { CHS_CLEAR = true; chatSetupRender('ch-clryes'); }));
    return clr;
  }
  /* ---------- end 0.6.0 chat ---------- */

  /* ---------- 0.10.0: Connectors (connectors.py, net.py, docs/connectors.md) ----------
     Setup › Connections › Connectors: add a YouTube channel or a news feed (Check shows what was found and the one
     place Callsheet will read; nothing is read on a schedule until Add), Pause and Remove (with a closed Advanced part
     each), What to watch for, and Places Callsheet contacts with the date each was added. The Briefing gets the plain
     list "From your channels and feeds". Feed text is someone else's words: textContent only, and the only links are
     the ones Callsheet built (checked again here); an item that reads like instructions gets a mark and no link. */
  var CN = { v: null, loading: false, err: null, at: 0, seq: 0, focus: null, msg: '',
    flow: null, addr: '', draft: null, other: null, label: '', open: {}, intText: '',
    rp: null, tell: 'important' };  /* 0.10.2a reports: rp = the folder picker */
  function cnLoad() {
    var seq = ++CN.seq;
    CN.loading = true;
    function done() {
      CN.at = Date.now();
      if (!inPlace('connections')) return;
      var fs = CN.focus; CN.focus = null;
      var ae = document.activeElement, fk = ae && ae.getAttribute && ae.getAttribute('data-fk');
      renderSetup(fs || (fk ? '[data-fk="' + cssq(fk) + '"]' : null));
    }
    return api('GET', 'api/connectors').then(function (r) { CN.loading = false; if (seq !== CN.seq) return; CN.v = r; CN.err = null; done(); },
      function (e) { CN.loading = false; if (seq !== CN.seq) return; CN.err = e.message; done(); });
  }
  function cnAgain(focus, msg) { CN.focus = focus || null; CN.msg = msg || ''; return cnLoad(); }
  function cnDay(d) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(d || ''))) return '';
    try { return new Date(d + 'T12:00:00Z').toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }); } catch (e) { return d; }
  }
  /* a link from a connector: https only, no user name, and YouTube links only to watch pages */
  function cnLink(u) {
    var s = safeHttps(u), x;
    if (!s) return null;
    try { x = new URL(s); } catch (e) { return null; }
    if (x.protocol !== 'https:' || x.username || x.password || (x.port && x.port !== '443')) return null;
    if (/(^|\.)youtube\.com$/i.test(x.hostname) && !/^\/watch$/.test(x.pathname)) return null;
    return x.href;
  }
  function cnFlag(m) { return mk8(m[0] === 'instructions' ? 'lure' : m[0] === 'hidden' ? 'unverified' : '', m[1]); }

  function connectorsSlot() {
    if (!CN.loading && (!CN.at || Date.now() - CN.at > 60000)) cnLoad();
    var v = CN.v, head = h('h2', { class: 'h3', id: 'cn-h' }, 'Connectors');
    if (!v) {
      return h('section', { class: 'where cn10', 'aria-labelledby': 'cn-h' }, [head,
        CN.err ? h('p', { class: 'err', role: 'alert' }, ['Couldn’t load the connectors (' + CN.err + '). ',
          btn('tbtn', 'Try again', { 'data-fk': 'cn-retry' }, function () { CN.err = null; CN.at = 0; renderSetup('[data-fk="cn-retry"]'); })])
          : h('p', { class: 'muted' }, 'Loading…')]);
    }
    return h('section', { class: 'where cn10', 'aria-labelledby': 'cn-h' }, [head,
      h('p', { class: 'note' }, 'Public channels and feeds, and folders other agents write reports to, that you add yourself. Callsheet only reads them, and treats what they say as someone else’s words.'),
      CN.msg ? h('p', { class: 'cnmsg', role: 'status' }, CN.msg) : null,
      cnList(v), CN.flow === 'reports' ? rpFlow(v) : CN.flow ? cnFlow(v) : cnAddBtns(v), cnInterests(v), cnPlaces(v)]);
  }
  function cnAddBtns(v) {
    var full = (v.connectors || []).length >= (v.max || 40);
    function go(kind, fk) {
      return btn('btn', kind === 'youtube' ? 'Add a YouTube channel' : kind === 'news' ? 'Add a news feed' : 'Add a reports folder', { 'data-fk': fk, disabled: full }, function () {
        CN.flow = kind; CN.addr = ''; CN.draft = null; CN.other = null; CN.msg = '';
        CN.tell = 'important';
        if (kind === 'reports') { CN.rpWhere = 'folder'; CN.dp = null; rpBrowse(null, '#rp-h'); }
        renderSetup(kind === 'reports' ? '#rp-h' : '#cn-addr');
      });
    }
    return h('div', { class: 'btns cnadd' }, [go('youtube', 'cn-add-yt'), go('news', 'cn-add-news'), go('reports', 'cn-add-rp')]);
  }
  function cnCancel() { var k = CN.flow; CN.flow = null; CN.draft = null; CN.other = null; CN.rp = null; CN.dp = null; CN.rpWhere = null; renderSetup('[data-fk="' + (k === 'news' ? 'cn-add-news' : k === 'reports' ? 'cn-add-rp' : 'cn-add-yt') + '"]'); }
  function cnLast(c) {
    if (c.paused) return 'paused';
    if (c.status === 'new') return 'not read yet';
    return c.last_read ? 'read ' + fmtWhen(c.last_read, true) + (c.last_new ? ' · ' + c.last_new + ' new' : '') : '';
  }
  function cnList(v) {
    var cs = v.connectors || [];
    if (!cs.length) return h('p', { class: 'muted cnnone' }, 'No connectors yet.');
    var n = {}; cs.forEach(function (c) { n[c.host] = (n[c.host] || 0) + 1; });
    return h('ul', { class: 'cnlist', 'aria-label': 'Your connectors' }, cs.map(function (c) { return cnRow(c, n[c.host] > 1); }));
  }
  function cnRow(c, shared) {
    var err = errBox(), open = !!CN.open[c.id], rmKey = 'cn-rm:' + c.id, advId = 'cn-adv-' + c.id, rp = c.kind === 'reports';
    var pauseB = btn('btn sm', c.paused ? 'Resume' : 'Pause', { 'data-fk': 'cn-pause:' + c.id, 'aria-label': (c.paused ? 'Resume ' : 'Pause ') + c.label }, function () {
      run([pauseB], err, function () { return api('PATCH', 'api/connectors/' + encodeURIComponent(c.id), { paused: !c.paused }); }, function () {
        return cnAgain('[data-fk="' + cssq('cn-pause:' + c.id) + '"]', c.paused ? '' : 'Paused. Nothing is read from ' + (rp ? c.folder : c.host) + ' until you resume it.');
      });
    });
    var acts = ui.confirm === rmKey ? confirmRow(rp ? 'Remove ' + c.label + '? Callsheet stops reading the folder and forgets what it read. The files stay where they are.'
      : 'Remove ' + c.label + '? Callsheet stops reading it' + (shared ? '' : ', and ' + c.host + ' leaves Places Callsheet contacts') + '. What was read from it is deleted.', 'Remove', function (yesB, noB, e2) {
      run([yesB, noB], e2, function () { return api('DELETE', 'api/connectors/' + encodeURIComponent(c.id)); }, function (r) {
        ui.confirm = null; CN.v = r || CN.v; CN.open[c.id] = false;
        CN.msg = 'Removed ' + c.label + '.' + (rp || shared ? '' : ' ' + c.host + ' is no longer contacted.');
        renderSetup('[data-fk="cn-add-yt"]');
      });
    }) : h('div', { class: 'btns cnacts' }, [pauseB,
      btn('tbtn', 'Remove', { 'data-fk': 'ask:' + rmKey, 'aria-label': 'Remove ' + c.label }, function () { ui.confirm = rmKey; renderSetup('[data-fk="cancel"]'); })]);
    var advB = btn('tbtn cnadvb', ['Advanced', h('span', { class: 'chev', 'aria-hidden': 'true' }, open ? ' −' : ' +')],
      { 'aria-expanded': String(open), 'aria-controls': advId, 'data-fk': 'cn-adv:' + c.id, 'aria-label': 'Advanced: ' + c.label }, function () {
        CN.open[c.id] = !open; renderSetup('[data-fk="' + cssq('cn-adv:' + c.id) + '"]');
      });
    return h('li', { class: 'cnrow' + (c.paused ? ' paused' : ''), 'data-cn': c.id }, [
      h('div', { class: 'cnmain' }, [h('b', { class: 'cnname' }, c.label),
        h('span', { class: 'muted cnline' }, (rp ? [c.noun, c.folder, c.tell_label, cnLast(c)] : [c.noun, c.host, c.every, c.tell_label, cnLast(c)]).filter(Boolean).join(' · '))]),
      acts,
      c.error && !c.paused ? cnWarn(c, err) : null,
      c.paused ? null : cnAsk(c, err),
      advB,
      h('div', { class: 'cnadv', id: advId, hidden: !open }, open ? (rp ? rpAdv(c, err) : cnAdv(c, err)) : null),
      err]);
  }
  /* 0.10.0 follow-up: a failed read says so, with Try again (one read, within the host's 15-minute gap) */
  function cnWarn(c, err) {
    var k = 'cn-retry:' + c.id;
    var b = btn('btn sm', 'Try again', { 'data-fk': k, 'aria-label': 'Try reading ' + c.label + ' again' }, function () {
      run([b], err, function () { return api('POST', 'api/connectors/' + encodeURIComponent(c.id) + '/read'); }, function (r) {
        return cnAgain('[data-fk="' + cssq(k) + '"]', r && r.error ? '' : 'Read ' + c.label + '.');
      });
    });
    return h('div', { class: 'cnwarn' }, [h('p', { class: 'cnerr' }, c.error), b]);
  }
  /* story links on another host of the feed's own site: asked once, never allowed by Callsheet itself */
  function cnAsk(c, err) {
    var hs = (c.link_offer || []).filter(function (x) { return /^[a-z0-9.-]{1,253}$/.test(String(x)); });
    if (!hs.length) return null;
    return h('div', { class: 'cnask' }, hs.map(function (host) {
      function answer(allow, k) {
        var b = btn(allow ? 'btn sm' : 'tbtn', allow ? 'Show links' : 'Keep hidden', { 'data-fk': k, 'aria-label': (allow ? 'Show links to ' : 'Keep links hidden to ') + host }, function () {
          run([b], err, function () { return api('POST', 'api/connectors/' + encodeURIComponent(c.id) + '/links', { host: host, allow: allow }); }, function () {
            return cnAgain('[data-fk="' + cssq('cn-adv:' + c.id) + '"]', allow ? 'Links to ' + host + ' show from the next read.' : 'Links to ' + host + ' stay hidden.');
          });
        });
        return b;
      }
      return h('div', { class: 'cnq', role: 'group', 'aria-label': 'Links to ' + host }, [
        h('p', null, ['Stories in ' + c.label + ' link to ', h('b', null, host), '. Show those links?']),
        h('div', { class: 'btns' }, [answer(true, 'cn-lk-y:' + c.id + ':' + host), answer(false, 'cn-lk-n:' + c.id + ':' + host)])]);
    }));
  }
  function cnLinksAllowed(c, err) {
    var ls = c.links_allowed || [];
    if (!ls.length) return null;
    return h('div', { class: 'field' }, [h('span', { class: 'label' }, 'Links shown from'),
      h('ul', { class: 'cnhosts' }, ls.map(function (x) {
        var k = 'cn-lk-rm:' + c.id + ':' + x.host;
        var b = btn('tbtn', 'Remove', { 'data-fk': k, 'aria-label': 'Stop showing links to ' + x.host }, function () {
          run([b], err, function () { return api('DELETE', 'api/connectors/' + encodeURIComponent(c.id) + '/links/' + encodeURIComponent(x.host)); }, function () {
            return cnAgain('[data-fk="' + cssq('cn-adv:' + c.id) + '"]', 'Links to ' + x.host + ' are hidden again.');
          });
        });
        return h('li', { class: 'cnhost' }, [h('span', null, [h('b', null, x.host), h('span', { class: 'muted' }, ' · added ' + cnDay(x.added))]), b]);
      }))]);
  }
  function cnAdv(c, err) {
    var every = (CN.v.every_choices || []).map(function (o) {
      var b = btn('chip', o[1], { 'aria-pressed': String(c.every_s === o[0]), 'data-fk': 'cn-every:' + c.id + ':' + o[0] }, function () {
        run([b], err, function () { return api('PATCH', 'api/connectors/' + encodeURIComponent(c.id), { every_s: o[0] }); },
          function () { return cnAgain('[data-fk="' + cssq('cn-every:' + c.id + ':' + o[0]) + '"]'); });
      });
      return b;
    });
    var out = [h('div', { class: 'field' }, [h('span', { class: 'label', id: 'cn-ev-' + c.id }, 'How often'),
      h('div', { class: 'chips seg', role: 'group', 'aria-labelledby': 'cn-ev-' + c.id }, every)]),
      /* 0.11.1: When to tell you, for channels and feeds too (for the rundown; today it orders the Briefing list) */
      rpTells(c.tells || SC_TELLS, c.tell, 'When to tell you', 'cn-tell:' + c.id, function (t, b) {
        run([b], err, function () { return api('PATCH', 'api/connectors/' + encodeURIComponent(c.id), { tell: t }); },
          function () { return cnAgain('[data-fk="' + cssq('cn-tell:' + c.id + ':' + t) + '"]'); });
      })];
    if (c.kind === 'youtube') {
      var s = sw(c.skip_shorts, 'Skip Shorts for ' + c.label, function () {
        run([s], err, function () { return api('PATCH', 'api/connectors/' + encodeURIComponent(c.id), { skip_shorts: !c.skip_shorts }); },
          function () { return cnAgain('[data-fk="' + cssq('cn-shorts:' + c.id) + '"]'); });
      });
      s.setAttribute('data-fk', 'cn-shorts:' + c.id);
      out.push(h('div', { class: 'swrow' }, [s, h('span', { 'aria-hidden': 'true' }, 'Skip Shorts')]),
        h('p', { class: 'note' }, 'Works when YouTube marks a video as a Short in the channel’s feed.'));
    }
    out.push(cnLinksAllowed(c, err));
    return out;
  }

  /* the add flow: Check (the owner's one read), then Add (the consent: the host is listed from now on) */
  function cnFlow(v) {
    var yt = CN.flow === 'youtube', err = errBox(), d = CN.draft, o = CN.other;
    var kids = [h('h3', { class: 'cnflow-h', id: 'cn-flow-h' }, yt ? 'Add a YouTube channel' : 'Add a news feed')];
    function check(btns, body) {
      run(btns, err, function () { return api('POST', 'api/connectors/check', body); }, function (r) {
        if (r && r.stage === 'other_host') { CN.other = r; CN.draft = null; renderSetup('[data-fk="cn-check-other"]'); return; }
        CN.draft = r; CN.label = r.label || r.title || ''; renderSetup('#cn-label');
      });
    }
    if (!d && o) {
      var goB = btn('btn primary', 'Check ' + o.host, { 'data-fk': 'cn-check-other' }, function () {
        check([goB], { kind: CN.flow, address: o.feed_url, via: o.token });
      });
      kids.push(h('p', null, ['This site’s feed is on another site, ', h('b', null, o.host), '. Check it there?']),
        h('div', { class: 'btns' }, [goB, btn('tbtn', 'Cancel', { 'data-fk': 'cn-cancel' }, cnCancel)]), err);
    } else if (!d) {
      var inp = h('input', { class: 'inp', type: 'text', id: 'cn-addr', autocomplete: 'off', spellcheck: 'false', inputmode: 'url',
        value: CN.addr, placeholder: yt ? 'youtube.com/@channel' : 'A feed or a site address' });
      on(inp, 'input', function () { CN.addr = inp.value; });
      var checkB = h('button', { type: 'submit', class: 'btn primary', 'data-fk': 'cn-check' }, 'Check');
      kids.push(on(h('form', { class: 'cnform', novalidate: '', 'aria-labelledby': 'cn-flow-h' }, [
        h('label', { class: 'label', for: 'cn-addr' }, yt ? 'Channel address or @handle' : 'Feed or site address'),
        h('div', { class: 'cnfld' }, [inp, checkB, btn('tbtn', 'Cancel', { 'data-fk': 'cn-cancel' }, cnCancel)]), err]), 'submit', function (e) {
        e.preventDefault();
        if (!inp.value.trim()) { err.textContent = 'Paste an address first.'; inp.focus(); return; }
        check([checkB], { kind: CN.flow, address: inp.value.trim() });
      }));
    } else {
      var lab = h('input', { class: 'inp', type: 'text', id: 'cn-label', autocomplete: 'off', maxlength: '60', value: CN.label });
      on(lab, 'input', function () { CN.label = lab.value; });
      var addB = h('button', { type: 'submit', class: 'btn primary', 'data-fk': 'cn-add-go' }, yt ? 'Add channel' : 'Add feed');
      var latest = Array.isArray(d.latest) ? d.latest : [];
      kids.push(h('div', { class: 'cnfound' }, [h('b', { class: 'cnfname' }, d.title || ''),
        latest.length ? h('span', { class: 'label' }, yt ? 'Latest videos' : 'Latest stories') : null,
        latest.length ? h('ol', { class: 'cnlatest' }, latest.map(function (x) {
          return h('li', null, [h('span', null, x.title || ''), x.flagged ? cnFlag(['instructions', 'Reads like instructions · shown as text']) : null,
            x.kind === 'short' ? mk8('', 'Short') : null]);
        })) : null,
        d.note ? h('p', { class: 'cnnote', role: 'status' }, d.note) : null]),
        d.duplicate ? h('p', { class: 'cnerr' }, 'You’ve already added this as ' + d.duplicate + '.') : null,
        on(h('form', { class: 'cnform', novalidate: '', 'aria-label': yt ? 'Add the channel' : 'Add the feed' }, [
          h('div', { class: 'field' }, [h('label', { class: 'label', for: 'cn-label' }, 'Name on Today'), lab]),
          rpTells(d.tells || SC_TELLS, CN.tell, 'When to tell you', 'cn-tell-new', function (t) { CN.tell = t; renderSetup('[data-fk="' + cssq('cn-tell-new:' + t) + '"]'); }),
          h('p', { class: 'cnhost' }, ['Callsheet will read ', h('b', null, d.host || ''), ' ' + (d.every || '') + '. ' + (v.nothing_sent || 'Nothing about you is sent.')]),
          h('div', { class: 'btns' }, [addB, btn('tbtn', 'Cancel', { 'data-fk': 'cn-cancel' }, cnCancel)]), err]), 'submit', function (e) {
          e.preventDefault();
          run([addB], err, function () { return api('POST', 'api/connectors', { token: d.token, label: lab.value.trim() || null, tell: CN.tell }); }, function (r) {
            var host = d.host; CN.flow = null; CN.draft = null; CN.other = null;
            return cnAgain('[data-cn="' + cssq(r.id) + '"] [data-fk^="cn-pause"]', 'Added. ' + host + ' is now listed in Places Callsheet contacts.');
          });
        }));
    }
    return h('div', { class: 'cnflow', role: 'group', 'aria-labelledby': 'cn-flow-h' }, kids);
  }

  function cnInterests(v) {
    var err = errBox(), ints = v.interests || [], levels = v.levels || [];
    var rows = ints.map(function (it) {
      var rm = btn('rm', '×', { 'aria-label': 'Remove ' + it.text, 'data-fk': 'cn-int-rm:' + it.id }, function () {
        run([rm], err, function () { return api('DELETE', 'api/connectors/interests/' + encodeURIComponent(it.id)); }, function () { return cnAgain('#cn-int'); });
      });
      var seg = h('div', { class: 'chips seg', role: 'group', 'aria-label': 'How to treat ' + it.text }, levels.map(function (l) {
        var b = btn('chip', l[1], { 'aria-pressed': String(it.level === l[0]), 'data-fk': 'cn-lv:' + it.id + ':' + l[0] }, function () {
          run([b], err, function () { return api('PATCH', 'api/connectors/interests/' + encodeURIComponent(it.id), { level: l[0] }); },
            function () { return cnAgain('[data-fk="' + cssq('cn-lv:' + it.id + ':' + l[0]) + '"]'); });
        });
        return b;
      }));
      return h('li', { class: 'cnint' }, [h('span', { class: 'cnintt' }, it.text), seg, rm]);
    });
    var inp = h('input', { class: 'inp', type: 'text', id: 'cn-int', autocomplete: 'off', maxlength: '80', value: CN.intText, placeholder: 'e.g. drone rules in Canada' });
    on(inp, 'input', function () { CN.intText = inp.value; });
    var addB = h('button', { type: 'submit', class: 'btn', 'data-fk': 'cn-int-add', disabled: ints.length >= (v.max_interests || 20) }, 'Add');
    return h('div', { class: 'cnints' }, [h('h3', { class: 'cnsub', id: 'cn-int-h' }, 'What to watch for'),
      h('p', { class: 'note' }, 'Callsheet mentions what matches under What you should know on Today.'),
      rows.length ? h('ul', { class: 'cnintl', 'aria-labelledby': 'cn-int-h' }, rows) : null,
      on(h('form', { class: 'cnform', novalidate: '', 'aria-label': 'Add something to watch for' }, [
        h('label', { class: 'label', for: 'cn-int' }, 'Something to watch for'),
        h('div', { class: 'cnfld' }, [inp, addB]), err]), 'submit', function (e) {
        e.preventDefault();
        if (!inp.value.trim()) { err.textContent = 'Write a topic first.'; inp.focus(); return; }
        run([addB], err, function () { return api('POST', 'api/connectors/interests', { text: inp.value.trim(), level: 'always' }); },
          function () { CN.intText = ''; return cnAgain('#cn-int'); });
      })]);
  }

  /* Places Callsheet contacts for connectors, each with the date the owner added it */
  function cnPlaces(v) {
    var hs = v.hosts || [];
    if (!hs.length) return null;
    return h('div', { class: 'cnplaces' }, [h('h3', { class: 'cnsub', id: 'cn-places-h' }, 'Places Callsheet contacts'),
      h('p', { class: 'note' }, 'Besides your own accounts, Callsheet contacts only these sites, for your connectors.'),
      h('ul', { class: 'cnhosts', 'aria-labelledby': 'cn-places-h' }, hs.map(function (x) {
        var key = 'cn-host:' + x.host;
        var acts = ui.confirm === key ? confirmRow('Stop contacting ' + x.host + '? This removes ' + plural(x.n, 'connector', 'connectors') + ' (' + x.connectors + ') and what was read from ' + (x.n === 1 ? 'it' : 'them') + '.', 'Remove', function (yesB, noB, e2) {
          run([yesB, noB], e2, function () { return api('DELETE', 'api/connectors/hosts/' + encodeURIComponent(x.host)); }, function (r) {
            ui.confirm = null; CN.v = r || CN.v; CN.msg = x.host + ' is no longer contacted.'; renderSetup('[data-fk="cn-add-yt"]');
          });
        }) : btn('tbtn', 'Remove', { 'data-fk': 'ask:' + key, 'aria-label': 'Stop contacting ' + x.host }, function () { ui.confirm = key; renderSetup('[data-fk="cancel"]'); });
        return h('li', { class: 'cnhost' }, [h('span', null, [h('b', null, x.host), h('span', { class: 'muted' }, ' · added ' + cnDay(x.added) + ' · ' + x.connectors)]), acts]);
      }))]);
  }

  /* ---------- 0.10.2a: Reports (conn_reports.py, docs/connectors.md "Reports") ----------
     A folder under /share or /media that another agent writes short Markdown digests to, one folder per source.
     Pick the folder (subfolder names only), Check (the report files found: title, name, date), name the source and
     choose when to be told; the instruction for the other agent is shown filled in, with Copy. The Briefing gets
     "From your reports". Report text is someone else's words: textContent only, no links. */
  var RP_SEQ = 0;
  function rpBrowse(path, focus) {
    var seq = ++RP_SEQ;
    CN.rp = { path: path, data: CN.rp && CN.rp.data, loading: true, err: null };
    api('POST', 'api/connectors/reports/folders', { path: path }).then(function (r) {
      if (seq !== RP_SEQ || !CN.rp) return;
      CN.rp = { path: path, data: r, loading: false, err: null };
      if (inPlace('connections')) renderSetup(focus || '#rp-h');
    }, function (e) {
      if (seq !== RP_SEQ || !CN.rp) return;
      CN.rp.loading = false; CN.rp.err = e.message;
      if (inPlace('connections')) renderSetup(focus || '#rp-h');
    });
  }
  function rpTells(list, cur, label, fk, onPick) {
    var gid = fk + '-h';
    return h('div', { class: 'field' }, [h('span', { class: 'label', id: gid }, label),
      h('div', { class: 'chips seg rptell', role: 'group', 'aria-labelledby': gid }, (list || []).map(function (o) {
        var b = btn('chip', o[1], { 'aria-pressed': String(cur === o[0]), 'data-fk': fk + ':' + o[0] }, function () { onPick(o[0], b); });
        return b;
      }))]);
  }
  /* 0.12.1 Reports from a Google Drive folder (conn_reports_drive.py): where the folder is, and the Drive folder picker
     shared by Setup and the chat card. First the owner's Google accounts, then the folders of one: My Drive, Shared
     with me or a name search, a page at a time, with breadcrumbs. Folder names only (text); nothing is read until
     Check. st = { acc, email, trail: [{ id, name }], q, data, list, next, loading, err, seq } */
  var RP_WHERE = [['folder', 'A folder on this box'], ['drive', 'A Google Drive folder']];
  var DP_SEQ = 0;
  function dpLoad(st, body, redraw, focus, more) {
    var seq = st.seq = ++DP_SEQ, b = { where: 'drive' };
    Object.keys(body || {}).forEach(function (k) { if (body[k] != null) b[k] = body[k]; });
    st.loading = true; st.err = null;
    api('POST', 'api/connectors/reports/folders', b).then(function (r) {
      if (seq !== st.seq) return;
      st.loading = false; st.data = r || {}; st.next = st.data.next || null;
      var fresh = Array.isArray(st.data.folders) ? st.data.folders : [];
      st.list = (more ? st.list || [] : []).concat(fresh);
      redraw(typeof focus === 'function' ? focus(fresh) : focus);
    }, function (e) {
      if (seq !== st.seq) return;
      st.loading = false; st.err = e.message; redraw(typeof focus === 'function' ? focus([]) : focus);
    });
  }
  function dpStart(st, redraw, focus) { st.acc = null; st.trail = []; st.q = ''; st.list = []; dpLoad(st, {}, redraw, focus); }
  function dpGo(st, trail, redraw, focus) {
    var at = trail[trail.length - 1];
    st.trail = trail;
    dpLoad(st, at.search ? { account: st.acc, q: at.search } : { account: st.acc, parent: at.id }, redraw, focus);
  }
  /* the folder this picker is in, when it can be a source (a real folder, not My Drive or a search) */
  function dpPicked(st) { var d = st && st.data; return d && d.pickable && !st.loading && st.acc && d.parent ? { account: st.acc, folder: d.parent } : null; }
  function dpView(st, pre, redraw) {
    var d = st.data, kids = [];
    if (st.err) kids.push(h('p', { class: 'cnerr err', role: 'alert' }, st.err));
    if (!d) { kids.push(h('p', { class: 'muted' }, 'Loading…')); return h('div', { class: 'dpick' }, kids); }
    if (!st.acc) {
      var accs = Array.isArray(d.accounts) ? d.accounts : [];
      kids.push(h('p', { class: 'rppath' }, 'Your Google accounts'),
        accs.length ? h('ul', { class: 'rpdirs', 'aria-label': 'Google accounts' }, accs.map(function (a) {
          return h('li', null, btn('rpdir', [h('span', { class: 'rpdn' }, a.email), a.granted === false ? h('span', { class: 'muted' }, ' · no Drive access yet') : null],
            { 'data-fk': pre + '-acc:' + a.id }, function () {
              st.acc = a.id; st.email = a.email;
              dpGo(st, [{ id: 'root', name: 'My Drive' }], redraw, '[data-fk="' + pre + '-crumb:0"]');
            }));
        })) : h('p', { class: 'muted rpnone' }, 'No Google account yet. Connect one in Setup › Accounts first.'));
      return h('div', { class: 'dpick' }, kids);
    }
    var trail = st.trail || [], crumbs = [btn('tbtn dpc', st.email || 'Accounts', { 'data-fk': pre + '-crumb:acc', 'aria-label': 'Back to your Google accounts' }, function () { dpStart(st, redraw, '[data-fk^="' + pre + '-acc:"]'); })];
    trail.forEach(function (c, i) {
      crumbs.push(h('span', { class: 'dpsep', 'aria-hidden': 'true' }, '›'));
      crumbs.push(i === trail.length - 1 ? h('b', { class: 'dpcur', 'aria-current': 'true', 'data-fk': pre + '-crumb:' + i, tabindex: '-1' }, c.name || '')
        : btn('tbtn dpc', c.name || '', { 'data-fk': pre + '-crumb:' + i }, function () { dpGo(st, trail.slice(0, i + 1), redraw, '[data-fk="' + pre + '-crumb:' + i + '"]'); }));
    });
    var q = h('input', { class: 'inp', type: 'search', id: pre + '-q', autocomplete: 'off', maxlength: '100', 'aria-label': 'Search your Drive folders by name', placeholder: 'Search folders' });
    q.value = st.q || '';
    function search() {
      st.q = q.value.trim();
      if (st.q) dpGo(st, [{ id: null, name: 'Search: ' + st.q, search: st.q }], redraw, '#' + pre + '-q');
    }
    on(q, 'input', function () { st.q = q.value; });
    on(q, 'keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); search(); } });
    kids.push(h('nav', { class: 'dpcrumbs', 'aria-label': 'Where you are in Drive' }, crumbs),
      h('div', { class: 'dpsearch' }, [q, btn('btn', 'Search', { 'data-fk': pre + '-search' }, search)]));
    if (d.need_access) { kids.push(h('p', { class: 'cnnote', role: 'status' }, d.note || 'This account hasn’t given Callsheet read-only Drive access.')); return h('div', { class: 'dpick' }, kids); }
    var here = trail[trail.length - 1] || {}, fs = Array.isArray(st.list) ? st.list : [];
    var list = fs.map(function (f) {
      return h('li', null, btn('rpdir', h('span', { class: 'rpdn' }, f.name), { 'data-fk': pre + '-dir:' + f.id, 'aria-label': 'Open ' + f.name }, function () {
        dpGo(st, (here.search ? [{ id: 'root', name: 'My Drive' }] : trail).concat([{ id: f.id, name: f.name }]), redraw, '[data-fk="' + pre + '-crumb:' + (here.search ? 1 : trail.length) + '"]');
      }));
    });
    if (here.id === 'root' && trail.length === 1) list.unshift(h('li', null, btn('rpdir', h('span', { class: 'rpdn' }, 'Shared with me'), { 'data-fk': pre + '-shared' }, function () {
      dpGo(st, [{ id: 'shared', name: 'Shared with me' }], redraw, '[data-fk="' + pre + '-crumb:0"]');
    })));
    kids.push(list.length ? h('ul', { class: 'rpdirs', 'aria-label': 'Folders' }, list) : h('p', { class: 'muted rpnone' }, st.loading ? 'Loading…' : here.search ? 'No folders with that name.' : 'No folders in here.'));
    if (st.next) kids.push(btn('tbtn', 'More folders', { 'data-fk': pre + '-more', disabled: st.loading }, function () {
      var b = here.search ? { account: st.acc, q: here.search, page: st.next } : { account: st.acc, parent: here.id, page: st.next };
      dpLoad(st, b, redraw, function (fresh) {   /* the first folder of the new page, or More again */
        return fresh.length ? '[data-fk="' + cssq(pre + '-dir:' + fresh[0].id) + '"]' : '[data-fk="' + pre + '-more"]';
      }, true);
    }));
    if (!d.pickable && !st.loading) kids.push(h('p', { class: 'muted' }, 'Open the folder for this source.'));
    return h('div', { class: 'dpick' }, kids);
  }
  /* the text to paste into the other agent, with Copy (the clipboard, or the text selected to copy by hand) */
  function rpInstr(text, id) {
    var ta = h('textarea', { class: 'inp rpins', id: id, rows: '9', readonly: '', spellcheck: 'false' });
    ta.value = text || '';
    var b = btn('btn sm', 'Copy', { 'data-fk': 'copy:' + id, 'aria-label': 'Copy the instruction for the other agent' }, function () {
      function said(t) { b.textContent = t; setTimeout(function () { b.textContent = 'Copy'; }, 2000); }
      function sel() {
        var ok = false;
        try { ta.focus(); ta.select(); ok = document.execCommand && document.execCommand('copy'); } catch (e) { ok = false; }
        said(ok ? 'Copied' : 'Selected: press Ctrl+C');
      }
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(ta.value).then(function () { said('Copied'); }, sel);
      else sel();
    });
    return h('div', { class: 'field rpinsw' }, [h('label', { class: 'label', for: id }, 'For the other agent'), ta, h('div', { class: 'btns' }, [b])]);
  }
  function rpUnread(list) {
    var xs = Array.isArray(list) ? list.filter(function (x) { return x && x.name; }) : [];
    if (!xs.length) return null;
    return h('div', { class: 'field' }, [h('span', { class: 'label' }, 'Couldn’t read'),
      h('ul', { class: 'rpbad' }, xs.map(function (x) { return h('li', null, [h('b', null, x.name), h('span', { class: 'muted' }, ' · ' + (x.why || ''))]); }))]);
  }
  function rpAdv(c, err) {
    return [rpTells(c.tells, c.tell, 'When to tell you', 'rp-tell:' + c.id, function (t, b) {
      run([b], err, function () { return api('PATCH', 'api/connectors/' + encodeURIComponent(c.id), { tell: t }); },
        function () { return cnAgain('[data-fk="' + cssq('rp-tell:' + c.id + ':' + t) + '"]'); });
    }), rpUnread(c.unreadable), rpInstr(c.instruction, 'rp-ins-' + c.id)];
  }
  function rpFlow(v) {
    var err = errBox(), d = CN.draft, p = CN.rp || {}, data = p.data;
    var kids = [h('h3', { class: 'cnflow-h', id: 'rp-h', tabindex: '-1' }, 'Add a reports folder')];
    if (!d) {
      var drive = CN.rpWhere === 'drive', dp = CN.dp || (CN.dp = {});
      var pick = drive ? dpPicked(dp) : null;
      var body = drive ? (pick ? { kind: 'reports', where: 'drive', account: pick.account, address: pick.folder } : null)
        : (data && data.pickable && !p.loading ? { kind: 'reports', address: data.path } : null);
      var checkB = btn('btn primary', 'Check this folder', { 'data-fk': 'rp-check', disabled: !body }, function () {
        run([checkB], err, function () { return api('POST', 'api/connectors/check', body); }, function (r) {
          CN.draft = r; CN.label = r.label || r.title || ''; renderSetup('#cn-label');
        });
      });
      kids.push(rpTells(RP_WHERE, drive ? 'drive' : 'folder', 'Where', 'rp-where', function (w) {
        CN.rpWhere = w;
        if (w === 'drive' && !CN.dp.data && !CN.dp.loading) dpStart(CN.dp, rpRedraw);
        renderSetup('[data-fk="' + cssq('rp-where:' + w) + '"]');
      }));
      if (drive) {
        kids.push(h('p', { class: 'note' }, 'Pick the Google Drive folder your agent in the cloud uploads its reports to, one folder for each source. Callsheet reads it with that account’s read-only Drive access.'),
          dpView(dp, 'rp-dp', rpRedraw), h('div', { class: 'btns' }, [checkB, btn('tbtn', 'Cancel', { 'data-fk': 'cn-cancel' }, cnCancel)]), err);
        return h('div', { class: 'cnflow rpflow', role: 'group', 'aria-labelledby': 'rp-h' }, kids);
      }
      kids.push(h('p', { class: 'note' }, 'Pick the folder another agent writes its reports to, one folder for each source. Other Home Assistant add-ons can read /share and /media too, so use a folder just for these short digests and keep full reports somewhere else.'));
      if (p.err) kids.push(h('p', { class: 'cnerr', role: 'alert' }, p.err));
      if (!data) kids.push(h('p', { class: 'muted' }, 'Loading…'));
      else {
        var fs = Array.isArray(data.folders) ? data.folders : [];
        kids.push(h('p', { class: 'rppath' }, data.path ? ['In ', h('b', null, data.path)] : 'Home Assistant’s shared folders'));
        var list = fs.map(function (f) {
          var n = typeof f.reports === 'number' ? (f.reports ? ' · ' + plural(f.reports, 'report', 'reports') : '') : '';
          return h('li', null, btn('rpdir', [h('span', { class: 'rpdn' }, f.name), n ? h('span', { class: 'muted' }, n) : null],
            { 'data-fk': 'rp-dir:' + f.path, 'aria-label': 'Open ' + f.name }, function () { rpBrowse(f.path, '#rp-h'); }));
        });
        if (data.path) list.unshift(h('li', null, btn('rpdir up', '↑ Up', { 'data-fk': 'rp-up', 'aria-label': 'Up to ' + (data.parent || 'the shared folders') }, function () { rpBrowse(data.parent || null, '#rp-h'); })));
        kids.push(list.length ? h('ul', { class: 'rpdirs', 'aria-label': 'Folders' }, list) : null);
        if (!fs.length) kids.push(h('p', { class: 'muted rpnone' }, data.path ? 'No folders in here.' : 'Callsheet can’t see /share or /media.'));
        kids.push(data.pickable ? null : h('p', { class: 'muted' }, 'Open the folder for this source, inside /share or /media.'),
          h('div', { class: 'btns' }, [checkB, btn('tbtn', 'Cancel', { 'data-fk': 'cn-cancel' }, cnCancel)]), err);
      }
      return h('div', { class: 'cnflow rpflow', role: 'group', 'aria-labelledby': 'rp-h' }, kids);
    }
    var lab = h('input', { class: 'inp', type: 'text', id: 'cn-label', autocomplete: 'off', maxlength: '60', value: CN.label });
    var ins = rpInstr(String(d.instruction || '').replace('\nfrom: ' + (d.label || '') + '\n', '\nfrom: ' + (CN.label || d.label || '') + '\n'), 'rp-ins-new');
    on(lab, 'input', function () {
      CN.label = lab.value;
      var ta = ins.querySelector('textarea');
      if (ta) ta.value = String(d.instruction || '').replace('\nfrom: ' + (d.label || '') + '\n', '\nfrom: ' + (lab.value.trim() || d.label || '') + '\n');
    });
    var addB = h('button', { type: 'submit', class: 'btn primary', 'data-fk': 'cn-add-go' }, 'Add folder');
    var latest = Array.isArray(d.latest) ? d.latest : [];
    kids.push(h('div', { class: 'cnfound' }, [h('b', { class: 'cnfname' }, d.folder || ''),
      h('span', { class: 'label' }, d.files ? plural(d.files, 'report', 'reports') : 'No reports yet'),
      latest.length ? h('ol', { class: 'cnlatest' }, latest.map(function (x) {
        return h('li', null, [h('span', null, x.title || ''), h('span', { class: 'muted' }, ' · ' + (x.name || '')),
          x.flagged ? cnFlag(['instructions', 'Reads like instructions · shown as text']) : null, x.expired ? mk8('', 'Expired') : null]);
      })) : null,
      rpUnread(d.unreadable),
      d.note ? h('p', { class: 'cnnote', role: 'status' }, d.note) : null]),
      d.duplicate ? h('p', { class: 'cnerr' }, 'You’ve already added this folder as ' + d.duplicate + '.') : null,
      on(h('form', { class: 'cnform', novalidate: '', 'aria-label': 'Add the folder' }, [
        h('div', { class: 'field' }, [h('label', { class: 'label', for: 'cn-label' }, 'Name on Today'), lab]),
        rpTells(d.tells, CN.tell, 'When to tell you', 'rp-tell-new', function (t) { CN.tell = t; renderSetup('[data-fk="' + cssq('rp-tell-new:' + t) + '"]'); }),
        h('p', { class: 'cnhost' }, d.hostline || ''),
        h('div', { class: 'btns' }, [addB, btn('tbtn', 'Cancel', { 'data-fk': 'cn-cancel' }, cnCancel)]), err]), 'submit', function (e) {
        e.preventDefault();
        run([addB], err, function () { return api('POST', 'api/connectors', { token: d.token, label: lab.value.trim() || null, tell: CN.tell }); }, function (r) {
          CN.flow = null; CN.draft = null; CN.rp = null; CN.dp = null; CN.rpWhere = null; CN.open[r.id] = true;
          return cnAgain('[data-cn="' + cssq(r.id) + '"] [data-fk^="cn-pause"]', 'Added. Paste the instruction under ' + (r.label || 'it') + ' › Advanced into the other agent.');
        });
      }), ins);
    return h('div', { class: 'cnflow rpflow', role: 'group', 'aria-labelledby': 'rp-h' }, kids);
  }
  function rpRedraw(focus) { if (inPlace('connections') && CN.flow === 'reports') renderSetup(focus || '#rp-h'); }
  /* ---------- end 0.10.0 connectors ---------- */

  /* ---------- 0.9.0: Find or ask (⌘K, Ctrl+K or "/"; the Find button in the header) ----------
     One box: actions on the open reply, people and threads, today's events, projects, tabs and Setup places, every
     setting (SETTINGS), and "Ask: …", which opens Chat with the text. Every item only reads, copies or opens (its
     kind is one of FA_KINDS): nothing marks done, hides, deletes, changes an account or sends. ARIA combobox and
     listbox; text only. "/" is ignored while typing; ⌘K and Ctrl+K work anywhere. */
  var FA_KINDS = ['open', 'copy', 'read'];
  var FA = { open: false, q: '', i: 0, items: [], from: null };
  var faRoot = h('div', { class: 'fa-wrap', hidden: '' });
  app.appendChild(faRoot);
  function faGo(tab, item) {
    U.pop = null;
    if (item) { U.open = item; }
    if (ui.view === 'brief' && B) { U.tab = tab; save(KEY_TAB, tab); history.replaceState(null, '', '#' + tab); renderBrief(item ? '.panel [data-close]' : '#tab-' + tab); }
    else { U.tab = tab; save(KEY_TAB, tab); location.hash = tab; }
  }
  function faItems(q) {
    var out = [], ql = String(q || '').trim().toLowerCase(), ws = ql.split(/\s+/).filter(Boolean);
    function hit(s) { s = String(s || '').toLowerCase(); return ws.every(function (w) { return s.indexOf(w) >= 0; }); }
    function item(group, kind, label, sub, fn, key) { out.push({ group: group, kind: kind, label: String(label || ''), sub: String(sub || ''), run: fn, key: key || '' }); }
    if (B) {
      if (ws.length) {
        events().filter(function (e) { return hit(e.title + ' ' + (e.time || '')); }).slice(0, 4).forEach(function (e) {
          item('Today', 'open', e.title, e.time || '', function () { faGo('agenda', { kind: 'event', id: e.id }); });
        });
        (PJ.d && PJ.d.projects ? PJ.d.projects : []).filter(function (p) { return hit(p.name); }).slice(0, 4).forEach(function (p) {
          item('Projects', 'open', p.name, 'Project', function () { faGo('projects'); pjOpen(p.id); });
        });
      }
    }
    TABS.filter(function (t) { return LIVE_TABS.indexOf(t[0]) >= 0 && !(t[0] === 'chat' && chatOff()); }).forEach(function (t) {
      if (hit(t[1] + ' tab')) item('Go to', 'open', t[1], 'Tab', function () { faGo(t[0]); });
    });
    [['home', 'Setup']].concat(PLACES.map(function (x) { return [x[0], 'Setup: ' + x[1]]; })).forEach(function (x) {
      if (ws.length && hit(x[1])) item('Go to', 'open', x[1], 'Setup', function () { ui.wiz = null; openSetup(x[0]); });
    });
    if (ws.length) findSettings(ql).slice(0, 8).forEach(function (x) { item('Settings', 'open', x[2], setWhere(x), function () { openSetting(x); }); });
    if (ws.length && !chatOff()) item('Ask', 'read', 'Ask: ' + String(q).trim(), 'Chat looks in your own mail, calendar and notes', function () { faAsk(String(q).trim()); }, '↵');
    return out;
  }
  function faAsk(text) {
    faGo('chat');
    var tries = 0;
    (function again() {   /* the Chat tab loads its state first */
      if (U.tab !== 'chat') return;
      if (CH.ov && !chatPending()) { chatAsk(text); return; }
      if (++tries > 20) { CH.input = text; chatRender('[data-ask-in]'); return; }
      setTimeout(again, 150);
    })();
  }
  function faOpen() {
    if (FA.open) return;
    FA.open = true; FA.q = ''; FA.i = 0; FA.from = document.activeElement;
    lookPanel.hidden = true; lookBtn.setAttribute('aria-expanded', 'false');
    faRoot.textContent = ''; faRoot.hidden = false;
    var inp = h('input', { class: 'inp fa-in', id: 'fa-in', type: 'text', role: 'combobox', 'aria-expanded': 'true', 'aria-controls': 'fa-list',
      'aria-autocomplete': 'list', 'aria-label': 'Find or ask', autocomplete: 'off', spellcheck: 'false', placeholder: 'Find a person, a setting, or ask…' });
    var list = h('div', { class: 'fa-list', id: 'fa-list', role: 'listbox', 'aria-label': 'Results' });
    var box = h('div', { class: 'fa', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Find or ask' }, [
      h('div', { class: 'fa-top' }, [ico('search'), inp, h('button', { type: 'button', class: 'fa-x', 'data-fax': '1', 'aria-label': 'Close' }, ico('close'))]),
      list, h('p', { class: 'fa-foot muted' }, '↑ ↓ move · Enter opens · Esc closes · copy and open only · nothing is sent from here')]);
    add(faRoot, [h('div', { class: 'fa-back', 'data-fax': '1' }), box]);
    on(inp, 'input', function () { FA.q = inp.value; FA.i = 0; faList(); });
    on(inp, 'keydown', faKeys);
    on(faRoot, 'mousedown', function (e) { if (e.target.closest('[role=option]')) e.preventDefault(); });
    on(faRoot, 'click', function (e) {
      var o = e.target.closest('[role=option]'); if (o) { faRun(+o.getAttribute('data-fa')); return; }
      if (e.target.closest('[data-fax]')) faClose(true);
    });
    ['header.top', '#main', '.bv-layer'].forEach(function (sel) { var el = document.querySelector(sel); if (el && !el.hasAttribute('inert')) { el.setAttribute('inert', ''); el.setAttribute('data-fa-inert', ''); } });
    document.documentElement.classList.add('fa-lock');
    faList();
    inp.focus();
  }
  function faClose(back) {
    if (!FA.open) return;
    FA.open = false; faRoot.textContent = ''; faRoot.hidden = true;
    document.querySelectorAll('[data-fa-inert]').forEach(function (el) { el.removeAttribute('inert'); el.removeAttribute('data-fa-inert'); });
    document.documentElement.classList.remove('fa-lock');
    if (back && FA.from && document.contains(FA.from)) FA.from.focus();
  }
  function faList() {
    var list = faRoot.querySelector('#fa-list'), inp = faRoot.querySelector('#fa-in');
    if (!list) return;
    var items = FA.items = faItems(FA.q);
    FA.i = Math.max(0, Math.min(FA.i, items.length - 1));
    list.textContent = '';
    var groups = [];
    items.forEach(function (x, i) {
      var g = groups[groups.length - 1];
      if (!g || g.name !== x.group) { g = { name: x.group, el: h('div', { role: 'group', 'aria-labelledby': 'fa-g' + groups.length }, h('div', { class: 'fa-g', id: 'fa-g' + groups.length, role: 'presentation' }, x.group)) }; groups.push(g); add(list, g.el); }
      add(g.el, h('div', { role: 'option', id: 'fa-o-' + i, class: 'fa-o' + (i === FA.i ? ' on' : ''), 'aria-selected': String(i === FA.i), 'data-fa': String(i) }, [
        h('span', { class: 'fa-l' }, x.label), x.sub ? h('span', { class: 'fa-s' }, x.sub) : null, x.key ? h('kbd', { 'aria-hidden': 'true' }, x.key) : null]));
    });
    if (!items.length) add(list, h('p', { class: 'fa-none muted', role: 'status' }, FA.q.trim() ? 'Nothing found.' : 'Type a name, a setting, or a question for Chat.'));
    if (items.length) inp.setAttribute('aria-activedescendant', 'fa-o-' + FA.i); else inp.removeAttribute('aria-activedescendant');
    var on1 = list.querySelector('.fa-o.on'); if (on1 && on1.scrollIntoView) on1.scrollIntoView({ block: 'nearest' });
  }
  function faKeys(e) {
    var n = FA.items.length;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { if (n) { FA.i = (FA.i + (e.key === 'ArrowDown' ? 1 : -1) + n) % n; faList(); } e.preventDefault(); }
    else if (e.key === 'Enter') { if (n) faRun(FA.i); e.preventDefault(); }
    else if (e.key === 'Escape') { faClose(true); e.preventDefault(); e.stopPropagation(); }
    else if (e.key === 'Tab') e.preventDefault();   /* the box keeps focus while it is open */
  }
  function faRun(i) {
    var x = FA.items[i];
    if (!x || FA_KINDS.indexOf(x.kind) < 0) return;
    faClose(false);
    x.run();
  }
  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); if (FA.open) faClose(true); else faOpen(); return; }
    if (FA.open) return;   /* the box's own keys (faKeys); the other key handlers check FA.open */
    if (e.key === '/' && !e.metaKey && !e.ctrlKey && !e.altKey && !e.isComposing && !typing(e.target)) { e.preventDefault(); faOpen(); }
  }, true);
  document.getElementById('find-btn').addEventListener('click', faOpen);

  function liveTabs() { return chatOff() ? LIVE_TABS.filter(function (t) { return t !== 'chat'; }) : LIVE_TABS; }   /* Chat off on this device */

  route();
})();
