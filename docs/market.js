/* '🚦 시장' 탭 — 밸류 신호등(사용자의 다른 대시보드) 시장 탭을 옮겨 온 화면.
   데이터: market.json (update.py 의 fetch_valmarket 이 그쪽 저장소에서 복사). 화면 코드는 그쪽 site/app.js 를 거의 그대로,
   차트만 Chart.js 대신 이 페이지의 Plotly(baseLayout·axis·CFG)로 그림 */
(() => {
"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// 탭 전체 기간 버튼 (모든 차트가 같이 바뀜). [키, 이름, 일수(0 = 전부)]
const PERIODS = [["1w", "1주", 7], ["3m", "3개월", 91], ["6m", "6개월", 183], ["1y", "1년", 365], ["3y", "3년", 1096], ["5y", "5년", 1826], ["all", "역사적", 0]];
let M = null, loading = null, VP = "5y", DRAWS = [];
try { VP = localStorage.getItem("vmPeriod") || VP; } catch (e) {}

function n(v, d = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return "N/A";
  return Number(v).toLocaleString("ko-KR", { minimumFractionDigits: d, maximumFractionDigits: d });
}
function pct(v, d = 1, sign = false) {
  if (v == null) return "N/A";
  const r = Number(v.toFixed(d)) || 0; // -0 방지
  return (sign && r > 0 ? "+" : "") + n(r, d) + "%";
}
function times(v, d = 1) { return v == null ? "N/A" : n(v, d) + "배"; }

/* ---------- 차트 ---------- */
function themeColors() {
  return { s1: css("--g1"), s2: css("--g2"), s3: css("--g3"), s4: css("--g4"), s7: css("--g7"), muted: css("--muted") };
}
// 그쪽 makeChart 와 같은 입력(날짜 목록 + 선 목록)을 Plotly 선 차트로. 마우스 올리면 날짜 전체 + 숫자
function makeChart(id, labels, datasets, opt = {}) {
  const fmt = opt.fmt || ((v) => n(v, 2));
  const dash = (d) => !d.borderDash || !d.borderDash.length ? "solid" : d.borderDash[0] <= 2 ? "dot" : "dash";
  const span = (new Date(labels[labels.length - 1]) - new Date(labels[0])) / 864e5;
  Plotly.react(id, datasets.map((d, i) => ({
    x: labels, y: d.data, type: "scatter", mode: i === 0 && labels.length < 30 ? "lines+markers" : "lines", name: d.label,
    line: { color: d.borderColor, width: d.borderWidth || 2, dash: dash(d) }, marker: { size: 5 },
    text: d.data.map((v) => (v == null ? "" : fmt(v))), hovertemplate: "%{text}",
  })), Object.assign(baseLayout(), {
    margin: { l: 48, r: 8, t: 8, b: 28 },
    xaxis: axis({ type: "date", tickformat: span <= 100 ? "%m.%d" : span <= 400 ? "%y.%m" : "%Y", nticks: 6, showgrid: false }),
    yaxis: axis({ nticks: 6, tickformat: opt.ytick || "", ticksuffix: opt.ysuf || "", rangemode: opt.zero ? "tozero" : "normal" }),
  }), CFG);
}
function legend(items) {
  return `<div class="legend">${items.map(([label, color, dash]) => `<span><i class="${dash ? "dash" : ""}" style="border-color:${color}"></i>${esc(label)}</span>`).join("")}</div>`;
}
// 지금 고른 기간의 시작 위치. 자료가 달마다 1개뿐이어도 점이 2개 이상 남게
function sliceP(dates) {
  const days = PERIODS.find((p) => p[0] === VP)[2];
  if (!days) return 0;
  const c = new Date(new Date(dates[dates.length - 1]) - days * 864e5).toISOString().slice(0, 10);
  const i = dates.findIndex((d) => d >= c);
  return Math.min(i < 0 ? 0 : i, Math.max(0, dates.length - 2));
}
/* ---------- 1. 금리 ---------- */
function rateCard(r, label, key, extra) {
  if (!r) return `<div class="card"><div class="empty">${esc(label)} 금리를 받지 못했어요.</div></div>`;
  return `<div class="card">
    <div class="chart-title" style="margin-top:0">${esc(label)}</div>
    <div><span class="big">${pct(r.latest, 2)}</span> <span class="src">${esc(r.date)} · ${esc(r.source)}</span></div>
    ${r.stale ? `<div class="stale">⚠ 이번에 못 받아서 직전 값(${esc(r.date)})이에요</div>` : ""}${extra || ""}
    <div class="chart-box" id="vm-rate-${key}"></div>
  </div>`;
}
// 기준금리(미국 FRED · 한국은행)는 이 대시보드 data.json 의 policy 에서 (밸류 신호등엔 없음)
const POL = () => (typeof DATA !== "undefined" && DATA && DATA.policy) || {};
// 미국채 10년 칸 아래 작은 코멘트: 최근 3개월 최고 → 지금(이 대시보드 Yahoo 일별 자료로 자동 계산) + 하락 이유(사용자 메모)
function us10yNote() {
  const U = typeof DATA !== "undefined" && DATA?.usmkt?.us10y;
  if (!U) return "";
  const i = U.x.length - 1, c = new Date(new Date(U.x[i]) - 91 * 864e5).toISOString().slice(0, 10);
  let hi = -1;
  U.x.forEach((d, j) => { if (d >= c && (hi < 0 || U.y[j] > U.y[hi])) hi = j; });
  const dd = U.y[i] - U.y[hi], md = (s) => s.slice(5).replace("-", "/");
  return `<div class="memo" style="margin:8px 0 0;font-size:12px">📉 3개월 최고 ${pct(U.y[hi], 2)}(${md(U.x[hi])}) → 지금 ${pct(U.y[i], 2)}(${md(U.x[i])}), <b>${dd >= 0 ? "+" : ""}${n(dd, 2)}%p</b>
    <br><b>10월 들어 금리가 내려온 이유</b> ① AI 인프라에 앞다퉈 돈 쏟던 투자 경쟁 심리가 식음 ② 불안해서 안전자산(국채)을 찾는 사람이 늘어 국채 값↑ = 금리↓
    <br>→ 금리가 내려가면 <b>배당주·방어주(경기 덜 타는 주식)·가치주(싼 주식)</b>가 상대적으로 매력적이라 오르는 이유가 돼요.
    <span class="src">이유: user: 사용자 메모(26.10.30)</span></div>`;
}
function drawMarketCharts() {
  const C = themeColors(), P = POL();
  for (const [key, r] of [["us", M.rates.us10y], ["usp", P.us], ["kr", M.rates.kr3y], ["krp", P.kr]]) {
    if (r && document.getElementById(`vm-rate-${key}`)) {
      const h = r.hist.slice(sliceP(r.hist.map((x) => x[0])));
      makeChart(`vm-rate-${key}`, h.map((x) => x[0]), [{ label: "금리", data: h.map((x) => x[1]), borderColor: C.s1 }], { ysuf: "%", fmt: (v) => v.toFixed(2) + "%" });
    }
  }
}

/* 미국 기준금리 시장 예상 — 연방기금 선물 가격으로 CME FedWatch 와 같은 방식 계산(update.py build_fedwatch) */
function fedwatchCard() {
  const F = typeof DATA !== "undefined" && DATA && DATA.fedwatch, el = $("#vm-fedwatch");
  if (!el) return;
  if (!F) { el.innerHTML = `<div class="empty">기준금리 시장 예상을 계산하지 못했어요.</div>`; return; }
  const md = (s) => s.slice(2).replace(/-/g, ".");
  const last = F.table[F.table.length - 1], hikes = (last.exp - F.upper) / 0.25;
  const W = F.paths.m1, cd = W && [...F.paths.now.x].reverse().find((d) => W.x.includes(d));
  const vs = cd ? `<br>1달 전(${md(W.date)})엔 ${md(cd)} 회의 뒤를 ${pct(W.y[W.x.indexOf(cd)], 2)}로 봤는데, 지금은 ${pct(F.paths.now.y[F.paths.now.x.indexOf(cd)], 2)}예요.` : "";
  el.innerHTML = `<div class="chart-title" style="margin-top:0">🇺🇸 앞으로 미국 기준금리, 시장은 어떻게 보나 (FedWatch 방식)</div>
    <div class="note" style="margin:2px 0 6px">금리 선물 가격에 담긴 <b>시장의 내기</b>예요. FOMC 회의마다 기준금리(목표 범위 위쪽)가 몇 %가 될 확률인지 보여 줘요. 진한 칸 = 가장 가능성 높은 금리.</div>
    <div class="src" style="font-size:12px">지금 ${POL().us ? pct(POL().us.lower, 2) + "~" : ""}${pct(F.upper, 2)} → ${md(last.date)} 회의 뒤 평균 예상 <b>${pct(last.exp, 2)}</b> (0.25%p ${hikes >= 0 ? "인상" : "인하"} 약 ${n(Math.abs(hikes), 1)}번만큼)${vs}</div>
    <div class="tbl-wrap"><table class="fw"><thead><tr><th>FOMC</th>${F.levels.map((l) => `<th>${l.toFixed(2)}</th>`).join("")}</tr></thead><tbody>
    ${F.table.map((r) => { const mx = Math.max(...r.p);
      return `<tr><td>${md(r.date)}</td>${r.p.map((p) => `<td class="${p === mx ? "fw-top" : p >= 10 ? "fw-mid" : ""}">${p ? p.toFixed(1) : "–"}</td>`).join("")}</tr>`; }).join("")}
    </tbody></table></div>
    <div class="note">칸 = 확률(%), 맨 윗줄 = 기준금리(%). ${md(F.date)} 미국 장 마감 가격 기준.</div>
    <h3>회의별 평균 예상 기준금리 — 지금 · 1주 전 · 1달 전</h3>
    <div class="chart-box" id="vm-fw-path"></div><div id="vm-fw-path-legend"></div>
    <h3>여태까지 — 시장 예상 금리 흐름</h3>
    <div class="note">그 달 평균 금리를 시장이 몇 %로 보는지(선물 가격으로 계산)와 실제 금리(실효 기준금리)예요. 선이 오르면 "금리가 더 오를 것"이라는 쪽으로 바뀐 거예요.</div>
    <div class="chart-box tall" id="vm-fw-hist"></div><div id="vm-fw-hist-legend"></div>
    <div class="memo">🐒 <b>메모</b><br>· 시장은 12월·3월·6월에 한 번씩 올리는 <b>추세적 금리 인상 사이클</b>에 들어섰다고 보고 있어요.<br>· 인상 사이클 중후반엔 돈이 빨려 나가면서(유동성 흡수) <b>무언가 부러지는 신호</b>가 나오는 경우가 많아 시장 난이도가 올라갈 수 있어요.
      <div class="src">메모 출처: user: 사용자가 보내 준 글(26.10.28)</div></div>
    <div class="note">자료: Yahoo Finance(CBOT 30일 연방기금 선물 ZQ 월물 종가) · FRED(실효 기준금리 EFFR·목표 범위) · <a href="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm" target="_blank" rel="noopener">연준 FOMC 일정</a>. 계산법은 <a href="https://www.cmegroup.com/ko/markets/interest-rates/cme-fedwatch-tool.html" target="_blank" rel="noopener">CME FedWatch</a>와 같은 방식(README)이에요. 26.10.28 CME 화면(사용자 제공)과 회의별 확률이 몇 %p 안에서 같았어요. 매일 갱신.</div>`;
  const C = themeColors(), cols = [C.s1, C.s3, C.s7, C.muted];
  const ps = [["now", "지금"], ["w1", "1주 전"], ["m1", "1달 전"]].filter(([k]) => F.paths[k]);
  const px = [...new Set(ps.flatMap(([k]) => F.paths[k].x))].sort();
  const pds = ps.map(([k, lb], i) => { const p = F.paths[k];
    return { label: `${lb} (${md(p.date)})`, data: px.map((d) => (p.x.includes(d) ? p.y[p.x.indexOf(d)] : null)), borderColor: cols[i], borderDash: i ? [6, 3] : [] }; });
  makeChart("vm-fw-path", px, pds, { ysuf: "%", fmt: (v) => v.toFixed(2) + "%" });
  $("#vm-fw-path-legend").innerHTML = legend(pds.map((d) => [d.label, d.borderColor, !!d.borderDash.length]));
  const draw = () => {
    const L = F.lines, x0 = L.length ? L[0].x : [];
    const ex = Object.fromEntries(F.effr_hist.x.map((d, i) => [d, F.effr_hist.y[i]]));
    let carry = null;  // 실효 기준금리는 발표 안 된 날 직전 값 유지
    const effr = x0.map((d) => (carry = ex[d] ?? carry));
    const i0 = sliceP(x0), xs = x0.slice(i0);
    const ds = L.map((l, i) => { const m = Object.fromEntries(l.x.map((d, j) => [d, l.y[j]]));
      return { label: `${l.month.slice(2, 4)}년 ${+l.month.slice(5)}월 평균 예상`, data: xs.map((d) => m[d] ?? null), borderColor: cols[i] }; });
    ds.push({ label: "실제 실효 기준금리", data: effr.slice(i0), borderColor: C.muted, borderDash: [2, 2] });
    makeChart("vm-fw-hist", xs, ds, { ysuf: "%", fmt: (v) => v.toFixed(2) + "%" });
    $("#vm-fw-hist-legend").innerHTML = legend(ds.map((d) => [d.label, d.borderColor, !!d.borderDash?.length]));
  };
  DRAWS.push(draw);
  draw();
}

/* 주식 4계 카드의 '우리 데이터로 대 보기': 금리(FedWatch 방식 1년 예상)·실적(코스피 내년 이익 증가율)·주가(코스피 MDD) 방향만 표와 맞춰 봄 */
function seasonsNow() {
  const el = $("#vm-seasons-now"), D = typeof DATA !== "undefined" ? DATA : null;
  if (!el || !D) return;
  const F = D.fedwatch, Md = D.mdd, g = M.market.kospi_forward?.years?.growth || {};
  const gy = Object.keys(g).find((y) => g[y] != null);
  if (!F || !Md || !gy) { el.innerHTML = ""; return; }
  const last = F.table[F.table.length - 1], hikes = (last.exp - F.upper) / 0.25, gr = g[gy];
  const r = hikes >= 0.5 ? "↑" : hikes <= -0.5 ? "↓" : "→", e = gr > 5 ? "↗" : gr < -5 ? "↘" : "→", p = Md.last <= -10 ? "↓" : "↗";
  const row = { "↓↘↗": "① 금융장세(봄)", "↑↗↗": "③ 실적장세(여름)", "↑↗↓": "④ 역금융장세(가을)", "↓↘↓": "⑥ 역실적장세(겨울)" }[r + e + p];
  const md = (s) => s.slice(2).replace(/-/g, ".");
  el.innerHTML = `<b>우리 데이터로 대 보기</b><br>· 금리 <b>${r}</b> — 시장은 1년 안에 미국 기준금리 0.25%p ${hikes >= 0 ? "인상" : "인하"} 약 ${n(Math.abs(hikes), 1)}번 예상(아래 FedWatch 방식 칸)
    <br>· 실적 <b>${e}</b> — 코스피 ${esc(gy)} 이익 증가율 ${pct(gr, 1, true)}(아래 연도별 표)
    <br>· 주가 <b>${p}</b> — 코스피 최고점(${md(Md.peak_date)} ${n(Md.peak, 0)}) 대비 ${pct(Md.last, 1)}(한국 탭 MDD)
    <br>→ ${row ? `표의 <b>${row}</b> 줄과 같은 모양이에요.${row.startsWith("④") ? " 글쓴이 판단과 같아요." : ""}` : "표의 어느 줄과도 딱 맞지 않아요."} <span class="src">세 숫자의 방향만 단순히 대 본 거라 참고용이에요.</span>${spreadLine(D)}`;
}
// DB증권이 말한 '임계 수준' 신호: 장단기 금리차(긴 금리 − 짧은 금리)가 마이너스로 들어갔나
function spreadLine(D) {
  const us = D.policy?.t10y2y, k10 = M.rates.kr10y, k3 = M.rates.kr3y;
  const kr = k10 && k3 ? k10.latest - k3.latest : null;
  const sp = (v) => `<b>${v >= 0 ? "+" : ""}${n(v, 2)}%p</b>`;
  const parts = [us ? `미국 10년−2년 ${sp(us.latest)} (${esc(us.date)})` : "", kr != null ? `한국 10년−3년 ${sp(kr)} (${esc(k10.date)})` : ""].filter(Boolean);
  if (!parts.length) return "";
  const neg = (us && us.latest < 0) || (kr != null && kr < 0);
  return `<br>· 장단기 금리차: ${parts.join(" · ")} → ${neg ? "<b>마이너스권에 들어갔어요</b> — DB증권이 본 '임계 수준' 신호예요." : "아직 플러스라 DB증권이 본 '임계 수준'(마이너스권 진입) 전이에요."}`;
}

/* ---------- 2. 지수 밸류에이션 ---------- */
function matrixTable(rows, cols, price, rowHead, what) {
  const near = (v) => v && Math.abs(v / price - 1) < 0.05;
  return `<div class="tbl-wrap"><table><thead><tr><th>${esc(rowHead)}</th>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>
    ${rows.map((r) => `<tr><td>${esc(r.label)} <span class="src">${r.mult == null ? "" : n(r.mult, 2) + "배"}</span></td>${cols.map((c) => {
      const v = r.cells[c];
      if (v == null) return "<td>N/A</td>";
      const up = (v / price - 1) * 100;
      return `<td class="${up >= 0 ? "cheap" : "dear"} ${near(v) ? "near" : ""}">${n(v)}<br><span class="src">${pct(up, 0, true)}</span></td>`;
    }).join("")}</tr>`).join("")}</tbody></table></div>
    <div class="note">칸 아래 %는 현재 ${what} ${n(price, 2)} 대비 차이 (초록 = 지금보다 높음, 빨강 = 낮음). 파란 테두리 = 지금과 5% 이내.</div>`;
}

function spBand() {
  const t = M.market.sp500_trailing;
  const f = M.market.sp500_forward;
  const el = $("#vm-spBand");
  if (!t || !t.hist) { el.innerHTML = `<div class="empty">N/A — S&P500 PER 데이터를 받지 못했어요.</div>`; return; }
  const tcur = t.hist[t.hist.length - 1];
  el.innerHTML = `<div class="tiles">
      <div class="tile"><div class="tl">선행 12개월 PER</div><div class="tv">${f ? times(f.value) : "N/A"}</div><div class="src">FactSet · ${esc(f?.date || "")}</div></div>
      <div class="tile"><div class="tl">후행 PER (차트 기준)</div><div class="tv">${times(tcur[1])}</div><div class="src">multpl · ${esc(tcur[0])}</div></div>
    </div>
    <h3>후행 PER 밴드</h3>
    ${bandHTML("vm-sp")}
    <div class="note">차트는 후행 PER(지난 12개월 실적, ${esc(t.source)}) 기준이에요. 선행 PER 과거 이력은 무료 출처가 없어서 FactSet 값을 매번 모으고 있어요.</div>
    <h3>선행 PER 기준값 <span class="src">FactSet</span></h3>
    <div class="kv">
      <div class="k">선행 12개월 PER</div><div class="v">${f ? times(f.value) : "N/A"}</div>
      <div class="k">선행 PER 5년 평균</div><div class="v">${f ? times(f.avg5) : "N/A"}</div>
      <div class="k">선행 PER 10년 평균</div><div class="v">${f ? times(f.avg10) : "N/A"}</div></div>
    ${f && f.eps ? indexEPS(f) : `<div class="note">연도별 EPS: N/A (FactSet 보고서를 읽지 못했어요)</div>`}`;
  wireHBand("vm-sp", t.hist, "S&P500 PER", "배", 1);
}

/* 지수 연도별 EPS 표 + 'EPS × PER = 지수' 시나리오 */
function indexEPS(f) {
  const ys = Object.keys(f.eps);
  const lvl = f.index;
  const extra = [["5년 평균", f.avg5], ["10년 평균", f.avg10]];
  const mults = [...(M.settings.index_scenarios?.sp500_per || [16, 18, 20, 22]).map((m) => [`${m}배`, m]), ...extra.map(([l, m]) => [`선행 ${l} ${m}배`, m])];
  const rows = mults.map(([label, m]) => ({ label, mult: null, cells: Object.fromEntries(ys.map((y) => [y, f.eps[y] ? Math.round(f.eps[y] * m) : null])) }));
  return `<h3>연도별 EPS·PER <span class="src">FactSet · ${esc(f.date)}</span></h3>
    <div class="tbl-wrap"><table><thead><tr><th></th>${ys.map((y) => `<th>${y}</th>`).join("")}</tr></thead><tbody>
      <tr><td>EPS ($)</td>${ys.map((y) => `<td>${f.eps[y] == null ? "N/A" : n(f.eps[y], 1)}</td>`).join("")}</tr>
      <tr><td>이익 증가율</td>${ys.map((y) => `<td>${f.growth?.[y] == null ? "N/A" : pct(f.growth[y], 1, true)}</td>`).join("")}</tr>
      <tr><td>PER (현재 지수 ${n(lvl)})</td>${ys.map((y) => `<td>${f.eps[y] ? times(lvl / f.eps[y]) : "N/A"}</td>`).join("")}</tr>
    </tbody></table></div>
    <div class="note">${esc(f.eps_note || "")}</div>
    <h3>지수 시나리오 (EPS × PER)</h3>${matrixTable(rows, ys, lvl, "PER ↓ · EPS →", "지수")}`;
}

/* 차트 칸 (기간은 탭 맨 위 버튼) */
function bandHTML(id) {
  return `<div class="chart-box tall" id="${id}-chart"></div><div id="${id}-legend"></div><div class="note" id="${id}-stats"></div>`;
}
/* 가로선 밴드 차트 (S&P500 PER, 코스피 PBR 등 지수 배수) — 상단·평균·하단은 고른 기간 안에서 계산 */
function wireHBand(id, hist, name, unit, dg, marks = []) {
  const draw = () => {
    const C = themeColors();
    const h = hist.slice(sliceP(hist.map((x) => x[0])));
    const v = h.map((x) => x[1]).sort((a, b) => a - b);
    const q = (p) => v[Math.min(v.length - 1, Math.floor(p * (v.length - 1)))];
    const avg = v.reduce((a, b) => a + b, 0) / v.length;
    const lo = q(M.settings.band.low_pct / 100), hi = q(M.settings.band.high_pct / 100);
    const cur = h[h.length - 1][1];
    const posv = (v.filter((x) => x < cur).length / v.length) * 100;
    const ds = [
      { label: name, data: h.map((x) => x[1]), borderColor: C.s1 },
      { label: `상단 ${hi.toFixed(dg)}${unit}`, data: h.map(() => hi), borderColor: C.s4, borderDash: [6, 4] },
      { label: `평균 ${avg.toFixed(dg)}${unit}`, data: h.map(() => avg), borderColor: C.s2, borderDash: [6, 4] },
      { label: `하단 ${lo.toFixed(dg)}${unit}`, data: h.map(() => lo), borderColor: C.s3, borderDash: [6, 4] },
      // 내 기준선 (예: 코스피 PBR 0.8·0.9배)
      ...marks.map((m) => ({ label: `내 기준 ${m.value}${unit}`, data: h.map(() => m.value), borderColor: "#d03b3b", borderDash: [2, 3], borderWidth: 1.5 })),
    ];
    makeChart(`${id}-chart`, h.map((x) => x[0]), ds, { fmt: (x) => x.toFixed(dg + 1) + unit, ytick: `.${dg}f` });
    $(`#${id}-legend`).innerHTML = legend(ds.map((d) => [d.label, d.borderColor, !!d.borderDash]));
    $(`#${id}-stats`).innerHTML = `현재 <b>${cur.toFixed(dg + 1)}${unit}</b> · 밴드 위치 <b>${posv.toFixed(0)}%</b> <span class="src">(하단 0% · 상단 100%) ${h[0][0]} ~ ${h[h.length - 1][0]}</span>`;
  };
  DRAWS.push(draw);
  draw();
}

/* 코스피 밸류에이션 */
function kospiCard() {
  const k = M.market.kospi_forward;
  const t = M.market.kospi_trailing || {};
  const el = $("#vm-kospiBand");
  if (!k || !k.years) { el.innerHTML = `<div class="empty">N/A — 코스피 컨센서스를 계산하지 못했어요.</div>`; return; }
  const Y = k.years;
  const ys = Object.keys(Y.per);
  const lvl = k.level;
  const sc = M.settings.index_scenarios || {};
  const perRows = (sc.kospi_per || [8, 9, 10, 11, 12]).map((m) => ({ label: `${m}배`, mult: null, cells: Object.fromEntries(ys.map((y) => [y, Y.eps[y] ? Math.round(Y.eps[y] * m) : null])) }));
  const pbrRows = (sc.kospi_pbr || [0.8, 1.0, 1.2]).map((m) => ({ label: `${m}배`, mult: null, cells: Object.fromEntries(ys.map((y) => [y, Y.bps[y] ? Math.round(Y.bps[y] * m) : null])) }));
  const hasBand = t.pbr_hist && t.pbr_hist.length > 20;
  el.innerHTML = `<div class="tiles">
      <div class="tile"><div class="tl">선행 12개월 PER</div><div class="tv">${times(k.value)}</div><div class="src">직접 계산 · ${esc(k.date)}</div></div>
      <div class="tile"><div class="tl">선행 12개월 PBR</div><div class="tv">${times(k.fwd_pbr, 2)}</div><div class="src">${k.pbr_src ? "키움·FnGuide 방식 · " + esc(k.pbr_src) : "직접 계산"} · ROE ${pct(k.fwd_roe)}</div></div>
      <div class="tile"><div class="tl">12개월 선행 순이익</div><div class="tv">${k.fwd_ni_jo == null ? "N/A" : n(k.fwd_ni_jo, 1) + "조"}</div><div class="src">26E ${n(k.ni_jo?.[ys[0]], 0)}조 · 27E ${n(k.ni_jo?.[ys[1]], 0)}조</div></div>
      <div class="tile"><div class="tl">PBR (지난 실적)</div><div class="tv">${times(hasBand ? t.pbr : k.pbr_now, 2)}</div><div class="src">${hasBand ? "KRX · " + esc(t.date) : "직접 계산"}</div></div>
    </div>
    ${(M.settings.kospi_notes || []).length ? `<div class="memo">📝 <b>증권사 코멘트 메모</b>${M.settings.kospi_notes.map((x) => `<br>· ${esc(x.text)} <span class="src">(${esc(x.source)}, ${esc(x.date)})</span>`).join("")}</div>` : ""}
    ${M.market.kospi_fwd_bands ? `<h3>12개월 선행 PBR 밴드</h3>${bandHTML("vm-kfpbr")}<h3>12개월 선행 PER 밴드</h3>${bandHTML("vm-kfper")}` : ""}
    ${hasBand ? kospiMarksTable() : ""}
    <h3>연도별 EPS·BPS·PER·PBR <span class="src">코스피 ${n(lvl, 2)} 기준</span></h3>
    <div class="tbl-wrap"><table><thead><tr><th></th>${ys.map((y) => `<th>${y}</th>`).join("")}</tr></thead><tbody>
      <tr><td>EPS (지수 포인트)</td>${ys.map((y) => `<td>${n(Y.eps[y], 0)}</td>`).join("")}</tr>
      <tr><td>이익 증가율</td>${ys.map((y) => `<td>${Y.growth[y] == null ? "-" : pct(Y.growth[y], 1, true)}</td>`).join("")}</tr>
      <tr><td>PER</td>${ys.map((y) => `<td>${times(Y.per[y], 2)}</td>`).join("")}</tr>
      <tr><td>BPS (지수 포인트)</td>${ys.map((y) => `<td>${n(Y.bps[y], 0)}</td>`).join("")}</tr>
      <tr><td>PBR</td>${ys.map((y) => `<td>${times(Y.pbr[y], 2)}</td>`).join("")}</tr>
    </tbody></table></div>
    <div class="note">${esc(k.method)} · ${esc(k.source)}</div>
    <h3>코스피 시나리오: EPS × PER</h3>${matrixTable(perRows, ys, lvl, "PER ↓ · EPS →", "지수")}
    <h3>코스피 시나리오: BPS × PBR</h3>${matrixTable(pbrRows, ys, lvl, "PBR ↓ · BPS →", "지수")}`;
  if (M.market.kospi_fwd_bands) {
    const kb = M.settings.kospi_bands || {};
    wireFwdBand("vm-kfpbr", "f_bps", kb.pbr || [0.7, 0.8, 0.9, 1.0, 1.1], "PBR");
    wireFwdBand("vm-kfper", "f_eps", kb.per || [5, 6, 7, 8], "PER");
  }
}

/* 코스피 12개월 선행 PER·PBR 밴드 (지수 선 + '선행 EPS·BPS × 배수' 선) */
function wireFwdBand(id, key, mults, unitName) {
  const B = M.market.kospi_fwd_bands;
  const draw = () => {
    const C = themeColors();
    const i0 = sliceP(B.dates);
    const dates = B.dates.slice(i0), lv = B.level.slice(i0), f = B[key].slice(i0);
    const cols = [C.s3, C.muted, C.s7, "#d03b3b", C.s4, C.s2];
    const ds = [{ label: "코스피", data: lv, borderColor: C.s1, borderWidth: 2 },
      ...mults.map((m, i) => ({ label: `${m}배`, data: f.map((x) => (x ? Math.round(x * m) : null)), borderColor: cols[i % cols.length],
        borderDash: m === 1 ? [6, 3] : [], borderWidth: m === 1 ? 2 : 1.5 }))];
    makeChart(`${id}-chart`, dates, ds, { fmt: (v) => n(v, 0), ytick: ",d" });
    $(`#${id}-legend`).innerHTML = legend(ds.map((d) => [d.label, d.borderColor, !!d.borderDash?.length]));
    const cur = lv[lv.length - 1], fc = f[f.length - 1];
    $(`#${id}-stats`).innerHTML = `지금 코스피 ${n(cur, 0)} = 선행 ${unitName} <b>${n(cur / fc, 2)}배</b> · ` +
      mults.map((m) => `${m}배 = <b>${n(fc * m, 0)}</b>`).join(" · ") + `<br><span class="src">${esc(B.note)}</span>`;
  };
  DRAWS.push(draw);
  draw();
}

/* 코스피 PBR 내 기준선: 그 PBR 이 되는 코스피 지수와 지금 대비 % */
function kospiMarks() {
  const t = M.market.kospi_trailing || {};
  const lvl = M.market.kospi_forward?.level;
  if (!t.pbr || !lvl) return [];
  const fp = M.market.kospi_forward?.fwd_pbr;
  return (M.settings.kospi_pbr_marks || []).map((m) => {
    const at = (lvl * m.value) / t.pbr;            // 지난 실적(KRX) PBR 기준
    const af = fp ? (lvl * m.value) / fp : null;     // 12개월 선행 PBR 기준 (증권사 차트와 같은 기준)
    return { ...m, level: at, chg: (at / lvl - 1) * 100, level_f: af, chg_f: af ? (af / lvl - 1) * 100 : null };
  });
}
function kospiMarksTable() {
  const ms = kospiMarks();
  if (!ms.length) return "";
  const t = M.market.kospi_trailing;
  const fp = M.market.kospi_forward?.fwd_pbr;
  return `<h3>내 기준선: 코스피가 얼마면? <span class="src">지금 선행 PBR ${n(fp, 2)}배 · 지난 실적 PBR ${n(t.pbr, 2)}배</span></h3>
    <div class="tbl-wrap"><table class="compact"><thead><tr><th>기준</th><th>PBR</th><th>선행 기준<br>코스피</th><th>지난 실적 기준<br>코스피</th></tr></thead><tbody>
    ${ms.map((m) => `<tr><td style="white-space:normal">${esc(m.label)}</td><td>${m.value}배</td>
      <td>${m.level_f ? n(m.level_f, 0) : "N/A"}<br><span class="src">${pct(m.chg_f, 1, true)}</span></td>
      <td>${n(m.level, 0)}<br><span class="src">${pct(m.chg, 1, true)}</span></td></tr>`).join("")}
    </tbody></table></div>
    <div class="note">선행 기준 = 12개월 선행 BPS(올해·내년 예상 자본을 남은 달 수로 섞은 값. 키움·FnGuide처럼 시총 상위 약 350개로 직접 계산) · 지난 실적 기준 = KRX PBR. 자본(BPS)이 그대로라고 보고 계산했어요.</div>`;
}

/* ---------- 3. 크로스체크 ---------- */
function crossCheck() {
  const w = M.market.woosan, el = $("#vm-crossCheck");
  if (!w) { el.innerHTML = `<div class="empty">우산 채널 숫자를 아직 못 받았어요.</div>`; return; }
  const k = M.market.kospi_forward || {}, sp = M.market.sp500_forward || {};
  const st = (code) => (M.mag7_fwd_per || {})[code];
  const rows = [
    ["KOSPI 12m fwd P/E", k.value, w.kospi_pe, "배"], ["KOSPI 12m fwd P/E 3년 평균", null, w.kospi_pe3, "배"], ["KOSPI 12m fwd P/E 5년 평균", null, w.kospi_pe5, "배"],
    ["KOSPI 12m fwd 순이익(지배)", k.fwd_ni_jo, w.kospi_ni, "조원"], ["코스닥 12m fwd P/E", null, w.kosdaq_pe, "배"],
    ["S&P500 12m fwd P/E", sp.value, w.sp_pe, "배"], ["S&P500 12m fwd P/E 3년 평균", null, w.sp_pe3, "배"],
    ["S&P500 12m fwd P/E 5년 평균", sp.avg5, w.sp_pe5, "배"], ["S&P500 12m fwd EPS", sp.fwd12_eps, w.sp_eps, ""],
    ...[["GOOGL", "GOOG", "알파벳"], ["NVDA", "NVDA", "엔비디아"], ["MSFT", "MSFT", "마이크로소프트"], ["META", "META", "메타"], ["TSLA", "TSLA", "테슬라"], ["AAPL", null, "애플"], ["AMZN", null, "아마존"]]
      .map(([t, code, nm]) => [`${nm} 12m P/E`, code ? st(code) : null, (w.mag7 || {})[t], "배"]),
  ];
  el.innerHTML = `<div class="tbl-wrap"><table class="compact" style="width:100%"><thead><tr><th style="text-align:left">항목</th><th>이 사이트</th><th>우산 채널</th><th>차이</th></tr></thead><tbody>
    ${rows.filter((r) => r[2] != null).map(([lab, a, b, u]) => {
      const d = a != null && b ? (a / b - 1) * 100 : null;
      return `<tr><td>${esc(lab)}</td><td style="text-align:right">${a == null ? '<span class="src">없음</span>' : n(a, 1) + u}</td><td style="text-align:right">${n(b, 1)}${u}</td>
        <td style="text-align:right">${d == null ? "-" : `${Math.abs(d) > 5 ? "⚠ " : "✓ "}${pct(d, 1, true)}`}</td></tr>`;
    }).join("")}</tbody></table></div>
    <div class="note src">우산 채널 ${esc(w.date || "")} 글 기준 (매그7 ${esc(w.mag7_date || "")}) · <a href="${esc(w.url || "https://t.me/WoosanXNNN")}" target="_blank" rel="noopener">원문</a> ·
      이 사이트(밸류 신호등): 코스피 = 시총 상위 150개 중 컨센서스 있는 종목 합산, S&P500 = FactSet 주간 보고서, 종목 = 12MF EPS(야후·나스닥·Finviz) 기준 · 채널은 Yardeni·LSEG(Datastream) 기준이라 조금 다를 수 있어요 · 5% 넘게 다르면 ⚠</div>`;
}

function render() {
  $("#vm-updated").textContent = `밸류 신호등 ${M.updated_at} (KST) 갱신분`;
  const bar = $("#vm-bar");
  bar.innerHTML = PERIODS.map(([k, label]) => `<button data-p="${k}" class="${k === VP ? "on" : ""}">${label}</button>`).join("");
  bar.onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    VP = b.dataset.p;
    try { localStorage.setItem("vmPeriod", VP); } catch (_) {}
    bar.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    drawMarketCharts();
    DRAWS.forEach((d) => d());
  };
  const k10 = M.rates.kr10y, P = POL();
  $("#vm-signals").innerHTML = rateCard(M.rates.us10y, "미국채 10년", "us", us10yNote()) +
    rateCard(P.us, "미국 기준금리 (목표 범위 위쪽)", "usp", P.us ? `<div class="src">목표 범위 ${pct(P.us.lower, 2)}~${pct(P.us.latest, 2)}</div>` : "") +
    rateCard(M.rates.kr3y, "국고채 3년", "kr", k10 ? `<div class="src">국고채 10년(참고) ${pct(k10.latest, 2)} · ${esc(k10.date)}</div>` : "") +
    rateCard(P.kr, "한국 기준금리", "krp", P.kr ? `<div class="src">날짜 = 마지막으로 바뀐 날 · <a href="${esc(P.kr.url)}" target="_blank" rel="noopener">한국은행 기준금리 표 ↗</a></div>` : "");
  DRAWS = [];
  drawMarketCharts();
  fedwatchCard();
  seasonsNow();
  kospiCard();
  spBand();
  crossCheck();
  const srcs = new Set();
  for (const r of Object.values(M.rates)) if (r?.source) srcs.add(r.source);
  for (const k of ["sp500_trailing", "sp500_forward", "kospi_forward"]) if (M.market[k]?.source) srcs.add(M.market[k].source.split(" (")[0]);
  ["네이버 증권", "와이즈리포트", "OpenDART"].forEach((x) => srcs.add(x));
  $("#vm-srcs").textContent = [...srcs].join(" · ");
}

// index.html 의 renderAll 이 부름 (다크모드 전환 때도 다시 그림)
window.renderValMarket = () => {
  if (!document.getElementById("mktv")) return;
  if (M) { render(); return; }
  loading = loading || fetch("market.json?t=" + Date.now()).then((r) => r.json()).then((d) => { M = d; render(); })
    .catch(() => { $("#vm-updated").textContent = "시장 탭 데이터(market.json)를 불러오지 못했어요."; });
};
})();
