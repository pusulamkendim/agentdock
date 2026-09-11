# AgentDock v0.10 — Supervised Mission Control

AgentDock is a local multi-agent control plane for Codex. A strong orchestrator (Sol/Astra when available) proposes a task graph; worker agents (typically Luna) execute approved tasks in isolated Git worktrees. The normal path reuses the existing ChatGPT-authenticated Codex CLI and requires no API key.

## What changed in v0.9

### Plan first, execution second

Creating a mission no longer means workers can start immediately.

```text
Mission prompt
   ↓
Orchestrator planning
   ↓
PLAN REVIEW  ← you inspect everything here
   ↓
Approve plan
   ↓
Start mission
   ↓
Parallel workers
```

The Plan Review screen shows every task's:

- title and objective;
- dependencies;
- in-scope / out-of-scope boundaries;
- allowed paths;
- proposed worker profile;
- read-only vs workspace-write access;
- current status.

Before approval you can reassign any task to another Agent Profile and change its access mode. If you edit an already-approved assignment, the mission automatically returns to `planned` and must be approved again.

### Explicit user approval gate

A real or demo mission moves through:

```text
planning → planned → approved → running → done / attention
```

`Start mission` is unavailable until the plan has been explicitly approved.

### Classic Working state

Running cards now keep the familiar Codex-style status visible:

```text
● Working (2m 14s · stop to interrupt)
```

Recent terminal/activity lines continue below it.

### Manual control on every worker

Each worker card exposes:

- **Message** — continue the same agent conversation;
- **Stop** — terminate a running Codex process;
- **Terminal** — open macOS Terminal in that task's worktree;
- **Inspect** — Activity / Diff / Contract / Raw.

If a worker is currently inside a `codex exec` turn, a message is not discarded. It is recorded as **queued** and automatically delivered as the next turn on the same Codex thread before the task is finalized/integrated. The UI clearly labels this behavior rather than pretending the CLI supports mid-turn steering.

The Inspector keeps a small user-message history with `queued / sending / delivered / failed` status.

### Talk to the orchestrator

The orchestrator Inspector now has Manual Control too. You can ask questions or request a recommendation while reviewing the plan. Agent assignment remains an explicit UI action in Plan Review.

### Demo mode follows the same approval flow

`Preview mission · no quota` now stops at Plan Review rather than auto-running the fake swarm. This lets you test:

- task review;
- assignment changes;
- explicit approval;
- Start mission;
- classic Working state;
- parallel workers;
- queued user messages;
- Stop;
- attention state;

without using Codex quota.

## What changed in v0.10

- Planner output is requested with a JSON Schema and the available Codex model catalog is detected from the installed CLI.
- Duplicate mission starts are rejected and missions interrupted by a server restart move to `attention` for an explicit retry.
- Worker changes are checked against the task contract's `allowed_paths` before they can be integrated.
- Write missions stop at `awaiting_apply` so you can inspect the final integration diff and click **Apply changes**.
- SQLite uses WAL/foreign-key enforcement, and the repository includes a standard-library unittest smoke suite.

## Quick start

```bash
./run-agentdock.sh
```

Or run the server directly:

```bash
python3 agentdock.py
```

Run the local checks with:

```bash
python3 -m unittest discover -s tests -v
```

## Core data model

```text
Workspace (repository)
  └── Mission
       └── Task (execution contract)
            ├── Agent assignment
            ├── Agent sessions / Codex thread
            ├── Structured Codex events
            └── User follow-up messages
```

Runtime state:

```text
~/.agentdock/agentdock.sqlite3
```

Human-readable mission mirror:

```text
~/.agentdock/missions/<mission-id>/
├── MISSION.md
├── PLAN.md
├── PREFLIGHT.md
├── FINAL.md
├── events.jsonl
└── tasks/TASK-xxx.md
```

## Workspaces and screenshots

Add a repository with the native macOS **Choose folder…** picker. Mission and manual-control composers accept screenshots by drag/drop or clipboard `⌘V`.

## Running on macOS

```bash
codex --version
codex login status
git --version
python3 --version
```

Then:

```bash
pkill -f "agentdock.py" 2>/dev/null || true
cd ~/Downloads
unzip agentdock-v09-supervised-control.zip
cd agentdock
python3 agentdock.py
```

Open `http://127.0.0.1:8765` if needed.

## Recommended quota-free test

1. Open **New mission**.
2. Choose your test workspace.
3. Click **Preview mission · no quota**.
4. Wait until the mission becomes `planned`.
5. In **Plan Review**, expand/inspect the four task contracts.
6. Reassign one task to a different Agent Profile.
7. Click **Approve plan**.
8. Confirm the mission says `approved · waiting to start` and no worker is running.
9. Click **Start mission**.
10. When two workers are running, confirm both show `Working (...)`.
11. Open one worker and send a message while it is Working; confirm the message shows `queued`, then `delivered` on the next turn.
12. Open another worker's worktree with **Terminal** (real macOS missions only; demo opens the root workspace).
13. Run a second demo and press **Stop** on a worker; the mission should end in `attention`, not silently retry it.

## Safety boundaries

AgentDock never blanket-runs `git clean -fd`, resets tracked user work, or deletes unknown files. Write workers remain isolated in Git worktrees. Unknown product/architecture decisions are escalated rather than guessed.
