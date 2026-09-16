#!/usr/bin/env python3
"""
The Adaptive Agent
===================

A single-file demo of a plan -> execute -> observe -> revise loop, applied to
a staged canary rollout of a fictional "payments-service v2.4.0".

Both agents below (Adaptive and Static) start from the identical plan and the
identical assumptions. The Static agent is a non-adaptive baseline: it follows
its fixed schedule no matter what it observes. The Adaptive agent checks its
assumptions against every incoming observation and only re-plans when one of
them actually breaks, using hysteresis so a single noisy reading is not enough
to act on. Every re-plan is logged with an explicit "I changed my mind
because ..." reason, and the whole run is journaled to a local SQLite file so
the trace and the final state both survive the process exiting.

Usage
-----
    python adaptive_agent.py --list
    python adaptive_agent.py --scenario canary_error_spike
    python adaptive_agent.py --scenario canary_error_spike --agent static
    python adaptive_agent.py --scenario canary_error_spike --agent both
    python adaptive_agent.py --scenario flapping_noise --no-hysteresis
    python adaptive_agent.py --all
    python adaptive_agent.py --selftest

No third-party dependencies are required. Everything here is standard library.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

Observation = Dict[str, Any]


# ---------------------------------------------------------------------------
# Core model: assumptions, steps, plans, contradictions, revisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Assumption:
    """A belief the current plan depends on.

    The agent only re-plans when one of these stops holding. `holds` receives
    the raw metric value and returns True while the assumption is still true.
    `breaches_to_trigger` is the hysteresis window: a single bad reading is
    not enough to act on, see WorldModel below.
    """

    id: str
    description: str
    metric: str
    holds: Callable[[Any], bool]
    breaches_to_trigger: int = 2
    recoveries_to_clear: int = 2

    def check(self, observation: Observation) -> bool:
        if self.metric not in observation:
            return True
        return bool(self.holds(observation[self.metric]))


@dataclass
class Step:
    id: str
    name: str
    action: str  # advance | rollback | pause | open_incident | escalate | cleanup
    stage_pct: Optional[int] = None
    dwell: int = 2


@dataclass
class Plan:
    version: int
    steps: List[Step]
    reason: str
    cursor: int = 0
    ticks_on_current_step: int = 0

    def current_step(self) -> Optional[Step]:
        if 0 <= self.cursor < len(self.steps):
            return self.steps[self.cursor]
        return None

    def remaining(self) -> List[Step]:
        return self.steps[self.cursor:]


@dataclass
class Contradiction:
    assumption_id: str
    description: str
    metric: str
    breach_count: int
    values: List[Any]
    tick: int


@dataclass
class Revision:
    tick: int
    from_version: int
    to_version: int
    reason: str
    new_steps: List[Step]
    contradiction: Optional[Contradiction] = None

    def evidence(self) -> Dict[str, Any]:
        if self.contradiction is None:
            return {}
        c = self.contradiction
        return {
            "assumption_id": c.assumption_id,
            "metric": c.metric,
            "breach_count": c.breach_count,
            "values": c.values,
        }


@dataclass
class RunState:
    stage_pct: int = 0
    rolled_back: bool = False
    paused: bool = False


def build_assumptions() -> List[Assumption]:
    return [
        Assumption(
            id="error_rate_stable",
            description="error rate stays at or below 1.0%",
            metric="error_rate_pct",
            holds=lambda v: v <= 1.0,
        ),
        Assumption(
            id="latency_stable",
            description="p99 latency stays at or below 260ms",
            metric="p99_latency_ms",
            holds=lambda v: v <= 260,
        ),
        Assumption(
            id="dependency_healthy",
            description="the payments-ledger-db dependency is healthy",
            metric="dependency_health",
            holds=lambda v: v == "healthy",
        ),
        Assumption(
            id="no_deploy_freeze",
            description="no deploy freeze is currently active",
            metric="freeze_active",
            holds=lambda v: v is False,
        ),
    ]


def build_plan() -> Plan:
    return Plan(
        version=1,
        reason="initial rollout plan for payments-service v2.4.0",
        steps=[
            Step(id="stage_5", name="shift 5% of traffic to v2.4.0", action="advance", stage_pct=5, dwell=2),
            Step(id="stage_25", name="shift 25% of traffic to v2.4.0", action="advance", stage_pct=25, dwell=2),
            Step(id="stage_50", name="shift 50% of traffic to v2.4.0", action="advance", stage_pct=50, dwell=2),
            Step(id="stage_100", name="shift 100% of traffic to v2.4.0", action="advance", stage_pct=100, dwell=2),
            Step(id="cleanup", name="decommission v2.3.9", action="cleanup", stage_pct=100, dwell=1),
        ],
    )


# ---------------------------------------------------------------------------
# World model: hysteresis-based contradiction detection
# ---------------------------------------------------------------------------


@dataclass
class _AssumptionState:
    assumption: Assumption
    consecutive_breaches: int = 0
    consecutive_recoveries: int = 0
    breach_values: List[Any] = field(default_factory=list)
    broken: bool = False


@dataclass
class Observed:
    contradictions: List[Contradiction]
    recoveries: List[str]


class WorldModel:
    """Live status of every assumption the plan currently relies on.

    A contradiction fires from data, not from a clock: every observation is
    checked against every registered assumption, and a given assumption only
    breaks after failing its check for `breaches_to_trigger` consecutive
    observations in a row. That hysteresis is what stops one noisy reading
    from causing a re-plan (see the flapping_noise scenario and the
    --selftest failure-containment check below).
    """

    def __init__(self, assumptions: List[Assumption]):
        self.states: Dict[str, _AssumptionState] = {a.id: _AssumptionState(a) for a in assumptions}

    def observe(self, tick: int, observation: Observation) -> Observed:
        contradictions: List[Contradiction] = []
        recoveries: List[str] = []

        for aid, state in self.states.items():
            still_holds = state.assumption.check(observation)

            if still_holds:
                state.consecutive_breaches = 0
                state.breach_values = []
                if state.broken:
                    state.consecutive_recoveries += 1
                    if state.consecutive_recoveries >= state.assumption.recoveries_to_clear:
                        state.broken = False
                        state.consecutive_recoveries = 0
                        recoveries.append(aid)
                continue

            state.consecutive_recoveries = 0
            state.consecutive_breaches += 1
            state.breach_values.append(observation.get(state.assumption.metric))

            if state.broken:
                continue  # already surfaced this break, keep collecting evidence
            if state.consecutive_breaches < state.assumption.breaches_to_trigger:
                continue  # not yet a pattern

            state.broken = True
            contradictions.append(
                Contradiction(
                    assumption_id=aid,
                    description=state.assumption.description,
                    metric=state.assumption.metric,
                    breach_count=state.consecutive_breaches,
                    values=list(state.breach_values),
                    tick=tick,
                )
            )

        return Observed(contradictions=contradictions, recoveries=recoveries)


# ---------------------------------------------------------------------------
# Reasoning: what to do about a contradiction, and how often it is allowed
# ---------------------------------------------------------------------------


class Reasoner(Protocol):
    def propose(self, plan: Plan, contradiction: Contradiction, tick: int) -> Revision: ...


def escalate_steps(reason: str) -> List[Step]:
    return [Step(id="escalate", name=f"halt auto re-planning and escalate to a human: {reason}",
                  action="escalate", dwell=10 ** 9)]


_PLAYBOOK: Dict[str, Callable[[Plan, Contradiction], List[Step]]] = {}


def _playbook(assumption_id: str):
    def register(fn):
        _PLAYBOOK[assumption_id] = fn
        return fn
    return register


@_playbook("error_rate_stable")
def _rollback(plan: Plan, c: Contradiction) -> List[Step]:
    return [
        Step(id="rollback", name="roll back to the last known-good version", action="rollback", dwell=1),
        Step(id="incident", name="open an incident and page on-call", action="open_incident", dwell=1),
    ]


@_playbook("dependency_healthy")
def _pause_for_dependency(plan: Plan, c: Contradiction) -> List[Step]:
    return [Step(id="pause_dependency",
                  name="pause the rollout and wait for the dependency to recover "
                       "(the new version itself is healthy, do not roll back)",
                  action="pause", dwell=10 ** 9)] + plan.remaining()


@_playbook("no_deploy_freeze")
def _pause_for_freeze(plan: Plan, c: Contradiction) -> List[Step]:
    return [Step(id="pause_freeze",
                  name="pause the rollout for the active deploy freeze "
                       "(system is healthy, this is a policy hold)",
                  action="pause", dwell=10 ** 9)] + plan.remaining()


@dataclass
class RuleBasedReasoner:
    """Deterministic, fully offline. Ships as the default so the demo and the
    self-test are reproducible without any network access or API key."""

    def propose(self, plan: Plan, contradiction: Contradiction, tick: int) -> Revision:
        make_steps = _PLAYBOOK.get(contradiction.assumption_id)
        if make_steps is None:
            new_steps = escalate_steps(f"no playbook covers assumption '{contradiction.assumption_id}'")
            reason = (f"assumption '{contradiction.description}' broke ({contradiction.breach_count} "
                       f"consecutive breaches, values={contradiction.values}) and no playbook covers it, "
                       f"so I am escalating instead of guessing")
        else:
            new_steps = make_steps(plan, contradiction)
            reason = (f"assumption '{contradiction.description}' broke: {contradiction.metric} breached "
                       f"its threshold for {contradiction.breach_count} consecutive observations "
                       f"(values={contradiction.values})")
        return Revision(tick=tick, from_version=plan.version, to_version=plan.version + 1,
                          reason=reason, new_steps=new_steps, contradiction=contradiction)


@dataclass
class ClaudeReasoner:
    """Wraps a base reasoner and asks Claude to restate its justification in
    plainer language. The action taken (`new_steps`) always comes from the
    deterministic base reasoner; the LLM call may only change the wording of
    `reason`, and only when it succeeds. Any failure (no network, bad key,
    malformed response) silently falls back to the base reasoner's own text,
    so nothing about what the agent actually does depends on the API call.
    """

    base: RuleBasedReasoner
    model: str = "claude-sonnet-5"

    def propose(self, plan: Plan, contradiction: Contradiction, tick: int) -> Revision:
        revision = self.base.propose(plan, contradiction, tick)
        try:
            narrated = self._narrate(revision, contradiction)
            if narrated:
                revision.reason = narrated
        except Exception:
            pass
        return revision

    def _narrate(self, revision: Revision, contradiction: Contradiction) -> Optional[str]:
        import anthropic  # optional dependency, only touched when a key is configured

        client = anthropic.Anthropic()
        prompt = (
            "Rewrite this incident note as one or two plain sentences, first person, "
            "starting with 'I changed my mind because'. Use only the facts given, "
            "do not invent numbers or causes that are not listed.\n\n"
            f"Assumption broken: {contradiction.description}\n"
            f"Metric: {contradiction.metric}\n"
            f"Breached values: {contradiction.values}\n"
            f"Action taken: {', '.join(s.name for s in revision.new_steps)}\n"
        )
        message = client.messages.create(
            model=self.model, max_tokens=200, messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(block, "text", "") for block in message.content).strip()
        return text or None


def build_reasoner() -> Reasoner:
    base = RuleBasedReasoner()
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
            return ClaudeReasoner(base=base)
        except ImportError:
            return base
    return base


class ReplanGovernor:
    """Safety valve on top of the world model: bounds how often, and for how
    long, the agent is allowed to keep changing its own plan. This is the
    containment mechanism for the flapping_noise failure test."""

    def __init__(self, cooldown_ticks: int = 2, max_replans_in_window: int = 2, window_ticks: int = 8):
        self.cooldown_ticks = cooldown_ticks
        self.max_replans_in_window = max_replans_in_window
        self.window_ticks = window_ticks
        self.replan_ticks: List[int] = []
        self.escalated = False

    def allow(self, tick: int) -> Tuple[bool, str]:
        if self.escalated:
            return False, "already escalated, waiting on a human before touching the plan again"

        if self.replan_ticks and (tick - self.replan_ticks[-1]) < self.cooldown_ticks:
            remaining = self.cooldown_ticks - (tick - self.replan_ticks[-1])
            return False, f"cooldown active, {remaining} tick(s) remaining since the last revision"

        recent = [t for t in self.replan_ticks if (tick - t) < self.window_ticks]
        if len(recent) >= self.max_replans_in_window:
            self.escalated = True
            return False, (f"{len(recent)} revisions happened within the last {self.window_ticks} ticks, "
                             f"which looks like thrash, not signal, so I am escalating instead of revising again")

        return True, ""

    def record(self, tick: int) -> None:
        self.replan_ticks.append(tick)


# ---------------------------------------------------------------------------
# Persistence: every observation and revision is journaled to SQLite
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    scenario TEXT, agent TEXT, tick INTEGER, payload TEXT,
    PRIMARY KEY (scenario, agent, tick)
);
CREATE TABLE IF NOT EXISTS revisions (
    scenario TEXT, agent TEXT, tick INTEGER, from_version INTEGER, to_version INTEGER,
    reason TEXT, evidence TEXT
);
CREATE TABLE IF NOT EXISTS run_state (
    scenario TEXT, agent TEXT, plan_version INTEGER, cursor INTEGER, escalated INTEGER,
    PRIMARY KEY (scenario, agent)
);
"""


class Journal:
    """SQLite-backed run journal. Every observation and every revision is
    written to disk as it happens and the agent's cursor is checkpointed
    after each tick, so a run's state survives the process exiting and can
    be inspected or resumed afterwards, not just replayed from memory."""

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def log_observation(self, scenario: str, agent: str, tick: int, payload: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO observations (scenario, agent, tick, payload) VALUES (?, ?, ?, ?)",
            (scenario, agent, tick, json.dumps(payload)),
        )
        self.conn.commit()

    def log_revision(self, scenario: str, agent: str, tick: int, from_version: int, to_version: int,
                       reason: str, evidence: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO revisions (scenario, agent, tick, from_version, to_version, reason, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (scenario, agent, tick, from_version, to_version, reason, json.dumps(evidence)),
        )
        self.conn.commit()

    def save_state(self, scenario: str, agent: str, plan_version: int, cursor: int, escalated: bool) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO run_state (scenario, agent, plan_version, cursor, escalated) "
            "VALUES (?, ?, ?, ?, ?)",
            (scenario, agent, plan_version, cursor, int(escalated)),
        )
        self.conn.commit()

    def load_state(self, scenario: str, agent: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT plan_version, cursor, escalated FROM run_state WHERE scenario = ? AND agent = ?",
            (scenario, agent),
        ).fetchone()
        if row is None:
            return None
        return {"plan_version": row[0], "cursor": row[1], "escalated": bool(row[2])}

    def revisions_for(self, scenario: str, agent: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT tick, from_version, to_version, reason, evidence FROM revisions "
            "WHERE scenario = ? AND agent = ? ORDER BY tick",
            (scenario, agent),
        ).fetchall()
        return [{"tick": t, "from_version": fv, "to_version": tv, "reason": r, "evidence": json.loads(e)}
                for t, fv, tv, r, e in rows]

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# Scenarios: each one is just an Environment.tick(...) function, pushing one
# observation per tick. The agent reacts to whatever arrives; there is no
# polling on a fixed timer, and a quiet environment produces zero re-plans.
# ---------------------------------------------------------------------------


class Environment(Protocol):
    def tick(self, t: int, stage_pct: int, rolled_back: bool, paused: bool) -> Observation: ...


class CanaryErrorSpikeEnv:
    """A defect in the new version only shows up once real traffic hits it.
    A static agent rides the bug to 100% of traffic; an adaptive agent rolls
    back after two consecutive bad readings, before that happens."""

    BUG_STAGE_THRESHOLD = 25
    BASE_ERROR_RATE = 0.4
    BASE_LATENCY_MS = 180

    def __init__(self) -> None:
        self._exposure = 0

    def tick(self, t: int, stage_pct: int, rolled_back: bool, paused: bool) -> Observation:
        if rolled_back or stage_pct < self.BUG_STAGE_THRESHOLD:
            self._exposure = 0
            error_rate = self.BASE_ERROR_RATE
        else:
            self._exposure += 1
            error_rate = min(1.0 + (stage_pct / 100) * 2 + self._exposure * 1.15, 9.5)
        return {"error_rate_pct": round(error_rate, 2), "p99_latency_ms": self.BASE_LATENCY_MS,
                "dependency_health": "healthy", "freeze_active": False}


class DependencyIncidentEnv:
    """A shared downstream dependency degrades for reasons unrelated to this
    rollout. error_rate_pct and p99_latency_ms stay nominal; only
    dependency_health flips. Rolling back would not fix anything here, the
    correct move is to pause and wait, then resume once it recovers."""

    DEGRADE_AT_TICK = 3
    RECOVER_AT_TICK = 8

    def tick(self, t: int, stage_pct: int, rolled_back: bool, paused: bool) -> Observation:
        degraded = self.DEGRADE_AT_TICK <= t < self.RECOVER_AT_TICK
        return {"error_rate_pct": 0.4, "p99_latency_ms": 180,
                "dependency_health": "degraded" if degraded else "healthy", "freeze_active": False}


class FreezeWindowEnv:
    """A change freeze gets declared mid-plan, a fact that simply did not
    exist when the plan was made. The service is healthy the whole time; the
    correct move is to hold the current stage, not roll back."""

    FREEZE_START_TICK = 3
    FREEZE_END_TICK = 7

    def tick(self, t: int, stage_pct: int, rolled_back: bool, paused: bool) -> Observation:
        freeze_active = self.FREEZE_START_TICK <= t < self.FREEZE_END_TICK
        return {"error_rate_pct": 0.4, "p99_latency_ms": 180,
                "dependency_health": "healthy", "freeze_active": freeze_active}


class FlappingNoiseEnv:
    """The failure-test scenario (see FAILURE_TEST.md). error_rate_pct
    bounces above and below the ceiling every other tick from telemetry
    jitter, never two ticks in a row. A re-planner with no hysteresis
    thrashes on every blip; the real WorldModel requires two consecutive
    breaches, so this scenario produces zero re-plans, which is correct."""

    LOW_READING = 0.3
    JITTER_READING = 1.3

    def tick(self, t: int, stage_pct: int, rolled_back: bool, paused: bool) -> Observation:
        error_rate = self.JITTER_READING if t % 2 == 1 else self.LOW_READING
        return {"error_rate_pct": error_rate, "p99_latency_ms": 180,
                "dependency_health": "healthy", "freeze_active": False}


class LatencyRegressionEnv:
    """p99 latency breaches its threshold and stays there, but no playbook is
    registered for the latency_stable assumption (only error_rate_stable,
    dependency_healthy, and no_deploy_freeze have one). This is deliberate:
    it proves the agent does not fabricate a remediation for a contradiction
    it was never taught how to handle. It halts and escalates to a human
    instead of guessing, which is the correct behavior for unknown territory."""

    REGRESS_AT_TICK = 3
    BAD_LATENCY_MS = 340

    def tick(self, t: int, stage_pct: int, rolled_back: bool, paused: bool) -> Observation:
        p99 = self.BAD_LATENCY_MS if t >= self.REGRESS_AT_TICK else 180
        return {"error_rate_pct": 0.4, "p99_latency_ms": p99,
                "dependency_health": "healthy", "freeze_active": False}


@dataclass
class Scenario:
    id: str
    title: str
    summary: str
    ticks: int
    make_environment: Callable[[], Environment]


SCENARIOS: Dict[str, Scenario] = {
    s.id: s for s in (
        Scenario(
            id="canary_error_spike", title="Canary error spike",
            summary=("A defect in v2.4.0 only manifests once it is serving real traffic at 25% "
                       "and above. The plan assumed error_rate_pct stays <= 1.0%. A static agent "
                       "rides the bug to 100% of traffic; an adaptive agent rolls back after two "
                       "consecutive bad readings, before that happens."),
            ticks=9, make_environment=CanaryErrorSpikeEnv,
        ),
        Scenario(
            id="dependency_incident", title="Unrelated dependency incident",
            summary=("The payments-ledger-db dependency degrades midway through the rollout for "
                       "reasons unrelated to v2.4.0 (error rate and latency stay nominal). An "
                       "adaptive agent pauses instead of rolling back, then resumes automatically "
                       "once the dependency recovers. A static agent keeps ramping traffic onto "
                       "the new version while the dependency is already struggling."),
            ticks=14, make_environment=DependencyIncidentEnv,
        ),
        Scenario(
            id="freeze_window", title="Unannounced deploy freeze",
            summary=("A change freeze gets declared partway through the rollout, a fact that did "
                       "not exist when the plan was made. The service is healthy the entire time. "
                       "An adaptive agent holds its current stage for the freeze window and "
                       "resumes once it lifts; a static agent has no freeze signal to check and "
                       "keeps advancing, which is a compliance violation, not an outage."),
            ticks=11, make_environment=FreezeWindowEnv,
        ),
        Scenario(
            id="flapping_noise", title="Telemetry jitter (failure test)",
            summary=("error_rate_pct bounces above and below the ceiling every other tick due to "
                       "sampling jitter, never two ticks in a row. This is the adaptation-goes-wrong "
                       "case: without hysteresis, a re-planner thrashes on every blip; with it, zero "
                       "re-plans fire, which is correct."),
            ticks=12, make_environment=FlappingNoiseEnv,
        ),
        Scenario(
            id="latency_regression_unknown", title="Latency regression with no playbook",
            summary=("p99 latency breaches its threshold and stays there, but no assumption about "
                       "latency has a registered remediation, only error rate, dependency health, "
                       "and freeze status do. Rather than guess, the adaptive agent halts and "
                       "escalates to a human. A static agent has no way to notice at all and rides "
                       "the regression all the way to 100% of traffic."),
            ticks=9, make_environment=LatencyRegressionEnv,
        ),
    )
}


# ---------------------------------------------------------------------------
# The loop: plan -> execute -> observe -> revise, for both agent types
# ---------------------------------------------------------------------------


@dataclass
class TraceEntry:
    tick: int
    observation: Observation
    stage_pct: int
    note: str


@dataclass
class RunResult:
    scenario_id: str
    agent_name: str
    trace: List[TraceEntry]
    final_state: RunState
    outcome: str  # OK | OUTAGE | COMPLIANCE_VIOLATION | ESCALATED
    revisions: List[Revision]
    escalated: bool


def _apply_step_action(step: Step, state: RunState) -> None:
    if step.action == "advance":
        state.stage_pct = step.stage_pct
        state.paused = False
    elif step.action == "rollback":
        state.rolled_back = True
        state.stage_pct = 0
        state.paused = False
    elif step.action in ("pause", "escalate"):
        state.paused = True
    elif step.action in ("open_incident", "cleanup"):
        pass
    else:
        raise ValueError(f"unknown step action: {step.action}")


def run_static(scenario: Scenario, journal: Optional[Journal] = None) -> RunResult:
    """The non-adaptive baseline: executes the original plan on a fixed
    dwell schedule and never looks at the observations it receives."""
    plan = build_plan()
    state = RunState()
    env = scenario.make_environment()
    trace: List[TraceEntry] = []
    worst_error_at_high_stage = 0.0
    compliance_violation = False

    for t in range(scenario.ticks):
        obs = env.tick(t, state.stage_pct, state.rolled_back, state.paused)
        if journal:
            journal.log_observation(scenario.id, "static", t, obs)

        step = plan.current_step()
        note = "plan complete, holding"
        if step is not None:
            if step.action == "advance" and obs.get("freeze_active") and step.stage_pct != state.stage_pct:
                compliance_violation = True
            _apply_step_action(step, state)
            plan.ticks_on_current_step += 1
            note = f"executed '{step.name}' (never checked assumptions)"
            if plan.ticks_on_current_step >= step.dwell:
                plan.cursor += 1
                plan.ticks_on_current_step = 0

        if state.stage_pct >= 50:
            worst_error_at_high_stage = max(worst_error_at_high_stage, obs.get("error_rate_pct", 0.0))

        trace.append(TraceEntry(tick=t, observation=obs, stage_pct=state.stage_pct, note=note))
        if journal:
            journal.save_state(scenario.id, "static", plan.version, plan.cursor, False)

    outcome = "OUTAGE" if worst_error_at_high_stage > 5.0 else ("COMPLIANCE_VIOLATION" if compliance_violation else "OK")
    return RunResult(scenario_id=scenario.id, agent_name="static", trace=trace, final_state=state,
                       outcome=outcome, revisions=[], escalated=False)


def run_adaptive(scenario: Scenario, reasoner: Optional[Reasoner] = None,
                   journal: Optional[Journal] = None, hysteresis: bool = True) -> RunResult:
    """The adaptive loop: observe every tick, check assumptions, and only
    re-plan when a contradiction survives the hysteresis and cooldown
    guards. `hysteresis=False` is only used by --selftest to reproduce the
    naive, ungoverned failure mode documented in FAILURE_TEST.md."""
    assumptions = build_assumptions()
    if not hysteresis:
        assumptions = [Assumption(id=a.id, description=a.description, metric=a.metric, holds=a.holds,
                                     breaches_to_trigger=1, recoveries_to_clear=1) for a in assumptions]

    plan = build_plan()
    state = RunState()
    env = scenario.make_environment()
    world_model = WorldModel(assumptions)
    governor = ReplanGovernor(cooldown_ticks=(2 if hysteresis else 0),
                                max_replans_in_window=(2 if hysteresis else 10 ** 9))
    reasoner = reasoner or build_reasoner()

    trace: List[TraceEntry] = []
    revisions: List[Revision] = []
    stash: Dict[str, List[Step]] = {}
    worst_error_at_high_stage = 0.0
    compliance_violation = False

    for t in range(scenario.ticks):
        obs = env.tick(t, state.stage_pct, state.rolled_back, state.paused)
        if journal:
            journal.log_observation(scenario.id, "adaptive", t, obs)

        observed = world_model.observe(t, obs)
        notes: List[str] = []

        for aid in observed.recoveries:
            if aid in stash:
                plan = Plan(version=plan.version + 1, steps=stash.pop(aid),
                             reason=f"resuming: {aid} recovered", cursor=0)
                notes.append(f"resumed because '{aid}' held for "
                              f"{world_model.states[aid].assumption.recoveries_to_clear} consecutive observations")

        for c in observed.contradictions:
            allowed, why = governor.allow(t)
            if not allowed:
                notes.append(f"contradiction on '{c.assumption_id}' noted but suppressed ({why})")
                continue
            revision = reasoner.propose(plan, c, t)
            governor.record(t)
            revisions.append(revision)
            if journal:
                journal.log_revision(scenario.id, "adaptive", t, revision.from_version,
                                       revision.to_version, revision.reason, revision.evidence())
            if revision.new_steps and revision.new_steps[0].action == "pause":
                stash[c.assumption_id] = revision.new_steps[1:]
                plan = Plan(version=revision.to_version, steps=[revision.new_steps[0]],
                             reason=revision.reason, cursor=0)
            else:
                plan = Plan(version=revision.to_version, steps=revision.new_steps,
                             reason=revision.reason, cursor=0)
            notes.append(f"I changed my mind because {revision.reason}")

        if governor.escalated and (plan.current_step() is None or plan.current_step().action != "escalate"):
            revision = Revision(tick=t, from_version=plan.version, to_version=plan.version + 1,
                                  reason="repeated contradictions in a short window, "
                                          "freezing autonomous re-planning",
                                  new_steps=escalate_steps("repeated contradictions"))
            revisions.append(revision)
            if journal:
                journal.log_revision(scenario.id, "adaptive", t, revision.from_version,
                                       revision.to_version, revision.reason, {})
            plan = Plan(version=revision.to_version, steps=revision.new_steps, reason=revision.reason, cursor=0)
            notes.append(f"I changed my mind because {revision.reason}")

        step = plan.current_step()
        if step is not None:
            if step.action == "advance" and obs.get("freeze_active") and step.stage_pct != state.stage_pct:
                compliance_violation = True  # should not trigger for the adaptive agent; kept for symmetry
            _apply_step_action(step, state)
            plan.ticks_on_current_step += 1
            notes.append(f"executed '{step.name}'")
            if plan.ticks_on_current_step >= step.dwell:
                plan.cursor += 1
                plan.ticks_on_current_step = 0
        else:
            notes.append("plan complete, holding")

        if state.stage_pct >= 50:
            worst_error_at_high_stage = max(worst_error_at_high_stage, obs.get("error_rate_pct", 0.0))

        trace.append(TraceEntry(tick=t, observation=obs, stage_pct=state.stage_pct, note="; ".join(notes)))
        if journal:
            journal.save_state(scenario.id, "adaptive", plan.version, plan.cursor, governor.escalated)

    human_escalated = governor.escalated or any(
        r.new_steps and r.new_steps[0].action == "escalate" for r in revisions
    )

    if human_escalated:
        outcome = "ESCALATED"
    elif worst_error_at_high_stage > 5.0:
        outcome = "OUTAGE"
    elif compliance_violation:
        outcome = "COMPLIANCE_VIOLATION"
    else:
        outcome = "OK"

    return RunResult(scenario_id=scenario.id, agent_name="adaptive", trace=trace, final_state=state,
                       outcome=outcome, revisions=revisions, escalated=human_escalated)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_trace(result: RunResult) -> None:
    print(f"\n--- {result.agent_name} agent: {result.scenario_id} ---")
    for entry in result.trace:
        marker = "  "
        if "I changed my mind" in entry.note or "resumed because" in entry.note:
            marker = ">>"
        print(f"{marker} t={entry.tick:>2}  stage={entry.stage_pct:>3}%  "
               f"obs={entry.observation}  {entry.note}")
    print(f"outcome: {result.outcome}"
           + (f"  ({len(result.revisions)} revision(s))" if result.agent_name == "adaptive" else ""))


def _run_one(scenario_id: str, agent: str, journal_dir: Optional[str], hysteresis: bool = True) -> None:
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        available = ", ".join(sorted(SCENARIOS))
        raise SystemExit(f"unknown scenario '{scenario_id}', available: {available}")

    journal = Journal(str(Path(journal_dir) / f"{scenario_id}.db")) if journal_dir else None
    try:
        print(f"\n=== {scenario.title} ===\n{scenario.summary}")
        if not hysteresis:
            print("(--no-hysteresis: reproducing the ungoverned failure mode, see FAILURE_TEST.md)")
        if agent in ("adaptive", "both"):
            _print_trace(run_adaptive(scenario, journal=journal, hysteresis=hysteresis))
        if agent in ("static", "both"):
            _print_trace(run_static(scenario, journal=journal))
    finally:
        if journal:
            journal.close()


# ---------------------------------------------------------------------------
# Self-test: run this with --selftest. Also importable and runnable under
# `python -m unittest adaptive_agent` since every check is a plain assert
# inside a function named test_*.
# ---------------------------------------------------------------------------


def test_hysteresis_ignores_single_blip() -> None:
    wm = WorldModel(build_assumptions())
    obs_ok = {"error_rate_pct": 0.4, "p99_latency_ms": 180, "dependency_health": "healthy", "freeze_active": False}
    obs_blip = {**obs_ok, "error_rate_pct": 1.3}
    assert wm.observe(0, obs_ok).contradictions == []
    result = wm.observe(1, obs_blip)
    assert result.contradictions == [], "a single breach must not fire a contradiction"
    result2 = wm.observe(2, obs_ok)
    assert result2.contradictions == [] and result2.recoveries == [], "assumption never broke, nothing to recover"


def test_hysteresis_fires_on_two_consecutive_breaches() -> None:
    wm = WorldModel(build_assumptions())
    obs_ok = {"error_rate_pct": 0.4, "p99_latency_ms": 180, "dependency_health": "healthy", "freeze_active": False}
    obs_bad = {**obs_ok, "error_rate_pct": 3.0}
    assert wm.observe(0, obs_bad).contradictions == []
    fired = wm.observe(1, obs_bad).contradictions
    assert len(fired) == 1 and fired[0].assumption_id == "error_rate_stable"


def test_canary_error_spike_adaptive_rolls_back_before_static_outage() -> None:
    scenario = SCENARIOS["canary_error_spike"]
    adaptive = run_adaptive(scenario)
    static = run_static(scenario)
    assert adaptive.outcome == "OK", adaptive.outcome
    assert adaptive.final_state.rolled_back is True
    assert any("I changed my mind" in "".join(e.note) for e in adaptive.trace)
    assert static.outcome == "OUTAGE", static.outcome
    assert static.final_state.stage_pct == 100


def test_dependency_incident_pauses_then_resumes_without_rollback() -> None:
    scenario = SCENARIOS["dependency_incident"]
    adaptive = run_adaptive(scenario)
    assert adaptive.final_state.rolled_back is False, "must not roll back for a problem it did not cause"
    assert any(r.new_steps and r.new_steps[0].action == "pause" for r in adaptive.revisions), (
        "the revision for a dependency outage must pause, not roll back or escalate"
    )
    assert any("resumed because" in e.note for e in adaptive.trace), "must resume once the dependency recovers"
    assert adaptive.final_state.stage_pct == 100, "rollout should complete once it is safe to"


def test_freeze_window_holds_stage_and_flags_static_as_violation() -> None:
    scenario = SCENARIOS["freeze_window"]
    adaptive = run_adaptive(scenario)
    static = run_static(scenario)
    assert adaptive.outcome == "OK"
    assert static.outcome == "COMPLIANCE_VIOLATION"


def test_latency_regression_escalates_instead_of_guessing() -> None:
    scenario = SCENARIOS["latency_regression_unknown"]
    adaptive = run_adaptive(scenario)
    static = run_static(scenario)
    assert adaptive.outcome == "ESCALATED", adaptive.outcome
    assert any("no playbook covers it" in r.reason for r in adaptive.revisions), (
        "must admit it has no remediation rather than inventing one"
    )
    assert adaptive.final_state.rolled_back is False, "must not roll back for a problem with no matching playbook"
    assert any("halt auto re-planning and escalate" in e.note for e in adaptive.trace)
    assert static.final_state.stage_pct == 100, "the static baseline has no way to notice and rides it to 100%"


def test_flapping_noise_is_contained_with_hysteresis() -> None:
    scenario = SCENARIOS["flapping_noise"]
    contained = run_adaptive(scenario, hysteresis=True)
    assert len(contained.revisions) == 0, "sensor jitter alone must never trigger a re-plan"
    assert contained.outcome == "OK"


def test_flapping_noise_thrashes_without_hysteresis() -> None:
    scenario = SCENARIOS["flapping_noise"]
    naive = run_adaptive(scenario, hysteresis=False)
    assert len(naive.revisions) >= 3, (
        "this asserts the failure mode itself: a re-planner with no hysteresis and no "
        "governor reacts to every blip and thrashes, which is exactly what containment "
        "in the real agent (hysteresis + cooldown + escalation) prevents"
    )


def test_persistence_round_trips_state(tmp_path: Optional[str] = None) -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        db_path = str(Path(d) / "test.db")
        journal = Journal(db_path)
        journal.save_state("s", "adaptive", plan_version=2, cursor=3, escalated=False)
        journal.log_revision("s", "adaptive", tick=1, from_version=1, to_version=2,
                                reason="because", evidence={"metric": "x"})
        journal.close()

        reopened = Journal(db_path)
        state = reopened.load_state("s", "adaptive")
        assert state == {"plan_version": 2, "cursor": 3, "escalated": False}
        revisions = reopened.revisions_for("s", "adaptive")
        assert len(revisions) == 1 and revisions[0]["reason"] == "because"
        reopened.close()


def _selftest() -> int:
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"ok   {test.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {test.__name__}: {e}")
        except Exception as e:  # pragma: no cover - defensive
            failures += 1
            print(f"ERROR {test.__name__}: {e!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="The Adaptive Agent: plan, execute, observe, revise.")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), help="run one scenario")
    parser.add_argument("--agent", choices=["adaptive", "static", "both"], default="both")
    parser.add_argument("--all", action="store_true", help="run every scenario")
    parser.add_argument("--list", action="store_true", help="list available scenarios")
    parser.add_argument("--journal-dir", default="runs", help="directory for the SQLite journal (default: runs)")
    parser.add_argument("--no-journal", action="store_true", help="do not persist a journal for this run")
    parser.add_argument("--no-hysteresis", action="store_true",
                          help="disable the hysteresis/governor containment and reproduce the "
                               "ungoverned failure mode (see FAILURE_TEST.md); only affects the "
                               "adaptive agent")
    parser.add_argument("--selftest", action="store_true", help="run the built-in test suite and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    if args.list or not (args.scenario or args.all):
        print("available scenarios:")
        for s in SCENARIOS.values():
            print(f"  {s.id:<22} {s.title}")
        if not (args.scenario or args.all):
            print("\npass --scenario <id> or --all to run one")
        return 0

    journal_dir = None if args.no_journal else args.journal_dir
    hysteresis = not args.no_hysteresis
    if args.all:
        for scenario_id in SCENARIOS:
            _run_one(scenario_id, "both", journal_dir, hysteresis=hysteresis)
    else:
        _run_one(args.scenario, args.agent, journal_dir, hysteresis=hysteresis)
    return 0


if __name__ == "__main__":
    sys.exit(main())
