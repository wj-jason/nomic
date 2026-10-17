# Nomic Arena

LLMs play [Nomic](https://en.wikipedia.org/wiki/Nomic) against each other through
[OpenRouter](https://openrouter.ai). The engine handles turn order, the event log, dice rolls and
applying rule changes. A Judge, either an LLM or you, interprets the natural-language rules each
turn. A small web UI lets you watch, pause, step, edit rules and scores, swap models, talk to
the table as game master, fork a game from any event, and take a seat or the judge's chair
yourself.

## Setup

```bash
conda env create -f environment.yml
conda activate nomic
pip install -e ".[dev]"
pre-commit install
export OPENROUTER_API_KEY=...   # or put it in .env
```

## Use

```bash
nomic serve                          # web UI at http://127.0.0.1:8000
nomic estimate configs/default.yaml  # rough $ per game for a config
nomic run configs/smoke.yaml         # headless game in the terminal (cheap models)
nomic run configs/mock.yaml          # offline game with scripted players, costs nothing
```

Games, events and every LLM call (prompt, response, tokens, cost) are stored in
`data/nomic.db`. Games come back paused after a restart.

## How a turn works

```
PROPOSE → DISCUSS (optional) → VOTE (parallel) → die roll → JUDGE → apply changes → next
```

- **Players** return structured JSON: one rule-change (`enact` / `repeal` / `amend` /
  `transmute`), a discussion message, then a vote with an optional dispute.
- **The engine** rolls a d6 every turn and gives it to the judge. Whether it counts is up to the
  current rules.
- **The judge** returns `{legal, passed, rule_changes, score_deltas, winner, rationale}`. The
  engine validates the ruling: it can't amend an immutable rule, name an unknown player, and so
  on. An invalid ruling goes back to the judge with the errors, up to 3 tries; after that the
  engine drops whatever parts it can't apply.
- **Judge modes:**
  - `llm`: the model's ruling stands.
  - `human`: you rule every turn, with the model's draft prefilled if a judge model is set.
  - `llm_with_human_override`: you approve or edit each draft, optionally with a timeout.
  - `human_on_appeal`: a player dispute sends the turn to you.
- **Seats:**
  - `model: null` is a human player.
  - `model: mock/...` is an offline scripted player.
  - `mock/slow` is the same with a delay, so you can watch it in the UI.

## Thinking

Every reply from a player or judge starts with a `thinking` field: its private reasoning, at
most 3 sentences. The server trims anything longer. The thinking is stored with each event and
shown in the feed when you tick **💭 Show thinking**. It never goes into another player's or the
judge's prompt.

Native (hidden) reasoning is off by default (`reasoning_effort: none`). That makes calls several
times faster and cheaper than letting the model reason privately at length. You can turn it back
on per game with `reasoning_effort` set to `low`, `medium` or `high`.

## Voiceovers

Any game can be turned into audio with local [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)
text-to-speech. It's free and offline, and each player gets its own voice. Install the optional
extra once:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu   # CPU build, ~200 MB
pip install -e ".[voice]"
```

In a game, **🔊 Voiceover** lets you choose what is spoken:
- thinking, read as quieter "asides";
- vote reasons;
- full rule text;
- post-game interviews.

It shows the length and then renders in the background (about 5× realtime on CPU). Every line
is cached, so updating a live game only renders its new lines.

A player bar under the game plays the whole game. It highlights each feed item as it's spoken
and has "▶" on each item to play from there. It also offers **⬇ MP3**, with chapters per turn,
and **⬇ Captions** (WebVTT). Files live in `data/audio/<game id>/`.

## Rulesets

- `lite` (default): 14 rules, first to 40 points, simple majority. About 1.1k tokens.
- `suber`: Peter Suber's 29-rule Initial Set, paraphrased, first to 100 points. It starts with
  unanimous votes and gives 10 points to anyone who votes against a proposal that passes. It
  costs about 3–4× more per game.

`win_points` in a config overrides either ruleset's target.

## Cost

Prices come live from OpenRouter's `/models`. The real cost of each call is recorded from the
`usage` field OpenRouter returns. A game pauses itself when `budget_usd` is spent; raise the
budget in the UI to continue.

To keep costs down:
- Prompts are ordered so providers can cache the stable prefix. Anthropic and Gemini models get
  explicit `cache_control` breakpoints.
- `reasoning_effort` defaults to `low`.
- Older turns are folded into one-line digests.
- Cost grows with the square of the number of players, so player count matters most.

## Tests

```bash
pytest            # offline: engine, judge validation, mock games, API
ruff check . && mypy
```
