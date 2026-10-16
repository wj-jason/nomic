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
