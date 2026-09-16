# The Adaptive Agent

An agent that runs a multi-step plan and continuously checks whether its
world model is still valid. When a new signal actually contradicts an
assumption the remaining plan depends on, it revises the plan and logs a
plain-language "I changed my mind because ..." reason. When nothing
contradicts its assumptions, it does nothing extra: there is no polling
timer and no re-prompting on a schedule.

Live demos:
- **https://claude.ai/artifact/3ZKSUiTFvct1ukHQwiUMjo**, a self-contained
  browser port of the same engine (identical thresholds, hysteresis,
  containment, and trace wording), with a side-by-side adaptive-vs-static
  view and a live containment on/off toggle for the failure test.
- **[add your Vercel URL here after deploying]**, the real thing: a
  Flask API (`app.py`) that calls the actual `run_static` /
  `run_adaptive` functions below, no logic is duplicated in the browser.
  See [Web demo](#web-demo-real-backend) to run it locally or deploy it.

90-second walkthrough: **[add your Loom link here]**

## The scenario

A fictional service, `payments-service`, is being rolled out in stages:
5% of traffic, then 25%, then 50%, then 100%, then the old version is
decommissioned. The plan is built once, up front, from four assumptions:

- error rate stays at or below 1.0%
- p99 latency stays at or below 260ms
- the `payments-ledger-db` dependency is healthy
- no deploy freeze is currently active

Two agents run against the identical plan and the identical assumptions:

- **Static** (the non-adaptive baseline): executes the fixed schedule on a
  fixed dwell time per stage and never looks at what it observes.
- **Adaptive**: observes every tick, checks each assumption the remaining
  plan still depends on, and only revises the plan when one of them
  actually breaks.

## Quick start

No third-party dependencies are required.

```bash
python adaptive_agent.py --list
python adaptive_agent.py --scenario canary_error_spike --agent both
python adaptive_agent.py --all
python adaptive_agent.py --selftest
```

Every run journals its observations, revisions, and checkpointed plan
cursor to a local SQLite file under `runs/` (override with
`--journal-dir`, or skip persistence with `--no-journal`), so a run's
state and trace survive the process exiting.

## Scenarios

| id | what happens | what a static agent does | what the adaptive agent does |
|---|---|---|---|
| `canary_error_spike` | v2.4.0 has a real defect that only shows up once it is serving 25%+ of traffic | rides the bug to 100% of traffic, an outage | rolls back after two consecutive bad readings, before 100% |
| `dependency_incident` | a shared dependency degrades for reasons unrelated to this rollout | keeps ramping traffic onto the new version while the dependency struggles | pauses (does not roll back, the new version itself is fine), resumes automatically once the dependency recovers |
| `freeze_window` | a change freeze gets declared mid-rollout, a fact the plan could not have known | keeps advancing, a compliance violation | holds its current stage for the freeze window, resumes once it lifts |
| `latency_regression_unknown` | p99 latency regresses and stays regressed, but the agent has no playbook for a latency problem | rides it to 100%, no way to notice | halts and escalates to a human instead of guessing at a fix it was never taught |
| `flapping_noise` | **the failure test.** error rate jitters above and below the ceiling every other tick, never two ticks in a row | n/a | correctly produces zero re-plans; see [FAILURE_TEST.md](FAILURE_TEST.md) for what happens without the containment this scenario is designed to test |

Run any one of them head to head:

```bash
python adaptive_agent.py --scenario dependency_incident --agent both
```

Lines beginning with `>>` in the trace are the moments the plan changed;
everything else is routine execution.

## How it decides when to re-plan

Re-planning is triggered by contradicted signals, not a timer. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the full plan, execute, observe,
revise loop and [FAILURE_TEST.md](FAILURE_TEST.md) for how the agent
avoids reacting to noise.

In short: every observation is checked against every assumption the
remaining plan depends on. An assumption only counts as broken after it
fails for two consecutive observations (hysteresis), which is what stops
a single noisy reading from causing a re-plan. A revision governor then
caps how often the plan is allowed to change at all: a cooldown between
revisions, and an automatic escalation to a human if too many revisions
happen inside a short window, because that pattern is thrash, not signal.

## Web demo (real backend)

`app.py` is a thin Flask wrapper around the exact same engine: it imports
`SCENARIOS`, `run_static`, and `run_adaptive` from `adaptive_agent.py`
and serves them over two endpoints, `/api/scenarios` and `/api/run`. The
page in `web/index.html` fetches from those endpoints and renders the
same console UI as the browser demo above, so this is the identical
experience backed by the real Python engine instead of a JavaScript
port.

Run it locally:

```bash
pip install -r requirements.txt
python app.py
# open http://localhost:5000
```

Deploy it to Vercel (the repo already includes `vercel.json`):

```bash
npm i -g vercel   # if you do not already have the CLI
vercel
```

Or import the GitHub repo directly at vercel.com/new; Vercel reads
`vercel.json` and builds `app.py` with its Python runtime automatically.
A serverless function has no durable disk between requests, so `/api/run`
always calls the engine with `journal=None`; SQLite persistence is
demonstrated by the CLI (`--journal-dir`) and by
`test_persistence_round_trips_state` in `--selftest` instead.

## Testing

```bash
python adaptive_agent.py --selftest
```

Nine checks: hysteresis behavior in isolation, each scenario's expected
outcome for both agents, the failure-containment case (with and without
hysteresis, to prove the failure mode is real and that the guardrail
actually stops it), and a round trip through the SQLite journal.

## Repository layout

```
adaptive_agent.py     the whole engine: model, world model, reasoner,
                       governor, journal, scenarios, CLI, self-test
app.py                 Flask API wrapping the engine (no duplicated logic)
web/index.html         frontend for app.py: fetches /api/run, renders the console
vercel.json             build/route config so Vercel deploys app.py
ARCHITECTURE.md        the plan -> execute -> observe -> revise loop
FAILURE_TEST.md        the adaptation-goes-wrong scenario and its containment
THESIS.md              two-year thesis on adaptive planning in production
NOTES.md                AI tools used, key decisions, what is out of scope
runs/                  SQLite journals from local runs (git-ignored)
```

## Why it matters

Static agents commit to a plan and execute it blindly. The moment reality
diverges from what the plan assumed, be it a defect, an unrelated outage,
a policy change, or a problem nobody anticipated, a static agent either
fails silently or barrels through the failure to completion. The
difference between a demo and a deployable system is whether the agent
notices that its world model broke, and does something safe and
explainable about it.
