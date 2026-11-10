/* '🚦 시장' 탭 — 밸류 신호등(사용자의 다른 대시보드) 시장 탭을 옮겨 온 화면.
   데이터: market.json (update.py 의 fetch_valmarket 이 그쪽 저장소에서 복사). 화면 코드는 그쪽 site/app.js 를 거의 그대로,
   차트만 Chart.js 대신 이 페이지의 Plotly(baseLayout·axis·CFG)로 그림 */
(() => {
"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// 탭 전체 기간 버튼 (모든 차트가 같이 바뀜). [키, 이름, 일수(0 = 전부)]
const PERIODS = [["1m", "1개월", 30], ["3m", "3개월", 91], ["6m", "6개월", 183], ["1y", "1년", 365], ["3y", "3년", 1096], ["5y", "5년", 1826], ["all", "전체", 0]];
let M = null, loading = null, VP = "5y", DRAWS = [];
try { VP = localStorage.getItem("vmPeriod") || VP; } catch (e) {}
if (!PERIODS.some((p) => p[0] === VP)) VP = "5y";  // 예전 '1주' 저장값

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
/* 2027 코스피 경로 전망 — 미래에셋증권 그림(사용자 26.11.10). 점 = 그림 속 PER × EPS, 날짜는 그림에서 읽은 대략의 시점.
   실제 코스피(네이버 일별 종가)와 이 탭 27E EPS(매일)로 지금 어느 길에 가까운지 봄 (README) */
const MA_D = ["2026-12-15", "2027-05-15", "2027-11-15"], MA_END = "2027-12-31";
const MA_STEP = ["바닥권 탐색 (26년 말)", "2분기 PER 회복 (27년 2분기)", "하반기 완만한 상승 (27년 하반기)"];
const MA = [  // [이름, 그림 속 [PER, EPS] 3개(없으면 null)]
  ["중심 경로(27F 이익 유지)", [[3.7, 1463], [4.3, 1463], [4.7, 1463]]],
  ["이익 하향 시", [[3.7, 1327], [4.3, 1327], [4.7, 1304]]],
  ["범위 위쪽", [null, [5.1, 1536], [5.6, 1536]]],
];
function miraePath() {
  const el = $("#mapath-mkt"), K = typeof DATA !== "undefined" && DATA?.usmkt?.kospi;
  if (!el) return;
  const lv = MA.map(([, ps]) => ps.map((p) => (p ? Math.round(p[0] * p[1]) : null))), [mid, low, top] = lv;
  const md = (s) => s.slice(2).replace(/-/g, "."), e27 = M.market.kospi_forward?.years?.eps?.["27E"];
  const C = themeColors(), ink = css("--ink"), cols = [css("--spread"), C.s1, css("--cds")];  // 그림과 같은 색: 중심 주황 · 이익 하향 파랑 · 위쪽 빨강
  let now = "";
  if (K) {
    let i = MA_D.findIndex((d) => d >= K.last_date);
    if (i < 0) i = MA_D.length - 1;  // 마지막 시점이 지나면 마지막 목표와 비교
    const v = K.last, m = mid[i], l = low[i], t = top[i], vs = (x) => `${n(x, 0)}(${pct((x / v - 1) * 100, 1, true)})`;
    const where = t && v >= t ? "범위 위쪽보다도 높아요" : v >= m ? "중심 경로보다 위예요" : v >= l ? "중심 경로와 이익 하향 경로 사이예요" : "이익 하향 경로보다도 아래예요";
    const earn = e27 == null ? "" : `<br>· 이익 점검: 이 탭 코스피 <b>27E EPS ${n(e27, 1)}</b> → ${e27 >= 1536 ? "그림의 범위 위쪽 이익(1,536) 이상이에요"
      : e27 >= 1463 ? "그림의 중심(1,463)과 위쪽(1,536) 사이 — 아직 '이익 하향' 쪽은 아니에요" : e27 >= 1327 ? "그림의 중심(1,463)보다 낮아져 '이익 하향'(1,327) 쪽으로 가는 중이에요" : "그림의 '이익 하향' 이익(1,327)보다도 낮아요"}`;
    now = `<div class="memo" style="margin:8px 0 0">📍 <b>지금 위치(자동)</b> — 코스피 <b>${n(v, 2)}</b>(${md(K.last_date)}) = 그림 이익(EPS 1,463) 기준 PER <b>${n(v / 1463, 2)}배</b>
      <br>· 다음 목표 '${MA_STEP[i]}': 중심 ${vs(m)} · 이익 하향 ${vs(l)}${t ? ` · 범위 위쪽 ${vs(t)}` : ""} → <b>${where}</b>${earn}
      <div class="src">코스피 = 네이버 금융 일별 종가 · 27E EPS = 아래 '코스피' 칸 연도별 표(시총 상위 종목 컨센서스 합산 — 그림의 FnGuide 숫자와 종목 범위가 달라 조금 차이 날 수 있어요) · 목표값 = 그림의 PER × EPS · 괄호 = 지금보다 몇 % · 매일 자동</div></div>`;
  }
  el.innerHTML = `<h3><span>🧭 2027년 코스피 경로 전망 — 미래에셋증권 그림</span></h3>
    <div class="small" style="margin:2px 4px 6px">증권사(미래에셋)가 <b>코스피가 버는 돈(EPS) × 몇 배를 쳐주나(PER)</b>로 그린 2027년 길이에요. 지금은 <b>바닥을 찾는 중</b>이고, 2분기에 PER이 회복되고 하반기에 천천히 오를 거라고 봐요(주황 점선). 이익이 줄면 파란 점선 길이에요.</div>
    <div class="imgbox" onclick="this.classList.toggle('big')"><img src="img/kospi_path_mirae.jpg" width="1039" height="672" loading="lazy" alt="2027년 코스피 EPS·PER 경로 — 중심 경로·이익 하향 시·범위(미래에셋증권, 자료 FnGuide)"></div>
    <div class="small" style="margin:4px">👆 그림을 누르면 크게 보여요. 한 번 더 누르면 원래대로.</div>
    <div class="chart-title">그림의 길 위에 실제 코스피 겹쳐 보기 (매일 바뀜)</div>
    <div class="chart-box tall" id="vm-mapath"></div>${legend([["실제 코스피", ink], [MA[0][0], cols[0], true], [MA[1][0], cols[1], true], [MA[2][0], cols[2], true], ["주황 음영 = 범위", cols[0]]])}
    ${now}
    <details class="fold"><summary><b>📋 그림 숫자 표</b> <span class="small">(▶ 누르면 펼쳐져요)</span></summary>
      <div class="tbl-wrap"><table><thead><tr><th>그림 시점</th>${MA.map(([nm]) => `<th>${nm.split("(")[0]}</th>`).join("")}</tr></thead><tbody>
      ${MA_STEP.map((s, i) => `<tr><td style="white-space:normal;text-align:left">${s}</td>${MA.map(([, ps]) => `<td>${ps[i] ? `<b>${n(ps[i][0] * ps[i][1], 0)}</b><br><span class="src">${ps[i][0]}배×${n(ps[i][1])}</span>` : "–"}</td>`).join("")}</tr>`).join("")}
      </tbody></table></div>
      <div class="note">칸 = 코스피 = PER × EPS(그림 숫자로 계산). 그림의 PER이 소수 첫째 자리까지라 그림 점과 몇십 포인트 다를 수 있어요. 차트의 점 날짜(26.12.15 · 27.05.15 · 27.11.15)는 그림 위치로 읽은 대략의 시점이에요.</div></details>
    <div class="memo" style="margin:8px 0 0">📌 <b>미래에셋 2027년 선호 업종</b>: 헬스케어 · 2차전지 · 화장품 · 인터넷 · 호텔/레저 · 유틸리티 · 반도체
      <br><b>Top Picks</b>: 삼성바이오로직스 · 셀트리온 · SK바이오팜 · 삼성SDI · LG에너지솔루션 · 에이피알 · 코스맥스 · NAVER · 삼성에스디에스 · 파라다이스 · GS피앤엘 · 대한항공 · 한국전력 · 삼성전자 · SK하이닉스 · SK스퀘어 · DB하이텍
      <div class="src">출처: user: 사용자가 보내 준 미래에셋증권 글(<a href="https://t.me/eqmirae" target="_blank" rel="noopener">텔레그램 eqmirae</a>, 26.11.10)</div></div>
    <p class="note" style="margin:6px 4px 2px">자료: user: 사용자가 보내 준 미래에셋증권 리서치센터 그림 '2027년 코스피 EPS, PER'(그림 속 자료: FnGuide, 미래에셋증권 · 26.11.10 받음). 증권사 전망이고 투자 권유가 아니에요. 그림·길은 자동으로 안 바뀌고, 실제 코스피와 27E EPS만 매일 바뀌어요.</p>`;
  // 그림의 길(점 3개 + 끝까지 평평) + 범위 음영 + 실제 코스피
  const xs = [...MA_D, MA_END], ext = (a) => [...a, a[a.length - 1]];
  const path = (k, dash) => { const ys = ext(lv[k]);
    return { x: xs, y: ys, type: "scatter", mode: "lines+markers", line: { color: cols[k], width: 2, dash }, marker: { size: ys.map((_, j) => (j < 3 ? 8 : 0.1)), color: cols[k] },
      text: ys.map((v, j) => (v == null ? "" : j < 3 ? `${MA_STEP[j]}<br>${MA[k][0]}: PER ${MA[k][1][j][0]}배 × EPS ${n(MA[k][1][j][1])} = ${n(v, 0)}` : `${MA[k][0]} ${n(v, 0)}`)),
      hovertemplate: "%{x|%Y-%m-%d}<br>%{text}<extra></extra>" }; };
  const tr = [{ x: [MA_D[0], MA_D[1], MA_D[2], MA_END, MA_END, MA_D[2], MA_D[1], MA_D[0]], y: [mid[0], top[1], top[2], top[2], low[2], low[2], low[1], low[0]],
    type: "scatter", mode: "lines", fill: "toself", fillcolor: cols[0], opacity: 0.18, line: { width: 0 }, hoverinfo: "skip" }, path(2, "dash"), path(1, "dot"), path(0, "dash")];
  const ks = K ? K.x.findIndex((d) => d >= "2026-01-01") : -1;
  if (ks >= 0) tr.push({ x: K.x.slice(ks), y: K.y.slice(ks), type: "scatter", mode: "lines", line: { color: ink, width: 1.6 }, hovertemplate: "%{x|%Y-%m-%d}<br>코스피 %{y:,.2f}<extra></extra>" });
  Plotly.react("vm-mapath", tr, Object.assign(baseLayout(), {
    margin: { l: 48, r: 8, t: 8, b: 28 }, showlegend: false, hovermode: "closest",
    xaxis: axis({ type: "date", range: ["2026-01-01", "2028-01-10"], tickformat: "%y.%m", dtick: "M6", hoverformat: "%Y-%m-%d", showgrid: false }),
    yaxis: axis({ nticks: 7, tickformat: "," }),
    shapes: K ? [{ type: "line", xref: "x", x0: K.last_date, x1: K.last_date, yref: "paper", y0: 0, y1: 1, line: { color: C.muted, width: 1, dash: "dot" } }] : [],
  }), CFG);
}

/* 주식 4계 카드의 '우리 데이터로 대 보기': 미국·한국을 따로, 각자의 금리·실적·주가 방향만 표의 줄과 맞춰 봄 (README) */
const SEASON_ROW = { "↓↘↗": "① 금융장세(봄)", "↑↗↗": "③ 실적장세(여름)", "↑↗↓": "④ 역금융장세(가을)", "↓↘↓": "⑥ 역실적장세(겨울)" };
const SEASON_PT = { "①": [61, 99], "③": [158, 52], "④": [221, 75], "⑥": [307, 123] };  // 산 모양 그림의 선 위 자리
function seasonsNow() {
  const el = $("#vm-seasons-now"), D = typeof DATA !== "undefined" ? DATA : null;
  if (!el || !D) return;
  const F = D.fedwatch, P = D.phase, Md = D.mdd, K = D.policy?.kr, k3 = M.rates.kr3y, g = M.market.kospi_forward?.years?.growth || {};
  const gy = Object.keys(g).find((y) => g[y] != null);
  if (!F || !P?.us?.dd || !P.us.eps_first || !Md || !K || !k3 || !gy) { el.innerHTML = ""; return; }
  const rA = (h) => (h >= 0.5 ? "↑" : h <= -0.5 ? "↓" : "→"), eA = (v) => (v > 5 ? "↗" : v < -5 ? "↘" : "→"), pA = (dd) => (dd <= -10 ? "↓" : "↗");
  const hk = (h) => `0.25%p ${h >= 0 ? "인상" : "인하"} 약 ${n(Math.abs(h), 1)}번만큼`, md = (s) => s.slice(2).replace(/-/g, ".");
  // 한국은 FedWatch 같은 무료 자료가 없어 국고채 3년 − 기준금리 차이로 시장 예상을 대신 봄
  const usH = (F.table[F.table.length - 1].exp - F.upper) / 0.25, krH = (k3.latest - K.latest) / 0.25;
  const usE = (P.us.eps_last / P.us.eps_first.value - 1) * 100, krE = g[gy];
  const mk = [
    { name: "🇺🇸 미국", short: "미국", col: "var(--ink)", a: [rA(usH), eA(usE), pA(P.us.dd.pct)], why: [
      `1년 안에 기준금리 ${hk(usH)} 예상(연방기금 선물 가격으로 CME FedWatch 방식 계산)`,
      `S&P500 12개월 예상 이익 1년 새 ${pct(usE, 1, true)}(${md(P.us.eps_first.date)} $${n(P.us.eps_first.value, 1)} → $${n(P.us.eps_last, 1)})`,
      `S&P500 최고점(${md(P.us.dd.peak_date)} ${n(P.us.dd.peak, 0)}) 대비 ${pct(P.us.dd.pct, 1)}`] },
    { name: "🇰🇷 한국", short: "한국", col: "var(--cds)", a: [rA(krH), eA(krE), pA(Md.last)], why: [
      `국고채 3년 ${n(k3.latest, 2)}% − 기준금리 ${n(K.latest, 2)}% = ${pct(k3.latest - K.latest, 2, true).replace("%", "%p")} → ${hk(krH)} 예상`,
      `코스피 ${esc(gy)} 이익 증가율 ${pct(krE, 1, true)}(아래 연도별 표)`,
      `코스피 최고점(${md(Md.peak_date)} ${n(Md.peak, 0)}) 대비 ${pct(Md.last, 1)}(한국 탭 MDD)`] },
  ].map((m) => ({ ...m, row: SEASON_ROW[m.a.join("")] || null }));
  const [us, kr] = mk, say = (m) => (m.row ? `<b>${m.row}</b>` : "표의 어느 줄과도 딱 맞지 않는 모양");
  const diff = ["금리", "실적", "주가"].filter((_, i) => us.a[i] !== kr.a[i]);
  const verdict = us.row && us.row === kr.row ? `<b>미국과 한국이 같아요</b> — 둘 다 ${say(us)}이에요.`
    : `<b>미국과 한국이 달라요</b> — 미국은 ${say(us)}, 한국은 ${say(kr)}이에요.${diff.length ? ` 갈리는 곳: ${diff.map((k) => { const i = ["금리", "실적", "주가"].indexOf(k); return `${k}(미국 ${us.a[i]} · 한국 ${kr.a[i]})`; }).join(", ")}` : ""}`;
  el.innerHTML = `${mk.map((m) => `<div style="margin-top:6px"><b>${m.name}</b> → ${say(m)}${["금리", "실적", "주가"].map((k, i) => `<br>· ${k} <b>${m.a[i]}</b> — ${m.why[i]}`).join("")}</div>`).join("")}
    <div style="margin-top:6px">→ ${verdict} <span class="src">세 숫자의 방향만 단순히 대 본 거라 참고용이에요. DB증권·블로그 글쓴이는 ③→④ 넘어가는 중으로 봤어요.</span></div>${spreadLine(D)}`;
  // 그림 위에 미국·한국 점 (같은 국면이면 점 하나에 '미국·한국')
  const svg = $("#vm-seasons-svg"), at = {};
  if (!svg) return;
  mk.filter((m) => m.row).forEach((m) => (at[m.row[0]] = [...(at[m.row[0]] || []), m]));
  svg.querySelector(".auto-dots")?.remove();
  svg.insertAdjacentHTML("beforeend", `<g class="auto-dots">${Object.entries(at).map(([k, ms]) => {
    const [x, y] = SEASON_PT[k], col = ms.length > 1 ? "var(--bad)" : ms[0].col, left = x > 290;
    return `<circle cx="${x}" cy="${y}" r="6" style="fill:${col};stroke:var(--surface);stroke-width:2"/><text x="${left ? x - 9 : x + 9}" y="${y + 4}" text-anchor="${left ? "end" : "start"}" style="font-size:10px;font-weight:800;fill:${col}">${ms.map((m) => m.short).join("·")}</text>`;
  }).join("")}</g>`);
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
    <div class="memo">📌 <b>PBR 바닥 메모</b> — 예전엔 어디까지 싸졌나<br><span class="src">PBR 1배 = 회사들이 가진 재산만큼의 값. 1배보다 낮으면 재산보다 싸게 팔린다는 뜻이에요.</span>
      <br>· 지금 코스피: <b>내년 기준 PBR 약 1배</b> <span class="src">(이 화면 ${ys[1]} PBR ${times(Y.pbr[ys[1]], 2)})</span>
      ${[["2020년 코로나 때", "0.6", "2020-03"], ["2024년 12월 계엄 때", "0.8", "2024-12"]].map(([nm, v, p]) => { const lo = (t.pbr_hist || []).filter((x) => x[0].startsWith(p)).reduce((a, x) => (!a || x[1] < a[1] ? x : a), null);
        return `<br>· ${nm}: <b>PBR ${v}배</b>${lo ? ` <span class="src">(KRX 지난 실적 PBR ${esc(lo[0])} ${times(lo[1], 2)})</span>` : ""}`; }).join("")}
      <div class="src">메모 출처: user: 사용자 메모(26.11.09) · 괄호 = 이 화면 자료로 확인한 값</div></div>
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
    DRAWS.forEach((d) => d());
  };
  DRAWS = [];
  seasonsNow();
  miraePath();
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
