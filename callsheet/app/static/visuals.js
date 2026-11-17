/* Callsheet 0.19.0 (rework stage 6a): visual blocks, drawn from filled blocks (visuals.py; docs/visuals.md).
   The brief model points at the store by id; the server checks the block and fills in the facts; this file only draws
   them in the current look. Data is text: every string goes in with textContent, never as HTML. Axes and bars are
   built here (inline SVG and positioned boxes): nothing drawn by a model is ever inserted. No URLs are fetched except
   the gallery's own relative api/visuals/gallery. A type this file doesn't know draws nothing.
   API: window.CallsheetVisuals = { render(block) -> element|null, gallery() -> element, TYPES }. */
(function () {
  'use strict';
  /* the SVG namespace, taken from the page's icon template (keeps literal URLs out of the static files) */
  var NS = (function () {
    var t = document.getElementById('icons');
    var s = t && t.content && t.content.querySelector('svg');
    return (s && s.namespaceURI) || ['http:', '', 'www.w3.org', '2000', 'svg'].join('/');
  })();

  function add(e, kids) {
    [].concat(kids == null ? [] : kids).forEach(function (k) {
      if (k == null || k === false) return;
      e.appendChild(typeof k === 'string' || typeof k === 'number' ? document.createTextNode(String(k)) : k);
    });
    return e;
  }
  function el(tag, cls, kids, attrs) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (attrs) Object.keys(attrs).forEach(function (k) { if (attrs[k] != null && attrs[k] !== false) e.setAttribute(k, String(attrs[k])); });
    return add(e, kids);
  }
  function sv(tag, attrs, kids) {
    var e = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, String(attrs[k])); });
    (kids || []).forEach(function (k) { e.appendChild(k); });
    return e;
  }
  function at(e, left) { e.style.left = left; return e; }
  function pct(v) { return Math.max(0, Math.min(100, +v || 0)).toFixed(2) + '%'; }
  function hide(e) { e.setAttribute('aria-hidden', 'true'); return e; }

  /* line icons (24 x 24), plain glyphs, not brand marks */
  var ICON = {
    mail: [['rect', { x: 3, y: 5.5, width: 18, height: 13, rx: 2 }], ['path', { d: 'M3.8 7.2 12 13l8.2-5.8' }]],
    signal: [['path', { d: 'M12 4a8 8 0 0 0-6.9 12.1L4 20l4-1.1A8 8 0 1 0 12 4z' }]],
    text: [['rect', { x: 7, y: 3, width: 10, height: 18, rx: 2.2 }], ['path', { d: 'M10.5 17.6h3' }]],
    slack: [['path', { d: 'M9.6 4 8 20M16 4l-1.6 16M4.5 9h15.5M4 15h15.5' }]],
    cal: [['rect', { x: 3.5, y: 5, width: 17, height: 15, rx: 2 }], ['path', { d: 'M3.5 10h17M8 3v4M16 3v4' }]],
    news: [['rect', { x: 4, y: 4, width: 16, height: 16, rx: 2 }], ['path', { d: 'M8 9h8M8 12.5h8M8 16h5' }]],
    plane: [['path', { d: 'M12 3c.8 0 1.5.7 1.5 1.5V10l7 4v2l-7-2v4.5l2 1.5v1.5L12 20.5 8.5 21.5V20l2-1.5V14l-7 2v-2l7-4V4.5C10.5 3.7 11.2 3 12 3z', transform: 'rotate(90 12 12)' }]],
    lock: [['rect', { x: 5, y: 10.5, width: 14, height: 10, rx: 2 }], ['path', { d: 'M8 10.5V8a4 4 0 0 1 8 0v2.5' }]],
    box: [['path', { d: 'M3.5 7.5 12 3.5l8.5 4v9L12 20.5l-8.5-4z' }], ['path', { d: 'M3.5 7.5 12 11.5l8.5-4M12 11.5v9' }]],
    repeat: [['path', { d: 'M5 11a7 7 0 0 1 12.4-4.4L19 8.5M19 3.5v5h-5M19 13a7 7 0 0 1-12.4 4.4L5 15.5M5 20.5v-5h5' }]],
    alert: [['path', { d: 'M12 4 21 20H3zM12 10v4.5M12 17.2v.3' }]],
    check: [['path', { d: 'M5 12.5l4.5 4.5L19 7.5' }]]
  };
  var SRCWORD = { mail: 'Email', signal: 'Signal', slack: 'Slack', text: 'Text', cal: 'Calendar', news: 'Feed' };
  function icon(name, cls) {
    var s = sv('svg', { viewBox: '0 0 24 24', 'class': 'vz-si' + (cls ? ' ' + cls : ''), 'aria-hidden': 'true', focusable: 'false' });
    (ICON[name] || ICON.mail).forEach(function (p) { s.appendChild(sv(p[0], p[1])); });
    return s;
  }
  function srcIcon(k) { return el('span', 'vz-src', [icon(k), el('span', 'vz-vh', SRCWORD[k] || 'Message')]); }

  /* ---------- the time axis: lanes on hours (day timeline, clash diagram, composite time_axis) ---------- */
  function gantt(d) {
    var span = d.b - d.a || 1;
    function P(h) { return pct((h - d.a) / span * 100); }
    function W(s, e) { return pct((Math.min(e, d.b) - Math.max(s, d.a)) / span * 100); }
    var nr = d.now != null && (d.now - d.a) / span > 0.85;
    function near(t) {                /* a tick label under the Now label: 2 = always hidden, 1 = hidden when narrow */
      if (d.now == null) return 0;
      var x = (t - d.now) / span * (nr ? -1 : 1);
      return x > -0.06 && x < 0.11 ? 2 : x > -0.12 && x < 0.3 ? 1 : 0;
    }
    var ax = el('div', 'vz-gax', (d.ticks || []).filter(function (t) { return near(t[0]) < 2; }).map(function (t) {
      return at(el('span', near(t[0]) ? 'nn' : null, t[1]), P(t[0]));
    }));
    if (d.now != null) add(ax, at(el('span', 'vz-nl' + (nr ? ' r' : ''), 'Now'), P(d.now)));
    var g = el('div', 'vz-gt', [el('div', 'vz-gl0'), ax]);
    (d.lanes || []).forEach(function (l) {
      add(g, el('div', 'vz-gl', [el('b', null, l.l), l.sub ? el('span', l.al ? 'al' : null, l.sub) : null]));
      var tk = el('div', 'vz-gtk');
      if (d.ov) add(tk, at(el('i', 'vz-ov'), P(d.ov[0]))).lastChild.style.width = W(d.ov[0], d.ov[1]);
      if (l.gap) {
        var gp = at(el('i', 'vz-gp'), P(l.s)); gp.style.width = W(l.s, l.e);
        add(tk, [gp, at(el('span', 'vz-gpl', l.gap), P((l.s + l.e) / 2))]);
      } else if (l.dot) {
        add(tk, at(el('i', 'vz-dot'), P(l.s)));
      } else {
        var bar = at(el('i', 'vz-bar' + (l.k === 'msg' ? ' msg' : '') + (l.al ? ' al' : '')), P(l.s));
        bar.style.width = W(l.s, l.e);
        add(tk, bar);
      }
      if (d.now != null) add(tk, at(el('i', 'vz-nw'), P(d.now)));
      add(g, tk);
    });
    return g;
  }

  /* ---------- the block types ---------- */
  var R = {};
  R.day = function (d) { return [el('p', 'vz-dl', d.date), gantt(d)]; };
  R.clash = function (d) {
    return el('div', 'vz-cdv', [el('div', 'vz-cdh ' + d.lv, [d.lv === 'al' ? el('span', 'vz-led') : el('i', null, '▲'), el('span', null, d.when),
      el('span', 'vz-until', d.until)]), gantt(d)]);
  };
  R.asks = function (d) {
    var rows = d.rows.map(function (r) {
      var tk = el('span', 'vz-tk', el('i')); tk.firstChild.style.width = pct(r.p);
      return el('div', 'vz-nsr', [srcIcon(r.src), el('span', 'vz-nm', r.who), hide(tk), el('span', 'vz-ag', r.age)]);
    });
    if (d.more) rows.push(el('p', 'vz-sub vz-mt8', d.more + ' more waiting'));
    return rows;
  };
  R.upcoming = function (d) {
    var strip = el('div', 'vz-strip'), cols = d.cells.length;
    strip.style.gridTemplateColumns = 'repeat(' + cols + ',minmax(0,1fr))';
    d.cells.forEach(function (c) { add(strip, el('i', c || null)); });
    d.weeks.forEach(function (w) {
      var s = el('span', 'vz-wl' + (w[1] < 5 ? ' sm' : ''), w[1] >= 4 ? w[2] : '');   /* a 4-day stub's label only shows when it fits */
      s.style.gridColumn = w[0] + ' / span ' + w[1];
      add(strip, s);
    });
    var out = [hide(strip)];
    d.groups.forEach(function (g) {
      out.push(el('div', 'vz-cgh', [el('span', null, g.h), el('span', 'r', g.r)]));
      g.rows.forEach(function (r) {
        out.push(el('div', 'vz-up', [el('span', 'vz-rel', r.rel), el('span', 'vz-w', r.w), el('span', 'vz-d', r.d),
          r.from ? el('span', 'vz-fx', [icon(r.src), r.from]) : null]));
      });
    });
    if (d.more > 0) out.push(el('p', 'vz-sub vz-mt8', d.more + ' more, to ' + d.last));
    return out;
  };
  function totalRow(label, v) { return v ? el('div', 'vz-motot', [el('span', null, label), el('b', null, v)]) : null; }
  R.money = function (d) {
    return d.rows.map(function (r) {
      return el('div', 'vz-mor', [el('b', null, r.n), el('span', 'vz-amt', r.amt || '·'), el('span', 'vz-due', r.due)]);
    }).concat(totalRow('Total', d.total));
  };
  R.trip = function (d) {
    return el('div', 'vz-tp', [el('p', 'vz-to', [d.to, d.nights ? el('span', null, d.nights) : null]),
      hide(el('div', 'vz-leg', [el('i', 'vz-dt2'), el('i', 'vz-ln2'), icon('plane'), el('i', 'vz-ln2'), el('i', 'vz-dt2')]))]
      .concat(d.rows.map(function (r) { return el('div', 'vz-tpr', [el('span', 'vz-fl', r.k), el('span', null, r.t)]); }))
      .concat(el('p', 'vz-sub vz-mt8', d.note)));
  };
  R.person = function (d) {
    var bt = el('div', 'vz-bt', d.parts.map(function (p) { return p[1] ? el('mark', null, p[0]) : p[0]; }));
    return [el('div', 'vz-pr', [hide(el('span', 'vz-av', d.initial)), el('b', null, d.name), el('span', 'vz-ag', d.age),
      el('span', 'vz-sub', d.sub)]),
      el('div', 'vz-bm' + (d.ask ? ' ask' : '') + (d.mine ? ' me' : ''), [el('span', 'vz-mt', [d.meta, d.ask ? el('span', 'vz-tag', 'The ask') : null]), bt])];
  };
  R.cleanup = function (d) {
    var cls = ['a', 'b', 'c', 'd'];
    var bar = el('div', 'vz-csb', d.groups.map(function (g, i) { var x = el('i', cls[i]); x.style.flex = String(g.n); return x; }));
    return [el('div', 'vz-cu', [el('b', null, String(d.total)), el('span', null, 'to clean up')]), hide(bar),
      el('div', 'vz-csl', d.groups.map(function (g, i) { return el('span', null, [hide(el('i', cls[i])), g.label + ' ' + g.n]); }))];
  };
  R.bills = function (d) {
    var P = function (x) { return pct(x / d.days * 100); };
    var head = el('div', 'vz-blr vz-blh', [el('span'), hide(el('div', 'vz-bax', d.axis.map(function (t, i) {
      return at(el('span', i === 0 ? 't0' : i === d.axis.length - 1 ? 't1' : 'mid', t[1]), P(t[0]));
    }))), el('span')]);
    return [head].concat(d.rows.map(function (r) {
      var when = (r.soon ? '▲ ' : '') + r.dt + (r.soon ? ' · ' + r.soon : '');
      var lab = at(el('em', (r.day > d.days * 0.6 ? 'l' : '') + (r.soon ? ' wn' : ''), when), P(r.day));
      return el('div', 'vz-blr row', [el('div', 'vz-bn', [el('b', null, r.n), el('span', null, r.s), el('span', 'vz-bdt' + (r.soon ? ' wn' : ''), when)]),
        el('div', 'vz-btk', [hide(at(el('i', r.soon ? 'soon' : null), P(r.day))), lab]),
        el('div', 'vz-ba', [r.a || '·', el('span', null, r.b)])]);
    })).concat(totalRow(d.total_label, d.total));
  };
  R.ledger = function (d) {
    return d.rows.map(function (r) {
      return el('div', 'vz-lgr', [el('span', 'vz-io ' + r.io, r.io === 'in' ? '↓ In' : '↑ Out'), el('b', null, r.w),
        el('span', 'vz-amt', r.amt || '·'), el('span', 'vz-sub', r.sub)]);
    }).concat(totalRow(d.net_label, d.net), d.split ? el('p', 'vz-sub vz-mt8', d.split) : null);
  };
  R.package = function (d) {
    return [el('div', 'vz-pkh', [el('span', 'vz-ic', icon('box')), el('b', null, d.name), el('span', 'vz-sub', d.sub)]),
      el('div', 'vz-pk', d.steps.map(function (s) { return el('div', s.st, [hide(el('i')), el('b', null, s.t), el('span', null, s.d)]); })),
      d.arrives ? el('p', 'vz-pkf', d.arrives) : null];
  };
  R.ticket = function (d) {
    return el('div', 'vz-tkt', [el('div', 'vz-stub', [el('span', null, d.dw), el('b', null, d.d), el('span', null, d.m)]),
      el('div', 'vz-tb', [el('b', null, d.t), el('span', 'vz-t', d.w), d.p ? el('span', 'vz-sub', d.p) : null,
        d.sw ? el('span', 'vz-st ' + d.st, [el('i', null, d.st === 'wn' ? '▲' : '✓'), ' ' + d.sw]) : null])]);
  };
  R.tasks = function (d) {
    return d.rows.map(function (r) {
      return el('div', 'vz-tsk', [hide(el('span', 'vz-cbx')), el('b', null, r.t), el('span', 'vz-dl2t', r.dl), el('span', 'vz-sub', r.s),
        el('span', 'vz-rl' + (r.wn ? ' wn' : ''), (r.wn ? '▲ ' : '') + r.rl)]);
    });
  };
  function link(href, label) {
    if (!/^https:\/\//.test(href || '')) return null;
    var a = el('a', 'vz-k', label, { target: '_blank', rel: 'noopener noreferrer' });
    a.href = href;
    return a;
  }
  R.health = function (d) {
    return [el('div', 'vz-hl', [el('span', 'vz-ic', icon('lock')), el('b', null, 'New message from ' + d.who), el('span', 'vz-sub', d.sub)]),
      el('div', 'vz-hseal', 'What it says stays behind the portal login. Callsheet only knows that a message is waiting, and never reads it out.')];
  };
  R.health.after = function (d) { return link(d.link, 'Open the message'); };
  R.stat = function (d) {
    var W = 700, H = 84, n = d.bars.length, gap = 14, bw = (W - gap * (n - 1)) / n;
    var s = sv('svg', { viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none', 'class': 'vz-spk', 'aria-hidden': 'true', focusable: 'false' });
    d.bars.forEach(function (b, i) {
      var hgt = Math.max(1, b.p / 100 * H);
      s.appendChild(sv('rect', { x: (i * (bw + gap)).toFixed(1), y: (H - hgt).toFixed(1), width: bw.toFixed(1), height: hgt.toFixed(1), 'class': b.hi ? 'hi' : '' }));
    });
    if (d.goal != null) {
      var y = (H - d.goal / 100 * H).toFixed(1);
      s.appendChild(sv('line', { x1: 0, x2: W, y1: y, y2: y, 'class': 'gl', 'vector-effect': 'non-scaling-stroke' }));
    }
    s.appendChild(sv('line', { x1: 0, x2: W, y1: H - 0.5, y2: H - 0.5, 'class': 'base', 'vector-effect': 'non-scaling-stroke' }));
    var sub = [d.change, d.over].filter(Boolean).join(' · ');
    return [el('div', 'vz-sth', [el('b', null, d.big), el('span', null, d.unit + (d.range ? ' · ' + d.range : ''))]),
      sub ? el('p', 'vz-sub', sub.charAt(0).toUpperCase() + sub.slice(1)) : null, s,
      hide(el('div', 'vz-spx', d.bars.map(function (b) { return el('span', null, b.l); }))),
      d.goal != null ? el('p', 'vz-spl', [hide(el('span', 'vz-gk')), d.goal_label, hide(el('span', 'vz-hk')), 'Over goal']) : null];
  };
  R.local = function (d) {
    return d.rows.map(function (r) {
      var sev = el('span', 'vz-sev', [1, 2, 3].map(function (i) { return el('i', i <= r.lv ? 'on' : null); }));
      var dl = el('div', 'vz-dl2', [el('i', 'you')]);
      if (r.mi != null) add(dl, at(el('i', 'at'), pct(r.mi)));
      return el('div', 'vz-la' + (r.wn ? ' wn' : ''), [el('div', 'vz-sv', [hide(sev), el('span', 'vz-st' + (r.wn ? ' wn' : ' ok'), [el('i', null, r.wn ? '▲' : '●'), ' ' + r.word])]),
        el('div', null, [el('b', null, r.t), r.s ? el('div', 'vz-sub', r.s) : null]),
        r.dist ? el('div', 'vz-dist', [hide(dl), el('span', null, r.dist)]) : el('div')]);
    }).concat(el('p', 'vz-sub vz-mt8', d.note));
  };
  R.price = function (d) {
    var pw = el('div', 'vz-pw');
    if (d.range) { var rg = at(el('i', 'vz-rg'), pct(d.range[0])); rg.style.width = pct(d.range[1] - d.range[0]); add(pw, rg); }
    add(pw, el('i', 'vz-ax'));
    d.marks.forEach(function (m) { add(pw, at(el('i', 'vz-m' + (m.in ? ' in' : '')), pct(m.x))); });
    d.ticks.forEach(function (t, i) { add(pw, at(el('span', 'vz-px' + (i === 0 ? ' p0' : i === d.ticks.length - 1 ? ' p1' : '')), pct(t[0]))).lastChild.textContent = t[1]; });
    return [el('div', 'vz-pwh', [el('b', null, d.name), el('span', 'vz-sub', d.sub)]), hide(pw),
      el('p', 'vz-spl', [d.range ? hide(el('span', 'vz-rgk')) : null, d.range ? 'Your range' : null, hide(el('span', 'vz-mk in')), 'In range',
        hide(el('span', 'vz-mk')), 'Outside'])].concat(d.rows.map(function (r, i) { return el('div', 'vz-vr' + (i ? '' : ' first'), el('b', null, r.t)); }));
  };
  R.leads = function (d) {
    return [el('div', 'vz-ldh', [el('b', null, d.n), el('span', null, d.what)])].concat(d.rows.map(function (r) {
      return el('div', 'vz-ldr', [el('b', null, r.t), r.r ? el('span', 'vz-r', r.r) : el('span'), r.s ? el('span', 'vz-sub', r.s) : null]);
    }), d.more ? el('p', 'vz-sub vz-mt8', d.more) : null);
  };
  R.series = function (d) {
    var cal = el('div', 'vz-mcal', ['M', 'T', 'W', 'T', 'F', 'S', 'S'].map(function (w) { return el('span', null, w); }));
    d.cells.forEach(function (c) { add(cal, el('i', c.s ? 's' : c.x ? 'x' : null, String(c.d))); });
    return [el('div', 'vz-pwh', [el('b', null, d.name), el('span', 'vz-sub', d.sub)]), hide(cal), el('p', 'vz-sub', d.first)];
  };
  R.invite = function (d) {
    return [el('div', 'vz-inv', [hide(el('span', 'vz-av', d.initial)), el('b', null, d.t), el('span', 'vz-sub', d.sub)]),
      el('p', 'vz-sub vz-note', d.note)];
  };
  R.invite.after = function (d) { return link(d.link, 'Open the invite'); };
  R.subs = function (d) {
    return d.rows.map(function (r) {
      return el('div', 'vz-sbr', [icon('repeat'), el('b', null, r.n), el('span', 'vz-amt', r.d), el('span', 'vz-sub', r.s),
        el('span', 'vz-nx' + (r.wn ? ' wn' : ''), (r.wn ? '▲ ' : '') + r.r)]);
    }).concat(totalRow(d.total_label, d.total), d.note ? el('p', 'vz-sub vz-mt8', d.note) : null);
  };

  /* ---------- composite: safe parts only (the path for model-defined types) ---------- */
  function node(n) {
    switch (n.t) {
      case 'row': return el('div', 'vz-c-row', n.c.map(node));
      case 'column': return el('div', 'vz-c-col', n.c.map(node));
      case 'text': return el('p', 'vz-c-text ' + (n.style || 'body'), n.text);
      case 'number': return el('p', 'vz-c-num', [el('b', null, n.value), n.unit ? el('span', null, n.unit) : null]);
      case 'time_axis': return gantt({ a: n.a, b: n.b, ticks: n.ticks, lanes: n.c.map(function (k) {
        return { l: k.label, sub: k.sub, s: k.s, e: k.t === 'dot' ? k.s : k.e, dot: k.t === 'dot', k: 'cal' };
      }) });
      case 'list': return el('ul', 'vz-c-list', n.items.map(function (x) { return el('li', null, x); }));
      case 'icon': return el('span', 'vz-c-icon', [icon(n.name), n.label ? el('span', null, n.label) : null]);
      case 'progress':
        var tk = el('span', 'vz-tk', el('i')); tk.firstChild.style.width = pct(n.p);
        return el('div', 'vz-c-prog', [el('span', 'vz-c-pl', n.value + ' of ' + n.of + (n.label ? ' ' + n.label : '')), hide(tk)]);
      case 'table':
        var t = el('table', 'vz-c-table');
        if (n.head && n.head.length) add(t, el('thead', null, el('tr', null, n.head.map(function (x) { return el('th', null, x, { scope: 'col' }); }))));
        return add(t, el('tbody', null, n.rows.map(function (r) { return el('tr', null, r.map(function (x) { return el('td', null, x); })); })));
      default: return null;     /* a part this file doesn't know: nothing */
    }
  }
  R.composite = function (d) { return [d.title ? el('p', 'vz-c-title', d.title) : null, node(d.body)]; };

  var CHECKWORD = { amount: 'an amount', balance: 'a balance', memo: 'a note', place: 'the place', name: 'the name', price: 'a price',
    title: 'a title', distance: 'a distance', level: 'the level', tracking: 'the tracking number', mark: 'the marked words' };
  /* one filled block -> <figure class="vz vz-<type>">: the drawing (hidden from screen readers), the text alternative
     (read instead) and, when the server left something out, a quiet line saying so (no silent drops) */
  function render(b) {
    if (!b || typeof b !== 'object' || !Object.prototype.hasOwnProperty.call(R, b.type) || !b.d) return null;
    var draw;
    try { draw = R[b.type](b.d); } catch (e) { return null; }       /* a malformed block draws nothing */
    var f = el('figure', 'vz vz-' + b.type, null, { 'aria-label': b.label || b.type, 'data-vz': b.type });
    add(f, [hide(el('div', 'vz-draw', draw)), el('figcaption', 'vz-vh', b.alt || '')]);
    var after = R[b.type].after && R[b.type].after(b.d);
    if (after) add(f, el('div', 'vz-keys', after));
    var left = (b.checks || []).filter(function (c) { return c.field || c.ref; });
    if (left.length) {
      var words = left.map(function (c) { return c.field ? (CHECKWORD[c.field.replace(/\[\d+\]$/, '')] || c.field) : 'a message'; });
      add(f, el('p', 'vz-left', 'Left out: ' + words.filter(function (w, i) { return words.indexOf(w) === i; }).join(', ') + ' (not found in the store)'));
    }
    return f;
  }

  /* ---------- the gallery (Setup › Advanced › Visuals): every block with sample data ---------- */
  var G = { data: null, err: null, loading: false, wait: [] };
  function galleryBody(box) {
    box.textContent = '';
    if (G.err) return add(box, el('p', 'note', G.err, { role: 'alert' }));
    if (!G.data) return add(box, el('p', 'note', 'Loading…', { role: 'status' }));
    var g = G.data;
    add(box, el('p', 'note', 'Every block the brief can show, drawn from sample data as of ' + g.as_of +
      '. The model points at messages by id; Callsheet fills in the facts and draws them in your look.'));
    var grid = el('div', 'vz-grid');
    g.blocks.forEach(function (x, i) {
      var fig = render(x.block);
      add(grid, el('section', 'vz-card', [el('h3', 'vz-lab', [el('span', null, (i + 1) + ' · ' + x.name), el('code', null, x.type)]),
        el('p', 'vz-sub vz-desc', x.desc), el('div', 'vz-box', fig || el('p', 'vz-sub', 'Not drawn: ' + ((x.problems[0] || {}).why || 'no data'))),
        el('pre', 'vz-js', JSON.stringify(x.json, null, 1), { tabindex: '0', 'aria-label': 'The JSON for ' + x.name })], { 'data-vz-card': x.type }));
    });
    add(box, grid);
    var dd = g.dropped_demo;
    if (dd) add(box, el('section', 'vz-card vz-drop', [el('h3', 'vz-lab', [el('span', null, 'Dropped, never drawn'), el('code', null, 'checks')]),
      el('p', 'vz-sub vz-desc', 'A type Callsheet doesn’t know, or a block that doesn’t match its schema, is dropped and counted; the words still play.'),
      el('pre', 'vz-js', JSON.stringify(dd.json, null, 1), { tabindex: '0', 'aria-label': 'Two blocks that are dropped' }),
      el('p', 'vz-sub vz-mt8', 'Dropped: ' + Object.keys(dd.dropped).filter(function (k) { return dd.dropped[k]; })
        .map(function (k) { return dd.dropped[k] + ' ' + k; }).join(', '))]));
    return box;
  }
  function gallery() {
    var box = el('div', 'vz-gallery', null, { 'data-vz-gallery': '' });
    galleryBody(box);
    if (!G.data && !G.loading) {
      G.loading = true; G.err = null;
      fetch('api/visuals/gallery', { headers: { Accept: 'application/json' } }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function (j) { G.data = j; }, function () { G.err = 'The gallery didn’t load. Try again in a moment.'; }).then(function () {
        G.loading = false;
        G.wait.splice(0).forEach(function (b) { if (b.isConnected) galleryBody(b); });
      });
    }
    if (!G.data) G.wait.push(box);
    return box;
  }

  window.CallsheetVisuals = { render: render, gallery: gallery, TYPES: Object.keys(R) };
})();
