from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

from .models import ApprovalRecord, ExecutionRecord, RequestClassification, TaskRecord


def utc_now_sql() -> str:
    return "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"


# Canonical schema — no incremental migrations.
# If the DB has an old schema (missing paperclip_issue_id), run scripts/reset_db.py.
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    -- core
    title TEXT,
    description TEXT,
    domain TEXT NOT NULL,
    status TEXT NOT NULL,
    task_mode TEXT NOT NULL DEFAULT 'direct',
    -- legacy fields retained for live-path compat
    intent_type TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    approval_state TEXT NOT NULL,
    user_request TEXT NOT NULL,
    result_summary TEXT,
    artifact_ref TEXT,
    artifact_path TEXT,
    external_ref TEXT,
    target TEXT,
    delivery_target TEXT,
    delivery_thread_id TEXT,
    request_metadata_json TEXT,
    operation_key TEXT,
    external_write INTEGER NOT NULL DEFAULT 0,
    policy_decision TEXT,
    action_source TEXT NOT NULL DEFAULT 'manual',
    retry_count INTEGER NOT NULL DEFAULT 0,
    claimed_at TEXT,
    claimed_by TEXT,
    dispatch_session_key TEXT,
    dispatch_attempts INTEGER NOT NULL DEFAULT 0,
    -- paperclip
    paperclip_issue_id TEXT,
    paperclip_assignee_agent_id TEXT,
    paperclip_project_id TEXT,
    paperclip_goal_id TEXT,
    plan_version INTEGER NOT NULL DEFAULT 0,
    approved_plan_revision_id TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_preview TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    artifact_id TEXT,
    action_target TEXT,
    operation_key TEXT,
    payload_json TEXT NOT NULL,
    decision_note TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    decided_at TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS executions (
    operation_key TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    approval_id TEXT,
    status TEXT NOT NULL,
    result_summary TEXT,
    session_key TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_domain ON tasks(domain, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_target ON tasks(target, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_action_source ON tasks(action_source, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_ready ON tasks(status, policy_decision, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_paperclip_issue ON tasks(paperclip_issue_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_task_id ON audit_events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_events_created_at ON audit_events(created_at, id);
CREATE INDEX IF NOT EXISTS idx_artifacts_task_id ON artifacts(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_task_id ON approvals(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);
"""

# Sentinel column used to detect an incompatible (pre-phase-0) schema.
_REQUIRED_COLUMN = "paperclip_issue_id"


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            self._check_schema_compatibility(connection)
            connection.executescript(SCHEMA)

    @staticmethod
    def _check_schema_compatibility(connection: sqlite3.Connection) -> None:
        """Fail fast if an incompatible old schema is detected."""
        rows = connection.execute("PRAGMA table_info(tasks)").fetchall()
        if not rows:
            return  # table doesn't exist yet — fresh DB, OK
        existing = {row["name"] for row in rows}
        if _REQUIRED_COLUMN not in existing:
            raise RuntimeError(
                "Incompatible task schema detected (pre-phase-0). "
                "Run scripts/reset_db.py to recreate the database."
            )

    @staticmethod
    def _next_task_id(connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT COALESCE(MAX(CAST(SUBSTR(id, 6) AS INTEGER)), 0) + 1 AS next_id FROM tasks"
        ).fetchone()
        return f"task_{row['next_id']:06d}"

    def create_task(
        self,
        *,
        classification: RequestClassification,
        user_request: str,
        artifact_ref: Optional[str] = None,
        result_summary: Optional[str] = None,
        external_ref: Optional[str] = None,
        target: Optional[str] = None,
        request_metadata_json: Optional[str] = None,
        operation_key: Optional[str] = None,
        external_write: bool = False,
        policy_decision: Optional[str] = None,
        action_source: str = "manual",
    ) -> TaskRecord:
        classification.validate()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task_id = self._next_task_id(connection)
            connection.execute(
                """
                INSERT INTO tasks (
                    id, domain, intent_type, risk_level, status,
                    approval_state, user_request, result_summary, artifact_ref, external_ref,
                    target, request_metadata_json, operation_key, external_write, policy_decision
                    , action_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    classification.domain,
                    classification.intent_type,
                    classification.risk_level,
                    classification.status,
                    classification.approval_state,
                    user_request,
                    result_summary,
                    artifact_ref,
                    external_ref,
                    target,
                    request_metadata_json,
                    operation_key,
                    1 if external_write else 0,
                    policy_decision,
                    action_source,
                ),
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return self._row_to_task(row)

    def update_task(
        self,
        task_id: str,
        *,
        status: Optional[str] = None,
        approval_state: Optional[str] = None,
        result_summary: Optional[str] = None,
        artifact_ref: Optional[str] = None,
        external_ref: Optional[str] = None,
        target: Optional[str] = None,
        request_metadata_json: Optional[str] = None,
        operation_key: Optional[str] = None,
        external_write: Optional[bool] = None,
        policy_decision: Optional[str] = None,
        action_source: Optional[str] = None,
        retry_count: Optional[int] = None,
        # Paperclip fields
        paperclip_issue_id: Optional[str] = None,
        paperclip_assignee_agent_id: Optional[str] = None,
        paperclip_project_id: Optional[str] = None,
        paperclip_goal_id: Optional[str] = None,
        plan_version: Optional[int] = None,
        approved_plan_revision_id: Optional[str] = None,
        # New schema fields
        title: Optional[str] = None,
        description: Optional[str] = None,
        task_mode: Optional[str] = None,
        delivery_target: Optional[str] = None,
        delivery_thread_id: Optional[str] = None,
        artifact_path: Optional[str] = None,
    ) -> TaskRecord:
        updates: list[str] = []
        values: list[Any] = []
        for field, value in (
            ("status", status),
            ("approval_state", approval_state),
            ("result_summary", result_summary),
            ("artifact_ref", artifact_ref),
            ("external_ref", external_ref),
            ("target", target),
            ("request_metadata_json", request_metadata_json),
            ("operation_key", operation_key),
            ("policy_decision", policy_decision),
            ("action_source", action_source),
            ("paperclip_issue_id", paperclip_issue_id),
            ("paperclip_assignee_agent_id", paperclip_assignee_agent_id),
            ("paperclip_project_id", paperclip_project_id),
            ("paperclip_goal_id", paperclip_goal_id),
            ("approved_plan_revision_id", approved_plan_revision_id),
            ("title", title),
            ("description", description),
            ("task_mode", task_mode),
            ("delivery_target", delivery_target),
            ("delivery_thread_id", delivery_thread_id),
            ("artifact_path", artifact_path),
        ):
            if value is not None:
                updates.append(f"{field} = ?")
                values.append(value)
        if external_write is not None:
            updates.append("external_write = ?")
            values.append(1 if external_write else 0)
        if retry_count is not None:
            updates.append("retry_count = ?")
            values.append(retry_count)
        if plan_version is not None:
            updates.append("plan_version = ?")
            values.append(plan_version)
        updates.append(f"updated_at = {utc_now_sql()}")
        values.append(task_id)
        query = f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?"
        with self.connect() as connection:
            connection.execute(query, values)
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown task_id: {task_id}")
        return self._row_to_task(row)

    def insert_artifact(
        self,
        *,
        artifact_id: str,
        task_id: str,
        artifact_type: str,
        path: str,
        version: int,
        content_preview: Optional[str],
        created_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (id, task_id, artifact_type, path, version, content_preview, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (artifact_id, task_id, artifact_type, path, version, content_preview, created_at),
            )

    def insert_audit_event(self, *, task_id: str, event_type: str, payload_json: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO audit_events (task_id, event_type, payload_json)
                VALUES (?, ?, ?)
                """,
                (task_id, event_type, payload_json),
            )
            return int(cursor.lastrowid)

    def list_tasks(self, limit: int = 20) -> list[TaskRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def query_tasks(
        self,
        *,
        limit: int = 20,
        status: Optional[str] = None,
        domain: Optional[str] = None,
        target: Optional[str] = None,
        action_source: Optional[str] = None,
    ) -> list[TaskRecord]:
        clauses: list[str] = []
        values: list[Any] = []
        for field, value in (
            ("status", status),
            ("domain", domain),
            ("target", target),
            ("action_source", action_source),
        ):
            if value is not None:
                clauses.append(f"{field} = ?")
                values.append(value)
        where_clause = ""
        if clauses:
            where_clause = "WHERE " + " AND ".join(clauses)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM tasks
                {where_clause}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (*values, limit),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def query_ready_tasks(self, *, limit: int = 20) -> list[TaskRecord]:
        """
        Return tasks eligible for execution (FIFO):
          - status = 'approved'
          - status = 'new' AND policy_decision = 'read_ok'
          - plan_first: status = 'approved_for_execution' AND approved_plan_revision_id IS NOT NULL
        """
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'approved'
                   OR (status = 'new' AND policy_decision = 'read_ok')
                   OR (
                      task_mode = 'plan_first'
                      AND status = 'approved_for_execution'
                      AND approved_plan_revision_id IS NOT NULL
                   )
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def pickup_task(self, task_id: str, *, claimed_by: str = "task_executor_cron") -> dict:
        """
        Atomically transition an eligible task to 'in_progress' (direct) or 'executing' (plan_first).
        Uses BEGIN IMMEDIATE to prevent concurrent cron double-dispatch.

        plan_first tasks must be in 'approved_for_execution' with approved_plan_revision_id set.

        Returns:
            {"success": True, "task_id": task_id}
            {"success": False, "task_id": task_id, "reason": str, "current_status": str}
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown task_id: {task_id}")
            task = self._row_to_task(row)

            if task.task_mode == "plan_first":
                eligible = (
                    task.status == "approved_for_execution"
                    and bool(task.approved_plan_revision_id)
                )
                new_status = "executing"
            else:
                eligible = (
                    task.status == "approved"
                    or (task.status == "new" and task.policy_decision == "read_ok")
                )
                new_status = "in_progress"

            if not eligible:
                already_running = task.status in ("in_progress", "executing")
                return {
                    "success": False,
                    "task_id": task_id,
                    "reason": "already_claimed" if already_running else "not_eligible",
                    "current_status": task.status,
                }
            connection.execute(
                f"""UPDATE tasks
                    SET status = ?,
                        claimed_at = {utc_now_sql()},
                        claimed_by = ?,
                        dispatch_attempts = COALESCE(dispatch_attempts, 0) + 1,
                        updated_at = {utc_now_sql()}
                    WHERE id = ?""",
                (new_status, claimed_by, task_id),
            )
        return {"success": True, "task_id": task_id}

    def update_dispatch_session_key(self, task_id: str, session_key: str) -> None:
        """Record the spawned session key on the task after successful dispatch."""
        with self.connect() as connection:
            connection.execute(
                f"UPDATE tasks SET dispatch_session_key = ?, updated_at = {utc_now_sql()} WHERE id = ?",
                (session_key, task_id),
            )

    def get_task(self, task_id: str) -> TaskRecord:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown task_id: {task_id}")
        return self._row_to_task(row)

    def get_task_by_operation_key(self, operation_key: str) -> Optional[TaskRecord]:
        tasks = self.list_tasks_by_operation_key(operation_key)
        if not tasks:
            return None
        return tasks[0]

    def get_task_by_external_ref(self, external_ref: str) -> Optional[TaskRecord]:
        tasks = self.list_tasks_by_external_ref(external_ref)
        if not tasks:
            return None
        return tasks[0]

    def get_task_by_paperclip_issue_id(self, issue_id: str) -> Optional[TaskRecord]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE paperclip_issue_id = ? ORDER BY created_at DESC LIMIT 1",
                (issue_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    def list_tasks_by_operation_key(self, operation_key: str) -> list[TaskRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE operation_key = ?",
                (operation_key,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_tasks_by_external_ref(self, external_ref: str) -> list[TaskRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE external_ref = ?
                ORDER BY created_at DESC, id DESC
                """,
                (external_ref,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_audit_events(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, task_id, event_type, payload_json, created_at
                FROM audit_events
                WHERE task_id = ?
                ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "event_type": row["event_type"],
                "payload_json": row["payload_json"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_recent_audit_events(
        self,
        *,
        limit: int = 20,
        domain: Optional[str] = None,
        target: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if domain is not None:
            clauses.append("tasks.domain = ?")
            values.append(domain)
        if target is not None:
            clauses.append("tasks.target = ?")
            values.append(target)
        where_clause = ""
        if clauses:
            where_clause = "WHERE " + " AND ".join(clauses)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    audit_events.id,
                    audit_events.task_id,
                    audit_events.event_type,
                    audit_events.payload_json,
                    audit_events.created_at,
                    tasks.domain,
                    tasks.status AS task_status,
                    tasks.target,
                    tasks.action_source
                FROM audit_events
                INNER JOIN tasks ON tasks.id = audit_events.task_id
                {where_clause}
                ORDER BY audit_events.id DESC
                LIMIT ?
                """,
                (*values, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, task_id, artifact_type, path, version, content_preview, created_at
                FROM artifacts
                WHERE task_id = ?
                ORDER BY version ASC, created_at ASC
                """,
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_approval(
        self,
        *,
        approval_id: str,
        task_id: str,
        status: str,
        subject_type: str,
        artifact_id: Optional[str],
        action_target: Optional[str],
        operation_key: Optional[str],
        payload_json: str,
    ) -> ApprovalRecord:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO approvals (
                    id, task_id, status, subject_type, artifact_id,
                    action_target, operation_key, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    task_id,
                    status,
                    subject_type,
                    artifact_id,
                    action_target,
                    operation_key,
                    payload_json,
                ),
            )
            row = connection.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return self._row_to_approval(row)

    def update_approval(
        self,
        approval_id: str,
        *,
        status: str,
        decision_note: Optional[str] = None,
    ) -> ApprovalRecord:
        with self.connect() as connection:
            connection.execute(
                f"""
                UPDATE approvals
                SET status = ?, decision_note = ?, updated_at = {utc_now_sql()},
                    decided_at = CASE WHEN ? IN ('approved', 'denied', 'cancelled')
                        THEN {utc_now_sql()}
                        ELSE decided_at
                    END
                WHERE id = ?
                """,
                (status, decision_note, status, approval_id),
            )
            row = connection.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown approval_id: {approval_id}")
        return self._row_to_approval(row)

    def get_approval(self, approval_id: str) -> ApprovalRecord:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown approval_id: {approval_id}")
        return self._row_to_approval(row)

    def list_approvals(self, task_id: Optional[str] = None) -> list[ApprovalRecord]:
        with self.connect() as connection:
            if task_id is None:
                rows = connection.execute(
                    "SELECT * FROM approvals ORDER BY created_at DESC, id DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM approvals WHERE task_id = ? ORDER BY created_at DESC, id DESC",
                    (task_id,),
                ).fetchall()
        return [self._row_to_approval(row) for row in rows]

    def list_approvals_by_status(self, status: str) -> list[ApprovalRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM approvals WHERE status = ? ORDER BY created_at DESC, id DESC",
                (status,),
            ).fetchall()
        return [self._row_to_approval(row) for row in rows]

    def get_pending_approval_for_task(self, task_id: str) -> Optional[ApprovalRecord]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM approvals
                WHERE task_id = ? AND status = 'pending'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_approval(row)

    def create_execution(
        self,
        *,
        operation_key: str,
        task_id: str,
        approval_id: Optional[str],
        status: str,
        result_summary: Optional[str],
    ) -> ExecutionRecord:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO executions (operation_key, task_id, approval_id, status, result_summary)
                VALUES (?, ?, ?, ?, ?)
                """,
                (operation_key, task_id, approval_id, status, result_summary),
            )
            row = connection.execute(
                "SELECT * FROM executions WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        return self._row_to_execution(row)

    def get_execution(self, operation_key: str) -> Optional[ExecutionRecord]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM executions WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_execution(row)

    def list_recent_executions(self, *, limit: int = 20) -> list[ExecutionRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM executions ORDER BY created_at DESC, operation_key DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_execution(row) for row in rows]

    def update_execution_session_key(self, *, operation_key: str, session_key: str) -> ExecutionRecord:
        """Update the session_key for an execution record."""
        with self.connect() as connection:
            connection.execute(
                "UPDATE executions SET session_key = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE operation_key = ?",
                (session_key, operation_key),
            )
            row = connection.execute(
                "SELECT * FROM executions WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        if row is None:
            raise KeyError(f"execution with operation_key {operation_key} not found")
        return self._row_to_execution(row)

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> TaskRecord:
        keys = row.keys()
        return TaskRecord(
            id=row["id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            domain=row["domain"],
            intent_type=row["intent_type"],
            risk_level=row["risk_level"],
            status=row["status"],
            approval_state=row["approval_state"],
            user_request=row["user_request"],
            result_summary=row["result_summary"],
            artifact_ref=row["artifact_ref"],
            external_ref=row["external_ref"],
            target=row["target"],
            request_metadata_json=row["request_metadata_json"],
            operation_key=row["operation_key"],
            external_write=bool(row["external_write"]),
            policy_decision=row["policy_decision"],
            action_source=row["action_source"],
            retry_count=row["retry_count"] if "retry_count" in keys else 0,
            claimed_at=row["claimed_at"] if "claimed_at" in keys else None,
            claimed_by=row["claimed_by"] if "claimed_by" in keys else None,
            dispatch_session_key=row["dispatch_session_key"] if "dispatch_session_key" in keys else None,
            dispatch_attempts=row["dispatch_attempts"] if "dispatch_attempts" in keys else 0,
            title=row["title"] if "title" in keys else None,
            description=row["description"] if "description" in keys else None,
            task_mode=row["task_mode"] if "task_mode" in keys else "direct",
            delivery_target=row["delivery_target"] if "delivery_target" in keys else None,
            delivery_thread_id=row["delivery_thread_id"] if "delivery_thread_id" in keys else None,
            artifact_path=row["artifact_path"] if "artifact_path" in keys else None,
            paperclip_issue_id=row["paperclip_issue_id"] if "paperclip_issue_id" in keys else None,
            paperclip_assignee_agent_id=row["paperclip_assignee_agent_id"] if "paperclip_assignee_agent_id" in keys else None,
            paperclip_project_id=row["paperclip_project_id"] if "paperclip_project_id" in keys else None,
            paperclip_goal_id=row["paperclip_goal_id"] if "paperclip_goal_id" in keys else None,
            plan_version=row["plan_version"] if "plan_version" in keys else 0,
            approved_plan_revision_id=row["approved_plan_revision_id"] if "approved_plan_revision_id" in keys else None,
        )

    @staticmethod
    def _row_to_approval(row: sqlite3.Row) -> ApprovalRecord:
        return ApprovalRecord(
            id=row["id"],
            task_id=row["task_id"],
            status=row["status"],
            subject_type=row["subject_type"],
            artifact_id=row["artifact_id"],
            action_target=row["action_target"],
            operation_key=row["operation_key"],
            payload_json=row["payload_json"],
            decision_note=row["decision_note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            decided_at=row["decided_at"],
        ).validate()

    @staticmethod
    def _row_to_execution(row: sqlite3.Row) -> ExecutionRecord:
        return ExecutionRecord(
            operation_key=row["operation_key"],
            task_id=row["task_id"],
            approval_id=row["approval_id"],
            status=row["status"],
            result_summary=row["result_summary"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            session_key=row["session_key"] if "session_key" in row.keys() else None,
        ).validate()
