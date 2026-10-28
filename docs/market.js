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
function drawMarketCharts() {
  const C = themeColors();
  for (const [key, rk] of [["us", "us10y"], ["kr", "kr3y"]]) {
    const r = M.rates[rk];
    if (r && document.getElementById(`vm-rate-${key}`)) {
      const h = r.hist.slice(sliceP(r.hist.map((x) => x[0])));
      makeChart(`vm-rate-${key}`, h.map((x) => x[0]), [{ label: "금리", data: h.map((x) => x[1]), borderColor: C.s1 }], { ysuf: "%", fmt: (v) => v.toFixed(2) + "%" });
    }
  }
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
  const k10 = M.rates.kr10y;
  $("#vm-signals").innerHTML = rateCard(M.rates.us10y, "미국채 10년", "us") +
    rateCard(M.rates.kr3y, "국고채 3년", "kr", k10 ? `<div class="src">국고채 10년(참고) ${pct(k10.latest, 2)} · ${esc(k10.date)}</div>` : "");
  DRAWS = [];
  drawMarketCharts();
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
