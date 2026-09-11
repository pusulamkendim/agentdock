# Refactor baseline

The modular refactor is frozen against commit `5b1c7e34a73ce44b2f017c9b755d536be2b5f580`.

Baseline command:

```text
python3 -m unittest discover -s tests -v
```

Baseline result on 2026-09-11: **37 tests passed, 0 failures, 0 errors**.

The refactor must preserve the following externally observable behavior:

- the existing SQLite schema and migration behavior;
- HTTP routes and response shapes;
- mission/task lifecycle state names;
- one orchestrator thread and worker-thread resume semantics;
- Git, worktree, fingerprint and preflight behavior;
- prompts and structured-output schemas;
- the static UI.

`agentdock.py` remains the executable and compatibility facade. New code is
owned by the focused modules under `agentdock/`; the facade bridge keeps legacy
imports and direct monkeypatch targets working during the transition.
