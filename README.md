# AgentDock v0.13 — Supervised Mission Control

AgentDock is a local multi-agent control plane for Codex. The orchestrator first decides what the mission needs; it creates worker tasks only when real execution is required. Write workers execute approved tasks in isolated Git worktrees. The normal path reuses the existing ChatGPT-authenticated Codex CLI and requires no API key.

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
- **Pause / Resume** — interrupt or continue the same worker conversation;
- **Terminal** — open macOS Terminal in that task's worktree;
- **Inspect** — Activity / Diff / Contract / Raw.

If a worker is currently inside a `codex exec` turn, a message is not discarded. It is recorded as **queued** and automatically delivered as the next turn on the same Codex thread before the task is finalized/integrated. The UI clearly labels this behavior rather than pretending the CLI supports mid-turn steering.

The Inspector presents user messages, agent replies, tool activity and control events in one chronological timeline. User messages retain `queued / sending / delivered / failed` status.

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

## What changed in v0.11

### Disposition before task creation

Every mission now follows a disposition-first control flow:

```text
Mission
   ↓
Read-only workspace snapshot
   ↓
Orchestrator disposition
   ├── already_satisfied  → no tasks; show evidence
   ├── answer_only        → no tasks; return the answer
   ├── needs_user_input   → no tasks; wait for a decision
   ├── blocked            → no tasks; explain the safety/authority block
   └── execute            → create the smallest necessary task graph
```

The planner contract is strict and bounded to zero through twelve tasks. An execution disposition requires at least one task; all other dispositions require zero tasks. A simple change can therefore be one task, while independent work can still be represented as a parallel graph.

No-task missions never enter execution preflight or create misleading queued worker cards. The mission view shows **No execution needed**, the final response, the evidence, and friendly actions to create a plan anyway, ask the orchestrator to reconsider, or run verification again.

### Deterministic workspace analysis

Before the planner is called, AgentDock records a read-only workspace snapshot containing Git classification (`NOT_GIT`, `LOCAL_GIT`, or `GIT_WITH_REMOTE`), repository root, branch, HEAD, upstream, remotes, tracked/untracked changes, and write-safety facts. This lets a Git verification mission finish with zero tasks when the repository already satisfies the request.

### Read-only preflight and explicit recovery

Preflight reports state but does not delete files, edit `.git/info/exclude`, remove locks, or prune worktree metadata automatically. If a write mission encounters existing user changes, the UI shows the affected paths and asks for an explicit choice: add selected files to Git, ignore them locally, move untracked files to AgentDock's preservation area, continue read-only when valid, or cancel. The chosen scope is shown before the action and no commit is created automatically.

The Activity view renders workspace analysis, disposition, reasoning summaries, task dependencies, commands, file changes, preflight, and user decisions as readable events. Structured planner JSON remains available in **Raw** for diagnostics.

### Optional Codex App Server transport

The default `codex exec --json` transport remains available. To test the opt-in App Server adapter:

```bash
AGENTDOCK_CODEX_TRANSPORT=app-server python3 agentdock.py
```

The adapter records thread, turn, plan, reasoning, command, file-change, and completion events while preserving AgentDock's approval and workspace safety gates. See the [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server).

## What changed in v0.12

### One orchestrator conversation per mission

Each mission has one durable, mission-scoped orchestrator thread. Initial disposition, manual orchestrator messages, worker consultations, recovery decisions, checkpoint summaries, and final synthesis all resume that same conversation. A failed resume moves the mission to attention; AgentDock never silently starts a second orchestrator thread. A new generation is created only through the explicit **Reconstruct context** action for legacy or unrecoverable missions.

Workers keep their own durable `worker_thread_id`. When a worker reaches a reserved product, architecture, scope, factual, or safety decision, it returns a structured consultation. The orchestrator processes consultations serially per mission and either sends a bounded handoff to the same worker, revises its contract, asks the user, or blocks the mission. Independent workers continue while only the affected worker and its dependents wait.

When user information is needed, the mission becomes `waiting_for_user` and the main mission view shows the question, evidence, options, free text, and image attachment controls. The answer is recorded before the orchestrator turn resumes; the updated contract and answer are then delivered to the same worker thread. Manual Control remains a separate orchestrator conversation action and is not used as a substitute for a pending execution answer.

### Baseline-based read-only safety

Read tasks compare a workspace fingerprint captured immediately before the task with the fingerprint after it. Existing staged, unstaged, and untracked user work is therefore a valid baseline. Only a new or changed path, content, index state, HEAD, or tracked diff fails the read task; no user change is automatically reverted. Git locks are warnings for read-only work and explicit blockers for writes.

### Restart and partial execution recovery

On restart, interrupted work moves to explicit attention while completed task checkpoints remain intact. Pending consultations are requeued from SQLite, completed read tasks are not rerun, and a persisted integration worktree is reused when safe. Partial read-only execution completes only independent read work; dependent write/test/review tasks and final synthesis wait for the next safe execution phase.

The durable coordination records live in `orchestrator_turns` and `consultations`, while the mission mirror includes the orchestrator thread, generation, turn status, pending questions, consultation history, and worker handoff messages.

## What changed in v0.13

### Mission controls and conversation continuity

- The original mission prompt is preserved in **Mission details** while the live header uses a short planner-generated title (with a deterministic fallback).
- Runtime settings can be changed from the mission bar for future orchestrator turns and remaining workers: model, reasoning, speed, and parallelism.
- Mission actions are status-aware: **Pause mission**, **Resume mission**, **Resume from issue**, **Reopen mission**, and **Restart as new mission**.
- Worker actions distinguish `paused_by_user` from permanent `cancelled`; resuming requires the persisted worker thread and never silently opens a replacement conversation.
- Activity and Conversation are one timeline. The Inspector and compact agent cards keep the newest meaningful activity at the bottom, preserve the user's scroll position, and expose a **New activity** jump when appropriate.

The control endpoints are:

```text
POST /api/mission-config/:plan_id
POST /api/pause-plan/:plan_id
POST /api/resume-plan/:plan_id
POST /api/pause-task/:task_id
POST /api/resume-task/:task_id
```

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

For an isolated installation or smoke test, override the state directory:

```bash
AGENTDOCK_STATE_ROOT=/tmp/agentdock-state ./run-agentdock.sh
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
unzip agentdock-v12-supervised-control.zip
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

AgentDock never blanket-runs `git clean -fd`, resets tracked user work, edits `.git/info/exclude`, removes Git locks, or deletes unknown files during preflight. Write workers remain isolated in Git worktrees. Unknown product/architecture decisions are escalated rather than guessed.
