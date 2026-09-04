/* Crypto Watch — GitHub Pages 側の表示。data/ 配下の JSON を読むだけの静的アプリ */
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const KIND = { sched: '定時', trigger: '発火', manual: '手動' };
const state = { site: null, cur: 'BTC', index: null, run: null, live: null };
const COLOR = { line: '#2a78d6', lineDark: '#3987e5', neg: '#eb6834', negDark: '#d95926' };
const dark = () => matchMedia('(prefers-color-scheme: dark)').matches;

// ---------------------------------------------------------------- utils
async function getJSON(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}
const isNum = (x) => typeof x === 'number' && isFinite(x);
function fmtUsd(x) {
  if (!isNum(x)) return '—';
  const a = Math.abs(x), s = x < 0 ? '-' : '';
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(1)}M`;
  return `${s}$${Math.round(a).toLocaleString('en-US')}`;
}
const fmtPrice = (x) => isNum(x) ? `$${Math.round(x).toLocaleString('en-US')}` : '—';
const fmtPct = (x, d = 2) => isNum(x) ? `${x >= 0 ? '+' : ''}${x.toFixed(d)}%` : '—';
const fmtPt = (x, d = 1) => isNum(x) ? `${x >= 0 ? '+' : ''}${x.toFixed(d)}pt` : '—';
const fmtNum = (x, d = 1) => isNum(x) ? x.toFixed(d) : '—';
function fmtTs(iso, withDate = true) {
  const d = new Date(iso);
  if (isNaN(d)) return iso || '';
  const o = withDate ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }
                     : { hour: '2-digit', minute: '2-digit' };
  return d.toLocaleString('ja-JP', o);
}
function md(text) {
  if (!text) return '';
  try { return marked.parse(text); }
  catch (e) { const p = document.createElement('pre'); p.textContent = text; return p.outerHTML; }
}
function setHTML(el, html) {
  el.innerHTML = html;
  el.querySelectorAll('a[href]').forEach(a => { a.target = '_blank'; a.rel = 'noopener'; });
}
function setStatus(msg) {
  const el = $('#status');
  el.hidden = !msg;
  el.textContent = msg || '';
}
function esc(s) { return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

// ---------------------------------------------------------------- init / nav
async function init() {
  const p = new URLSearchParams(location.search);
  try { state.site = await getJSON('data/site.json'); }
  catch (e) { setStatus('まだ公開された考察がありません。'); return; }
  const curs = state.site.currencies?.length ? state.site.currencies : ['BTC', 'ETH'];
  state.cur = curs.includes(p.get('c')) ? p.get('c') : curs[0];
  renderTabs(curs);
  $('#foot-updated').textContent = `サイト更新: ${fmtTs(state.site.updated)} / 保持 ${state.site.keep_days} 日`;
  $('#run-select').addEventListener('change', e => loadRun(e.target.value));
  $('#btn-live').addEventListener('click', fetchLive);
  $('#btn-copy').addEventListener('click', copyReport);
  addEventListener('resize', debounce(() => state.index && renderSeries(), 200));
  await loadCurrency(state.cur, p.get('r'));
}

function renderTabs(curs) {
  const nav = $('#tabs');
  nav.innerHTML = '';
  for (const c of curs) {
    const b = document.createElement('button');
    b.type = 'button'; b.textContent = c; b.setAttribute('role', 'tab');
    b.setAttribute('aria-selected', c === state.cur);
    b.addEventListener('click', () => { if (c !== state.cur) loadCurrency(c); });
    nav.appendChild(b);
  }
}

async function loadCurrency(cur, stamp) {
  state.cur = cur; state.live = null;
  $('#tabs').querySelectorAll('button').forEach(b => b.setAttribute('aria-selected', b.textContent === cur));
  setStatus('読み込み中…');
  try { state.index = await getJSON(`data/${cur}/index.json`); }
  catch (e) { state.index = null; $('#report').hidden = true; setStatus(`${cur} の考察はまだありません。`); return; }
  const runs = state.index.runs || [];
  const sel = $('#run-select');
  sel.innerHTML = '';
  for (const r of runs) {
    const o = document.createElement('option');
    o.value = r.stamp;
    o.textContent = `${fmtTs(r.ts)} ${KIND[r.kind] || r.kind}${r.reasons?.length ? ' — ' + r.reasons[0].slice(0, 28) : ''}`;
    sel.appendChild(o);
  }
  if (!runs.length) { $('#report').hidden = true; setStatus(`${cur} の考察はまだありません。`); return; }
  const target = runs.some(r => r.stamp === stamp) ? stamp : runs[0].stamp;
  sel.value = target;
  await loadRun(target);
}

async function loadRun(stamp) {
  setStatus('読み込み中…');
  try { state.run = await getJSON(`data/${state.cur}/runs/${stamp}.json`); }
  catch (e) { setStatus('考察の読み込みに失敗しました。'); return; }
  history.replaceState(null, '', `?c=${state.cur}&r=${stamp}`);
  setStatus('');
  renderRun();
  renderSeries();
  if (state.live) renderLive();
}

// ---------------------------------------------------------------- render
function tiles(run) {
  const s = run.summary || {};
  const flipPct = isNum(s.flip) && isNum(s.spot) ? ` (${fmtPct((s.flip - s.spot) / s.spot * 100, 1)})` : '';
  const fund = s.etf_fund || {};
  return [
    { key: 'spot', k: `${run.currency} 現物指数`, v: fmtPrice(s.spot) },
    { key: 'dvol', k: 'DVOL', v: fmtNum(s.dvol), s: isNum(s.dvol) ? (s.dvol >= 70 ? '高ボラ' : s.dvol >= 45 ? '中程度' : '低ボラ') : '' },
    { key: 'funding', k: 'ファンディング (8h)', v: fmtPct(s.funding, 4), s: 'Deribit 無期限' },
    { k: 'ネット GEX / 1%', v: fmtUsd(s.net_gex), s: isNum(s.net_gex) ? (s.net_gex >= 0 ? 'ガンマ+圏: 値動き抑制' : 'ガンマ-圏: 値動き増幅') : '' },
    { k: 'ガンマフリップ', v: fmtPrice(s.flip), s: flipPct ? `現値比${flipPct}` : '' },
    { k: '25Δ スキュー (30d)', v: fmtPt(s.skew_25d), s: 'Put − Call' },
    { key: 'basis', k: '先物ベーシス (年率)', v: fmtPct(s.basis_ann, 1), s: s.basis_name || '' },
    { key: 'cb', k: 'Coinbase プレミアム', v: fmtPct(s.cb_premium, 3), s: 'vs Binance USDT' },
    { k: `${fund.symbol || 'ETF'} 資金流出入`, v: isNum(fund.flow_usd) ? fmtUsd(fund.flow_usd) : '蓄積中', s: fund.asof ? `${fund.asof} 口数ベース` : '' },
    { k: 'HL 建玉', v: isNum(s.hl_oi) ? `${Math.round(s.hl_oi).toLocaleString('en-US')} 枚` : '—', s: isNum(s.hl_funding_8h) ? `HL funding ${fmtPct(s.hl_funding_8h, 4)}` : '' },
    { k: 'PCR (建玉)', v: fmtNum(s.pcr, 2), s: 'Deribit オプション' },
    { k: '先物建玉 (Deribit)', v: fmtUsd(s.fut_oi_usd) },
  ];
}

function renderRun() {
  const run = state.run;
  $('#report').hidden = false;
  $('#run-title').textContent = `${run.currency} 考察 — ${fmtTs(run.ts)} (${KIND[run.kind] || run.kind})`;
  $('#run-meta').textContent = `観測時刻 ${new Date(run.ts).toLocaleString('ja-JP')} / ${run.stamp}`;

  const ul = $('#reasons');
  ul.innerHTML = '';
  for (const r of run.reasons || []) {
    const li = document.createElement('li');
    li.textContent = r;
    if (r.startsWith('定時')) li.className = 'sched';
    ul.appendChild(li);
  }

  $('#tiles').innerHTML = tiles(run).map(t =>
    `<div class="tile"${t.key ? ` data-key="${t.key}"` : ''}><div class="k">${esc(t.k)}</div><div class="v">${esc(t.v)}</div>` +
    `${t.s ? `<div class="s">${esc(t.s)}</div>` : ''}<div class="live"></div></div>`).join('');
  $('#live-note').hidden = true;

  setHTML($('#overview'), md(run.overview_md));

  const fig = $('#figure');
  if (run.image) {
    fig.hidden = false;
    $('#terrain').src = run.image;
    $('#caption').textContent = run.caption || '';
  } else fig.hidden = true;

  const ch = $('#chapters');
  ch.innerHTML = '';
  const chapters = (run.chapters || []).filter(c => c.md !== run.overview_md || !run.overview_md);
  for (const c of chapters) {
    if (!c.md && !c.title) continue;
    const d = document.createElement('details');
    d.open = true;
    d.innerHTML = `<summary>${esc(c.title || '前置き')}</summary><div class="body md"></div>`;
    setHTML(d.querySelector('.body'), md(c.md));
    ch.appendChild(d);
  }

  const raw = $('#raw');
  raw.innerHTML = '';
  for (const s of run.report_sections || []) {
    const d = document.createElement('details');
    d.innerHTML = `<summary>${esc(s.title)}</summary><div class="body"><pre></pre></div>`;
    d.querySelector('pre').textContent = s.text;
    raw.appendChild(d);
  }

  const src = $('#sources');
  src.innerHTML = '';
  for (const u of run.sources || []) {
    const li = document.createElement('li');
    const a = document.createElement('a');
    a.href = u; a.target = '_blank'; a.rel = 'noopener'; a.textContent = u;
    li.appendChild(a); src.appendChild(li);
  }
  $('#sec-sources').hidden = !(run.sources || []).length;

  updateAiLinks();
}

// ---------------------------------------------------------------- AI 相談 / コピー
function overviewText() {
  const run = state.run;
  const t = tiles(run).map(x => `${x.k}: ${x.v}${x.s ? ` (${x.s})` : ''}`).join('\n');
  return `${t}\n\n${(run.overview_md || '').slice(0, 1500)}`;
}
function updateAiLinks() {
  const run = state.run;
  const url = location.href;
  const prompt = `次の暗号資産 (${run.currency}) の市場レポートを踏まえて相談に乗ってください。` +
    `まず要点を3行で要約し、そのあと私の質問に答えてください。\n\nレポートURL: ${url}\n\n## 概要 (${fmtTs(run.ts)})\n${overviewText()}`;
  const q = encodeURIComponent(prompt);
  $('#btn-claude').href = `https://claude.ai/new?q=${q}`;
  $('#btn-chatgpt').href = `https://chatgpt.com/?q=${q}`;
}
async function copyReport() {
  const run = state.run;
  if (!run) return;
  const text = `# ${run.currency} 考察 ${fmtTs(run.ts)} (${KIND[run.kind] || run.kind})\n` +
    `${location.href}\n\n${(run.reasons || []).map(r => `・${r}`).join('\n')}\n\n` +
    `${run.analysis_md}\n\n---\n## 解析データ (advisor.py)\n\n${run.report_txt}`;
  const b = $('#btn-copy');
  try { await navigator.clipboard.writeText(text); b.textContent = 'コピーしました'; }
  catch (e) { b.textContent = 'コピーできませんでした'; }
  setTimeout(() => { b.textContent = 'レポートをコピー'; }, 1800);
}

// ---------------------------------------------------------------- 最新データ (ブラウザから公開APIを直接叩く)
function parseExpiry(name) {
  const m = /-(\d{1,2})([A-Z]{3})(\d{2})$/.exec(name);
  if (!m) return null;
  const mon = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'].indexOf(m[2]);
  if (mon < 0) return null;
  return Date.UTC(2000 + +m[3], mon, +m[1], 8, 0, 0);
}
function frontBasis(rows, spot) {
  const now = Date.now(), cs = [];
  for (const r of rows || []) {
    if (!r.instrument_name || r.instrument_name.endsWith('-PERPETUAL')) continue;
    const exp = parseExpiry(r.instrument_name);
    if (!exp || !r.mark_price || !spot) continue;
    const days = (exp - now) / 864e5;
    if (days < 20) continue;
    const basis = (r.mark_price / spot - 1) * 100;
    cs.push({ name: r.instrument_name, oi: +r.open_interest || 0, ann: basis * 365 / days });
  }
  if (!cs.length) return null;
  return cs.reduce((a, b) => (b.oi > a.oi ? b : a));
}
async function fetchLive() {
  const c = state.cur, btn = $('#btn-live');
  btn.disabled = true; btn.textContent = '取得中…';
  const out = { ts: new Date().toISOString() };
  const D = 'https://www.deribit.com/api/v2/public';
  const tasks = [
    getJSON(`${D}/get_index_price?index_name=${c.toLowerCase()}_usd`).then(r => { out.spot = r.result.index_price; }),
    getJSON(`${D}/ticker?instrument_name=${c}-PERPETUAL`).then(r => { out.funding = r.result.funding_8h * 100; }),
    getJSON(`${D}/get_volatility_index_data?currency=${c}&start_timestamp=${Date.now() - 6 * 36e5}&end_timestamp=${Date.now()}&resolution=3600`)
      .then(r => { const a = r.result.data; if (a?.length) out.dvol = a[a.length - 1][4]; }),
    Promise.all([
      getJSON(`https://api.exchange.coinbase.com/products/${c}-USD/ticker`),
      getJSON(`https://api.binance.com/api/v3/ticker/price?symbol=${c}USDT`),
    ]).then(([cb, bn]) => { out.cb = (+cb.price / +bn.price - 1) * 100; }),
  ];
  await Promise.allSettled(tasks);
  try {
    const f = await getJSON(`${D}/get_book_summary_by_currency?currency=${c}&kind=future`);
    const b = frontBasis(f.result, out.spot);
    if (b) { out.basis = b.ann; out.basisName = b.name; }
  } catch (e) { /* 省略 */ }
  state.live = out;
  renderLive();
  btn.disabled = false; btn.textContent = '⟳ 最新データを取得';
}
function renderLive() {
  const l = state.live, s = state.run?.summary || {};
  if (!l) return;
  const put = (key, text) => { const el = $(`.tile[data-key="${key}"] .live`); if (el) el.textContent = text; };
  const d = (a, b, f) => isNum(a) && isNum(b) ? ` (${f(a - b)})` : '';
  put('spot', isNum(l.spot) ? `いま ${fmtPrice(l.spot)}${isNum(s.spot) ? ` (${fmtPct((l.spot - s.spot) / s.spot * 100, 2)})` : ''}` : 'いま: 取得失敗');
  put('dvol', isNum(l.dvol) ? `いま ${fmtNum(l.dvol)}${d(l.dvol, s.dvol, x => fmtNum(x, 1).replace(/^(?!-)/, '+'))}` : 'いま: 取得失敗');
  put('funding', isNum(l.funding) ? `いま ${fmtPct(l.funding, 4)}` : 'いま: 取得失敗');
  put('basis', isNum(l.basis) ? `いま ${fmtPct(l.basis, 1)} (${l.basisName})` : 'いま: 取得失敗');
  put('cb', isNum(l.cb) ? `いま ${fmtPct(l.cb, 3)}${d(l.cb, s.cb_premium, x => fmtPt(x, 3))}` : 'いま: 取得失敗');
  const note = $('#live-note');
  note.hidden = false;
  note.textContent = `「いま」は ${fmtTs(l.ts)} にブラウザから Deribit / Coinbase / Binance の公開APIを直接読んだ値。考察時点との差を括弧で示す。ETFフローと建玉地形は日次/観測時のみ。`;
}

// ---------------------------------------------------------------- 推移 (直近数日のスナップショット)
const CHARTS = [
  { key: 'spot', title: '現物指数', fmt: fmtPrice },
  { key: 'dvol', title: 'DVOL', fmt: x => fmtNum(x, 1) },
  { key: 'funding', title: 'ファンディング (8h, %)', fmt: x => fmtPct(x, 4), zero: true },
  { key: 'basis_ann', title: '先物ベーシス (年率, %)', fmt: x => fmtPct(x, 1), zero: true },
  { key: 'cb_premium', title: 'Coinbase プレミアム (%)', fmt: x => fmtPct(x, 3), zero: true },
  { key: 'skew_25d', title: '25Δ スキュー (pt)', fmt: x => fmtPt(x, 1), zero: true },
  { key: 'abs_gex', title: 'ABS GEX', fmt: fmtUsd },
  { key: 'hl_oi', title: 'HL 建玉 (枚)', fmt: x => Math.round(x).toLocaleString('en-US') },
];

function renderSeries() {
  const idx = state.index, box = $('#charts');
  box.innerHTML = '';
  if (!idx) return;
  const ser = idx.series || { ts: [] };
  const n = ser.ts.length;
  $('#series-note').textContent = n
    ? `毎時の観測 ${n} 点 (${fmtTs(ser.ts[0])} 〜 ${fmtTs(ser.ts[n - 1])})。線にカーソルを乗せると値が出る。`
    : '観測の蓄積がまだありません。';
  for (const c of CHARTS) {
    const pts = ser.ts.map((t, i) => ({ t: new Date(t).getTime(), v: ser[c.key]?.[i] })).filter(p => isNum(p.v));
    box.appendChild(chartCard(c.title, pts, c.fmt, { zero: c.zero, kind: 'line', mark: state.run ? new Date(state.run.ts).getTime() : null }));
  }
  const flows = (idx.etf_flows || []).map(([d, v]) => ({ t: new Date(d + 'T21:00:00Z').getTime(), v, label: d }));
  const fund = state.run?.summary?.etf_fund;
  box.appendChild(chartCard(`${fund?.symbol || 'ETF'} 資金流出入 (日次)`, flows, fmtUsd, { zero: true, kind: 'bar' }));
}

function chartCard(title, pts, fmt, opt) {
  const card = document.createElement('div');
  card.className = 'chart';
  const last = pts.length ? pts[pts.length - 1] : null;
  card.innerHTML = `<div class="h"><span>${esc(title)}</span><b>${last ? esc(fmt(last.v)) : '—'}</b></div>`;
  if (pts.length < 2) {
    card.insertAdjacentHTML('beforeend', '<div class="empty">データ不足</div>');
    return card;
  }
  const W = Math.max(280, Math.min(600, (card.clientWidth || 320) - 24)), H = 110, P = { l: 6, r: 6, t: 8, b: 6 };
  const xs = pts.map(p => p.t), ys = pts.map(p => p.v);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (opt.zero) { y0 = Math.min(y0, 0); y1 = Math.max(y1, 0); }
  if (y1 === y0) { y1 += 1; y0 -= 1; }
  const pad = (y1 - y0) * 0.08; y0 -= pad; y1 += pad;
  const X = t => P.l + (x1 === x0 ? 0.5 : (t - x0) / (x1 - x0)) * (W - P.l - P.r);
  const Y = v => P.t + (1 - (v - y0) / (y1 - y0)) * (H - P.t - P.b);
  const stroke = dark() ? COLOR.lineDark : COLOR.line, neg = dark() ? COLOR.negDark : COLOR.neg;
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  const el = (tag, attrs) => { const e = document.createElementNS(ns, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
  const grid = 'rgba(128,128,128,.28)';
  if (opt.zero && y0 < 0 && y1 > 0) svg.appendChild(el('line', { x1: P.l, x2: W - P.r, y1: Y(0), y2: Y(0), stroke: grid, 'stroke-width': 1 }));
  else svg.appendChild(el('line', { x1: P.l, x2: W - P.r, y1: H - P.b, y2: H - P.b, stroke: grid, 'stroke-width': 1 }));

  if (opt.kind === 'bar') {
    const bw = Math.max(3, (W - P.l - P.r) / pts.length - 2);
    for (const p of pts) {
      const x = X(p.t) - bw / 2, y = Math.min(Y(p.v), Y(0)), h = Math.max(1, Math.abs(Y(p.v) - Y(0)));
      svg.appendChild(el('rect', { x, y, width: bw, height: h, rx: 2, fill: p.v >= 0 ? stroke : neg }));
    }
  } else {
    const d = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p.t).toFixed(1)},${Y(p.v).toFixed(1)}`).join('');
    svg.appendChild(el('path', { d, fill: 'none', stroke, 'stroke-width': 2, 'vector-effect': 'non-scaling-stroke', 'stroke-linejoin': 'round' }));
    if (opt.mark) {
      const near = pts.reduce((a, b) => Math.abs(b.t - opt.mark) < Math.abs(a.t - opt.mark) ? b : a);
      if (Math.abs(near.t - opt.mark) < 90 * 60e3)
        svg.appendChild(el('circle', { cx: X(near.t), cy: Y(near.v), r: 4, fill: stroke, stroke: dark() ? '#232322' : '#f0efec', 'stroke-width': 2 }));
    }
  }
  // ホバー: 最寄りの点に十字線とツールチップ
  const cursor = el('line', { y1: P.t, y2: H - P.b, stroke: grid, 'stroke-width': 1, visibility: 'hidden' });
  const dot = el('circle', { r: 4, fill: stroke, visibility: 'hidden' });
  svg.appendChild(cursor); svg.appendChild(dot);
  const tip = document.createElement('div');
  tip.className = 'tip';
  card.appendChild(svg); card.appendChild(tip);
  svg.addEventListener('mousemove', ev => {
    const r = svg.getBoundingClientRect();
    const t = x0 + ((ev.clientX - r.left) / r.width * W - P.l) / (W - P.l - P.r) * (x1 - x0);
    const p = pts.reduce((a, b) => Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a);
    cursor.setAttribute('x1', X(p.t)); cursor.setAttribute('x2', X(p.t)); cursor.setAttribute('visibility', 'visible');
    dot.setAttribute('cx', X(p.t)); dot.setAttribute('cy', Y(p.v)); dot.setAttribute('visibility', opt.kind === 'bar' ? 'hidden' : 'visible');
    tip.style.display = 'block';
    tip.textContent = `${p.label || fmtTs(p.t)}  ${fmt(p.v)}`;
    const cx = ev.clientX - card.getBoundingClientRect().left;
    tip.style.left = `${Math.min(cx + 10, card.clientWidth - tip.offsetWidth - 6)}px`;
    tip.style.top = `${ev.clientY - card.getBoundingClientRect().top - 30}px`;
  });
  svg.addEventListener('mouseleave', () => { cursor.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); tip.style.display = 'none'; });
  return card;
}

function debounce(fn, ms) { let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; }

init();
