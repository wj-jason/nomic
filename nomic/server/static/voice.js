// Voiceover: a generation panel, a sticky player bar, and feed sync (highlight + "play from
// here"). One <audio> plays game.mp3; manifest line timings map time <-> feed items (data-seq).
// Uses h(), fill(), api(), usd(), showModal() from app.js. Model text only via textContent.
"use strict";

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h_ = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h_ ? `${h_}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(s).padStart(2, "0")}`;
}

function createVoice(gameId, feed, scroller) {
  let manifest = null;
  let lines = [];
  let status = { state: "idle" };
  let available = true;
  let current = -1;
  let skipAsides = false;
  let follow = true;
  let panel = null; // live elements of the open generation panel
  const audio = new Audio();
  audio.preload = "metadata";
  const base = `/api/games/${gameId}/voice`;

  // ------------------------------------------------------------ player bar
  const bar = h("div", { class: "voice-bar", hidden: true });
  const playBtn = h("button", { class: "btn primary", title: "Play / pause (space)", onclick: () => toggle() }, "▶");
  const timeLbl = h("span", { class: "small mono voice-time" }, "0:00");
  const nowLbl = h("span", { class: "small voice-now" });
  const seek = h("input", { type: "range", min: 0, max: 1000, value: 0, class: "voice-seek", "aria-label": "Seek" });
  seek.addEventListener("input", () => { if (manifest) audio.currentTime = (seek.value / 1000) * manifest.duration; });
  const speed = h("select", { title: "Speed", onchange: (e) => { audio.playbackRate = Number(e.target.value); } },
    ...[1, 1.25, 1.5, 2].map((r) => h("option", { value: r }, `${r}×`)));
  const skipBox = h("input", { type: "checkbox", onchange: (e) => { skipAsides = e.target.checked; } });
  const skipLbl = h("label", { class: "small row" }, skipBox, "skip 💭");
  const followBox = h("input", { type: "checkbox", checked: true, onchange: (e) => { follow = e.target.checked; } });
  // Thoughts are opt-in: offer to add them to an existing voiceover (spoken lines are reused).
  const addThoughts = h("button", { class: "btn tiny", title: "Re-render with each player's private thinking as quieter asides",
    onclick: async () => {
      addThoughts.disabled = true;
      try { onStatus(await api("POST", base, { ...manifest.options, thinking: true })); } catch (err) { flash(err.message); addThoughts.disabled = false; }
    } }, "💭 Add thoughts");
  fill(bar,
    h("button", { class: "btn", title: "Previous turn", onclick: () => jumpChapter(-1) }, "⏮"),
    h("button", { class: "btn", title: "Previous line", onclick: () => jumpLine(-1) }, "◀"),
    playBtn,
    h("button", { class: "btn", title: "Next line", onclick: () => jumpLine(1) }, "▶|"),
    h("button", { class: "btn", title: "Next turn", onclick: () => jumpChapter(1) }, "⏭"),
    timeLbl, seek, nowLbl,
    h("span", { class: "grow" }),
    speed,
    addThoughts,
    skipLbl,
    h("label", { class: "small row" }, followBox, "follow"),
    h("a", { class: "btn tiny", href: `${base}/files/game.mp3`, download: `nomic-${gameId}.mp3` }, "⬇ MP3"),
    h("a", { class: "btn tiny", href: `${base}/files/game.vtt`, download: `nomic-${gameId}.vtt` }, "⬇ Captions"));

  function toggle() {
    if (!manifest) return;
    if (audio.paused) audio.play(); else audio.pause();
  }
  function lineAt(t) {
    let lo = 0, hi = lines.length - 1, ans = 0;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (lines[mid].start <= t + 0.01) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
    return ans;
  }
  function seekLine(i) {
    if (!lines.length) return;
    i = Math.max(0, Math.min(lines.length - 1, i));
    audio.currentTime = lines[i].start + 0.01;
    highlight(i);
  }
  function jumpLine(step) {
    let i = lineAt(audio.currentTime) + step;
    while (skipAsides && lines[i] && lines[i].kind === "aside") i += step;
    seekLine(i);
  }
  function jumpChapter(step) {
    const chs = manifest ? manifest.chapters : [];
    const t = audio.currentTime;
    let idx = chs.findIndex((c, k) => t >= c.start && (k === chs.length - 1 || t < chs[k + 1].start));
    if (step < 0 && t - chs[idx].start > 2) step = 0; // first press: back to this turn's start
    const target = chs[Math.max(0, Math.min(chs.length - 1, idx + step))];
    if (target) { audio.currentTime = target.start + 0.01; highlight(lineAt(audio.currentTime)); }
  }
  function playFromSeq(seq) {
    const i = lines.findIndex((l) => l.event_seq === seq && l.kind !== "aside");
    if (i < 0) return;
    seekLine(i);
    audio.play();
  }

  function highlight(i) {
    if (i === current || !lines[i]) return;
    current = i;
    const line = lines[i];
    feed.querySelectorAll(".speaking, .speaking-aside").forEach((el) => el.classList.remove("speaking", "speaking-aside"));
    const el = feed.querySelector(`[data-seq="${line.event_seq}"]`);
    if (el) {
      el.classList.add("speaking");
      if (line.kind === "aside") { const th = el.querySelector(".thinking"); if (th) th.classList.add("speaking-aside"); }
      if (follow && !audio.paused) {
        const box = el.getBoundingClientRect(), sc = scroller.getBoundingClientRect();
        if (box.top < sc.top || box.bottom > sc.bottom) el.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    }
    nowLbl.textContent = `${line.chapter} · ${line.speaker}${line.kind === "aside" ? " (thinking)" : ""}`;
  }

  audio.addEventListener("timeupdate", () => {
    if (!manifest) return;
    const t = audio.currentTime;
    let i = lineAt(t);
    if (skipAsides && lines[i] && lines[i].kind === "aside" && !audio.paused) {
      let j = i;
      while (lines[j] && lines[j].kind === "aside") j++;
      if (lines[j]) { audio.currentTime = lines[j].start + 0.01; i = j; }
    }
    highlight(i);
    timeLbl.textContent = `${fmtTime(t)} / ${fmtTime(manifest.duration)}`;
    if (document.activeElement !== seek) seek.value = String(Math.round((t / manifest.duration) * 1000));
  });
  audio.addEventListener("play", () => { playBtn.textContent = "⏸"; feed.classList.add("voice-playing"); });
  audio.addEventListener("pause", () => { playBtn.textContent = "▶"; feed.classList.remove("voice-playing"); });

  // ------------------------------------------------------------ feed decoration
  function decorate() {
    if (!lines.length) return;
    const seqs = new Set(lines.filter((l) => l.kind !== "aside").map((l) => l.event_seq));
    feed.querySelectorAll("[data-seq]").forEach((el) => {
      const seq = Number(el.dataset.seq);
      if (!seqs.has(seq) || el.querySelector(":scope > .voice-play, :scope > .actions > .voice-play")) return;
      const btn = h("button", { class: "btn tiny voice-play", title: "Play from here",
        onclick: (e) => { e.stopPropagation(); playFromSeq(seq); } }, "▶");
      const actions = el.querySelector(":scope > .actions");
      if (actions) actions.prepend(btn); else el.append(" ", btn);
    });
  }

  function setManifest(m) {
    if (!m) return;
    const wasPlaying = !audio.paused;
    const t = audio.currentTime;
    manifest = m;
    lines = m.lines;
    current = -1;
    audio.src = `${base}/files/game.mp3?v=${m.version}`;
    if (t) audio.currentTime = Math.min(t, m.duration);
    if (wasPlaying) audio.play();
    bar.hidden = false;
    const hasThoughts = lines.some((l) => l.kind === "aside");
    skipLbl.hidden = !hasThoughts;
    addThoughts.hidden = Boolean(m.options && m.options.thinking);
    addThoughts.disabled = false;
    timeLbl.textContent = `0:00 / ${fmtTime(m.duration)}`;
    feed.querySelectorAll(".voice-play").forEach((b) => b.remove());
    decorate();
  }

  async function load() {
    try {
      const v = await api("GET", base);
      available = v.available;
      status = v.status;
      setManifest(v.manifest);
    } catch (e) { /* no voiceover yet */ }
  }

  // ------------------------------------------------------------ generation panel
  function openPanel() {
    const opt = (key, label, checked, hint) => {
      const box = h("input", { type: "checkbox", checked });
      box.addEventListener("change", refreshEstimate);
      return { key, box, el: h("label", { class: "row small", title: hint || "" }, box, label) };
    };
    const prev = manifest ? manifest.options : {};
    const opts = [
      opt("votes", "Votes, with each player's reason", prev.votes ?? true),
      opt("rulings", "⚖ The judge's rulings", prev.rulings ?? true),
      opt("rule_text", "Read the full rule text (not just the pitch)", prev.rule_text ?? false),
      opt("thinking", "💭 Internal thoughts, as quieter asides (off by default)", prev.thinking ?? false),
      opt("interviews", "Post-game interviews (off by default)", prev.interviews ?? false),
    ];
    const est = h("div", { class: "small muted" }, "Estimating…");
    const prog = h("div", { class: "meter voice-progress" }, h("div", { style: "width:0%" }));
    const progLbl = h("div", { class: "small muted" });
    const cast = h("div", { class: "voice-cast" }, h("div", { class: "small muted" }, "Loading voices…"));
    buildCastPicker(cast, refreshEstimate);
    const go = h("button", { class: "btn primary", onclick: generate }, manifest ? "Update voiceover" : "Generate voiceover");
    const stop = h("button", { class: "btn danger", hidden: true, title: "Stop after the current line; finished lines are kept",
      onclick: async () => {
        stop.disabled = true;
        try { onStatus(await api("DELETE", base)); } catch (err) { progLbl.textContent = err.message; }
      } }, "Cancel");
    const read = () => Object.fromEntries(opts.map((o) => [o.key, o.box.checked]));
    async function refreshEstimate() {
      const q = new URLSearchParams(Object.entries(read()).map(([k, v]) => [k, v ? "true" : "false"]));
      try {
        const e = await api("GET", `${base}/estimate?${q}`);
        available = e.available;
        const genMin = Math.max(1, Math.round(e.minutes / e.realtime_factor));
        est.textContent = `${e.lines} lines · about ${e.minutes} minutes of audio · roughly ${genMin} min to generate on ${e.device === "cuda" ? "the GPU" : "CPU"} (lines already made are reused)`;
        if (!available) { go.disabled = true; est.textContent = "Kokoro isn't installed on the server. See the README's voiceover section."; }
      } catch (err) { est.textContent = err.message; }
    }
    async function generate() {
      go.disabled = true;
      try { onStatus(await api("POST", base, read())); } catch (err) { progLbl.textContent = err.message; go.disabled = false; }
    }
    panel = { prog, progLbl, go, stop };
    showModal("🔊 Voiceover", h("div", { class: "stack" },
      h("div", { class: "small" }, "Only the table speaks, each in its own voice: the players talking to each other, and the judge giving its rulings. Turns become chapters. Generated locally with Kokoro."),
      ...opts.map((o) => o.el), est, cast, h("div", { class: "row" }, go, stop), prog, progLbl));
    $("#modal").addEventListener("close", () => { panel = null; }, { once: true });
    refreshEstimate();
    renderPanelStatus();
  }

  // One row per player: voice dropdown + preview. Choices are saved per game on the server
  // and used by every render (lines whose voice changed get re-rendered).
  let catalog = null;
  let previewAudio = null;
  async function buildCastPicker(box, onChange) {
    try {
      if (!catalog) catalog = (await api("GET", "/api/voices")).voices;
      let { cast: current, custom } = await api("GET", `${base}/cast`);
      const groups = [["American", "female"], ["American", "male"], ["British", "female"], ["British", "male"]];
      const warn = h("div", { class: "small muted" });
      const checkDupes = () => {
        const used = Object.values(current);
        const dupes = [...new Set(used.filter((v, i) => used.indexOf(v) !== i))];
        warn.textContent = dupes.length ? `Note: ${dupes.join(", ")} is used by more than one player.` : "";
      };
      const rows = Object.keys(current).map((player) => {
        const sel = h("select", { "aria-label": `Voice for ${player}` },
          groups.map(([accent, gender]) => h("optgroup", { label: `${accent} · ${gender}` },
            catalog.filter((v) => v.accent === accent && v.gender === gender)
              .map((v) => h("option", { value: v.id, selected: v.id === current[player] }, `${v.name} (${v.id})`)))));
        const tag = h("span", { class: "small muted" }, player in custom ? "" : "default");
        sel.addEventListener("change", async () => {
          try {
            ({ cast: current, custom } = await api("PUT", `${base}/cast`, { [player]: sel.value }));
            tag.textContent = "";
            checkDupes();
            onChange();
          } catch (err) { flash(err.message); }
        });
        const play = h("button", { class: "btn tiny", type: "button", title: "Preview this voice", onclick: async () => {
          if (previewAudio) previewAudio.pause();
          play.textContent = "…";
          previewAudio = new Audio(`/api/voices/${sel.value}/preview`);
          previewAudio.addEventListener("playing", () => { play.textContent = "▶"; }, { once: true });
          previewAudio.addEventListener("error", () => { play.textContent = "▶"; flash("Preview failed (is Kokoro installed?)"); }, { once: true });
          previewAudio.play().catch(() => { play.textContent = "▶"; });
        } }, "▶");
        return h("div", { class: "voice-cast-row" }, h("strong", {}, player === "judge" ? "⚖ judge" : player), sel, play, tag);
      });
      checkDupes();
      fill(box, h("div", { class: "small" }, h("strong", {}, "Voices"), " (saved for this game; ▶ plays a short sample)"), rows, warn);
    } catch (err) { fill(box, h("div", { class: "small error" }, err.message)); }
  }

  function renderPanelStatus() {
    if (!panel) return;
    const s = status;
    const pct = s.total_lines ? (100 * s.done_lines) / s.total_lines : 0;
    panel.prog.firstChild.style.width = `${s.state === "done" ? 100 : pct}%`;
    const eta = s.eta_seconds ? ` · about ${fmtTime(s.eta_seconds)} left` : "";
    panel.progLbl.textContent = {
      idle: "",
      queued: "Queued (another voiceover is rendering)…",
      running: `Rendering ${s.done_lines} / ${s.total_lines} lines (${s.new_lines} new)${eta}`,
      done: s.duration ? `Done: ${fmtTime(s.duration)} of audio. Use the player below the game.` : "Done.",
      error: `Failed: ${s.error}`,
      cancelled: `Cancelled. ${s.done_lines} of ${s.total_lines} lines are saved; generating again picks up from there.`,
    }[s.state] || "";
    const busy = s.state === "queued" || s.state === "running";
    panel.go.disabled = busy;
    panel.stop.hidden = !busy;
    if (busy) panel.stop.disabled = false;
  }

  function onStatus(s) {
    status = s;
    renderPanelStatus();
    if (s.state === "done") load();
  }

  function button() {
    const busy = status.state === "running" || status.state === "queued";
    const pct = status.total_lines ? Math.round((100 * status.done_lines) / status.total_lines) : 0;
    return h("button", { class: "btn", title: "Listen to the game", onclick: openPanel },
      busy ? `🔊 Rendering ${pct}%` : "🔊 Voiceover");
  }

  const onKey = (e) => {
    if (e.code === "Space" && manifest && !bar.hidden && !/INPUT|TEXTAREA|SELECT|BUTTON/.test(document.activeElement.tagName)) {
      e.preventDefault();
      toggle();
    }
  };
  document.addEventListener("keydown", onKey);

  load();
  return {
    bar, button, onStatus, decorate,
    destroy() { document.removeEventListener("keydown", onKey); audio.pause(); audio.removeAttribute("src"); },
  };
}
