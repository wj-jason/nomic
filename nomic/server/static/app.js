// Nomic Arena UI: plain JS, no build step. Every model-written string goes through
// textContent (via h()), never innerHTML.
"use strict";

// ---------------------------------------------------------------- helpers
const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// A player's post-game interview (used in the feed and the stats tab).
function reflectionView(d) {
  const part = (label, text) => h("div", { class: "reflect-part" }, h("div", { class: "reflect-label" }, label), h("div", {}, text));
  return h("div", { class: "stack" },
    h("strong", {}, `🗣 ${d.player} looks back`),
    part("How it went", d.overview),
    part("Key decisions", d.key_decisions),
    part("Would change", d.would_change),
    part("On the others", d.about_others));
}

// replaceChildren, skipping null/false children (which the DOM would print as text)
function fill(el, ...children) {
  el.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
    throw new Error(msg);
  }
  return data;
}

const usd = (x) => "$" + (x || 0).toFixed(x >= 1 ? 2 : 4);
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

function flash(msg) {
  alert(msg);
}

function showModal(title, body) {
  $("#modal-title").textContent = title;
  const mb = $("#modal-body");
  mb.replaceChildren(body);
  $("#modal").showModal();
}

async function showCall(callId) {
  try {
    const c = await api("GET", `/api/calls/${callId}`);
    showModal(`${c.seat} · ${c.purpose} · ${c.model}`, h("div", { class: "stack" },
      h("div", { class: "muted small" },
        `${c.prompt_tokens} prompt tokens (${c.cached_tokens} cached), ${c.completion_tokens} completion ` +
        `(${c.reasoning_tokens} reasoning) · ${usd(c.cost)} · ${c.latency_ms} ms` +
        (c.error ? ` · ${c.error}` : "")),
      ...c.request.map((m) => h("div", {}, h("h3", {}, m.role), h("pre", {}, m.content))),
      h("h3", {}, "response"), h("pre", {}, c.response),
    ));
  } catch (e) { flash(e.message); }
}

// ---------------------------------------------------------------- router
let current = null; // teardown for the active view

function route() {
  if (current) current();
  current = null;
  $("#nav-extra").replaceChildren();
  const m = location.hash.match(/^#\/game\/(\w+)/);
  if (m) current = renderGame(m[1]);
  else current = renderHome();
}
window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", route);

// ---------------------------------------------------------------- home
const PRESETS = {
  "Recommended (~$3)": {
    seats: ["google/gemini-3.7-flash", "moonshotai/kimi-k2.6", "deepseek/deepseek-v4-pro",
      "anthropic/claude-sonnet-5.5"],
    judge: "deepseek/deepseek-v4-pro",
  },
  "Budget (<$1)": {
    seats: ["anthropic/claude-haiku-5.5", "openai/gpt-6-luna", "deepseek/deepseek-v4-flash"],
    judge: "deepseek/deepseek-v4-flash",
  },
  "Mock (free, offline)": {
    seats: ["mock/slow", "mock/slow", "mock/slow"],
    judge: "mock/judge",
  },
};

// Seat names come from the model: "deepseek/deepseek-v4-flash" -> "deepseek". Different models
// of one family get their descriptors ("deepseek-flash" / "deepseek-pro", "claude-sonnet" /
// "claude-haiku") or, lacking any, their slug ("gpt-5.5"). The same model twice is numbered
// ("deepseek-1" / "deepseek-2").
const VERSION_TOKEN = /^(v|k)?\d[\d.]*[a-z]?$/i;

function nameCandidates(model) {
  const [vendor, rest] = model.includes("/") ? model.split("/", 2) : ["", model];
  if (vendor === "mock") return ["mock", "mock", "mock"];
  const slug = rest.split(":")[0].toLowerCase();
  const tokens = slug.split(/[-_]/).filter(Boolean);
  const family = tokens[0].replace(/[\d.]+.*$/, "") || tokens[0];
  const descriptors = tokens.slice(1).filter((t) => !VERSION_TOKEN.test(t));
  return [family, descriptors.length ? [family, ...descriptors].join("-") : slug, slug];
}

function seatNames(models) {
  // name each distinct model, escalating only where different models clash
  const ids = [...new Set(models.filter(Boolean))];
  const parts = ids.map(nameCandidates);
  const names = parts.map((p) => p[0]);
  for (let level = 1; level < 3; level++) {
    const clash = names.map((n) => names.filter((x) => x === n).length > 1);
    names.forEach((_, i) => { if (clash[i]) names[i] = parts[i][level]; });
  }
  const byId = Object.fromEntries(ids.map((id, i) => [id, names[i]]));
  // then number repeats of the same model
  const total = {};
  const seen = {};
  models.forEach((m) => { if (m) total[m] = (total[m] || 0) + 1; });
  return models.map((m) => {
    if (!m) return null;
    if (total[m] === 1) return byId[m];
    seen[m] = (seen[m] || 0) + 1;
    return `${byId[m]}-${seen[m]}`;
  });
}

function renderHome() {
  const app = $("#app");
  const gamesBox = h("div", { class: "panel" }, h("h2", {}, "Games"), h("div", { class: "muted" }, "Loading…"));
  const formBox = h("div", { class: "panel" });
  app.replaceChildren(h("div", { class: "home" }, gamesBox, formBox));

  const loadGames = async () => {
    const games = await api("GET", "/api/games");
    const rows = games.map((g) => h("tr", {},
      h("td", {}, h("a", { href: `#/game/${g.id}` }, g.id), g.parent_id ? h("div", { class: "muted small" }, `fork of ${g.parent_id}`) : null),
      h("td", {}, h("span", { class: `pill ${g.status}` }, g.status), g.pending ? h("div", { class: "small" }, "⏳ needs you") : null),
      h("td", {}, g.ruleset),
      h("td", { class: "small" }, g.players.map((p) => h("div", { class: p.name === g.winner ? "leader" : "" },
        `${p.name} ${p.score}`, h("span", { class: "muted" }, ` · ${p.model || "human"}`)))),
      h("td", {}, g.turns),
      h("td", {}, usd(g.cost)),
    ));
    gamesBox.replaceChildren(h("h2", {}, "Games"), games.length
      ? h("table", {}, h("tr", {}, ...["Game", "Status", "Rules", "Players", "Turns", "Cost"].map((t) => h("th", {}, t))), ...rows)
      : h("div", { class: "muted" }, "No games yet. Create one →"));
  };
  loadGames().catch((e) => gamesBox.append(h("div", { class: "error" }, e.message)));
  const timer = setInterval(() => loadGames().catch(() => {}), 5000);

  buildForm(formBox);
  return () => clearInterval(timer);
}

async function buildForm(box) {
  const [models, rulesets] = await Promise.all([
    api("GET", "/api/models").catch(() => []), api("GET", "/api/rulesets"),
  ]);
  const dl = h("datalist", { id: "models" },
    h("option", { value: "mock/slow" }, "offline scripted player (free)"),
    h("option", { value: "mock/judge" }, "offline mechanical judge (free)"),
    ...models.map((m) => h("option", { value: m.id }, `$${m.prompt.toFixed(2)} / $${m.completion.toFixed(2)} per M`)));

  const seatsBox = h("div", { class: "stack" });
  // Names follow the model until you type your own name into a seat.
  const renameSeats = () => {
    const rows = [...seatsBox.children];
    const names = seatNames(rows.map((r) => r.modelValue()));
    rows.forEach((r, i) => { if (r.autoName && names[i]) r.nameIn.value = names[i]; });
  };
  const addSeat = (model = "") => {
    const human = h("input", { type: "checkbox", title: "Human seat (you play)" });
    const modelIn = h("input", { list: "models", placeholder: "openrouter model id", value: model });
    // placeholder name until a model is picked
    const nameIn = h("input", { placeholder: "name", class: "seat-name", value: `p${seatsBox.children.length + 1}` });
    const row = h("div", { class: "seat-row" }, nameIn, modelIn,
      h("label", { class: "small row" }, human, "human"),
      h("button", { class: "btn tiny", type: "button", onclick: () => { row.remove(); renameSeats(); update(); } }, "✕"));
    row.nameIn = nameIn;
    row.autoName = true;
    row.modelValue = () => (human.checked ? null : modelIn.value.trim());
    row.read = () => ({ name: nameIn.value.trim(), model: row.modelValue() });
    nameIn.addEventListener("input", () => { row.autoName = nameIn.value.trim() === ""; if (row.autoName) renameSeats(); });
    modelIn.addEventListener("input", renameSeats);
    human.addEventListener("change", () => {
      modelIn.disabled = human.checked;
      if (human.checked && row.autoName) { nameIn.value = "you"; row.autoName = false; }
      renameSeats(); update();
    });
    seatsBox.append(row);
    renameSeats();
  };

  const f = {
    ruleset: h("select", {}, ...rulesets.map((r) => h("option", { value: r.id }, `${r.name} (${r.rules} rules, ${r.win_points} pts)`))),
    win: h("input", { type: "number", min: 1, placeholder: "default" }),
    judgeMode: h("select", {},
      h("option", { value: "llm" }, "LLM judge"),
      h("option", { value: "llm_with_human_override" }, "LLM drafts, I approve each ruling"),
      h("option", { value: "human_on_appeal" }, "LLM, but disputes come to me"),
      h("option", { value: "human" }, "I am the judge")),
    judgeModel: h("input", { list: "models", placeholder: "judge model" }),
    review: h("input", { type: "number", min: 0, placeholder: "∞", title: "Seconds before the LLM draft stands" }),
    judgeEffort: h("select", { title: "Hidden reasoning for the judge (it does the score bookkeeping)" },
      ...["none", "minimal", "low", "medium", "high"].map((e) => h("option", { value: e, selected: e === "low" }, e))),
    budget: h("input", { type: "number", min: 0, step: "0.5", value: 5 }),
    maxTurns: h("input", { type: "number", min: 1, value: 150 }),
    discussion: h("input", { type: "checkbox", checked: true }),
    reflection: h("input", { type: "checkbox", checked: true }),
    effort: h("select", {}, ...["none", "minimal", "low", "medium", "high"].map((e) => h("option", { value: e, selected: e === "none" }, e))),
    seed: h("input", { type: "number", placeholder: "random" }),
  };
  const estBox = h("div", { class: "estimate small" }, "…");
  const errBox = h("div", { class: "error small" });

  const read = () => {
    const cfg = {
      ruleset: f.ruleset.value,
      seats: [...seatsBox.children].map((r) => r.read()),
      judge: { mode: f.judgeMode.value, model: f.judgeModel.value.trim() || null,
               review_seconds: f.review.value === "" ? null : Number(f.review.value),
               reasoning_effort: f.judgeEffort.value },
      budget_usd: Number(f.budget.value || 0),
      max_turns: Number(f.maxTurns.value || 150),
      discussion: f.discussion.checked,
      reflection: f.reflection.checked,
      reasoning_effort: f.effort.value,
    };
    if (f.win.value) cfg.win_points = Number(f.win.value);
    if (f.seed.value) cfg.seed = Number(f.seed.value);
    return cfg;
  };

  const update = debounce(async () => {
    try {
      const est = await api("POST", "/api/estimate", read());
      estBox.replaceChildren(h("div", {},
        h("strong", {}, `≈ ${usd(est.total)} per game`), ` (range ${usd(est.low)}–${usd(est.high)}) · ~${est.rounds} rounds, ${est.turns} turns to ${est.win_points} pts`,
        h("div", { class: "muted" }, Object.entries(est.seats).map(([n, c]) => `${n} ${usd(c)}`).join(" · ") + ` · judge ${usd(est.judge)}`),
        est.unknown_models.length ? h("div", { class: "error" }, `Unknown model ids: ${est.unknown_models.join(", ")}`) : null,
      ));
      errBox.textContent = "";
    } catch (e) { estBox.textContent = "Estimate unavailable: " + e.message; }
  }, 300);

  const applyPreset = (name) => {
    const p = PRESETS[name];
    seatsBox.replaceChildren();
    p.seats.forEach((m) => addSeat(m));
    f.judgeModel.value = p.judge;
    update();
  };

  box.replaceChildren(
    dl,
    h("h2", {}, "New game"),
    h("div", { class: "row small" }, "Presets:", ...Object.keys(PRESETS).map((n) => h("button", { class: "btn tiny", type: "button", onclick: () => applyPreset(n) }, n))),
    h("h3", {}, "Seats"),
    seatsBox,
    h("button", { class: "btn tiny", type: "button", onclick: () => { addSeat(""); update(); } }, "+ seat"),
    h("h3", {}, "Rules & judge"),
    h("div", { class: "row" },
      h("label", { class: "field" }, "Ruleset", f.ruleset),
      h("label", { class: "field" }, "Win points", f.win),
      h("label", { class: "field" }, "Judge", f.judgeMode)),
    h("div", { class: "row", style: "margin-top:8px" },
      h("label", { class: "field grow" }, "Judge model", f.judgeModel),
      h("label", { class: "field" }, "Judge reasoning", f.judgeEffort),
      h("label", { class: "field" }, "Review timeout (s)", f.review)),
    h("h3", {}, "Limits"),
    h("div", { class: "row" },
      h("label", { class: "field" }, "Budget $", f.budget),
      h("label", { class: "field" }, "Turn cap", f.maxTurns),
      h("label", { class: "field" }, "Player reasoning", f.effort),
      h("label", { class: "field" }, "Seed", f.seed),
      h("label", { class: "small row" }, f.discussion, "discussion phase"),
      h("label", { class: "small row", title: "When the game ends, each AI player reviews its decisions" }, f.reflection, "post-game interviews")),
    h("h3", {}, "Estimate"),
    estBox,
    h("div", { class: "row", style: "margin-top:12px" },
      h("button", { class: "btn primary", type: "button", onclick: async () => {
        try {
          const g = await api("POST", "/api/games", read());
          location.hash = `#/game/${g.id}`;
        } catch (e) { errBox.textContent = e.message; }
      } }, "Start game"), errBox),
  );
  box.addEventListener("input", update);
  box.addEventListener("change", update);
  applyPreset("Recommended (~$3)");
}

// ---------------------------------------------------------------- game view
function renderGame(id) {
  const app = $("#app");
  const bar = h("div", { class: "bar" });
  const rulesBox = h("div", { class: "panel" });
  const pendingBox = h("div");
  const feed = h("div", { class: "feed" });
  const centre = h("div", { class: "panel" }, pendingBox, feed);
  const scoreBox = h("div");
  const toolsBox = h("div");
  const side = h("div", { class: "panel side" }, scoreBox, toolsBox);
  const gameGrid = h("div", { class: "game" }, rulesBox, centre, side);
  const statsBox = h("div", { class: "viz-root" });
  let tab = location.hash.endsWith("/stats") ? "stats" : "game";
  const tabBtn = (key, label) => h("button", { class: "tab", role: "tab", "data-tab": key, onclick: () => setTab(key) }, label);
  const tabs = h("div", { class: "tabs", role: "tablist" }, tabBtn("game", "Game"), tabBtn("stats", "📊 Stats"));
  const renderStatsNow = () => { if (state) renderStats(statsBox, state, events); };
  const renderStatsSoon = debounce(renderStatsNow, 400);
  function setTab(key) {
    tab = key;
    gameGrid.hidden = key !== "game";
    statsBox.hidden = key !== "stats";
    for (const b of tabs.children) b.setAttribute("aria-selected", String(b.dataset.tab === key));
    history.replaceState(null, "", `#/game/${id}${key === "stats" ? "/stats" : ""}`);
    if (key === "stats") renderStatsNow();
  }
  const voice = createVoice(id, feed, centre);
  app.replaceChildren(bar, tabs, gameGrid, statsBox, voice.bar);

  let state = null;
  let pendingKey = "";
  let toolsBuilt = false;
  const openRules = new Set();
  let ws = null;
  let closed = false;
  let showThinking = false;
  try { showThinking = localStorage.getItem("nomic.showThinking") === "1"; } catch (e) { /* storage blocked */ }
  feed.classList.toggle("show-thinking", showThinking);
  const setThinking = (on) => {
    showThinking = on;
    feed.classList.toggle("show-thinking", on);
    try { localStorage.setItem("nomic.showThinking", on ? "1" : "0"); } catch (e) { /* storage blocked */ }
  };

  const control =(action) => api("POST", `/api/games/${id}/control`, { action }).catch((e) => flash(e.message));
  const intervene = (kind, data) => api("POST", `/api/games/${id}/intervene`, { kind, data }).catch((e) => flash(e.message));

  function renderBar() {
    const s = state;
    const frac = s.budget_usd > 0 ? Math.min(1, s.total_cost / s.budget_usd) : 0;
    fill(bar,
      h("strong", {}, `Game ${id}`),
      h("span", { class: `pill ${s.status}` }, s.status),
      s.status_reason && s.status !== "over" ? h("span", { class: "muted small" }, s.status_reason) : null,
      s.status === "over"
        ? h("strong", {}, s.winner ? `🏆 ${s.winner} wins` : "No winner", h("span", { class: "muted small" }, ` (${s.end_reason})`))
        : h("span", { class: "small" }, `Turn ${s.completed_turns + 1} · round ${s.round} · ${s.phase} · next: ${s.current_turn ? s.current_turn.player : s.next_player}`),
      h("span", { class: "grow" }),
      interviewButton(s),
      h("label", { class: "small row", title: "Each model's private reasoning (max 3 sentences)" },
        h("input", { type: "checkbox", checked: showThinking, onchange: (e) => setThinking(e.target.checked) }), "💭 Show thinking"),
      voice.button(),
      h("a", { class: "btn", href: `/api/games/${id}/transcript`, download: `nomic-${id}.md`, title: "Markdown transcript, including each model's thinking" }, "⬇ Transcript"),
      s.status === "running" ? h("button", { class: "btn", onclick: () => control("pause") }, "⏸ Pause") : null,
      s.status === "paused" ? h("button", { class: "btn primary", onclick: () => control("resume") }, "▶ Resume") : null,
      s.status === "paused" ? h("button", { class: "btn", onclick: () => control("step") }, "⏭ Step one turn") : null,
      h("span", { class: "small" }, `${usd(s.total_cost)}`, s.budget_usd > 0 ? ` / ${usd(s.budget_usd)}` : ""),
      s.budget_usd > 0 ? h("div", { class: `meter ${frac > 0.9 ? "hot" : ""}` }, h("div", { style: `width:${frac * 100}%` })) : null,
    );
  }

  // After the game: ask model seats that haven't given an interview yet.
  function interviewButton(s) {
    if (s.status !== "over") return null;
    if (s.reflecting) return h("button", { class: "btn", disabled: true }, "🗣 Interviewing players…");
    const missing = s.players.filter((p) => !p.human && !(p.name in (s.reflections || {})));
    if (!missing.length) return null;
    return h("button", { class: "btn", title: "Each AI player reviews the game: decisions, regrets, rivals",
      onclick: (e) => {
        e.target.disabled = true;
        e.target.textContent = "🗣 Interviewing players…";
        api("POST", `/api/games/${id}/reflect`).catch((err) => flash(err.message));
      } }, `🗣 Interview players (${missing.length})`);
  }

  function renderRules() {
    const s = state;
    const recentSeq = recentSeqThreshold();
    fill(rulesBox, h("h2", {}, `Rules (${s.rules.length})`), h("div", { class: "muted small" }, `First to ${s.win_points} points (initial)`),
      ...s.rules.map((r) => {
        const last = r.history[r.history.length - 1];
        const changed = last && last.seq >= recentSeq && last.op !== "initial";
        const det = h("details", { open: openRules.has(r.number) },
          h("summary", {}, `history (${r.history.length})`),
          ...r.history.slice().reverse().map((x) => h("div", { class: "hist" },
            h("div", { class: "muted" }, `#${x.seq} ${x.op} · ${x.source}`), x.text ? h("div", {}, x.text) : null)));
        det.addEventListener("toggle", () => det.open ? openRules.add(r.number) : openRules.delete(r.number));
        return h("div", { class: `rule ${changed ? "changed" : ""}` },
          h("span", { class: "num" }, r.number), h("span", { class: `tag ${r.mutable ? "M" : "I"}` }, r.mutable ? "M" : "I"),
          h("span", {}, r.text),
          r.history.length > 1 ? det : null);
      }));
  }

  function recentSeqThreshold() {
    // highlight rules changed during the last two turns
    const starts = events.filter((e) => e.type === "turn_started").map((e) => e.seq);
    return starts.length >= 2 ? starts[starts.length - 2] : 0;
  }

  function renderScores() {
    const s = state;
    const top = Math.max(...s.players.map((p) => p.score));
    scoreBox.replaceChildren(h("h2", {}, "Scoreboard"),
      h("table", { class: "score-table" },
        h("tr", {}, h("th", {}, "Player"), h("th", {}, "Pts"), h("th", {}, "Spend")),
        ...s.players.map((p) => h("tr", {},
          h("td", { class: p.score === top ? "leader" : "" }, p.name, h("div", { class: "muted small" }, p.model || "👤 human")),
          h("td", { class: "num" }, p.score),
          h("td", { class: "num small" }, usd(p.cost)))),
        h("tr", {}, h("td", { class: "muted" }, "Judge", h("div", { class: "small" }, `${s.config.judge.mode} · ${s.config.judge.model || "—"}`)),
          h("td", {}), h("td", { class: "num small" }, usd(s.judge_cost)))));
  }

  // ------------------------------ interventions (built once; players don't change)
  function buildTools() {
    const s = state;
    const playerSel = () => h("select", {}, ...s.players.map((p) => h("option", { value: p.name }, p.name)));
    const gmText = h("textarea", { placeholder: "Message every player sees as [GM]…" });
    const ruleNum = h("input", { type: "number", placeholder: "rule #" });
    const ruleText = h("textarea", { placeholder: "New text (blank = keep)" });
    const ruleMut = h("select", {}, h("option", { value: "" }, "keep status"), h("option", { value: "true" }, "mutable"), h("option", { value: "false" }, "immutable"));
    ruleNum.addEventListener("change", () => {
      const r = state.rules.find((x) => x.number === Number(ruleNum.value));
      if (r && !ruleText.value) ruleText.value = r.text;
    });
    const scorePlayer = playerSel();
    const scoreVal = h("input", { type: "number", placeholder: "score" });
    const swapPlayer = playerSel();
    const swapModel = h("input", { list: "game-models", placeholder: "model id (blank = human)" });
    const budget = h("input", { type: "number", step: "0.5", min: 0, value: s.budget_usd });
    const winnerSel = h("select", {}, h("option", { value: "" }, "no winner"), ...s.players.map((p) => h("option", { value: p.name }, p.name)));
    toolsBox.replaceChildren(
      h("datalist", { id: "game-models" }, h("option", { value: "mock/slow" })),
      h("h3", {}, "Game master"),
      h("div", { class: "stack" }, gmText, h("button", { class: "btn", onclick: () => { if (gmText.value.trim()) intervene("gm_message", { text: gmText.value }).then(() => { gmText.value = ""; }); } }, "Send to table")),
      h("h3", {}, "Edit rule"),
      h("div", { class: "stack" },
        h("div", { class: "row" }, ruleNum, ruleMut),
        ruleText,
        h("div", { class: "row" },
          h("button", { class: "btn", onclick: () => intervene("edit_rule", { number: Number(ruleNum.value), text: ruleText.value.trim() || null, mutable: ruleMut.value === "" ? null : ruleMut.value === "true" }).then(() => { ruleText.value = ""; }) }, "Save / add"),
          h("button", { class: "btn danger", onclick: () => { if (confirm(`Repeal rule ${ruleNum.value}?`)) intervene("repeal_rule", { number: Number(ruleNum.value) }); } }, "Repeal"))),
      h("h3", {}, "Set score"),
      h("div", { class: "row" }, scorePlayer, scoreVal, h("button", { class: "btn", onclick: () => intervene("set_score", { player: scorePlayer.value, score: Number(scoreVal.value) }) }, "Set")),
      h("h3", {}, "Swap a seat's model"),
      h("div", { class: "stack" }, h("div", { class: "row" }, swapPlayer, h("button", { class: "btn", onclick: () => intervene("swap_model", { player: swapPlayer.value, model: swapModel.value.trim() || null }) }, "Swap")), swapModel),
      h("h3", {}, "Budget"),
      h("div", { class: "row" }, "$", budget, h("button", { class: "btn", onclick: () => intervene("set_budget", { budget_usd: Number(budget.value) }) }, "Update")),
      h("h3", {}, "End game"),
      h("div", { class: "row" }, winnerSel, h("button", { class: "btn danger", onclick: () => { if (confirm("End this game?")) intervene("end_game", { winner: winnerSel.value || null }); } }, "End")),
      h("h3", {}, "Usage"),
      h("button", { class: "btn tiny", onclick: showStats }, "Token & cost breakdown"),
    );
    api("GET", "/api/models").then((ms) => $("#game-models").append(...ms.map((m) => h("option", { value: m.id })))).catch(() => {});
  }

  async function showStats() {
    const rows = await api("GET", `/api/games/${id}/stats`);
    showModal("Usage by seat", h("table", {},
      h("tr", {}, ...["Seat", "Model", "Calls", "Prompt", "Cached", "Completion", "Cost", "Avg ms"].map((t) => h("th", {}, t))),
      ...rows.map((r) => h("tr", {}, h("td", {}, r.seat), h("td", { class: "small" }, r.model), h("td", {}, r.calls),
        h("td", {}, r.prompt_tokens), h("td", {}, r.cached_tokens), h("td", {}, r.completion_tokens),
        h("td", {}, usd(r.cost)), h("td", {}, Math.round(r.avg_latency_ms || 0))))));
  }

  // ------------------------------ human requests
  function renderPending(pending) {
    const key = pending.map((p) => p.id).join(",");
    if (key === pendingKey) return;
    pendingKey = key;
    pendingBox.replaceChildren(...pending.map(pendingForm));
    if (pending.length) document.title = "⏳ Nomic Arena"; else document.title = "Nomic Arena";
  }

  function pendingForm(req) {
    const submit = (body) => api("POST", `/api/games/${id}/pending/${req.id}`, body).catch((e) => flash(e.message));
    const ctx = req.context;
    const prop = ctx.proposal;
    const title = { propose: `Your turn, ${req.seat}: propose a rule-change (#${ctx.next_number})`,
      discuss: `${req.seat}: say something about #${prop && prop.number}`,
      rebut: `${req.seat}: answer the table's comments on your proposal #${prop && prop.number}`,
      vote: `${req.seat}: vote on #${prop && prop.number}`,
      ruling: `Judge: rule on #${prop && prop.number}` }[req.kind];
    const box = h("div", { class: "panel pending stack", style: "margin-bottom:12px" }, h("h2", {}, "⏳ " + title));
    if (prop && req.kind !== "propose") box.append(proposalView(prop));

    if (req.kind === "propose") {
      const op = h("select", {}, ...["enact", "amend", "repeal", "transmute"].map((o) => h("option", { value: o }, o)));
      const target = h("input", { type: "number", placeholder: "rule #" });
      const text = h("textarea", { placeholder: "Full rule text (enact/amend)" });
      const summary = h("input", { placeholder: "One-line pitch", class: "grow" });
      box.append(h("div", { class: "row" }, op, target, summary), text,
        h("button", { class: "btn primary", onclick: () => submit({ op: op.value, target_rule: target.value ? Number(target.value) : null, text: text.value.trim() || null, summary: summary.value }) }, "Propose"));
    } else if (req.kind === "discuss" || req.kind === "rebut") {
      if (req.kind === "rebut") {
        const cur = state && state.current_turn;
        (cur ? cur.messages : []).forEach((m) => box.append(h("div", { class: "small" }, h("strong", {}, `${m.player}: `), m.text)));
      }
      const msg = h("textarea", { placeholder: req.kind === "rebut" ? "Your reply before they vote" : "Your message to the table" });
      box.append(msg, h("button", { class: "btn primary", onclick: () => submit({ message: msg.value || "(no comment)" }) }, "Send"));
    } else if (req.kind === "vote") {
      const reason = h("input", { placeholder: "reason", class: "grow" });
      const dispute = h("input", { placeholder: "dispute (optional): why is this illegal?", class: "grow" });
      const send = (v) => submit({ vote: v, reason: reason.value || "-", dispute: dispute.value.trim() || null });
      box.append(h("div", { class: "row" }, reason), h("div", { class: "row" }, dispute),
        h("div", { class: "row" }, h("button", { class: "btn primary", onclick: () => send("yes") }, "Vote YES"), h("button", { class: "btn", onclick: () => send("no") }, "Vote NO")));
    } else if (req.kind === "ruling") {
      const d = ctx.draft || { legal: true, passed: false, rule_changes: [], score_deltas: [], winner: null, rationale: "" };
      box.append(h("div", { class: "small" }, Object.entries(ctx.votes || {}).map(([n, v]) =>
        h("div", {}, `${n}: `, h("span", { class: v.vote }, v.vote), ` — ${v.reason}`, v.dispute ? h("strong", { class: "error" }, ` DISPUTE: ${v.dispute}`) : null))),
        h("div", { class: "small" }, `Die roll for ${ctx.active}: ${ctx.dice}`));
      if (ctx.feedback) box.append(h("div", { class: "error small" }, "Engine rejected the last ruling: " + ctx.feedback.join("; ")));
      if (ctx.draft) box.append(h("div", { class: "muted small" }, "Prefilled with the LLM judge's draft."));
      const legal = h("input", { type: "checkbox", checked: d.legal });
      const passed = h("input", { type: "checkbox", checked: d.passed });
      const winner = h("input", { placeholder: "winner (blank = none)", value: d.winner || "" });
      const rationale = h("textarea", {}, d.rationale);
      const changes = h("textarea", { class: "mono" }, JSON.stringify(d.rule_changes, null, 1));
      const deltas = h("textarea", { class: "mono" }, JSON.stringify(d.score_deltas, null, 1));
      box.append(h("div", { class: "row" }, h("label", { class: "row small" }, legal, "legal"), h("label", { class: "row small" }, passed, "passed"), winner),
        h("label", { class: "field" }, "Rationale", rationale),
        h("label", { class: "field" }, "Rule changes (JSON)", changes),
        h("label", { class: "field" }, "Score deltas (JSON)", deltas),
        h("div", { class: "row" },
          h("button", { class: "btn primary", onclick: () => {
            let rc, sd;
            try { rc = JSON.parse(changes.value || "[]"); sd = JSON.parse(deltas.value || "[]"); } catch (e) { return flash("Invalid JSON: " + e.message); }
            submit({ legal: legal.checked, passed: passed.checked, rule_changes: rc, score_deltas: sd, winner: winner.value.trim() || null, rationale: rationale.value });
          } }, "Submit ruling"),
          ctx.draft ? h("button", { class: "btn", onclick: () => submit(ctx.draft) }, "Approve draft as-is") : null));
    }
    return box;
  }

  // ------------------------------ feed
  let events = [];
  function proposalView(p) {
    const target = p.target_rule !== null && p.target_rule !== undefined ? ` rule ${p.target_rule}` : "";
    return h("div", {}, h("strong", {}, `#${p.number} ${p.player}: ${p.op.toUpperCase()}${target}`), " — ", p.summary,
      p.text ? h("div", { class: "ruletext" }, `“${p.text}”`) : null);
  }

  function eventView(ev) {
    const d = ev.data;
    const actions = h("span", { class: "actions" },
      d.call_id ? h("button", { class: "btn tiny", title: "raw prompt/response", onclick: () => showCall(d.call_id) }, "raw") : null,
      h("button", { class: "btn tiny", title: "new paused game from this point", onclick: async () => {
        if (!confirm(`Fork a new game from event #${ev.seq}?`)) return;
        try { const g = await api("POST", `/api/games/${id}/fork`, { seq: ev.seq }); location.hash = `#/game/${g.id}`; } catch (e) { flash(e.message); }
      } }, "fork"));
    const thinking = d.thinking ? h("div", { class: "thinking" }, "💭 ", d.thinking) : null;
    const item = (cls, ...kids) => h("div", { class: `ev ${cls}`, "data-seq": ev.seq }, ...kids, thinking, actions);
    switch (ev.type) {
      case "turn_started": return h("div", { class: "turn-head", "data-seq": ev.seq }, `Turn ${d.turn} · round ${d.round} · ${d.player}`);
      case "proposal": return item("proposal", proposalView(d));
      case "message": return d.kind === "gm"
        ? item("gm", h("strong", {}, "[GM] "), d.text)
        : d.kind === "rebuttal"
          ? item("rebuttal", h("strong", {}, `↩ ${d.player} replies: `), d.text)
          : item("", h("strong", {}, `${d.player}: `), d.text);
      case "vote": return item("", h("span", { class: d.vote }, d.vote.toUpperCase()), ` ${d.player} — ${d.reason}`,
        d.dispute ? h("div", { class: "error small" }, `Dispute: ${d.dispute}`) : null);
      case "dice_roll": return item("system", `🎲 ${d.player} rolled ${d.value}`);
      case "ruling": {
        const verdict = d.passed ? "PASSED" : (d.legal ? "FAILED" : "ILLEGAL");
        return item(`ruling ${d.passed ? "pass" : "fail"}`, h("strong", {}, `⚖ ${verdict}`), h("span", { class: "muted small" }, ` (${d.by})`), " ", d.rationale);
      }
      case "rule_changed": return item(ev.actor === "human" ? "human" : "system",
        `${ev.actor === "human" ? "[human] " : ""}rule ${d.op} ${d.number}${d.new_number ? ` → ${d.new_number}` : ""}`);
      case "score_changed": return item(ev.actor === "human" ? "human" : "system",
        `${d.player} ${d.delta > 0 ? "+" : ""}${d.delta} (${d.reason})`);
      case "status": return item("system", `${d.status === "paused" ? "⏸" : "▶"} ${d.status}${d.reason ? ": " + d.reason : ""}`);
      case "turn_aborted": return item("system", `Turn ${d.turn} aborted: ${d.reason}`);
      case "model_swapped": return item("human", `[human] ${d.player} is now ${d.model || "human"}`);
      case "budget_changed": return item("human", `[human] budget set to ${usd(d.budget_usd)}`);
      case "reflection": return item("reflection", reflectionView(d));
      case "game_over": return item("ruling pass", h("strong", {}, `Game over: ${d.winner ? d.winner + " wins" : "no winner"}`), ` (${d.reason})`);
      case "game_created": return item("system", `Game created: ${d.config.ruleset}, ${d.players.map((p) => `${p.name} (${p.model || "human"})`).join(", ")}`);
      default: return null; // llm_usage, turn_ended
    }
  }

  function appendEvents(evs) {
    const nearBottom = centre.scrollHeight - centre.scrollTop - centre.clientHeight < 80;
    for (const ev of evs) {
      events.push(ev);
      const el = eventView(ev);
      if (el) feed.append(el);
    }
    if (nearBottom) centre.scrollTop = centre.scrollHeight;
    voice.decorate();
  }

  function applyState(s, ruleChange) {
    state = s;
    renderBar();
    renderScores();
    if (ruleChange || !rulesBox.childElementCount) renderRules();
    renderPending(s.pending || []);
    if (!toolsBuilt) { buildTools(); toolsBuilt = true; }
    if (tab === "stats") renderStatsSoon();
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/games/${id}`);
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.type === "snapshot") {
        events = [];
        feed.replaceChildren();
        appendEvents(msg.events);
        applyState(msg.state, true);
      } else if (msg.type === "event") {
        appendEvents([msg.event]);
        applyState(msg.state, ["rule_changed", "turn_started"].includes(msg.event.type));
      } else if (msg.type === "pending") {
        if (state) { state.pending = msg.pending; renderPending(msg.pending); }
      } else if (msg.type === "voice") {
        voice.onStatus(msg.status);
        if (state) renderBar();
      }
    };
    ws.onclose = (e) => {
      if (closed) return;
      if (e.code === 4404) { app.replaceChildren(h("div", { class: "panel error" }, `No game ${id}.`)); return; }
      setTimeout(connect, 1500);
    };
  }
  setTab(tab);
  connect();
  return () => { closed = true; voice.destroy(); if (ws) ws.close(); };
}
