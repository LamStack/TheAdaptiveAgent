# Failure test: when adaptation goes wrong, and how it is contained

## The scenario: `flapping_noise`

`error_rate_pct` bounces between 0.3% (fine) and 1.3% (over the 1.0%
ceiling) on every other tick, from sampling jitter rather than a real
regression. It never breaches for two ticks in a row.

This is deliberately the case where naive adaptation fails. A re-planner
that reacts to every contradiction it sees will roll the service back on
tick 1, notice it looks fine again on tick 2, get told it is broken again
on tick 3, and so on. The plan never finishes, the rollout never
completes, and every one of those "decisions" is technically justified by
the observation it fired on. That is adaptation going wrong: it is not
lying about its reasoning, its reasoning is just built on a signal that
was never real evidence of the world changing.

## Reproducing the failure live

```bash
python adaptive_agent.py --scenario flapping_noise --agent adaptive --no-hysteresis
```

`--no-hysteresis` sets `breaches_to_trigger=1` and `recoveries_to_clear=1`
for every assumption, and removes the revision governor's cooldown and
window cap. This is not a hypothetical: it is the same code path
(`run_adaptive(..., hysteresis=False)`), stripped of the two guardrails
described below, run against the same scenario. It produces 6 revisions
in 12 ticks and never gets the rollout past 0%, an "I changed my mind"
trace that is individually correct and collectively useless.

## The containment

Two independent mechanisms, both on by default, stop this:

1. **Hysteresis in the `WorldModel`.** An assumption is only reported
   broken after it fails for `breaches_to_trigger` (default 2)
   *consecutive* observations, and only reported recovered after
   `recoveries_to_clear` (default 2) consecutive successes. A single
   noisy reading cannot fire a contradiction on its own; it takes a
   pattern. `flapping_noise` never produces two consecutive breaches, so
   with hysteresis on, zero contradictions ever fire.
2. **The `ReplanGovernor`.** Even if a real pattern of contradictions
   does fire, this is the second line of defense: a cooldown between
   revisions, and a hard cap on how many revisions are allowed inside a
   rolling window. If that cap is exceeded, the governor stops handing
   out revisions and escalates to a human instead, on the reasoning that
   a plan changing that often is itself evidence of a bad signal, not a
   real one, even if each individual contradiction looked legitimate on
   its own.

Run it the normal way (hysteresis on) and it produces zero revisions:

```bash
python adaptive_agent.py --scenario flapping_noise --agent adaptive
```

## What this does and does not prove

This does not claim hysteresis is a universal fix. A real regression that
straddles the threshold (bouncing between just-under and just-over) could
in principle still take a few extra ticks to confirm, which is the
correct trade: slightly slower to react, in exchange for not treating
sensor jitter as ground truth. The governor's escalation path exists for
exactly the case hysteresis cannot fully solve: if contradictions keep
firing even with the pattern requirement, the agent stops guessing and
hands the decision to a person, rather than assuming its own containment
is infallible.

## Automated coverage

Both the failure and its containment are asserted in the self-test, not
just described here:

- `test_flapping_noise_thrashes_without_hysteresis`: asserts the naive
  path produces at least 3 revisions, the failure mode is real, not a
  hypothetical.
- `test_flapping_noise_is_contained_with_hysteresis`: asserts the
  governed path produces exactly 0 revisions on the identical
  observation stream.

```bash
python adaptive_agent.py --selftest
```
