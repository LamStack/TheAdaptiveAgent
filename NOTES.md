# Notes

## AI tools used

Built with Claude Code (Anthropic). The core engine, scenarios, and
self-test suite were written and iterated on in-session; nothing here
was pasted in from an external generator without being read, run, and
verified against a failing test first. The `ClaudeReasoner` wrapper is
also a working integration point, not just a mention: when
`ANTHROPIC_API_KEY` is set, it calls the Claude API to restate the
deterministic revision's reason in plainer language, and falls back
silently to the deterministic text on any failure.

## Key decisions

- **Assumptions are typed and independent of the plan**, not comments or
  inline conditionals in a step. `WorldModel` tracks their state
  (breach/recovery counts) separately from `Plan.cursor`, so a revised
  plan can depend on the same live beliefs without redeclaring them.
- **Hysteresis over instant reaction.** An assumption only counts as
  broken after two consecutive contradicting observations, by default.
  This is the single mechanism responsible for the difference between
  the contained and uncontained runs in `FAILURE_TEST.md`.
- **A governor is a second, independent guardrail**, not a duplicate of
  hysteresis. Hysteresis filters noisy signals; the governor caps how
  often even legitimate-looking signals are allowed to change the plan,
  and escalates to a human when that cap is hit. Two different failure
  modes, two different mechanisms.
- **No playbook means escalate, not guess.** `latency_regression_unknown`
  exists specifically to prove this path is real: a contradiction with no
  registered remediation produces an explicit escalation, not a
  fabricated fix.
- **SQLite over an in-memory log.** Every observation and revision is
  written as it happens, not buffered and flushed at the end, so a crash
  mid-run still leaves an inspectable trace.
- **A static baseline runs the identical plan and identical observation
  stream.** The before/after in every scenario is a controlled
  comparison, not a narrated claim.
- **Python standard library only for the core engine.** No dependencies
  to install to run the demo, the scenarios, or the self-test; the one
  optional dependency (`anthropic`) only changes the wording of a trace
  line, never the agent's behavior.

## Out of scope

- **Real external systems.** The tool calls (`advance`, `rollback`,
  `pause`, and so on) are simulated state transitions, not calls to a
  real deployment API, ticketing system, or paging service. The
  interfaces (`Environment`, `Reasoner`, `Journal`) are designed so a
  real integration would replace one class, not the loop.
- **LLM-driven plan revision.** `RuleBasedReasoner` is deterministic by
  design, so the demo and self-test are reproducible offline without an
  API key. `ClaudeReasoner` shows where an LLM plugs in (narration
  today, could own more of the reasoning in a follow-up) without making
  the default path depend on a network call.
- **Multi-agent coordination.** This is a single agent adapting a single
  plan. Coordinating contradictions and revisions across multiple
  cooperating agents is a real, harder problem and is not addressed
  here.
- **A persistent web UI.** The engine persists to SQLite and is driven
  from a CLI; the published live demo is a separate, browser-based
  visualization of the same plan, execute, observe, revise loop, not a
  hosted deployment of `adaptive_agent.py` itself.
