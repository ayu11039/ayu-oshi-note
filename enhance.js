/* enhance.js ― 推し活ノートの追加パーツ
   ① 次の参戦予定カードと、あと何日かのカウントダウン
   ② 年ごとのイベント数グラフ(押すとその年だけ表示)

   使い方: index.html の </body> の直前に次の1行を足すだけです。
     <script src="enhance.js"></script>
   元に戻すときは、その1行を消せばOK。
   既存の表示・データ・絞り込みには一切触れません。events.json が読めない時は何も出しません。 */
(function () {
  'use strict';
  const $ = s => document.querySelector(s);
  const stats = $('#stats');
  if (!stats) return;

  const style = document.createElement('style');
  style.textContent = `
.next{display:flex;align-items:center;gap:20px;margin:22px 0 4px;padding:18px 22px;border:1px solid var(--line-strong);border-radius:var(--radius);background:linear-gradient(135deg,rgba(76,195,240,.14),rgba(155,140,255,.16)),var(--card);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);box-shadow:0 10px 30px rgba(0,0,0,.28)}
.next-count{flex:none;min-width:92px;text-align:center;line-height:1.2}
.next-count small{display:block;font-size:.72rem;font-weight:700;color:var(--muted)}
.next-count b{display:block;margin:2px 0;font-size:3rem;line-height:1.1;background:linear-gradient(135deg,#fff 35%,var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.next-body{min-width:0}
.next-title{font-size:1.1rem;font-weight:700;line-height:1.55;overflow-wrap:anywhere}
.next-title a{color:var(--ink)}
.next-title a:hover{color:var(--accent2);text-decoration:underline}
.next-meta{margin-top:4px;color:var(--muted);font-size:.9rem;overflow-wrap:anywhere}
.next-more{margin-top:6px;color:var(--dim);font-size:.8rem}
.hist{margin:0 0 6px;padding:16px 18px 10px;border:1px solid var(--line);border-radius:18px;background:var(--card);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}
.hist-title{margin-bottom:8px;font-size:.82rem;font-weight:700;color:var(--dim)}
.hist-bars{display:flex;align-items:flex-end;gap:6px;overflow-x:auto;scrollbar-width:none}
.hist-bars::-webkit-scrollbar{display:none}
.hist-col{flex:1 1 0;min-width:44px;min-height:44px;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:4px;padding:4px 0;cursor:pointer}
.hist-n{font-size:.78rem;font-weight:700;color:var(--muted)}
.hist-bar{display:block;width:100%;max-width:38px;height:var(--h);min-height:4px;border-radius:8px 8px 3px 3px;background:linear-gradient(180deg,var(--accent2),var(--accent));opacity:.6;transition:opacity .2s ease}
.hist-y{font-size:.78rem;color:var(--dim)}
.hist-col:hover .hist-bar,.hist-col[aria-pressed="true"] .hist-bar{opacity:1}
.hist-col[aria-pressed="true"] .hist-y{color:var(--ink);font-weight:700}
@media(max-width:480px){.next{gap:14px;padding:14px 16px}.next-count{min-width:72px}.next-count b{font-size:2.4rem}.next-title{font-size:1rem}}`;
  document.head.append(style);

  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  const nextBox = el('div', 'next');
  const histBox = el('div', 'hist');
  nextBox.hidden = histBox.hidden = true;
  stats.before(nextBox);
  stats.after(histBox);

  const pad = n => String(n).padStart(2, '0');
  const t = new Date();
  const today = t.getFullYear() + '-' + pad(t.getMonth() + 1) + '-' + pad(t.getDate());
  const WD = ['日', '月', '火', '水', '木', '金', '土'];

  function renderNext(events) {
    const up = events
      .filter(e => e.date >= today)
      .sort((a, b) => a.date.localeCompare(b.date) || String(a.start || '').localeCompare(String(b.start || '')));
    if (!up.length) { nextBox.hidden = true; return; }
    const e = up[0];
    const days = Math.round((new Date(e.date + 'T00:00:00') - new Date(today + 'T00:00:00')) / 864e5);
    const d = new Date(e.date + 'T12:00:00');

    const count = el('div', 'next-count');
    count.append(el('small', null, days ? '次の参戦まで' : '次の参戦は'), el('b', null, days ? String(days) : '今日'));
    if (days) count.append(el('small', null, '日'));

    const title = el('div', 'next-title');
    if (/^https?:\/\//i.test(e.url || '')) {
      const a = el('a', null, e.title);
      a.href = e.url; a.target = '_blank'; a.rel = 'noopener noreferrer';
      title.append(a);
    } else {
      title.textContent = e.title;
    }
    const meta = [(d.getMonth() + 1) + '月' + d.getDate() + '日(' + WD[d.getDay()] + ')'];
    if (e.venue) meta.push(e.venue);
    if (e.start && e.start !== '-') meta.push('開演 ' + e.start);

    const body = el('div', 'next-body');
    body.append(title, el('div', 'next-meta', meta.join('　')));
    if (up.length > 1) body.append(el('div', 'next-more', 'このあとも ' + (up.length - 1) + '件の予定があります'));
    nextBox.replaceChildren(count, body);
    nextBox.hidden = false;
  }

  const pill = y => document.querySelector('#years .pill[data-k="' + y + '"]');
  const isOn = y => { const b = pill(y); return !!b && b.getAttribute('aria-pressed') === 'true'; };
  const sync = () => histBox.querySelectorAll('.hist-col').forEach(b => b.setAttribute('aria-pressed', isOn(b.dataset.y)));

  function renderHist(events) {
    const c = {};
    events.forEach(e => { const y = e.date.slice(0, 4); c[y] = (c[y] || 0) + 1; });
    const ys = Object.keys(c).sort();
    if (ys.length < 2) { histBox.hidden = true; return; }
    const max = Math.max(...ys.map(y => c[y]));
    const bars = el('div', 'hist-bars');
    ys.forEach(y => {
      const b = el('button', 'hist-col');
      b.type = 'button';
      b.dataset.y = y;
      b.title = y + '年だけ表示(もう一度押すと全期間に戻ります)';
      const bar = el('span', 'hist-bar');
      bar.style.setProperty('--h', Math.round(c[y] / max * 76) + 'px');
      b.append(el('span', 'hist-n', String(c[y])), bar, el('span', 'hist-y', y));
      b.addEventListener('click', () => { const p = pill(isOn(y) ? 'all' : y); if (p) p.click(); });
      bars.append(b);
    });
    histBox.replaceChildren(el('div', 'hist-title', '年ごとのイベント数'), bars);
    histBox.hidden = false;
    sync();
  }

  const yearsRow = $('#years');
  if (yearsRow) new MutationObserver(sync).observe(yearsRow, { subtree: true, childList: true, attributes: true, attributeFilter: ['aria-pressed'] });

  fetch('./events.json?ts=' + Date.now(), { cache: 'no-store' })
    .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(data => {
      const seen = new Set();
      const events = (data.events || [])
        .filter(e => e && e.date && e.title)
        .map(e => ({ ...e, date: String(e.date), title: String(e.title) }))
        .filter(e => !e.url || (!seen.has(e.url) && seen.add(e.url)));
      renderNext(events);
      renderHist(events);
    })
    .catch(() => { /* 読めなければ何も足さない(元のページのまま) */ });
})();
