// Stats tab: charts derived entirely from the game's event log. Uses h() from app.js.
// Colors come from CSS tokens (--series-N, --seq-N, chart chrome) so light/dark swap in CSS.
// Every model-written string goes through textContent.
"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const MAX_SERIES = 8; // categorical slots; more players than this fold into small text only

function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) el.setAttribute(k, v);
  for (const c of children.flat()) if (c !== null && c !== undefined) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

const seriesVar = (i) => `var(--series-${(i % MAX_SERIES) + 1})`;

// ---------------------------------------------------------------- data
// Both rulesets phrase the goal "the first player to reach N points"; follow amendments to it.
const WIN_RE = /first player to reach (\d+)/i;

function deriveStats(state, events) {
  const players = state.players.map((p) => p.name);
  const score = Object.fromEntries(players.map((n) => [n, 0]));
  let target = state.win_points;
  const timeline = [{ turn: 0, scores: { ...score }, target }];
  const proposals = []; // {turn, number, player, op, target, text, summary, outcome, yes, no}
  const votes = []; // {proposer, voter, vote}
  // A turn's proposal and votes only count once the turn ends (aborted turns are dropped).
  let turn = null;
  let pending = null;
  let pendingVotes = [];
  for (const ev of events) {
    const d = ev.data;
    if (ev.type === "turn_started") {
      turn = { turn: d.turn, player: d.player };
      pending = null;
      pendingVotes = [];
    } else if (ev.type === "score_changed" && d.player in score) score[d.player] += d.delta;
    else if (ev.type === "rule_changed" && d.text && WIN_RE.test(d.text)) target = Number(d.text.match(WIN_RE)[1]);
    else if (ev.type === "proposal") {
      pending = { turn: turn && turn.turn, number: d.number, player: d.player, op: d.op, target: d.target_rule,
        text: d.text, summary: d.summary, outcome: "pending", yes: 0, no: 0 };
    } else if (ev.type === "vote" && turn) {
      if (pending) { if (d.vote === "yes") pending.yes++; else pending.no++; }
      if (!d.auto && d.player !== turn.player) pendingVotes.push({ proposer: turn.player, voter: d.player, vote: d.vote });
    } else if (ev.type === "ruling" && pending) {
      pending.outcome = d.passed ? "passed" : (d.legal ? "failed" : "illegal");
    } else if (ev.type === "turn_ended") {
      if (pending && pending.outcome !== "pending") proposals.push(pending);
      votes.push(...pendingVotes);
      pending = null;
      pendingVotes = [];
      timeline.push({ turn: d.turn, scores: { ...score }, target });
    }
  }
  // human score edits after the last turn still show at the end
  const last = timeline[timeline.length - 1];
  if (players.some((n) => last.scores[n] !== score[n])) timeline.push({ turn: last.turn, scores: { ...score }, target, edit: true });
  return { players, timeline, proposals, votes, target };
}

// ---------------------------------------------------------------- tooltip
function makeTooltip(host) {
  const tip = h("div", { class: "viz-tip", role: "status" });
  tip.style.display = "none";
  host.append(tip);
  return {
    show(x, y, rows, title) {
      tip.replaceChildren(
        title ? h("div", { class: "viz-tip-title" }, title) : null,
        ...rows.map((r) => h("div", { class: "viz-tip-row" },
          r.key ? h("span", { class: "viz-key", style: `background:${r.key}` }) : null,
          h("strong", {}, r.value), r.label ? h("span", { class: "muted" }, " ", r.label) : null)),
      );
      tip.style.display = "block";
      const hostBox = host.getBoundingClientRect();
      const w = tip.offsetWidth;
      const left = Math.min(Math.max(8, x + 14), hostBox.width - w - 8);
      tip.style.left = `${left}px`;
      tip.style.top = `${Math.max(4, y - 10)}px`;
    },
    hide() { tip.style.display = "none"; },
  };
}

function legend(names) {
  return h("div", { class: "viz-legend" }, names.map((n, i) =>
    h("span", { class: "viz-legend-item" }, h("span", { class: "viz-line-key", style: `background:${seriesVar(i)}` }), n)));
}

function niceTicks(min, max, count = 5) {
  const span = max - min || 1;
  const step0 = span / count;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((m) => span / m <= count) || 10 * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + 1e-9; v += step) ticks.push(Math.round(v * 100) / 100);
  return ticks;
}

// ---------------------------------------------------------------- points over time
function pointsChart(stats) {
  const { players, timeline } = stats;
  const W = 720, H = 300, M = { l: 40, r: 110, t: 16, b: 30 };
  const iw = W - M.l - M.r, ih = H - M.t - M.b;
  const allScores = timeline.flatMap((t) => players.map((n) => t.scores[n]));
  const targets = timeline.map((t) => t.target);
  const ticks = niceTicks(Math.min(0, ...allScores), Math.max(...targets, ...allScores));
  const yMin = ticks[0], yMax = ticks[ticks.length - 1];
  const n = timeline.length;
  const x = (i) => M.l + (n === 1 ? iw / 2 : (i / (n - 1)) * iw);
  const y = (v) => M.t + ih - ((v - yMin) / (yMax - yMin)) * ih;

  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "viz-svg", role: "img", tabindex: "0",
    "aria-label": `Points after each turn for ${players.join(", ")}. Use left and right arrow keys to step through turns.` });
  // grid + y ticks
  for (const t of ticks) {
    svg.append(s("line", { x1: M.l, x2: M.l + iw, y1: y(t), y2: y(t), class: t === 0 ? "viz-baseline" : "viz-grid" }));
    svg.append(s("text", { x: M.l - 8, y: y(t) + 4, class: "viz-tick", "text-anchor": "end" }, t));
  }
  // win target reference, stepped where the rules changed it
  const tpath = timeline.map((t, i) => `${i ? `H${x(i)}V` : `M${x(0)},`}${y(t.target)}`).join("");
  svg.append(s("path", { d: tpath, class: "viz-ref", fill: "none" }));
  timeline.forEach((t, i) => {
    if (i === 0 || t.target !== timeline[i - 1].target) {
      svg.append(s("text", { x: x(i) + 4, y: y(t.target) - 5, class: "viz-tick" }, `win: ${t.target}`));
    }
  });
  // x ticks: every turn if few, else ~8 labels
  const every = Math.max(1, Math.ceil(n / 9));
  timeline.forEach((t, i) => {
    if (i % every === 0 || i === n - 1) svg.append(s("text", { x: x(i), y: H - 8, class: "viz-tick", "text-anchor": "middle" }, t.turn));
  });
  svg.append(s("text", { x: M.l + iw, y: H - 8, class: "viz-tick", "text-anchor": "end", dy: "-14" }, "turn"));

  // lines, end dots, end labels (skip a label that would collide; legend + tooltip cover it)
  const ends = [];
  players.forEach((p, i) => {
    const pts = timeline.map((t, j) => `${x(j)},${y(t.scores[p])}`).join(" ");
    svg.append(s("polyline", { points: pts, fill: "none", stroke: seriesVar(i), "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round" }));
    ends.push({ p, i, v: timeline[n - 1].scores[p] });
  });
  ends.sort((a, b) => y(a.v) - y(b.v));
  let lastY = -Infinity;
  for (const e of ends) {
    const ey = y(e.v);
    svg.append(s("circle", { cx: x(n - 1), cy: ey, r: 4, fill: seriesVar(e.i), class: "viz-dot" }));
    if (players.length <= 4 && ey - lastY >= 13) {
      svg.append(s("text", { x: x(n - 1) + 10, y: ey + 4, class: "viz-label" }, `${e.p} ${e.v}`));
      lastY = ey;
    }
  }

  // crosshair + tooltip
  const cross = s("line", { y1: M.t, y2: M.t + ih, class: "viz-cross", visibility: "hidden" });
  const hover = s("g", {});
  svg.append(cross, hover);
  const overlay = s("rect", { x: M.l, y: M.t, width: iw, height: ih, fill: "transparent" });
  svg.append(overlay);

  const wrap = h("div", { class: "viz-plot" }, svg);
  const tip = makeTooltip(wrap);
  let idx = n - 1;
  const showAt = (i) => {
    idx = Math.max(0, Math.min(n - 1, i));
    const t = timeline[idx];
    cross.setAttribute("x1", x(idx)); cross.setAttribute("x2", x(idx));
    cross.setAttribute("visibility", "visible");
    hover.replaceChildren(...players.map((p, k) => s("circle", { cx: x(idx), cy: y(t.scores[p]), r: 4, fill: seriesVar(k), class: "viz-dot" })));
    const scale = svg.getBoundingClientRect().width / W;
    const rows = players.map((p, k) => ({ p, k, v: t.scores[p] })).sort((a, b) => b.v - a.v)
      .map((r) => ({ key: seriesVar(r.k), value: r.v, label: r.p }));
    tip.show(x(idx) * scale, M.t * scale, rows, t.edit ? "After human edit" : (t.turn === 0 ? "Start" : `After turn ${t.turn}`));
  };
  const hide = () => { cross.setAttribute("visibility", "hidden"); hover.replaceChildren(); tip.hide(); };
  overlay.addEventListener("pointermove", (e) => {
    const box = svg.getBoundingClientRect();
    const px = ((e.clientX - box.left) / box.width) * W;
    showAt(n === 1 ? 0 : Math.round(((px - M.l) / iw) * (n - 1)));
  });
  overlay.addEventListener("pointerleave", hide);
  svg.addEventListener("focus", () => showAt(idx));
  svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") { showAt(idx - 1); e.preventDefault(); }
    if (e.key === "ArrowRight") { showAt(idx + 1); e.preventDefault(); }
  });
  return wrap;
}

// ---------------------------------------------------------------- proposals per player
const OUTCOMES = [["passed", "Passed", 1], ["failed", "Failed", 2], ["illegal", "Illegal", 3]];

function proposalsChart(stats) {
  const counts = stats.players.map((p) => {
    const mine = stats.proposals.filter((x) => x.player === p);
    return { p, total: mine.length, ...Object.fromEntries(OUTCOMES.map(([k]) => [k, mine.filter((x) => x.outcome === k).length])) };
  });
  const max = Math.max(1, ...counts.map((c) => c.total));
  const wrap = h("div", { class: "viz-plot" });
  const tip = makeTooltip(wrap);
  const used = OUTCOMES.filter(([k]) => counts.some((c) => c[k] > 0));
  const rows = counts.map((c) => {
    const segs = OUTCOMES.filter(([k]) => c[k] > 0).map(([k, label, slot]) => {
      const seg = h("div", { class: "viz-seg", tabindex: "0", style: `flex:${c[k]} 0 0;background:var(--series-${slot})`,
        "aria-label": `${c.p}: ${c[k]} ${label.toLowerCase()}` });
      const show = () => {
        const b = seg.getBoundingClientRect(), hb = wrap.getBoundingClientRect();
        seg.classList.add("lift");
        tip.show(b.left - hb.left + b.width / 2, b.top - hb.top - 28, [{ key: `var(--series-${slot})`, value: c[k], label: `${label.toLowerCase()} of ${c.total}` }], c.p);
      };
      const hide = () => { seg.classList.remove("lift"); tip.hide(); };
      seg.addEventListener("pointerenter", show); seg.addEventListener("pointerleave", hide);
      seg.addEventListener("focus", show); seg.addEventListener("blur", hide);
      return seg;
    });
    const pct = c.total ? Math.round((100 * c.passed) / c.total) : 0;
    return h("div", { class: "viz-bar-row" },
      h("div", { class: "viz-bar-name" }, c.p),
      h("div", { class: "viz-bar-track" },
        h("div", { class: "viz-bar", style: `width:${(100 * c.total) / max}%` }, segs),
        h("span", { class: "viz-bar-value" }, c.total ? `${c.passed}/${c.total} passed (${pct}%)` : "no proposals")));
  });
  wrap.append(
    used.length > 1 ? h("div", { class: "viz-legend" }, used.map(([, label, slot]) =>
      h("span", { class: "viz-legend-item" }, h("span", { class: "viz-rect-key", style: `background:var(--series-${slot})` }), label))) : null,
    ...rows);
  return wrap;
}

// ---------------------------------------------------------------- voting heatmap
const SEQ_STEPS = 7; // --seq-1 (lightest) .. --seq-7 (darkest)

function votesChart(stats) {
  const { players, votes } = stats;
  const wrap = h("div", { class: "viz-plot" });
  const tip = makeTooltip(wrap);
  const head = h("tr", {}, h("th", { class: "muted small" }, "voter ↓ / proposer →"), players.map((p) => h("th", { class: "small" }, p)));
  const body = players.map((voter) => h("tr", {}, h("th", { class: "small" }, voter), players.map((proposer) => {
    if (voter === proposer) return h("td", { class: "viz-cell viz-cell-self" }, "—");
    const vs = votes.filter((v) => v.voter === voter && v.proposer === proposer);
    if (!vs.length) return h("td", { class: "viz-cell viz-cell-empty" }, "·");
    const yes = vs.filter((v) => v.vote === "yes").length;
    const rate = yes / vs.length;
    const step = 1 + Math.round(rate * (SEQ_STEPS - 1));
    const cell = h("td", { class: `viz-cell ${step >= 5 ? "on-dark" : ""}`, tabindex: "0",
      style: `background:var(--seq-${step})`, "aria-label": `${voter} voted yes on ${yes} of ${vs.length} of ${proposer}'s proposals` },
    `${Math.round(rate * 100)}%`);
    const show = () => {
      const b = cell.getBoundingClientRect(), hb = wrap.getBoundingClientRect();
      tip.show(b.left - hb.left + b.width / 2, b.top - hb.top - 30, [{ value: `${yes} of ${vs.length}`, label: "voted yes" }], `${voter} on ${proposer}'s proposals`);
    };
    cell.addEventListener("pointerenter", show); cell.addEventListener("pointerleave", tip.hide);
    cell.addEventListener("focus", show); cell.addEventListener("blur", tip.hide);
    return cell;
  })));
  wrap.append(
    h("table", { class: "viz-heat" }, head, ...body),
    h("div", { class: "viz-ramp-legend small muted" }, "0% yes",
      h("span", { class: "viz-ramp" }, Array.from({ length: SEQ_STEPS }, (_, i) => h("span", { style: `background:var(--seq-${i + 1})` }))),
      "100% yes"));
  return wrap;
}

// ---------------------------------------------------------------- adopted proposals table
function adoptedTable(stats) {
  const adopted = stats.proposals.filter((p) => p.outcome === "passed");
  if (!adopted.length) return h("div", { class: "muted" }, "No proposals adopted yet.");
  return h("table", { class: "viz-table" },
    h("tr", {}, ["Turn", "By", "Change", "Votes", "Text"].map((t) => h("th", {}, t))),
    adopted.map((p) => h("tr", {},
      h("td", { class: "num" }, p.turn), h("td", {}, p.player),
      h("td", {}, `${p.op}${p.target !== null && p.target !== undefined ? " " + p.target : " → " + p.number}`),
      h("td", { class: "num" }, `${p.yes}–${p.no}`),
      h("td", { class: "small" }, p.text || p.summary))));
}

function scoresTable(stats) {
  const { players, timeline } = stats;
  return h("details", { class: "viz-data" }, h("summary", {}, "Points as a table"),
    h("table", { class: "viz-table" },
      h("tr", {}, h("th", {}, "Turn"), players.map((p) => h("th", {}, p))),
      timeline.map((t) => h("tr", {}, h("td", { class: "num" }, t.edit ? "edit" : t.turn), players.map((p) => h("td", { class: "num" }, t.scores[p]))))));
}

// ---------------------------------------------------------------- tab
function renderStats(container, state, events) {
  const stats = deriveStats(state, events);
  const done = stats.timeline.length > 1;
  const adopted = stats.proposals.filter((p) => p.outcome === "passed").length;
  const leaderScore = Math.max(...state.players.map((p) => p.score));
  const leaders = state.players.filter((p) => p.score === leaderScore).map((p) => p.name);
  const tile = (label, value, sub) => h("div", { class: "viz-tile" }, h("div", { class: "muted small" }, label), h("div", { class: "viz-tile-value" }, value), sub ? h("div", { class: "muted small" }, sub) : null);
  const card = (title, sub, body) => h("section", { class: "panel viz-card" }, h("h2", {}, title), sub ? h("div", { class: "muted small viz-sub" }, sub) : null, body);
  fill(container,
    h("div", { class: "viz-tiles" },
      tile(state.winner ? "Winner" : "Leading", state.winner || leaders.join(", "),
        `${leaderScore} points · target ${stats.target}` + (stats.target !== state.win_points ? ` (was ${state.win_points})` : "")),
      tile("Turns played", state.completed_turns, `round ${state.round}`),
      tile("Proposals adopted", `${adopted} of ${stats.proposals.length}`, stats.proposals.length ? `${Math.round((100 * adopted) / stats.proposals.length)}% pass rate` : null),
      tile("Spend", usd(state.total_cost), state.budget_usd > 0 ? `of ${usd(state.budget_usd)} budget` : null)),
    !done ? h("div", { class: "panel muted" }, "No completed turns yet. Charts appear after the first turn.") : [
      card("Points over time", "Score after each completed turn. Hover or use arrow keys for exact values.",
        h("div", {}, stats.players.length > 1 ? legend(stats.players) : null, pointsChart(stats), scoresTable(stats))),
      h("div", { class: "viz-row" },
        card("Proposals by player", "Outcome of each player's proposals.", proposalsChart(stats)),
        card("Who backs whom", "Share of each proposer's proposals that a voter supported (automatic self-votes excluded).", votesChart(stats))),
      card(`Adopted proposals (${adopted})`, null, adoptedTable(stats)),
      Object.keys(state.reflections || {}).length
        ? card("Post-game interviews", "Each AI player looking back on its own game.",
          h("div", { class: "reflect-grid" }, state.players.filter((p) => state.reflections[p.name])
            .map((p) => h("div", { class: "ev reflection" }, reflectionView(state.reflections[p.name])))))
        : null,
    ]);
}
