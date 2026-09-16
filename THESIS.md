# Two-year thesis: adaptive planning in production agents

Within two years, the default unit of deployment for an agent will not be
a prompt or a plan, it will be a world model with a plan attached. Today
most production agents are graded on whether they produce a good plan.
That bar is already being cleared. The bar that is not being cleared is
noticing, cheaply and reliably, the moment a plan's assumptions stop
holding, and doing something bounded about it. That gap, not planning
quality, is what currently separates an impressive demo from a system
you trust to run unattended.

The shift will be architectural before it is a model capability. Teams
will stop treating "replan" as a fallback wired to a timer or a retry
loop, and start treating assumptions as first-class, typed, observable
objects that live independently of the plan and can be checked against
a real signal stream. Contradiction detection will move from implicit
(an agent silently fails a task) to explicit (an agent logs exactly
which belief broke, on what evidence, and why the response it chose
follows from that). That log is not a debugging nicety, it becomes the
audit trail regulators, on-call engineers, and the agents' own future
runs depend on.

The open problem is not detection, it is governance: how often an agent
is allowed to change its own plan, and what happens when the signal
itself is unreliable. Every adaptive system will eventually face its own
version of this project's `flapping_noise` case, a plausible-looking
contradiction that is actually noise. The teams that win will be the
ones who treat hysteresis, cooldowns, and human escalation as load
bearing infrastructure, not polish, because an agent that revises its
plan on every blip is not adaptive, it is just unstable in a way that
sounds reasonable one decision at a time.
