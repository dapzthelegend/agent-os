# agentic-os

**A governance layer for autonomous AI agents.**

agentic-os is the backend that turns autonomous agents into a governed, observable system. It owns durable task state, policy enforcement, human approval gates, execution contracts, and a full audit trail — so agents execute reliably and operators retain control.

> *"Agents execute; the backend decides."*

Read the design story: [Building a Multi-Agent OS: Key Design Decisions That Matter](https://pub.towardsai.net/building-a-multi-agent-os-key-design-decisions-that-matter-d07c3e7ae30c) (Towards AI, April 2026)

---

## Why Agent Governance Matters

Most agent frameworks focus on *making agents work*. agentic-os focuses on *making agents accountable*.

When autonomous agents operate across code repositories, communication channels, and business workflows, the questions shift from "can the agent do this?" to:

- **Who authorized this action?** Every task passes through a deterministic policy engine before execution. High-risk actions require explicit human approval.
- **What did the agent actually do?** Every state transition, policy decision, approval, and execution result is recorded in an immutable audit trail.
- **What happens when things go wrong?** Agents self-report blocked states. The system detects stalls, flags drift between expected and actual state, and surfaces failures for operator review.

agentic-os was built from encountering these problems in production — not in theory.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Operator Layer                           │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │  Dashboard    │  │  Discord     │  │  CLI                  │  │
│  │  (FastAPI +   │  │  (approve /  │  │  task list/show/trace │  │
│  │   HTMX)      │  │   deny /     │  │  audit tail           │  │
│  │              │  │   notify)    │  │  recap today          │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬────────────┘  │
└─────────┼─────────────────┼─────────────────────┼───────────────┘
          │                 │                     │
          ▼                 ▼                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                     agentic-os Backend                          │
│                                                                 │
│  ┌────────────┐  ┌────────────────┐  ┌───────────────────────┐  │
│  │  Policy     │  │  Task          │  │  Approval             │  │
│  │  Engine     │  │  Service       │  │  System               │  │
│  │            │  │                │  │                       │  │
│  │  domain +  │  │  create →      │  │  request → pending →  │  │
│  │  risk +    │  │  classify →    │  │  approve/deny →       │  │
│  │  origin →  │  │  gate →        │  │  promote/cancel       │  │
│  │  verdict   │  │  dispatch →    │  │                       │  │
│  │            │  │  receive →     │  │  Discord DM + dash    │  │
│  │  (execute  │  │  complete      │  │  notifications        │  │
│  │   plan     │  │                │  │                       │  │
│  │   approve  │  │                │  │                       │  │
│  │   approve  │  │                │  │                       │  │
│  │   _plan)   │  │                │  │                       │  │
│  └────────────┘  └────────────────┘  └───────────────────────┘  │
│                                                                 │
│  ┌────────────┐  ┌────────────────┐  ┌───────────────────────┐  │
│  │  Audit     │  │  Artifact      │  │  Execution            │  │
│  │  Trail     │  │  Storage       │  │  Receiver             │  │
│  │            │  │                │  │                       │  │
│  │  40+ event │  │  versioned     │  │  parse agent output   │  │
│  │  types     │  │  plans,        │  │  extract artifacts    │  │
│  │  JSONL +   │  │  results,      │  │  store + writeback    │  │
│  │  gzip      │  │  documents     │  │  handle callbacks     │  │
│  │  rotation  │  │                │  │                       │  │
│  └────────────┘  └────────────────┘  └───────────────────────┘  │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Storage: SQLite (tasks, approvals, artifacts, audit)    │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────────────────┬────────────────────────────────────┘
                             │
                    Reconciler (sync)
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Control Plane (Paperclip)                     │
│                                                                 │
│   Issue lifecycle ─── Agent wakeup ─── Heartbeat execution      │
│   Session management ─── Operator comments ─── Routine tasks    │
└─────────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Agent Layer                              │
│                                                                 │
│   Claude ─── Codex ─── Any LLM via adapter                     │
│                                                                 │
│   Each agent receives a structured brief, executes within       │
│   policy constraints, and posts results back via callback.      │
└─────────────────────────────────────────────────────────────────┘
```

### Separation of Concerns

| Layer | Owns | Does NOT own |
|-------|------|-------------|
| **agentic-os** (backend) | Task state, policy decisions, approvals, audit trail, artifact storage | Agent runtime, model selection, prompt execution |
| **Control plane** (Paperclip) | Agent waking, session lifecycle, heartbeat execution, operator UI | Policy decisions, approval authority, audit |
| **Agents** (Claude, Codex, etc.) | Task execution within their brief | State transitions, policy bypass, self-approval |

---

## Key Design Decisions

These emerged across five evolutionary phases of building the system ([full writeup](https://pub.towardsai.net/building-a-multi-agent-os-key-design-decisions-that-matter-d07c3e7ae30c)):

1. **Authority lives where enforcement is possible.** The backend decides what runs and under what constraints. Agents never self-authorize.

2. **Visibility is not control.** Operator dashboards show what's happening; the backend enforces what's allowed. These are separate systems with separate responsibilities.

3. **Assignment-driven, not polling-based.** Agents wake when assigned work, not by scanning for it. Eliminates wasted compute and race conditions.

4. **Execution is protocol, not prompt convention.** Structured briefs, typed callbacks, and versioned artifacts — not "please remember to do X at the end."

5. **Centralized capability resolution.** Skills are resolved from `global_defaults + shared_integrations + project_defaults[project]` and hydrated before runtime. No capability drift across workspaces.

---

## Features

- **Deterministic policy engine** — domain + risk + origin = verdict (`execute`, `plan`, `approve`, `approve_plan`)
- **Human-in-the-loop approvals** — Discord DM notifications, dashboard UI, configurable auto-approve rules
- **Full audit trail** — 40+ event types, JSONL with gzip rotation, per-task trace views
- **Versioned artifact storage** — plans, results, and documents stored with full version history
- **Drift detection & reconciliation** — automatic sync between backend state and control plane
- **Background jobs** — reconciliation, approval reminders, health checks, backups
- **Dashboard** — FastAPI + HTMX with task lists, approval queues, audit viewer, health status, recap views
- **CLI** — full task lifecycle management, audit tailing, recaps
- **Multi-agent support** — agent-agnostic; works with Claude, Codex, or any LLM via adapter pattern

---

## Quick Start

```bash
# Clone
git clone https://github.com/<your-username>/agent-os.git
cd agent-os

# Install
pip install -r requirements.txt  # or: pip install -e .

# Initialize
PYTHONPATH=src python3 -m agentic_os.cli init

# Configure
cp agentic_os.config.example.json agentic_os.config.json
# Edit agentic_os.config.json with your control plane settings

# Run dashboard
PYTHONPATH=src uvicorn agentic_os.web:app --reload --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080`.

---

## Project Layout

```
src/agentic_os/
├── service.py              # Core task lifecycle orchestration
├── models.py               # Domain models and validation
├── policy_engine.py         # Deterministic execution gating
├── storage.py              # SQLite persistence layer
├── audit.py                # Immutable event trail (JSONL)
├── artifacts.py            # Versioned artifact storage
├── dispatcher.py           # Task brief builder
├── execution_receiver.py    # Agent output parser + callback handler
├── task_control_plane.py    # Control plane integration
├── paperclip_client.py      # REST API client
├── paperclip_reconciler.py  # Bidirectional state sync
├── notification_router.py   # Discord notification delivery
├── api_routes.py           # RESTful API endpoints
├── web_routes.py           # Dashboard HTML routes
├── web.py                  # FastAPI app factory
├── cli.py                  # Operator CLI
├── jobs.py                 # Background job scheduler
└── templates/              # Jinja2 dashboard templates
data/                       # SQLite database + audit logs
artifacts/                  # Versioned task artifacts
```

---

## API Surface

### Tasks
- `GET /api/tasks` — list tasks (filterable by status, domain, target)
- `GET /api/tasks/{id}` — task detail with full trace
- `POST /api/tasks` — create task (triggers policy engine)
- `POST /api/tasks/{id}/approve` — approve pending task
- `POST /api/tasks/{id}/deny` — deny pending task

### Approvals
- `GET /api/approvals` — grouped approval queue
- `POST /api/approvals/{id}/approve` — grant approval
- `POST /api/approvals/{id}/deny` — deny approval

### Execution
- `POST /api/execution/callback` — receive agent execution results

### Observability
- `GET /api/audit` — audit trail (filterable by task, target)
- `GET /api/health` — system health
- `GET /api/recap/today` — daily summary
- `GET /api/recap/failures` — failure report
- `GET /api/recap/overdue` — overdue tasks
- `GET /api/recap/in-progress` — active work

---

## CLI

```bash
# Task management
agentic-os task list [--status pending] [--domain technical]
agentic-os task show <task_id>
agentic-os task trace <task_id>        # Full lifecycle trace

# Operations
agentic-os task list-ready             # Tasks eligible for dispatch
agentic-os task pickup --task-id ...
agentic-os task record-result --task-id ... --output-file ...

# Observability
agentic-os recap today
agentic-os recap failures
agentic-os audit tail [--limit 50] [--target task_000042]
```

---

## How It Works

```
1. Task arrives (API, CLI, or imported from control plane)
         │
2. Policy engine evaluates: domain × risk × origin → verdict
         │
    ┌────┴──────────────────┐
    │                       │
  execute              approve/plan
    │                       │
    │            3. Approval request created
    │               Discord DM + dashboard
    │                       │
    │            4. Operator approves/denies
    │                       │
    ▼                       ▼
5. Task dispatched with structured brief
         │
6. Agent executes within policy constraints
         │
7. Agent posts result via callback endpoint
         │
8. Execution receiver parses output, stores artifacts
         │
9. Audit trail updated, control plane synced
         │
10. Task marked complete ✓
```

Every step from 1-10 is recorded as an audit event. The full trace is viewable per-task in the dashboard and CLI.

---

## License

MIT

---

## Acknowledgments

Built on [FastAPI](https://fastapi.tiangolo.com/), [SQLite](https://sqlite.org/), and [HTMX](https://htmx.org/). Control plane integration via [Paperclip](https://paperclip.dev/).
