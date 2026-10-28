/* Callsheet: Daily Brief page.
   Fetches api/brief (relative URL: works under the HA ingress prefix) and renders one responsive layout:
   v3 frame A on desktop, v3 frame B's structure and order under 700px.
   Data is only ever inserted as text (textContent), never as HTML. */
(function () {
  'use strict';
  var app = document.getElementById('app');
  var main = document.getElementById('main');
  var LOOKS = ['bold', 'clean', 'easy'];
  var MODES = ['auto', 'light', 'dark'];
  var KEY_LOOK = 'callsheet.look', KEY_MODE = 'callsheet.mode';

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
  /* "text with **highlight**" -> text nodes and <b>, safely */
  function rich(s) {
    return String(s || '').split('**').map(function (part, i) { return i % 2 ? h('b', null, part) : part; });
  }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
  function clampColour(c) { c = parseInt(c, 10); return c >= 1 && c <= 4 ? c : 4; }
  function pctOf(p) { return typeof p === 'number' && isFinite(p) ? Math.max(0, Math.min(100, Math.round(p))) : null; }

  /* ---------- look + mode ---------- */
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
    app.dataset.mode = v; press('data-set-mode', v); syncBg();
    if (persist) save(KEY_MODE, v);
  }
  setLook(load(KEY_LOOK)); setMode(load(KEY_MODE));
  onMedia('(prefers-color-scheme: dark)', syncBg);
  onMedia('(max-width: 700px)', syncBg);

  var lookBtn = document.getElementById('look-btn'), lookPanel = document.getElementById('look-panel');
  function openPanel(open) { lookPanel.hidden = !open; lookBtn.setAttribute('aria-expanded', String(open)); }
  lookBtn.addEventListener('click', function () { openPanel(lookPanel.hidden); });
  lookPanel.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { openPanel(false); lookBtn.focus(); }
  });
  document.addEventListener('click', function (e) {
    var t = e.target.closest ? e.target.closest('button') : null;
    if (!t) return;
    if (t.dataset.setLook) setLook(t.dataset.setLook, true);
    else if (t.dataset.setMode) setMode(t.dataset.setMode, true);
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

  /* ---------- render ---------- */
  function render(d) {
    var areas = {}, order = [];
    (d.areas || []).forEach(function (a) { areas[a.id] = a; order.push(a.id); });
    function area(id) { return areas[id] || { id: id, name: String(id || ''), colour: 4 }; }
    function tag(id) { var a = area(id); return h('span', { class: 'tag a' + clampColour(a.colour) }, a.name); }

    var sched = d.schedule || [], replies = d.replies || [], heads = d.heads_up || [], projects = d.projects || [];
    var first = sched.filter(function (e) { return e.first; })[0];
    var clash = sched.filter(function (e) { return e.clash; })[0];
    var sources = [];
    replies.forEach(function (r) { if (r.source && sources.indexOf(r.source) < 0) sources.push(r.source); });

    document.title = 'Daily Brief · ' + (d.date_short || '');
    document.getElementById('sample-tag').hidden = !d.sample;

    /* masthead (desktop) */
    var mast = h('div', { class: 'mast d-only' }, [
      h('div', { class: 't' }, [
        h('div', { class: 'label' }, [d.kind || 'Brief', ' · ', d.written_at || '', d.next_checkin ? ' · next check-in ' + d.next_checkin : '']),
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
      h('div', { class: 'label' }, ['Daily Brief · ', d.date_short || '', ' · ', d.written_at || '']),
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
          h('td', null, [e.title, e.flag ? h('span', { class: 'flag' + (e.flag.ok ? ' ok' : '') }, e.flag.text) : null]),
          h('td', null, tag(e.area)),
          h('td', { class: 'nt' }, e.note || '—')]);
      }))]);
    var schedList = h('div', { class: 'list p-only' }, sched.map(function (e) {
      var warn = e.flag && !e.flag.ok;
      var sm = e.phone_note || (warn ? e.flag.text : e.note || (e.flag && e.flag.text));
      return h('div', { class: 'it', 'data-area': e.area }, [
        h('div', { class: 'tm' }, e.time),
        h('div', null, [e.short || e.title, ' ', tag(e.area), sm ? h('div', { class: 'sm' + (warn ? ' warn' : '') }, sm) : null])]);
    }));
    var secToday = h('section', { class: 'slab s-today', id: 'sec-today', 'aria-labelledby': 'h-today' }, [
      h('h2', { class: 'sec-h', id: 'h-today' }, [h('span', { class: 'd-only' }, 'Schedule'), h('span', { class: 'p-only' }, 'Today'),
        h('span', { class: 'n d-only' }, 'Calendar')]),
      sched.length ? schedTable : h('p', { class: 'muted' }, 'Nothing on the calendar today.'),
      sched.length ? schedList : null]);

    /* needs a reply */
    var replyTable = h('table', { class: 'reply d-only' }, [
      h('thead', null, h('tr', null, [h('th', null, 'Who / what'), h('th', null, 'Age'), h('th', null, h('span', { class: 'sr' }, 'Action'))])),
      h('tbody', null, replies.map(function (r) {
        return h('tr', { 'data-area': r.area }, [
          h('td', null, [h('span', { class: 'who' }, r.who), ' ', tag(r.area), ' ', h('span', { class: 'src' }, r.source), h('br'), r.text]),
          h('td', { class: 'age' }, r.age),
          h('td', null, h('span', { class: 'act' }, r.action))]);
      }))]);
    var replyList = h('div', { class: 'list p-only' }, replies.map(function (r) {
      return h('div', { class: 'it', 'data-area': r.area }, [
        h('div', { class: 'tm' }, r.age),
        h('div', null, [h('b', null, r.who), ' · ', r.short || r.text,
          h('div', { class: 'sm' }, [r.source, area(r.area).name, String(r.action || '').toLowerCase()].filter(Boolean).join(' · '))])]);
    }));
    var secReply = h('section', { class: 'slab s-reply', id: 'sec-replies', 'aria-labelledby': 'h-replies' }, [
      h('h2', { class: 'sec-h', id: 'h-replies' }, ['Needs a reply', sources.length ? h('span', { class: 'n d-only' }, sources.join(' · ')) : null]),
      replies.length ? replyTable : h('p', { class: 'muted' }, 'Nothing waiting on you.'),
      replies.length ? replyList : null]);

    /* heads up */
    var callout = heads.length ? h('div', { class: 'callout', role: 'note', 'aria-label': 'Heads up' }, [
      h('div', { class: 'h' }, 'Heads up'),
      h('ul', null, heads.map(function (x) { return h('li', null, x); }))]) : null;

    /* projects */
    var cards = h('div', { class: 'areas d-only' }, order.map(function (id) {
      var a = area(id), mine = projects.filter(function (p) { return p.area === id; });
      return h('div', { class: 'acard a' + clampColour(a.colour), 'data-area': id }, [
        h('h3', { class: 'areahead' }, a.name)].concat(mine.length ? mine.map(function (p) {
          var pct = pctOf(p.pct), bar = h('i');
          bar.style.width = (pct || 0) + '%';
          return h('div', { class: 'proj' }, [
            h('div', { class: 'nm' }, [p.name, ' ', h('span', null, pct === null ? '—' : pct + '%')]),
            h('div', { class: 'bar', role: 'presentation' }, bar),
            h('div', { class: 'nx' }, rich(p.note))]);
        }) : [h('div', { class: 'proj nx' }, 'Nothing tracked yet')]));
    }));
    cards.style.setProperty('--cols', String(Math.max(1, order.length)));
    var projList = h('div', { class: 'list p-only' }, projects.map(function (p) {
      var pct = pctOf(p.pct);
      return h('div', { class: 'it', 'data-area': p.area }, [
        h('div', { class: 'tm' }, pct === null ? '—' : pct + '%'),
        h('div', null, [h('b', null, p.name), ' · '].concat(rich(p.note), [h('div', { class: 'sm' }, area(p.area).name)]))]);
    }));
    var secProj = h('section', { class: 'projects slab s-proj', id: 'sec-projects', 'aria-labelledby': 'h-projects' }, [
      h('h2', { class: 'sec-h', id: 'h-projects' }, ['Projects', h('span', { class: 'n d-only' }, 'One column per area')]),
      projects.length || order.length ? cards : null,
      projects.length ? projList : h('p', { class: 'muted' }, 'No projects tracked yet.')]);

    var foot = h('div', { class: 'foot' }, [h('span', null, d.sources || ''), h('span', null, 'Nothing was sent. Drafts wait for you.')]);

    var sheet = h('div', { class: 'sheet brief' }, [mast, phTop, chips, h('div', { class: 'grid2' }, [secToday, secReply]), callout, secProj, foot]);
    var tabs = h('nav', { class: 'tabs p-only', 'aria-label': 'Sections' }, [
      h('button', { type: 'button', 'data-target': 'sec-today', 'aria-current': 'true' }, 'Today'),
      h('button', { type: 'button', 'data-target': 'sec-replies' }, 'Replies'),
      h('button', { type: 'button', 'data-target': 'sec-projects' }, 'Projects')]);

    main.textContent = '';
    add(main, [sheet, tabs]);
    syncBg();
  }

  function fail(err) {
    var s = document.getElementById('status');
    if (s) s.textContent = 'Could not load the brief (' + (err && err.message ? err.message : 'unknown error') + ').';
  }

  fetch('api/brief', { cache: 'no-store', headers: { Accept: 'application/json' } })
    .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(render)
    .catch(fail);
})();
