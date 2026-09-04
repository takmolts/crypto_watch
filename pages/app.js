/* Crypto Watch — GitHub Pages 側の表示。data/ 配下の JSON を読み、章を構造化して描く静的アプリ */
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const KIND = { sched: '定時', trigger: '発火', manual: '手動' };
const state = { site: null, cur: 'BTC', index: null, run: null, live: null };
const dark = () => matchMedia('(prefers-color-scheme: dark)').matches;
const TZ = (() => {
  try {
    const name = Intl.DateTimeFormat().resolvedOptions().timeZone || 'ローカル';
    const off = -new Date().getTimezoneOffset() / 60;
    return { name, off, label: `${name} (UTC${off >= 0 ? '+' : ''}${off})` };
  } catch (e) { return { name: 'ローカル', off: 0, label: 'ローカル時刻' }; }
})();

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
function fmtTs(iso, mode = 'dt') {
  const d = new Date(iso);
  if (isNaN(d)) return iso || '';
  const o = mode === 'dt' ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }
          : mode === 'd' ? { month: '2-digit', day: '2-digit', weekday: 'short' }
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
function setStatus(msg) { const el = $('#status'); el.hidden = !msg; el.textContent = msg || ''; }
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const stripMd = (s) => String(s ?? '').replace(/\*\*/g, '').replace(/`/g, '').trim();
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

// トーン判定 (notify_discord.py と同じ語彙)
const BULL = ['上昇', '強気', '買い', '上抜け', 'ブレイク', '上値', 'ロング', '突破', '上放れ', '反発', 'ハト派', '流入', '順ザヤ回復'];
const BEAR = ['下落', '弱気', '売り', '割れ', '崩れ', '下抜け', 'ショート', '急落', '下放れ', '引き戻', '流出', '利上げ実施', '逆ザヤ化'];
const WARN = ['注意', '警戒', 'リスク', '不透明', '急変', 'テール', '空白', '加速', '止まりにくい', '反転', '跨'];
function tone(text, { warnFirst = true } = {}) {
  const t = String(text || '');
  const b = BULL.filter(w => t.includes(w)).length, s = BEAR.filter(w => t.includes(w)).length;
  const w = WARN.filter(w => t.includes(w)).length;
  if (warnFirst && w && b === s) return 'warn';
  if (s > b) return 'bear';
  if (b > s) return 'bull';
  return w ? 'warn' : 'info';
}

// ---------------------------------------------------------------- init / nav
async function init() {
  const p = new URLSearchParams(location.search);
  $('#tz-note').textContent = `時刻はすべてブラウザのタイムゾーン ${TZ.label} で表示。`;
  try { state.site = await getJSON('data/site.json'); }
  catch (e) { setStatus('まだ公開された考察がありません。'); return; }
  const curs = state.site.currencies?.length ? state.site.currencies : ['BTC', 'ETH'];
  state.cur = curs.includes(p.get('c')) ? p.get('c') : curs[0];
  renderTabs(curs);
  $('#foot-updated').textContent = `サイト更新: ${fmtTs(state.site.updated)} (${TZ.label}) / 保持 ${state.site.keep_days} 日`;
  $('#btn-live').addEventListener('click', fetchLive);
  $('#btn-copy').addEventListener('click', copyReport);
  addEventListener('resize', debounce(() => { if (state.run) { renderSeries(); renderCalendar(); renderLadder(); } }, 200));
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
  catch (e) { state.index = null; $('#report').hidden = true; $('#run-list').innerHTML = ''; setStatus(`${cur} の考察はまだありません。`); return; }
  const runs = state.index.runs || [];
  renderRunList(runs, null);
  if (!runs.length) { $('#report').hidden = true; setStatus(`${cur} の考察はまだありません。`); return; }
  await loadRun(runs.some(r => r.stamp === stamp) ? stamp : runs[0].stamp);
}

function renderRunList(runs, current) {
  const ul = $('#run-list');
  ul.innerHTML = '';
  for (const r of runs) {
    const li = document.createElement('li');
    li.setAttribute('aria-current', r.stamp === current);
    li.innerHTML = `<div class="t"><span>${esc(fmtTs(r.ts))}</span><span class="badge ${esc(r.kind)}">${esc(KIND[r.kind] || r.kind)}</span></div>` +
      `<div class="r">${esc(r.reasons?.[0] || (isNum(r.spot) ? fmtPrice(r.spot) : ''))}</div>`;
    li.addEventListener('click', () => loadRun(r.stamp));
    ul.appendChild(li);
  }
}

async function loadRun(stamp) {
  setStatus('読み込み中…');
  try { state.run = await getJSON(`data/${state.cur}/runs/${stamp}.json`); }
  catch (e) { setStatus('考察の読み込みに失敗しました。'); return; }
  history.replaceState(null, '', `?c=${state.cur}&r=${stamp}`);
  setStatus('');
  renderRunList(state.index.runs || [], stamp);
  renderRun();
  renderLadder();
  renderCalendar();
  renderAnalysis();
  renderSeries();
  if (state.live) renderLive();
}

// ---------------------------------------------------------------- 概要
function tiles(run) {
  const s = run.summary || {};
  const fund = s.etf_fund || {};
  const flipPct = isNum(s.flip) && isNum(s.spot) ? (s.flip - s.spot) / s.spot * 100 : null;
  const T = (cond, yes, no = 'info') => cond == null ? '' : (cond ? yes : no);
  return [
    { key: 'spot', k: `${run.currency} 現物指数`, v: fmtPrice(s.spot), tone: '' },
    { key: 'dvol', k: 'DVOL', v: fmtNum(s.dvol), s: isNum(s.dvol) ? (s.dvol >= 70 ? '高ボラ: 値幅は大きめに' : s.dvol >= 45 ? '中程度' : '低ボラ: 圧縮が続けば放れに警戒') : '', tone: isNum(s.dvol) ? (s.dvol >= 70 ? 'warn' : s.dvol < 45 ? 'warn' : 'info') : '' },
    { key: 'funding', k: 'ファンディング (8h)', v: fmtPct(s.funding, 4), s: isNum(s.funding) ? (s.funding > 0.03 ? 'ロング過密' : s.funding > 0.01 ? 'ややロング優勢' : s.funding < -0.01 ? 'ショート優勢: 踏み上げ燃料' : '中立圏') : 'Deribit 無期限', tone: isNum(s.funding) ? (s.funding > 0.03 ? 'warn' : s.funding > 0.01 ? 'bull' : s.funding < -0.01 ? 'bear' : 'info') : '' },
    { k: 'ネット GEX / 1%', v: fmtUsd(s.net_gex), s: isNum(s.net_gex) ? (s.net_gex >= 0 ? 'ガンマ+圏: 値動き抑制' : 'ガンマ−圏: 値動き増幅') : '', tone: T(isNum(s.net_gex) ? s.net_gex >= 0 : null, 'info', 'bear') },
    { k: 'ガンマフリップ', v: fmtPrice(s.flip), s: isNum(flipPct) ? `現値比 ${fmtPct(flipPct, 1)}` : '', tone: isNum(flipPct) ? (Math.abs(flipPct) < 3 ? 'warn' : 'info') : '' },
    { k: '25Δ スキュー (30d)', v: fmtPt(s.skew_25d), s: isNum(s.skew_25d) ? (s.skew_25d >= 8 ? 'Put大幅割高: 下方ヘッジ需要強' : s.skew_25d >= 3 ? 'Put優位' : s.skew_25d <= -3 ? 'Call優位: 上方向の追いかけ' : '中立圏') : 'Put − Call', tone: isNum(s.skew_25d) ? (s.skew_25d >= 8 ? 'bear' : s.skew_25d <= -3 ? 'bull' : 'info') : '' },
    { key: 'basis', k: '先物ベーシス (年率)', v: fmtPct(s.basis_ann, 1), s: isNum(s.basis_ann) ? (s.basis_ann >= 10 ? 'キャリー妙味大: アービ資金混入' : s.basis_ann >= 5 ? '平時' : s.basis_ann >= 0 ? 'キャリー薄: 流入は実需寄り' : '逆ザヤ: ストレス') : (s.basis_name || '未観測'), tone: isNum(s.basis_ann) ? (s.basis_ann >= 10 ? 'warn' : s.basis_ann < 0 ? 'bear' : 'info') : '' },
    { key: 'cb', k: 'Coinbase プレミアム', v: fmtPct(s.cb_premium, 3), s: isNum(s.cb_premium) ? (s.cb_premium >= 0.1 ? '米国の現物買いが先行' : s.cb_premium <= -0.1 ? '米国から現物売り' : '中立圏 (vs Binance USDT)') : '未観測', tone: isNum(s.cb_premium) ? (s.cb_premium >= 0.1 ? 'bull' : s.cb_premium <= -0.1 ? 'bear' : 'info') : '' },
    { k: `${fund.symbol || 'ETF'} 資金流出入`, v: isNum(fund.flow_usd) ? fmtUsd(fund.flow_usd) : (fund.asof ? '蓄積中' : '未観測'), s: fund.asof ? `${fund.asof} 口数ベース` : '', tone: isNum(fund.flow_usd) ? (fund.flow_usd > 0 ? 'bull' : fund.flow_usd < 0 ? 'bear' : 'info') : '' },
    { k: 'HL 建玉', v: isNum(s.hl_oi) ? `${Math.round(s.hl_oi).toLocaleString('en-US')} 枚` : '—', s: isNum(s.hl_funding_8h) ? `HL funding ${fmtPct(s.hl_funding_8h, 4)}` : '', tone: '' },
    { k: 'PCR (建玉)', v: fmtNum(s.pcr, 2), s: 'Deribit オプション', tone: '' },
    { k: '先物建玉 (Deribit)', v: fmtUsd(s.fut_oi_usd), s: '', tone: '' },
  ];
}

function renderRun() {
  const run = state.run;
  $('#report').hidden = false;
  $('#run-kind').textContent = KIND[run.kind] || run.kind;
  $('#run-kind').className = `badge ${run.kind}`;
  $('#run-title').textContent = `${run.currency} 考察 — ${new Date(run.ts).toLocaleString('ja-JP', { month: 'long', day: 'numeric', weekday: 'short', hour: '2-digit', minute: '2-digit' })}`;
  $('#run-meta').textContent = `観測 ${fmtTs(run.ts)} ${TZ.name} / ${run.stamp}`;

  const ul = $('#reasons');
  ul.innerHTML = '';
  for (const r of run.reasons || []) {
    const li = document.createElement('li');
    li.textContent = r;
    li.className = r.startsWith('定時') ? 'tone-info' : `tone-${tone(r)}`;
    ul.appendChild(li);
  }

  $('#tiles').innerHTML = tiles(run).map(t =>
    `<div class="tile${t.tone ? ` tone-${t.tone}` : ''}"${t.key ? ` data-key="${t.key}"` : ''}><div class="k">${esc(t.k)}</div><div class="v">${esc(t.v)}</div>` +
    `${t.s ? `<div class="s">${esc(t.s)}</div>` : ''}<div class="live"></div></div>`).join('');
  $('#live-note').hidden = true;

  setHTML($('#overview'), md(run.overview_md));

  const fig = $('#figure');
  if (run.image) { fig.hidden = false; $('#terrain').src = run.image; $('#caption').textContent = run.caption || ''; }
  else fig.hidden = true;

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
    a.href = u; a.target = '_blank'; a.rel = 'noopener'; a.textContent = sourceLabel(run, u);
    li.appendChild(a); src.appendChild(li);
  }
  $('#sec-sources').hidden = !(run.sources || []).length;
  updateAiLinks();
}
function sourceLabel(run, url) {
  // 考察中の [タイトル](url) を拾えれば表示名にする
  const m = new RegExp(`\\[([^\\]]{3,120})\\]\\(${url.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\)`).exec(run.analysis_md || '');
  return m ? `${m[1]} — ${new URL(url).hostname}` : url;
}

// ---------------------------------------------------------------- 価格ラダー (生レポートの地形/GEX/ETF節から水準を抜く)
function parseLevels(run) {
  const secs = run.report_sections || [];
  const text = (t) => (secs.find(s => s.title.startsWith(t)) || {}).text || '';
  const P = (s) => parseFloat(String(s).replace(/[$,]/g, ''));
  const all = (re, t) => [...t.matchAll(re)];
  const lv = [];
  const spot = run.summary?.spot;
  const g = text('地形'), gex = text('GEX'), etf = secs.find(s => /米国現物ETF/.test(s.title))?.text || '';
  let m;
  if ((m = /Call壁は\s*\$([\d,]+)\s*\(OI\s*([\d,]+)枚/.exec(g))) lv.push({ p: P(m[1]), type: 'wall', label: `Call壁 ${m[2]}枚` });
  if ((m = /その先の壁:\s*([^\n]+)/.exec(g))) for (const x of all(/\$([\d,]+)/g, m[1])) lv.push({ p: P(x[1]), type: 'wall2', label: 'Call壁' });
  if ((m = /Put支持は\s*\$([\d,]+)\s*\(OI\s*([\d,]+)枚/.exec(g))) lv.push({ p: P(m[1]), type: 'support', label: `Put支持 ${m[2]}枚` });
  if ((m = /その下の支持:\s*([^\n]+)/.exec(g))) for (const x of all(/\$([\d,]+)/g, m[1])) lv.push({ p: P(x[1]), type: 'support2', label: 'Put支持' });
  if ((m = /磁力の強いストライク:\s*\$([\d,]+)/.exec(g))) lv.push({ p: P(m[1]), type: 'magnet', label: '磁石' });
  if ((m = /Max Pain[^:]*:\s*\$([\d,]+)/.exec(g))) lv.push({ p: P(m[1]), type: 'maxpain', label: 'Max Pain' });
  if ((m = /ガンマフリップ推定:\s*\$([\d,]+)/.exec(gex))) lv.push({ p: P(m[1]), type: 'flip', label: 'ガンマフリップ' });
  if ((m = /上の壁 \(原資産換算\):\s*([^\n]+)/.exec(etf))) for (const x of all(/\$([\d,]+)\s*\(([A-Z]+)\s*\$(\d+)/g, m[1])) lv.push({ p: P(x[1]), type: 'etfwall', label: `${x[2]} $${x[3]} Call` });
  if ((m = /下の支持 \(原資産換算\):\s*([^\n]+)/.exec(etf))) for (const x of all(/\$([\d,]+)\s*\(([A-Z]+)\s*\$(\d+)/g, m[1])) lv.push({ p: P(x[1]), type: 'etfsup', label: `${x[2]} $${x[3]} Put` });
  // 7日IV の 1σ 帯
  let band = null;
  const vs = text('ボラ構造');
  const iv7 = /\b7d\s+([\d.]+)%/.exec(vs);
  if (iv7 && isNum(spot)) { const w = spot * (parseFloat(iv7[1]) / 100) * Math.sqrt(7 / 365); band = { lo: spot - w, hi: spot + w, label: `7日IV 1σ ±${fmtPct(w / spot * 100, 1).replace('+', '')}` }; }
  return { levels: lv.filter(l => isNum(l.p)), spot, band };
}
// 色は地形図と同じ: Call = 青、Put = 橙
const LADDER_STYLE = {
  wall: { c: '--accent', w: 2, dash: '' }, wall2: { c: '--accent', w: 1, dash: '4 3' },
  support: { c: '--c-expiry', w: 2, dash: '' }, support2: { c: '--c-expiry', w: 1, dash: '4 3' },
  magnet: { c: '--c-cb', w: 2, dash: '' }, maxpain: { c: '--neutral', w: 2, dash: '2 3' },
  flip: { c: '--warn', w: 2, dash: '6 3' }, etfwall: { c: '--accent', w: 1, dash: '1 3' }, etfsup: { c: '--c-expiry', w: 1, dash: '1 3' },
};
function renderLadder() {
  const box = $('#ladder');
  box.innerHTML = '';
  const { levels, spot, band } = parseLevels(state.run);
  if (!isNum(spot) || !levels.length) { box.innerHTML = '<p class="empty">水準を読める生レポートがありません。</p>'; return; }
  // 表示範囲: 現値 ±25% 以内の水準 (遠い LEAPS 壁は落とす)
  const lv = levels.filter(l => Math.abs(l.p - spot) / spot <= 0.20).sort((a, b) => b.p - a.p);
  lv.push({ p: spot, type: 'spot', label: '現値' });
  lv.sort((a, b) => b.p - a.p);
  const ps = lv.map(l => l.p).concat([spot], band ? [band.lo, band.hi] : []);
  let hi = Math.max(...ps), lo = Math.min(...ps);
  const pad = (hi - lo) * 0.06 || spot * 0.02; hi += pad; lo -= pad;
  const W = Math.max(300, Math.min(520, box.clientWidth || 340)), rowH = 22;
  const H = Math.max(240, (lv.length + 1) * rowH + 30);
  const Y = (p) => 16 + (1 - (p - lo) / (hi - lo)) * (H - 32);
  const x0 = 10, x1 = Math.round(W * 0.3);   // 目盛りの線 (右側はラベル)
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const el = (tag, attrs, txt) => { const e = document.createElementNS(ns, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (txt != null) e.textContent = txt; return e; };
  if (band) svg.appendChild(el('rect', { x: x0, y: Y(band.hi), width: x1 - x0, height: Y(band.lo) - Y(band.hi), fill: cssVar('--accent'), opacity: .12, rx: 3 }));
  svg.appendChild(el('line', { x1: (x0 + x1) / 2, x2: (x0 + x1) / 2, y1: 8, y2: H - 8, stroke: cssVar('--border'), 'stroke-width': 1 }));
  // ラベルの重なり回避: 上から順に最低 rowH 空ける
  let lastY = -Infinity;
  const rows = [];
  for (const l of lv) {
    let y = Y(l.p);
    if (y - lastY < rowH) y = lastY + rowH;
    lastY = y; rows.push({ l, y });
  }
  // 下端をはみ出したら全体を詰め直す
  const over = lastY - (H - 10);
  if (over > 0) rows.forEach((r, i) => { r.y -= over * (i + 1) / rows.length; });
  for (const { l, y } of rows) {
    if (l.type === 'spot') {
      const sy = Y(spot);
      svg.appendChild(el('polygon', { points: `${x0 - 6},${sy - 5} ${x0 - 6},${sy + 5} ${x0 - 1},${sy}`, fill: cssVar('--text') }));
      svg.appendChild(el('line', { x1: x0, x2: x1, y1: sy, y2: sy, stroke: cssVar('--text'), 'stroke-width': 2.5 }));
      if (Math.abs(sy - y) > 2) svg.appendChild(el('line', { x1: x1, x2: x1 + 8, y1: sy, y2: y, stroke: cssVar('--text'), 'stroke-width': 1, opacity: .7 }));
      svg.appendChild(el('text', { x: x1 + 10, y: y + 4, class: 'pr', 'font-weight': 700 }, `${fmtPrice(spot)}  現値`));
      continue;
    }
    const st = LADDER_STYLE[l.type] || LADDER_STYLE.wall2;
    const col = cssVar(st.c);
    const ly = Y(l.p);
    svg.appendChild(el('line', { x1: x0, x2: x1, y1: ly, y2: ly, stroke: col, 'stroke-width': st.w, 'stroke-dasharray': st.dash }));
    if (Math.abs(ly - y) > 2) svg.appendChild(el('line', { x1: x1, x2: x1 + 8, y1: ly, y2: y, stroke: col, 'stroke-width': 1, opacity: .7 }));
    const pct = (l.p - spot) / spot * 100;
    svg.appendChild(el('text', { x: x1 + 10, y: y + 4, class: 'pr' }, fmtPrice(l.p)));
    svg.appendChild(el('text', { x: x1 + 74, y: y + 4 }, `${fmtPct(pct, 1)}  ${l.label}`));
  }
  const wrap = document.createElement('div');
  wrap.className = 'ladder';
  wrap.appendChild(svg);
  box.appendChild(wrap);
  const far = levels.length - (lv.length - 1);
  const note = document.createElement('p');
  note.className = 'note';
  note.textContent = `現値 ±20% の水準のみ${far ? ` (遠い ${far} 本は省略)` : ''}。実線=主力、破線=次点、点線=米国ETFの原資産換算。` +
    (band ? `薄い帯は ${band.label} (${fmtPrice(band.lo)}〜${fmtPrice(band.hi)})。` : '');
  box.appendChild(note);
}

// ---------------------------------------------------------------- イベント年表 (3章から日付を抜く)
const CATS = [
  { key: 'expiry', label: 'オプション満期', re: /満期|Max Pain|クアドラプル|SQ/, c: '--c-expiry' },
  { key: 'cb', label: '中銀・FOMC', re: /FOMC|FRB|Fed\b|ECB|日銀|利上げ|利下げ|議長会見/, c: '--c-cb' },
  { key: 'macro', label: '経済指標', re: /CPI|PCE|雇用統計|NFP|GDP|PMI|ISM|小売|失業|指標|物価|統計/, c: '--c-macro' },
  { key: 'crypto', label: '暗号資産・規制', re: /ETF|SEC|CFTC|法案|CLARITY|GENIUS|上院|下院|投票|規制|取引所|Coinbase|ステーブル/, c: '--c-crypto' },
  { key: 'other', label: '地政学・その他', re: /./, c: '--c-other' },
];
const DATE_RE = /(?:(\d{4})[\/年])?(\d{1,2})[\/月](\d{1,2})日?\s*(?:\(([月火水木金土日])\))?(?:[^\n]{0,12}?(\d{1,2}):(\d{2})\s*(JST|UTC|ET|EST|EDT))?/g;
function parseEvents(run) {
  const ch = (run.chapters || []).find(c => /^\s*3[\.．)]/.test(c.title) || /カレンダー/.test(c.title));
  if (!ch) return { events: [], md: '' };
  const ref = new Date(run.ts);
  const refY = ref.getUTCFullYear(), refM = ref.getUTCMonth() + 1;
  const events = [];
  const lines = ch.md.split('\n');
  let last = null;
  for (const raw of lines) {
    const line = raw.replace(/\t/g, '    ');
    const top = /^[-*・]\s+/.test(line), sub = /^\s{2,}[-*・]\s+/.test(line);
    if (!top && !sub && !line.trim()) continue;
    if (sub || (!top && last && line.trim())) { if (last) last.detail += (last.detail ? '\n' : '') + line.replace(/^\s+[-*・]\s+/, '- ').trim(); continue; }
    const body = line.replace(/^[-*・]\s+/, '');
    const ms = [...body.matchAll(DATE_RE)].filter(m => +m[2] >= 1 && +m[2] <= 12 && +m[3] >= 1 && +m[3] <= 31);
    if (!ms.length) { const tt = stripMd(body).split(/[。：:]/)[0].slice(0, 60); events.push({ t: null, title: tt, detail: tidyMd(body.slice(body.indexOf('。') + 1)), cat: catOf(tt) }); last = events[events.length - 1]; continue; }
    ms.forEach((m, i) => {
      const seg = body.slice(m.index, i + 1 < ms.length ? ms[i + 1].index : undefined);
      let y = m[1] ? +m[1] : refY;
      const mo = +m[2], d = +m[3];
      if (!m[1] && mo < refM - 6) y += 1;
      let t;
      if (m[5]) {
        const tz = m[7] || 'JST';
        const offH = tz === 'JST' ? 9 : tz === 'UTC' ? 0 : (tz === 'EDT' ? -4 : -5);
        t = Date.UTC(y, mo - 1, d, +m[5] - offH, +m[6]);
      } else t = Date.UTC(y, mo - 1, d, 12 - TZ.off);   // 時刻不明: その日の正午 (ローカル)
      // 「21:30 JST / 12:30 UTC — 名称」のような併記時刻と区切り記号を落とす
      let rest = seg.slice(m[0].length).replace(/^(\s*[\/／]?\s*\d{1,2}:\d{2}\s*(?:JST|UTC|ET|EST|EDT))*[\s—–\-:：]*/, '');
      const cut = rest.indexOf('。');
      let title = stripMd(cut >= 0 ? rest.slice(0, cut) : rest).replace(/^(本日|明日|来週)\s*/, '').replace(/[\s:：—–\-]+$/, '');
      if (title.length > 60) title = title.slice(0, 58) + '…';
      const detail = tidyMd(cut >= 0 ? rest.slice(cut + 1) : '');
      events.push({ t, allDay: !m[5], title: title || stripMd(seg).slice(0, 60), detail, cat: catOf(title) });
    });
    last = events[events.length - 1];
  }
  // 満期は本文に無くても補う (Deribit 月次 = 最終金曜 08:00 UTC)
  const hasExpiry = (t) => events.some(e => e.t && Math.abs(e.t - t) < 36e5 * 20 && /満期/.test(e.title + e.detail));
  for (let k = 0; k < 2; k++) {
    const t = lastFriday(refY, refM - 1 + k);
    if (t > ref.getTime() - 864e5 && !hasExpiry(t)) events.push({ t, title: 'Deribit 月次満期 (最終金曜 08:00 UTC)', detail: '計算で補った予定。オプションカット直後はガンマ総量が減り壁が柔らかくなる', cat: CATS[0], auto: true });
  }
  events.sort((a, b) => (a.t ?? Infinity) - (b.t ?? Infinity));
  return { events, md: ch.md };
}
function tidyMd(s) {
  // 先頭の区切りを落とし、対になっていない ** (題名側で切れた分) を除く
  let t = String(s || '').replace(/^\s*[—–\-:：、]\s*/, '').trim();
  if ((t.match(/\*\*/g) || []).length % 2) t = t.replace(/\*\*/g, '');
  return t;
}
function catOf(title) {
  // 題名だけで決める (本文は他の予定への言及を含みやすく、種別を誤らせる)
  return CATS.slice(0, -1).find(c => c.re.test(title)) || CATS[CATS.length - 1];
}
function lastFriday(y, m0) { const d = new Date(Date.UTC(y, m0 + 1, 0, 8)); while (d.getUTCDay() !== 5) d.setUTCDate(d.getUTCDate() - 1); return d.getTime(); }

function renderCalendar() {
  const run = state.run;
  const { events, md: raw } = parseEvents(run);
  const tl = $('#timeline'), list = $('#event-list');
  tl.innerHTML = ''; list.innerHTML = '';
  $('#cal-sub').textContent = events.length ? `3章から抽出 / 時刻は ${TZ.label}` : '';
  setHTML($('#cal-raw-body'), md(raw));
  $('#cal-raw').hidden = !raw;
  if (!events.length) { tl.innerHTML = '<p class="note">イベントカレンダーの章がありません。</p>'; $('#cal-legend').innerHTML = ''; return; }
  const used = new Set(events.map(e => e.cat.key));
  $('#cal-legend').innerHTML = CATS.filter(c => used.has(c.key)).map(c => `<span style="--c:var(${c.c})">${esc(c.label)}</span>`).join('') +
    `<span style="--c:var(--text-3)">▲ 考察時点 / ┆ いま</span>`;

  const dated = events.filter(e => isNum(e.t));
  const now = Date.now(), ref = new Date(run.ts).getTime();
  if (dated.length) {
    const dayMs = 864e5;
    const startDay = (t) => { const d = new Date(t); d.setHours(0, 0, 0, 0); return d.getTime(); };
    let t0 = startDay(Math.min(ref, now, dated[0].t)) - dayMs * 0.5;
    let t1 = Math.min(Math.max(...dated.map(e => e.t), now) + dayMs * 1.5, t0 + dayMs * 40);
    const omitted = dated.filter(e => e.t > t1).length;
    const days = Math.ceil((t1 - t0) / dayMs);
    const W = Math.max(600, tl.clientWidth || 700), P = { l: 10, r: 10, t: 22, b: 30 };
    const X = (t) => P.l + (t - t0) / (t1 - t0) * (W - P.l - P.r);
    // レーン割り当て (ラベル幅で重なりを避ける)
    const lanes = [];
    const items = dated.filter(e => e.t <= t1).map(e => {
      const x = X(e.t), w = Math.min(e.title.length * 12, 190) + 16;
      let li = lanes.findIndex(end => end < x - 2);
      if (li < 0) { li = lanes.length; lanes.push(0); }
      lanes[li] = x + w;
      return { e, x, lane: li };
    });
    const laneH = 24, H = P.t + Math.max(1, lanes.length) * laneH + P.b;
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.style.height = `${H}px`;
    const el = (tag, attrs, txt) => { const e = document.createElementNS(ns, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (txt != null) e.textContent = txt; return e; };
    const grid = cssVar('--border'), axisY = H - P.b + 4;
    // 日の目盛と週末
    const step = Math.max(1, Math.ceil(days / ((W - P.l - P.r) / 72)));   // ラベル1つに約72px
    for (let d = startDay(t0 + dayMs * 0.5), i = 0; d < t1; d += dayMs, i++) {
      const wd = new Date(d).getDay();
      if (wd === 0 || wd === 6) svg.appendChild(el('rect', { x: X(d), y: P.t - 6, width: X(d + dayMs) - X(d), height: axisY - P.t + 6, fill: cssVar('--text-3'), opacity: .07 }));
      if (i % step === 0) {
        svg.appendChild(el('line', { x1: X(d), x2: X(d), y1: axisY, y2: axisY + 4, stroke: grid }));
        svg.appendChild(el('text', { x: X(d) + 3, y: axisY + 16 }, fmtTs(d, 'd').replace(/\(|\)/g, ' ')));
      }
    }
    svg.appendChild(el('line', { x1: P.l, x2: W - P.r, y1: axisY, y2: axisY, stroke: grid }));
    // 考察時点 / いま
    svg.appendChild(el('polygon', { points: `${X(ref) - 5},${axisY + 1} ${X(ref) + 5},${axisY + 1} ${X(ref)},${axisY - 6}`, fill: cssVar('--text-2') }));
    if (now > t0 && now < t1) {
      svg.appendChild(el('line', { x1: X(now), x2: X(now), y1: P.t - 8, y2: axisY, stroke: cssVar('--text-3'), 'stroke-dasharray': '3 3' }));
      svg.appendChild(el('text', { x: X(now) + 3, y: P.t - 10, class: 'now' }, 'いま'));
    }
    const tip = document.createElement('div');
    tip.className = 'tip';
    for (const { e, x, lane } of items) {
      const y = P.t + lane * laneH + 8, col = cssVar(e.cat.c);
      const g = el('g', { cursor: 'default' });
      g.appendChild(el('line', { x1: x, x2: x, y1: y, y2: axisY, stroke: col, 'stroke-width': 1, opacity: .45 }));
      g.appendChild(el('circle', { cx: x, cy: y, r: 5, fill: col, stroke: cssVar('--surface'), 'stroke-width': 2 }));
      const label = el('text', { x: x + 9, y: y + 4, class: 'lbl' }, e.title.length > 18 ? e.title.slice(0, 17) + '…' : e.title);
      if (e.t < now) { label.setAttribute('opacity', .6); }
      g.appendChild(label);
      g.addEventListener('mousemove', ev => {
        tip.style.display = 'block';
        tip.innerHTML = `<b>${esc(fmtTs(e.t, e.allDay ? 'd' : 'dt'))}${e.allDay ? ' (時刻未定)' : ''} — ${esc(e.title)}</b>${esc(e.detail.slice(0, 220))}${e.detail.length > 220 ? '…' : ''}`;
        const r = tl.getBoundingClientRect();
        tip.style.left = `${Math.max(0, Math.min(ev.clientX - r.left + 12, tl.clientWidth - 340))}px`;
        tip.style.top = `${ev.clientY - r.top + 14}px`;
      });
      g.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
      svg.appendChild(g);
    }
    tl.appendChild(svg); tl.appendChild(tip);
    if (omitted) { const p = document.createElement('p'); p.className = 'note'; p.textContent = `40日より先の ${omitted} 件は年表に載せず、下の一覧にだけ出している。`; tl.appendChild(p); }
  }
  for (const e of events) {
    const li = document.createElement('li');
    li.style.setProperty('--c', `var(${e.cat.c})`);
    if (isNum(e.t) && e.t < now) li.classList.add('past');
    li.innerHTML = `<div class="d">${isNum(e.t) ? esc(fmtTs(e.t, e.allDay ? 'd' : 'dt')) + (e.allDay ? '' : ` ${TZ.name}`) : '日付なし'} · ${esc(e.cat.label)}${e.auto ? ' · 自動補完' : ''}</div>` +
      `<div class="t">${esc(e.title)}</div>${e.detail ? '<div class="e md"></div>' : ''}`;
    if (e.detail) setHTML(li.querySelector('.e'), md(e.detail));
    list.appendChild(li);
  }
}

// ---------------------------------------------------------------- 分析 (章の構造化)
function splitTail(mdText) {
  // "---" 以降の 総括 / 出典 / 補足 を切り分ける
  const parts = mdText.split(/\n-{3,}\n/);
  const out = { body: parts[0] || '', verdict: '', notes: [] };
  for (const p of parts.slice(1)) {
    const s = p.trim();
    if (!s) continue;
    if (/^\*{0,2}総括/.test(s)) out.verdict = s.replace(/^\*\*総括[:：]?\*\*\s*/, '').replace(/^総括[:：]?\s*/, '');
    else if (/^#{1,4}\s*出典/.test(s) || /^出典/.test(s)) continue;
    else out.notes.push(s);
  }
  return out;
}
function parseScenarios(mdText) {
  const groups = []; let g = null;
  for (const raw of mdText.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const bold = /^\*\*(.+?)\*\*[:：]?$/.exec(line);
    if (bold && !/^[-*・]/.test(line)) { g = { title: bold[1], items: [] }; groups.push(g); continue; }
    const b = /^[-*・]\s+(.*)$/.exec(raw.replace(/^\s+/, ''));
    if (b) {
      if (!g) { g = { title: '', items: [] }; groups.push(g); }
      const text = b[1];
      const pm = /確度\s*(?:約\s*)?(\d{1,3})\s*%/.exec(text);
      const tm = /^\*\*(.+?)\*\*/.exec(text);
      let title = tm ? tm[1] : stripMd(text).split(/[。]/)[0];
      title = title.replace(/\s*[—–\-]\s*確度.*$/, '').replace(/\s*\(確度.*$/, '').trim();
      const body = tm ? text.slice(tm[0].length).replace(/^[。\s]+/, '') : text;
      g.items.push({ title, prob: pm ? +pm[1] : null, body, tone: tone(title + ' ' + body.slice(0, 80), { warnFirst: false }) });
    } else if (g && g.items.length) g.items[g.items.length - 1].body += '\n' + line;
  }
  return groups.filter(x => x.items.length);
}
function parseTriggers(mdText) {
  const groups = []; let g = null;
  for (const raw of mdText.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const bold = /^\*\*(.+?)\*\*[:：]?$/.exec(line);
    if (bold) { g = { title: bold[1], items: [], tone: /弱気/.test(bold[1]) ? 'bear' : /強気/.test(bold[1]) ? 'bull' : 'warn' }; groups.push(g); continue; }
    const b = /^[-*・]\s+(.*)$/.exec(line);
    if (b) {
      if (!g) { g = { title: 'トリガー', items: [], tone: 'warn' }; groups.push(g); }
      const [cond, ...rest] = b[1].split(/\s*(?:→|⇒)\s*/);
      g.items.push({ cond: stripMd(cond), fx: stripMd(rest.join(' → ')) });
    } else if (g && g.items.length) g.items[g.items.length - 1].fx += ' ' + stripMd(line);
  }
  return groups.filter(x => x.items.length);
}
const CH_COLORS = ['--c-macro', '--c-cb', '--c-crypto', '--c-expiry', '--warn', '--c-other'];
function renderAnalysis() {
  const run = state.run;
  const chBox = $('#chapters'), scBox = $('#scenarios'), trBox = $('#triggers'), notes = $('#notes'), verdict = $('#verdict');
  chBox.innerHTML = ''; scBox.innerHTML = ''; trBox.innerHTML = ''; notes.innerHTML = ''; notes.hidden = true; verdict.hidden = true;
  const allNotes = []; let verdictMd = '';
  let ci = 0;
  for (const c of run.chapters || []) {
    const num = (/^\s*(\d)[\.．)]/.exec(c.title) || [])[1];
    if (!c.md.trim() || num === '1' || num === '3' || c.md === run.overview_md) continue;
    const { body, verdict: v, notes: n } = splitTail(c.md);
    if (v) verdictMd = v;
    allNotes.push(...n);
    if (num === '6') {
      const groups = parseScenarios(body);
      if (groups.length) {
        const h = document.createElement('h3'); h.textContent = c.title; h.style.marginTop = '16px'; scBox.appendChild(h);
        for (const g of groups) {
          if (g.title) { const t = document.createElement('div'); t.className = 'scenario-group'; t.textContent = g.title; scBox.appendChild(t); }
          const grid = document.createElement('div'); grid.className = 'scenarios';
          for (const it of g.items) {
            const d = document.createElement('div');
            d.className = 'scn'; d.style.setProperty('--c', `var(--${it.tone})`);
            d.innerHTML = `<div class="h"><span>${esc(it.title)}</span>${isNum(it.prob) ? `<span class="p">確度 ${it.prob}%</span>` : ''}</div>` +
              `${isNum(it.prob) ? `<div class="bar"><i style="width:${Math.min(100, it.prob)}%"></i></div>` : ''}<div class="b md"></div>`;
            setHTML(d.querySelector('.b'), md(it.body));
            grid.appendChild(d);
          }
          scBox.appendChild(grid);
        }
        continue;
      }
    }
    if (num === '7') {
      const groups = parseTriggers(body);
      if (groups.length) {
        const h = document.createElement('h3'); h.textContent = c.title; h.style.marginTop = '16px'; trBox.appendChild(h);
        const cols = document.createElement('div'); cols.className = 'trig-cols';
        for (const g of groups) {
          const d = document.createElement('div');
          d.className = 'trig'; d.style.setProperty('--c', `var(--${g.tone})`);
          d.innerHTML = `<h4>${esc(g.title)}</h4><ul>${g.items.map(it => `<li><div class="c">${esc(it.cond)}</div>${it.fx ? `<div class="fx">${esc(it.fx)}</div>` : ''}</li>`).join('')}</ul>`;
          cols.appendChild(d);
        }
        trBox.appendChild(cols);
        continue;
      }
    }
    const d = document.createElement('details');
    d.className = 'chapter'; d.open = true;
    d.style.setProperty('--c', `var(${CH_COLORS[ci++ % CH_COLORS.length]})`);
    d.innerHTML = `<summary>${esc(c.title || '前置き')}</summary><div class="body md"></div>`;
    setHTML(d.querySelector('.body'), md(body));
    chBox.appendChild(d);
  }
  if (verdictMd) { verdict.hidden = false; verdict.innerHTML = '<h3>総括</h3><div class="md"></div>'; setHTML(verdict.querySelector('.md'), md(verdictMd)); }
  if (allNotes.length) { notes.hidden = false; setHTML(notes, md(allNotes.join('\n\n'))); }
}

// ---------------------------------------------------------------- AI 相談 / コピー
function overviewText() {
  const run = state.run;
  const t = tiles(run).map(x => `${x.k}: ${x.v}${x.s ? ` (${x.s})` : ''}`).join('\n');
  return `${t}\n\n${stripMd(run.overview_md || '').slice(0, 1500)}`;
}
function updateAiLinks() {
  const run = state.run, url = location.href;
  const prompt = `次の暗号資産 (${run.currency}) の市場レポートを踏まえて相談に乗ってください。` +
    `まず要点を3行で要約し、そのあと私の質問に答えてください。\n\nレポートURL: ${url}\n\n## 概要 (${fmtTs(run.ts)} ${TZ.name})\n${overviewText()}`;
  const q = encodeURIComponent(prompt);
  $('#btn-claude').href = `https://claude.ai/new?q=${q}`;
  $('#btn-chatgpt').href = `https://chatgpt.com/?q=${q}`;
}
async function copyReport() {
  const run = state.run;
  if (!run) return;
  const text = `# ${run.currency} 考察 ${fmtTs(run.ts)} ${TZ.name} (${KIND[run.kind] || run.kind})\n${location.href}\n\n` +
    `${(run.reasons || []).map(r => `・${r}`).join('\n')}\n\n${run.analysis_md}\n\n---\n## 解析データ (advisor.py)\n\n${run.report_txt}`;
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
  return mon < 0 ? null : Date.UTC(2000 + +m[3], mon, +m[1], 8, 0, 0);
}
function frontBasis(rows, spot) {
  const now = Date.now(), cs = [];
  for (const r of rows || []) {
    if (!r.instrument_name || r.instrument_name.endsWith('-PERPETUAL')) continue;
    const exp = parseExpiry(r.instrument_name);
    if (!exp || !r.mark_price || !spot) continue;
    const days = (exp - now) / 864e5;
    if (days < 20) continue;
    cs.push({ name: r.instrument_name, oi: +r.open_interest || 0, ann: (r.mark_price / spot - 1) * 100 * 365 / days });
  }
  return cs.length ? cs.reduce((a, b) => (b.oi > a.oi ? b : a)) : null;
}
async function fetchLive() {
  const c = state.cur, btn = $('#btn-live');
  btn.disabled = true; btn.textContent = '取得中…';
  const out = { ts: new Date().toISOString() };
  const D = 'https://www.deribit.com/api/v2/public';
  await Promise.allSettled([
    getJSON(`${D}/get_index_price?index_name=${c.toLowerCase()}_usd`).then(r => { out.spot = r.result.index_price; }),
    getJSON(`${D}/ticker?instrument_name=${c}-PERPETUAL`).then(r => { out.funding = r.result.funding_8h * 100; }),
    getJSON(`${D}/get_volatility_index_data?currency=${c}&start_timestamp=${Date.now() - 6 * 36e5}&end_timestamp=${Date.now()}&resolution=3600`)
      .then(r => { const a = r.result.data; if (a?.length) out.dvol = a[a.length - 1][4]; }),
    Promise.all([getJSON(`https://api.exchange.coinbase.com/products/${c}-USD/ticker`), getJSON(`https://api.binance.com/api/v3/ticker/price?symbol=${c}USDT`)])
      .then(([cb, bn]) => { out.cb = (+cb.price / +bn.price - 1) * 100; }),
  ]);
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
  put('dvol', isNum(l.dvol) ? `いま ${fmtNum(l.dvol)}${d(l.dvol, s.dvol, x => (x >= 0 ? '+' : '') + x.toFixed(1))}` : 'いま: 取得失敗');
  put('funding', isNum(l.funding) ? `いま ${fmtPct(l.funding, 4)}` : 'いま: 取得失敗');
  put('basis', isNum(l.basis) ? `いま ${fmtPct(l.basis, 1)} (${l.basisName})` : 'いま: 取得失敗');
  put('cb', isNum(l.cb) ? `いま ${fmtPct(l.cb, 3)}${d(l.cb, s.cb_premium, x => fmtPt(x, 3))}` : 'いま: 取得失敗');
  const note = $('#live-note');
  note.hidden = false;
  note.textContent = `「いま」は ${fmtTs(l.ts)} ${TZ.name} にブラウザから Deribit / Coinbase / Binance の公開APIを直接読んだ値。括弧は考察時点との差。ETFフローと建玉地形は日次/観測時のみ。`;
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
  $('#series-tz').textContent = `時刻は ${TZ.label}`;
  $('#series-note').textContent = n
    ? `毎時の観測 ${n} 点 (${fmtTs(ser.ts[0])} 〜 ${fmtTs(ser.ts[n - 1])} ${TZ.name})。● は表示中の考察の時点。線にカーソルを乗せると値が出る。`
    : '観測の蓄積がまだありません。';
  const mark = state.run ? new Date(state.run.ts).getTime() : null;
  for (const c of CHARTS) {
    const pts = ser.ts.map((t, i) => ({ t: new Date(t).getTime(), v: ser[c.key]?.[i] })).filter(p => isNum(p.v));
    box.appendChild(chartCard(c.title, pts, c.fmt, { zero: c.zero, kind: 'line', mark }));
  }
  const flows = (idx.etf_flows || []).map(([d, v]) => ({ t: new Date(d + 'T21:00:00Z').getTime(), v, label: `${d} (米国引け)` }));
  const fund = state.run?.summary?.etf_fund;
  box.appendChild(chartCard(`${fund?.symbol || 'ETF'} 資金流出入 (日次)`, flows, fmtUsd, { zero: true, kind: 'bar' }));
}
function chartCard(title, pts, fmt, opt) {
  const card = document.createElement('div');
  card.className = 'chart';
  const last = pts.length ? pts[pts.length - 1] : null;
  card.innerHTML = `<div class="h"><span>${esc(title)}</span><b>${last ? esc(fmt(last.v)) : '—'}</b></div>`;
  if (pts.length < 2) { card.insertAdjacentHTML('beforeend', '<div class="empty">データ不足</div>'); return card; }
  const W = Math.max(280, Math.min(600, (card.clientWidth || 320) - 24)), H = 110, P = { l: 6, r: 6, t: 8, b: 6 };
  const xs = pts.map(p => p.t), ys = pts.map(p => p.v);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (opt.zero) { y0 = Math.min(y0, 0); y1 = Math.max(y1, 0); }
  if (y1 === y0) { y1 += 1; y0 -= 1; }
  const pad = (y1 - y0) * 0.08; y0 -= pad; y1 += pad;
  const X = t => P.l + (x1 === x0 ? 0.5 : (t - x0) / (x1 - x0)) * (W - P.l - P.r);
  const Y = v => P.t + (1 - (v - y0) / (y1 - y0)) * (H - P.t - P.b);
  const stroke = cssVar('--accent'), neg = cssVar('--c-expiry');
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
        svg.appendChild(el('circle', { cx: X(near.t), cy: Y(near.v), r: 4, fill: stroke, stroke: cssVar('--surface-2'), 'stroke-width': 2 }));
    }
  }
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
