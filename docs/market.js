/* '🚦 시장' 탭 — 밸류 신호등(사용자의 다른 대시보드) 시장 탭을 옮겨 온 화면.
   데이터: market.json (update.py 의 fetch_valmarket 이 그쪽 저장소에서 복사). 화면 코드는 그쪽 site/app.js 를 거의 그대로,
   차트만 Chart.js 대신 이 페이지의 Plotly(baseLayout·axis·CFG)로 그림 */
(() => {
"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const LIGHT = {
  green: { emoji: "🟢", market: "매력 구간" },
  yellow: { emoji: "🟡", market: "중립" },
  red: { emoji: "🔴", market: "위험 구간" },
  na: { emoji: "⚪", market: "N/A" },
};
let M = null, loading = null;

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
function lamp(sig) { const L = LIGHT[sig] || LIGHT.na; return `<div class="lamp ${sig}" aria-hidden="true">${L.emoji}</div>`; }

/* ---------- 차트 ---------- */
function themeColors() {
  return { s1: css("--g1"), s2: css("--g2"), s3: css("--g3"), s4: css("--g4"), s7: css("--g7"), muted: css("--muted") };
}
// 그쪽 makeChart 와 같은 입력(날짜 목록 + 선 목록)을 Plotly 선 차트로. 마우스 올리면 날짜 전체 + 숫자
function makeChart(id, labels, datasets, opt = {}) {
  const fmt = opt.fmt || ((v) => n(v, 2));
  const dash = (d) => !d.borderDash || !d.borderDash.length ? "solid" : d.borderDash[0] <= 2 ? "dot" : "dash";
  Plotly.react(id, datasets.map((d) => ({
    x: labels, y: d.data, type: "scatter", mode: "lines", name: d.label,
    line: { color: d.borderColor, width: d.borderWidth || 2, dash: dash(d) },
    text: d.data.map((v) => (v == null ? "" : fmt(v))), hovertemplate: "%{text}",
  })), Object.assign(baseLayout(), {
    margin: { l: 48, r: 8, t: 8, b: 28 },
    xaxis: axis({ type: "date", tickformat: "%Y", nticks: 6, showgrid: false }),
    yaxis: axis({ nticks: 6, tickformat: opt.ytick || "", ticksuffix: opt.ysuf || "", rangemode: opt.zero ? "tozero" : "normal" }),
  }), CFG);
}
function legend(items) {
  return `<div class="legend">${items.map(([label, color, dash]) => `<span><i class="${dash ? "dash" : ""}" style="border-color:${color}"></i>${esc(label)}</span>`).join("")}</div>`;
}
function sliceYears(dates, years) {
  if (!years) return 0;
  const end = new Date(dates[dates.length - 1]);
  const cut = new Date(end); cut.setFullYear(end.getFullYear() - years);
  const c = cut.toISOString().slice(0, 10);
  const i = dates.findIndex((d) => d >= c);
  return i < 0 ? 0 : i;
}
// 기간 버튼(5년·10년·전체)
function wireSeg(id, draw) {
  const seg = document.getElementById(`${id}-seg`);
  seg.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    draw(Number(b.dataset.y));
  });
}

/* ---------- 1. 금리 vs 주식 신호등 ---------- */
function marketCard(sig, rateObj, extraRate, key) {
  const L = LIGHT[sig.signal] || LIGHT.na;
  if (sig.signal === "na") {
    return `<div class="card"><div class="light">${lamp("na")}<div><div class="verdict na">${esc(sig.name)} · N/A</div>
      <div class="sub">${esc(sig.reason || "")}</div></div></div></div>`;
  }
  const stale = sig.stale || rateObj?.stale ? `<div class="stale">⚠ ${esc(sig.rate_label)} 금리는 이번에 못 받아서 직전 값(${esc(rateObj?.date || "")})이에요</div>` : "";
  return `<div class="card">
    <div class="light">${lamp(sig.signal)}<div>
      <div class="verdict ${sig.signal}">${esc(sig.name)} · ${L.market}</div>
      <div class="sub">비율 <span class="big">${n(sig.ratio, 2)}</span></div>
    </div></div>
    <div class="kv">
      <div class="k kgrp">배수 비교</div><div class="v"></div>
      <div class="k">${esc(sig.per_label)}</div><div class="v">${times(sig.per)}<span class="src">${esc(sig.per_source)}</span></div>
      <div class="k">채권 환산 PER <span class="src nowrap">1 ÷ 금리</span></div><div class="v">${times(sig.bond_per)}</div>
      <div class="k kgrp">수익률 비교</div><div class="v"></div>
      <div class="k">주식 이익수익률 <span class="src nowrap">1 ÷ PER</span></div><div class="v">${pct(sig.earnings_yield, 2)}</div>
      <div class="k">${esc(sig.rate_label)}</div><div class="v">${pct(sig.rate, 2)}<span class="src">${esc(rateObj?.source || "")} · ${esc(rateObj?.date || "")}</span></div>
      ${extraRate ? `<div class="k" style="font-size:12px">${esc(extraRate.label)} (참고)</div><div class="v" style="font-size:12px;font-weight:400">${pct(extraRate.latest, 2)}</div>` : ""}
    </div>${stale}${key === "kr" ? kospiPbrBrief() : ""}
    <div class="chart-title">${esc(sig.rate_label)} 추이 (5년)</div>
    <div class="chart-box" id="vm-rate-${key}"></div>
  </div>`;
}
function drawMarketCharts() {
  const C = themeColors();
  for (const [key, rk] of [["us", "us10y"], ["kr", "kr3y"]]) {
    const r = M.rates[rk];
    if (r && document.getElementById(`vm-rate-${key}`)) {
      const h = r.hist.slice(sliceYears(r.hist.map((x) => x[0]), 5));
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
    ${hBandHTML("vm-sp")}
    <div class="note">차트는 후행 PER(지난 12개월 실적, ${esc(t.source)}) 기준이에요. 선행 PER 과거 이력은 무료 출처가 없어서 FactSet 값을 매번 모으고 있어요.</div>
    <h3>선행 PER 기준값 <span class="src">FactSet</span></h3>
    <div class="kv">
      <div class="k">선행 12개월 PER</div><div class="v">${f ? times(f.value) : "N/A"}</div>
      <div class="k">선행 PER 5년 평균</div><div class="v">${f ? times(f.avg5) : "N/A"}</div>
      <div class="k">선행 PER 10년 평균</div><div class="v">${f ? times(f.avg10) : "N/A"}</div></div>
    ${f && f.eps ? indexEPS(f) : `<div class="note">연도별 EPS: N/A (FactSet 보고서를 읽지 못했어요)</div>`}`;
  wireHBand("vm-sp", t.hist, "S&P500 PER", "배", 1, 10);
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

/* 가로선 밴드 차트 (S&P500 PER, 코스피 PBR 등 지수 배수) */
function hBandHTML(id) {
  return `<div class="seg" id="${id}-seg"><button data-y="0">역사적</button><button data-y="10" class="on">10년</button><button data-y="5">5년</button></div>
    <div class="chart-box tall" id="${id}-chart"></div><div id="${id}-legend"></div><div class="note" id="${id}-stats"></div>`;
}
function wireHBand(id, hist, name, unit, dg, startYears, marks = []) {
  const draw = (years) => {
    const C = themeColors();
    const h = hist.slice(sliceYears(hist.map((x) => x[0]), years));
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
  draw(startYears);
  wireSeg(id, draw);
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
      <div class="tile"><div class="tl">선행 12개월 PBR</div><div class="tv">${times(k.fwd_pbr, 2)}</div><div class="src">직접 계산 · ROE ${pct(k.fwd_roe)}</div></div>
      <div class="tile"><div class="tl">12개월 선행 순이익</div><div class="tv">${k.fwd_ni_jo == null ? "N/A" : n(k.fwd_ni_jo, 1) + "조"}</div><div class="src">26E ${n(k.ni_jo?.[ys[0]], 0)}조 · 27E ${n(k.ni_jo?.[ys[1]], 0)}조</div></div>
      <div class="tile"><div class="tl">PBR (지난 실적)</div><div class="tv">${times(hasBand ? t.pbr : k.pbr_now, 2)}</div><div class="src">${hasBand ? "KRX · " + esc(t.date) : "직접 계산"}</div></div>
    </div>
    ${(M.settings.kospi_notes || []).length ? `<div class="memo">📝 <b>증권사 코멘트 메모</b>${M.settings.kospi_notes.map((x) => `<br>· ${esc(x.text)} <span class="src">(${esc(x.source)}, ${esc(x.date)})</span>`).join("")}</div>` : ""}
    ${M.market.kospi_fwd_bands ? fwdBandHTML("vm-kfpbr", "12개월 선행 PBR 밴드") + fwdBandHTML("vm-kfper", "12개월 선행 PER 밴드") : ""}
    ${hasBand ? kospiMarksTable() : ""}
    <h3>PBR 밴드 (지난 실적 기준, KRX)</h3>
    ${hasBand ? hBandHTML("vm-kp") : `<div class="warnbox">코스피 PBR 10년 이력은 한국거래소(KRX) 자료에만 있어요.</div>`}
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
  if (hasBand) wireHBand("vm-kp", t.pbr_hist, "코스피 PBR", "배", 2, 10, M.settings.kospi_pbr_marks || []);
  if (M.market.kospi_fwd_bands) {
    const kb = M.settings.kospi_bands || {};
    wireFwdBand("vm-kfpbr", "f_bps", kb.pbr || [0.7, 0.8, 0.9, 1.0, 1.1], "PBR");
    wireFwdBand("vm-kfper", "f_eps", kb.per || [5, 6, 7, 8], "PER");
  }
}

/* 코스피 12개월 선행 PER·PBR 밴드 (지수 선 + '선행 EPS·BPS × 배수' 선) */
function fwdBandHTML(id, title) {
  return `<h3>${title}</h3><div class="seg" id="${id}-seg"><button data-y="0">전체</button><button data-y="10">10년</button><button data-y="5" class="on">5년</button></div>
    <div class="chart-box tall" id="${id}-chart"></div><div id="${id}-legend"></div><div class="note" id="${id}-stats"></div>`;
}
function wireFwdBand(id, key, mults, unitName) {
  const B = M.market.kospi_fwd_bands;
  const draw = (years) => {
    const C = themeColors();
    const i0 = sliceYears(B.dates, years);
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
  draw(5);
  wireSeg(id, draw);
}

/* 금리 신호등 한국 카드 안에 넣는 코스피 PBR 요약 */
function kospiPbrBrief() {
  const t = M.market.kospi_trailing || {};
  const h = t.pbr_hist || [];
  if (!t.pbr || h.length < 20) return "";
  const v = h.slice(sliceYears(h.map((x) => x[0]), 10)).map((x) => x[1]);
  const pos = (v.filter((x) => x < t.pbr).length / v.length) * 100;
  return `<div class="memo" style="margin-top:12px">
    <b>코스피 PBR ${n(t.pbr, 2)}배</b> · 10년 밴드 ${n(pos, 0)}% 위치
    · 선행 PBR ${n(M.market.kospi_forward?.fwd_pbr, 2)}배
    ${kospiMarks().map((m) => `<br>· PBR ${m.value}배${m.label.startsWith("PBR") ? "" : ` (${esc(m.label.split(" (")[0])})`} = 코스피 선행 기준 <b>${m.level_f ? n(m.level_f, 0) : "N/A"}</b> <span class="nowrap">(${pct(m.chg_f, 0, true)})</span> · 지난 실적 기준 ${n(m.level, 0)}`).join("")}
    <br><a href="#vm-kospiBand" onclick="document.getElementById('vm-kospiBand').scrollIntoView({behavior:'smooth'});return false;">PBR 밴드 차트 보기 ↓</a></div>`;
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
    <div class="note">선행 기준 = 12개월 선행 BPS(직접 계산, 증권사 차트와 같은 방식) · 지난 실적 기준 = KRX PBR. 자본(BPS)이 그대로라고 보고 계산했어요.</div>`;
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
  const b = M.settings.bond_signal;
  $("#vm-updated").textContent = `밸류 신호등 ${M.updated_at} (KST) 갱신분`;
  $("#vm-ruleLine").textContent = `비율 = 주식 선행 PER ÷ 채권 환산 PER(1 ÷ 금리). ${b.green_max} 이하 🟢 매력 · ${b.yellow_max} 이하 🟡 중립 · 그 위 🔴 위험`;
  $("#vm-signals").innerHTML =
    marketCard(M.signals.us, M.rates.us10y, null, "us") +
    marketCard(M.signals.kr, M.rates.kr3y, M.rates.kr10y ? { label: "국고채 10년", latest: M.rates.kr10y.latest } : null, "kr");
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
