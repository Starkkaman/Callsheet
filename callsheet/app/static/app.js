/* Callsheet: Daily Brief page + Setup.
   Hash routes: #brief (default) and #setup (the step lives in memory).
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
  /* "text with **highlight**" -> text nodes and <b>, safely */
  function rich(s) {
    return String(s || '').split('**').map(function (part, i) { return i % 2 ? h('b', null, part) : part; });
  }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
  function clampColour(c) { c = parseInt(c, 10); return c >= 1 && c <= 4 ? c : 4; }
  function pctOf(p) { return typeof p === 'number' && isFinite(p) ? Math.max(0, Math.min(100, Math.round(p))) : null; }
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
    else if (t.dataset.filter) setFilter(t.dataset.filter);
    else if (t.dataset.target) jump(t.dataset.target);
  });

  /* ---------- phone: area filter + section tabs ---------- */
  function setFilter(id) {
    press('data-filter', id);
    main.querySelectorAll('[data-area]').forEach(function (el) {
      el.classList.toggle('fx', id !== 'all' && el.getAttribute('data-area') !== id);
    });
  }
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  function jump(id) {
    var el = document.getElementById(id);
    if (!el) return;
    el.scrollIntoView({ behavior: reduceMotion.matches ? 'auto' : 'smooth', block: 'start' });
    markTab(id);
  }
  function markTab(id) {
    document.querySelectorAll('.tabs [data-target]').forEach(function (b) {
      if (b.dataset.target === id) b.setAttribute('aria-current', 'true'); else b.removeAttribute('aria-current');
    });
  }
  var ticking = false;
  window.addEventListener('scroll', function () {
    if (ticking) return; ticking = true;
    window.requestAnimationFrame(function () {
      ticking = false;
      var cur = null, line = window.innerHeight * 0.35;
      document.querySelectorAll('.tabs [data-target]').forEach(function (b) {
        var s = document.getElementById(b.dataset.target);
        if (s && s.getBoundingClientRect().top <= line) cur = b.dataset.target;
      });
      var tabs = document.querySelectorAll('.tabs [data-target]');
      if (!cur && tabs.length) cur = tabs[0].dataset.target;
      if (cur) markTab(cur);
    });
  }, { passive: true });

  /* ---------- header: route buttons, last refreshed, refresh ---------- */
  var setupBtn = document.getElementById('setup-btn'), backBtn = document.getElementById('back-btn');
  var fresh = document.getElementById('fresh'), freshT = document.getElementById('fresh-t');
  var refreshBtn = document.getElementById('refresh-btn'), refreshErr = document.getElementById('refresh-err');
  setupBtn.addEventListener('click', function () { ui.step = 1; location.hash = 'setup'; });
  backBtn.addEventListener('click', function () { location.hash = 'brief'; });
  refreshBtn.addEventListener('click', function () {
    run([refreshBtn], refreshErr, function () { return api('POST', 'api/sync', {}); }, function (ls) {
      if (S && ls) S.last_sync = ls;
      if (ls && ls.ok === false) refreshErr.textContent = 'Refreshed with problems: see Heads up.';
      return showBrief();
    });
  });

  /* ---------- render: brief ---------- */
  function status(text) { main.textContent = ''; add(main, h('p', { class: 'status', role: 'status' }, text)); }
  function noAccounts() {
    return !S || !(S.areas || []).some(function (a) { return (a.accounts || []).length; });
  }

  function render(d) {
    var areas = {}, order = [];
    (d.areas || []).forEach(function (a) { areas[a.id] = a; order.push(a.id); });
    /* rows may point at areas that aren't in data.areas (sample projects in a live brief): neutral tag, never dropped */
    function area(id, row) {
      return areas[id] || { id: id, name: String((row && (row.area_name || row.area_label)) || id || '').toUpperCase(), unknown: true };
    }
    function acls(a) { return a.unknown ? 'an' : 'a' + clampColour(a.colour); }
    function tag(id, row) { var a = area(id, row); return h('span', { class: 'tag ' + acls(a) }, a.name); }
    var secs = d.sections || {};
    function sampleTag(k) { return secs[k] === 'sample' ? h('span', { class: 'tag sample' }, 'Sample') : null; }
    function flagsOf(e) {
      var out = [];
      if (e.flag) out.push(e.flag);
      (Array.isArray(e.flags) ? e.flags : []).forEach(function (f) { out.push(typeof f === 'string' ? { text: f } : f); });
      return out.filter(function (f) { return f && f.text; });
    }
    function reasons(r) {
      var rs = Array.isArray(r.reasons) ? r.reasons : [];
      return rs.length ? h('span', { class: 'reasons' }, rs.map(function (x) { return h('span', { class: 'reason' }, String(x)); })) : null;
    }
    function who(r, tagName) {
      var link = safeHttps(r.link);
      if (!link) return h(tagName, { class: 'who' }, r.who);
      return h('a', { class: 'who', href: link, target: '_blank', rel: 'noopener noreferrer' },
        [r.who, h('span', { class: 'sr' }, ' (Open in Gmail)')]);
    }

    var sched = d.schedule || [], replies = d.replies || [], heads = d.heads_up || [], projects = d.projects || [];
    var first = sched.filter(function (e) { return e.first; })[0];
    var clash = sched.filter(function (e) { return e.clash; })[0];
    var sources = [];
    replies.forEach(function (r) { if (r.source && sources.indexOf(r.source) < 0) sources.push(r.source); });
    var written = fmtWhen(d.written_at);

    document.title = 'Daily Brief · ' + (d.date_short || '');
    document.getElementById('sample-tag').hidden = !d.sample;
    freshT.textContent = written ? 'Last refreshed ' + written : 'Not refreshed yet';

    /* first run banner */
    var banner = S && !S.setup_done && noAccounts() ? h('div', { class: 'banner', role: 'region', 'aria-label': 'Setup' }, [
      h('p', null, 'This is sample data. Set up Callsheet to see your own day.'),
      btn('btn primary', 'Start setup', null, function () { ui.step = 1; location.hash = 'setup'; })]) : null;

    /* masthead (desktop) */
    var mast = h('div', { class: 'mast d-only' }, [
      h('div', { class: 't' }, [
        h('div', { class: 'label' }, [d.kind || 'Brief', written ? ' · ' + written : '', d.next_checkin ? ' · next check-in ' + d.next_checkin : '']),
        h('h1', null, d.date_long || '')]),
      h('div', { class: 'box call' }, [
        h('div', { class: 'label' }, 'First meeting'),
        h('div', { class: 'disp' }, first ? first.time : '—'),
        h('div', { class: 's' }, first ? (first.short || first.title) + ' · ' + area(first.area).name : 'Nothing booked')]),
      h('div', { class: 'box' }, [
        h('div', { class: 'label' }, 'Today'),
        h('div', { class: 'disp' }, sched.length + ' / ' + replies.length + ' / ' + heads.length),
        h('div', { class: 's' }, 'events · replies due · heads-ups')])
    ]);

    /* top (phone) */
    var phTop = h('div', { class: 'ph-top p-only' }, [
      h('div', { class: 'label' }, ['Daily Brief · ', d.date_short || '', written ? ' · ' + written : '']),
      h('h1', null, [plural(sched.length, 'event', 'events') + ' today', h('br'),
        h('span', null, plural(replies.length, 'reply', 'replies') + ' due')]),
      h('div', { class: 'stat' }, [
        h('div', { class: 's-call' }, [h('div', { class: 'label' }, 'First meeting'), h('div', { class: 'disp' }, first ? first.time : '—')]),
        h('div', { class: 's-clash' }, [h('div', { class: 'label' }, 'Clash'), h('div', { class: 'disp' }, clash ? clash.time : 'None')])])
    ]);

    var chips = h('div', { class: 'chips p-only', role: 'group', 'aria-label': 'Show area' },
      [h('button', { type: 'button', class: 'chip', 'data-filter': 'all', 'aria-pressed': 'true' }, 'All')].concat(
        order.map(function (id) { return h('button', { type: 'button', class: 'chip', 'data-filter': id, 'aria-pressed': 'false' }, area(id).name); })));

    /* schedule */
    var schedTable = h('table', { class: 'sched d-only' }, [
      h('thead', null, h('tr', null, [h('th', null, 'Time'), h('th', null, 'What'), h('th', null, 'Area'), h('th', null, 'Notes')])),
      h('tbody', null, sched.map(function (e) {
        return h('tr', { 'data-area': e.area }, [
          h('td', { class: 'time' }, e.time),
          h('td', null, [e.title, flagsOf(e).map(function (f) { return h('span', { class: 'flag' + (f.ok ? ' ok' : '') }, f.text); })]),
          h('td', null, tag(e.area, e)),
          h('td', { class: 'nt' }, e.note || '—')]);
      }))]);
    var schedList = h('div', { class: 'list p-only' }, sched.map(function (e) {
      var fl = flagsOf(e), warn = fl.filter(function (f) { return !f.ok; });
      var sm = e.phone_note || (warn.length ? warn.map(function (f) { return f.text; }).join(' · ') : e.note || (fl[0] && fl[0].text));
      return h('div', { class: 'it', 'data-area': e.area }, [
        h('div', { class: 'tm' }, e.time),
        h('div', null, [e.short || e.title, ' ', tag(e.area, e), sm ? h('div', { class: 'sm' + (warn.length ? ' warn' : '') }, sm) : null])]);
    }));
    var secToday = h('section', { class: 'slab s-today', id: 'sec-today', 'aria-labelledby': 'h-today' }, [
      h('h2', { class: 'sec-h', id: 'h-today' }, [h('span', { class: 'd-only' }, 'Schedule'), h('span', { class: 'p-only' }, 'Today'),
        sampleTag('schedule'), h('span', { class: 'n d-only' }, 'Calendar')]),
      sched.length ? schedTable : h('p', { class: 'muted empty' }, 'Nothing on your calendar today.'),
      sched.length ? schedList : null]);

    /* needs a reply */
    var replyTable = h('table', { class: 'reply d-only' }, [
      h('thead', null, h('tr', null, [h('th', null, 'Who / what'), h('th', null, 'Age'), h('th', null, h('span', { class: 'sr' }, 'Action'))])),
      h('tbody', null, replies.map(function (r) {
        return h('tr', { 'data-area': r.area }, [
          h('td', null, [who(r, 'span'), ' ', tag(r.area, r), ' ', h('span', { class: 'src' }, r.source), h('br'), r.text, reasons(r)]),
          h('td', { class: 'age' }, r.age),
          h('td', null, !r.action ? null : safeHttps(r.link)
            ? h('a', { class: 'act', href: safeHttps(r.link), target: '_blank', rel: 'noopener noreferrer', tabindex: '-1', 'aria-hidden': 'true' }, r.action)
            : h('span', { class: 'act' }, r.action))]);
      }))]);
    var replyList = h('div', { class: 'list p-only' }, replies.map(function (r) {
      return h('div', { class: 'it', 'data-area': r.area }, [
        h('div', { class: 'tm' }, r.age),
        h('div', null, [who(r, 'b'), ' · ', r.short || r.text,
          h('div', { class: 'sm' }, [r.source, area(r.area, r).name, String(r.action || '').toLowerCase()].filter(Boolean).join(' · ')),
          reasons(r)])]);
    }));
    var secReply = h('section', { class: 'slab s-reply', id: 'sec-replies', 'aria-labelledby': 'h-replies' }, [
      h('h2', { class: 'sec-h', id: 'h-replies' }, ['Needs a reply', sampleTag('replies'), sources.length ? h('span', { class: 'n d-only' }, sources.join(' · ')) : null]),
      replies.length ? replyTable : h('p', { class: 'muted empty' }, 'No replies waiting.'),
      replies.length ? replyList : null]);

    /* heads up */
    var callout = heads.length || secs.headsup === 'live' ? h('div', { class: 'callout', role: 'note', 'aria-label': 'Heads up' }, [
      h('div', { class: 'h' }, ['Heads up', sampleTag('headsup')]),
      heads.length ? h('ul', null, heads.map(function (x) { return h('li', null, x); })) : h('p', { class: 'empty' }, 'Nothing to flag.')]) : null;

    /* projects */
    var cardOrder = order.slice(), rowOf = {};
    projects.forEach(function (p) { if (cardOrder.indexOf(p.area) < 0) { cardOrder.push(p.area); rowOf[p.area] = p; } });
    var cards = h('div', { class: 'areas d-only' }, cardOrder.map(function (id) {
      var a = area(id, rowOf[id]), mine = projects.filter(function (p) { return p.area === id; });
      return h('div', { class: 'acard ' + acls(a), 'data-area': id }, [
        h('h3', { class: 'areahead' }, a.name)].concat(mine.length ? mine.map(function (p) {
          var pct = pctOf(p.pct), bar = h('i');
          bar.style.width = (pct || 0) + '%';
          return h('div', { class: 'proj' }, [
            h('div', { class: 'nm' }, [p.name, ' ', h('span', null, pct === null ? '—' : pct + '%')]),
            h('div', { class: 'bar', role: 'presentation' }, bar),
            h('div', { class: 'nx' }, rich(p.note))]);
        }) : [h('div', { class: 'proj nx' }, 'Nothing tracked yet')]));
    }));
    cards.style.setProperty('--cols', String(Math.max(1, cardOrder.length)));
    var projList = h('div', { class: 'list p-only' }, projects.map(function (p) {
      var pct = pctOf(p.pct);
      return h('div', { class: 'it', 'data-area': p.area }, [
        h('div', { class: 'tm' }, pct === null ? '—' : pct + '%'),
        h('div', null, [h('b', null, p.name), ' · '].concat(rich(p.note), [h('div', { class: 'sm' }, area(p.area, p).name)]))]);
    }));
    var secProj = h('section', { class: 'projects slab s-proj', id: 'sec-projects', 'aria-labelledby': 'h-projects' }, [
      h('h2', { class: 'sec-h', id: 'h-projects' }, ['Projects', sampleTag('projects'), h('span', { class: 'n d-only' }, 'One column per area')]),
      projects.length || order.length ? cards : null,
      projects.length ? projList : h('p', { class: 'muted' }, 'No projects tracked yet.')]);

    var foot = h('div', { class: 'foot' }, [h('span', null, d.sources || ''), h('span', null, 'Nothing was sent. Drafts wait for you.')]);

    var sheet = h('div', { class: 'sheet brief' }, [mast, phTop, chips, h('div', { class: 'grid2' }, [secToday, secReply]), callout, secProj, foot]);
    var tabs = h('nav', { class: 'tabs p-only', 'aria-label': 'Sections' }, [
      h('button', { type: 'button', 'data-target': 'sec-today', 'aria-current': 'true' }, 'Today'),
      h('button', { type: 'button', 'data-target': 'sec-replies' }, 'Replies'),
      h('button', { type: 'button', 'data-target': 'sec-projects' }, 'Projects')]);

    main.textContent = '';
    add(main, [banner, sheet, tabs]);
    syncBg();
  }

  /* ---------- state ---------- */
  var ui = {
    view: null, step: 1,
    openCals: {},        /* account id -> calendars disclosure open */
    connect: {},         /* area id -> {flow_id, auth_url} while a sign-in runs */
    confirm: null,       /* 'key' | 'acct:<id>' | 'area:<id>' */
    replaceKey: false,
    newArea: { name: '', colour: 2 }
  };
  function applyServerLook() {
    var st = S && S.settings;
    if (!st) return;
    if (lookPending) { st.look = app.dataset.look; st.mode = app.dataset.mode; return; }
    if (LOOKS.indexOf(st.look) >= 0 && st.look !== app.dataset.look) setLook(st.look, true);
    if (MODES.indexOf(st.mode) >= 0 && st.mode !== app.dataset.mode) setMode(st.mode, true);
  }
  function loadState() {
    return api('GET', 'api/state').then(function (s) {
      S = s || {};
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
    var v = location.hash === '#setup' ? 'setup' : 'brief';
    var changed = v !== ui.view;
    ui.view = v;
    setupBtn.hidden = v === 'setup'; backBtn.hidden = v !== 'setup'; fresh.hidden = v !== 'brief';
    refreshErr.textContent = '';
    if (!changed && main.querySelector('.sheet')) return;
    window.scrollTo(0, 0);
    if (v === 'setup') { ui.confirm = null; loadState().then(function () { return showSetup(); }, function () { return showSetup(); }); }
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
      wnav]);
    main.textContent = '';
    add(main, sheet);
    press('data-set-look', app.dataset.look); press('data-set-mode', app.dataset.mode);
    syncBg();
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
  function miniCard(look, desc) {
    return h('button', { type: 'button', class: 'lookcard', 'data-set-look': look, 'aria-pressed': String(app.dataset.look === look) },
      h('span', { class: 'app mini', 'data-look': look, 'data-mode': app.dataset.mode }, [
        h('span', { class: 'm-head' }, [h('span', { class: 'm-word' }, 'Daily Brief'), h('span', { class: 'm-date' }, 'Wed 28 Oct')]),
        h('span', { class: 'm-row' }, [h('span', { class: 'm-time' }, '09:30'), h('span', null, 'Daily stand-up'), h('span', { class: 'm-tag a2' }, 'CO. A')]),
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
    return [
      h('div', { class: 'cols' }, [
        h('div', null, [
          h('div', { class: 'field' }, [h('label', { class: 'label', for: 'f-name' }, 'Your name'), name, nameErr]),
          h('div', { class: 'field' }, [h('label', { class: 'label', for: 'f-tz' }, 'Time zone'), h('span', { class: 'selw' }, tz), tzErr])]),
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
  function areaCard(a) {
    var c = clampColour(a.colour), reads = a.reads || {}, err = errBox();
    function readChip(key, label) {
      var b = btn('chip', label, { 'aria-pressed': String(!!reads[key]) }, function () {
        var next;
        run([b], err, function () {
          next = { calendar: !!reads.calendar, mail: !!reads.mail, chat: !!reads.chat, tasks: !!reads.tasks };
          next[key] = !reads[key];
          return api('PATCH', 'api/areas/' + encodeURIComponent(a.id), { reads: next });
        }, function () {
          reads[key] = next[key]; a.reads = reads; b.setAttribute('aria-pressed', String(next[key]));
        });
      });
      return b;
    }
    function later(label) { return h('button', { type: 'button', class: 'chip later', disabled: true, 'aria-pressed': 'false' }, [label, h('span', { class: 'cl' }, ' · Coming later')]); }
    var accts = a.accounts || [], key = 'area:' + a.id;
    return h('div', { class: 'acard a' + c }, [
      h('h3', { class: 'areahead', tabindex: '-1', 'data-fk': 'areah:' + a.id }, a.name),
      accts.length ? h('div', { class: 'accts' }, accts.map(function (x) { return accountRow(a, x); })) : h('p', { class: 'note none' }, 'No Google account yet.'),
      connectBlock(a),
      h('div', { class: 'reads' }, [h('div', { class: 'q', id: 'q-' + a.id }, 'What may the brief read?'),
        h('div', { class: 'chips', role: 'group', 'aria-labelledby': 'q-' + a.id }, [readChip('calendar', 'Calendar'), readChip('mail', 'Mail'), later('Chat'), later('Tasks')]),
        err]),
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
          h('p', { class: 'note' }, 'Only the number of days can change for now.')])])];
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
        row(false, 'Phone push', 'via the Home Assistant app', 'Coming later')]),
      h('div', { class: 'where' }, [h('h2', { class: 'h3' }, 'Writer'),
        h('p', { class: 'note' }, 'Rules only for now. Claude or a local model comes in a later version.')])];
  }

  /* step 6: review */
  function daysSummary(w) {
    w = DAYS.filter(function (d) { return (w || []).indexOf(d) >= 0; });
    if (!w.length) return 'no working days';
    if (w.join() === 'mon,tue,wed,thu,fri') return 'Mon–Fri';
    return w.map(function (d) { return DAYNAME[d]; }).join(', ');
  }
  function stepReview() {
    var st = S.settings, nAcc = 0, err = errBox();
    S.areas.forEach(function (a) { nAcc += (a.accounts || []).length; });
    var cts = (st.call_times || []).filter(function (c) { return c.enabled; });
    var morning = cts.filter(function (c) { return c.id === 'morning'; })[0], checks = cts.filter(function (c) { return c.id !== 'morning'; });
    var timesSum = [morning ? morning.time + ' brief' : 'No morning brief',
      checks.length ? checks.map(function (c) { return c.time; }).join(' and ') + (checks.length > 1 ? ' check-ins' : ' check-in') +
        (st.skip_checkins_days_off ? ', skipped on days off' : '') : 'no check-ins'].join(' · ');
    var lines = [
      [st.name || 'No name yet', (st.timezone || '').replace(/_/g, ' '), daysSummary(st.workdays), 'Look: ' + LOOKNAME[app.dataset.look] + ', ' + MODENAME[app.dataset.mode]].filter(Boolean).join(' · '),
      [S.areas.map(function (a) { return a.name; }).join(', '),
        S.google_client.configured ? plural(nAcc, 'Google account', 'Google accounts') + ' connected' : 'Google sign-in key not added yet'].join(' · '),
      plural(S.vips.length, 'person', 'people') + ' · needs a reply after ' + plural(st.reply_age_days || 2, 'day', 'days'),
      timesSum,
      'Sidebar page in Home Assistant · Writer: rules only'];
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

  route();
})();
