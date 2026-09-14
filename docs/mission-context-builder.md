# Mission Context Builder

## Goal

Give every mission-scoped orchestrator turn a deterministic, bounded snapshot
of persisted mission state. The same context contract is used for planning,
manual messages, worker consultations, recovery, contract revision,
checkpoints, reconstruction, and final synthesis.

## Context contract

```text
MISSION CONTEXT

MISSION
status: running
goal: ...
decision: execute

TASK GRAPH
TASK-001 Architect  done
TASK-002 Researcher done
TASK-003 Coder      running deps:1,2
TASK-004 Reviewer   pending deps:3

NEW RESULTS SINCE YOUR LAST TURN
TASK-001 Architect
result:
...

TASK-002 Researcher
result:
...

OPEN CONSULTATIONS
TASK-003
question: ...

INTEGRATION STATE
TASK-003: not integrated
apply: not ready
```

## Design decisions

- SQLite is the source of truth; the builder does not ask a model to infer
  lifecycle state.
- The builder is read-only and does not mutate mission, task, consultation, or
  integration state.
- The task graph is always included so dependencies and ownership remain
  explicit.
- Worker outputs are included only when they became terminal after the latest
  completed orchestrator turn. Reconstruction includes all terminal outputs.
- Open consultations and integration/apply state are always current.
- Worker result text is explicitly marked as untrusted evidence so embedded
  instructions cannot silently become control-plane authority.
- Mission goals and worker results are bounded per item and in aggregate.
- Turn-specific input remains separate from generated mission context.
- The generated context is persisted in `orchestrator_turns.context_json` for
  auditability and restart diagnosis.

## Ownership

- `agentdock/mission_context.py`: snapshot queries, delta selection, bounding,
  and rendering.
- `agentdock/orchestrator.py`: injects the generated block through the single
  mission orchestrator gateway.
- Callers continue supplying only purpose-specific input; they do not rebuild
  mission state independently.

## Acceptance criteria

- Every orchestrator turn contains `MISSION`, `TASK GRAPH`, `NEW RESULTS SINCE
  YOUR LAST TURN`, `OPEN CONSULTATIONS`, and `INTEGRATION STATE`.
- Initial planning works with an empty graph.
- New terminal worker results appear after the latest orchestrator checkpoint;
  older results are not resent during normal continuation.
- Reconstruction receives all terminal task results.
- Open worker questions survive restart and are represented in context.
- Write task integration and mission apply status are explicit.
- Context size remains bounded and deterministic.
- Existing orchestrator thread continuity and all lifecycle states remain
  unchanged.
