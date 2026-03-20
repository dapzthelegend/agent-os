from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, is_dataclass
from typing import Any, Optional

from .config import default_paths
from .models import ACTION_SOURCES, DOMAINS, OperatorError, RequestClassification, STATUSES
from .service import AgenticOSService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Week 4 operator CLI for agentic-os")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize local storage")

    request_parser = subparsers.add_parser("request", help="Request operations")
    request_subparsers = request_parser.add_subparsers(dest="request_command", required=True)
    create_request_parser(request_subparsers)

    task_parser = subparsers.add_parser("task", help="Task inspection and state")
    task_subparsers = task_parser.add_subparsers(dest="task_command", required=True)
    create_task_list_parser(task_subparsers)
    create_task_show_parser(task_subparsers)
    create_task_trace_parser(task_subparsers)
    create_task_complete_parser(task_subparsers)
    create_task_fail_parser(task_subparsers)
    create_task_execute_parser(task_subparsers)

    approval_parser = subparsers.add_parser("approval", help="Approval operations")
    approval_subparsers = approval_parser.add_subparsers(dest="approval_command", required=True)
    create_approval_list_parser(approval_subparsers)
    create_approval_show_parser(approval_subparsers)
    create_approval_approve_parser(approval_subparsers)
    create_approval_deny_parser(approval_subparsers)
    create_approval_cancel_parser(approval_subparsers)

    execution_parser = subparsers.add_parser("execution", help="Execution inspection")
    execution_subparsers = execution_parser.add_subparsers(dest="execution_command", required=True)
    create_execution_show_parser(execution_subparsers)

    audit_parser = subparsers.add_parser("audit", help="Audit inspection")
    audit_subparsers = audit_parser.add_subparsers(dest="audit_command", required=True)
    create_audit_tail_parser(audit_subparsers)

    recap_parser = subparsers.add_parser("recap", help="Simple recap commands over durable state")
    recap_subparsers = recap_parser.add_subparsers(dest="recap_command", required=True)
    create_recap_today_parser(recap_subparsers)
    create_recap_approvals_parser(recap_subparsers)
    create_recap_drafts_parser(recap_subparsers)
    create_recap_failures_parser(recap_subparsers)
    create_recap_external_actions_parser(recap_subparsers)

    artifact_parser = subparsers.add_parser("artifact", help="Artifact operations")
    artifact_subparsers = artifact_parser.add_subparsers(dest="artifact_command", required=True)
    create_artifact_revise_parser(artifact_subparsers)

    openclaw_parser = subparsers.add_parser("openclaw", help="Record OpenClaw-mediated operations")
    openclaw_subparsers = openclaw_parser.add_subparsers(dest="openclaw_command", required=True)
    create_openclaw_read_parser(openclaw_subparsers)
    create_openclaw_draft_parser(openclaw_subparsers)
    create_openclaw_execution_parser(openclaw_subparsers)

    adapter_parser = subparsers.add_parser("adapter", help="Future custom-adapter seam")
    adapter_subparsers = adapter_parser.add_subparsers(dest="adapter_command", required=True)
    create_adapter_execute_parser(adapter_subparsers)

    return parser


def create_request_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("create", help="Store, classify, and policy-evaluate a request")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--intent", required=True, dest="intent_type")
    parser.add_argument("--risk", required=True, dest="risk_level")
    parser.add_argument("--request", required=True, dest="user_request")
    parser.add_argument("--target")
    parser.add_argument("--metadata-json")
    parser.add_argument("--external-write", action="store_true")
    parser.add_argument("--operation-key")
    parser.add_argument("--result-summary")
    parser.add_argument("--external-ref")
    parser.add_argument("--artifact-type")
    parser.add_argument("--artifact-text")
    parser.add_argument("--artifact-json")


def create_task_list_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("list", help="List recent tasks")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--status", choices=STATUSES)
    parser.add_argument("--domain", choices=DOMAINS)
    parser.add_argument("--target")
    parser.add_argument("--action-source", choices=ACTION_SOURCES)


def create_task_show_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("show", help="Show task detail with approvals, artifacts, execution, and audit")
    parser.add_argument("task_id")


def create_task_trace_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("trace", help="Show task, audit, artifact, approval, and execution trace")
    parser.add_argument("task_id")


def create_task_complete_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("complete", help="Mark a task completed")
    parser.add_argument("task_id")
    parser.add_argument("--result-summary", required=True)


def create_task_fail_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("fail", help="Mark a task failed")
    parser.add_argument("task_id")
    parser.add_argument("--reason", required=True)


def create_task_execute_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("execute", help="Record an executed action with idempotency")
    parser.add_argument("task_id")
    parser.add_argument("--result-summary", required=True)
    parser.add_argument("--tool-name")
    parser.add_argument("--tool-result-json")


def create_approval_list_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("list", help="List approvals")
    parser.add_argument("--task-id")


def create_approval_show_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("show", help="Show approval detail")
    parser.add_argument("approval_id")


def create_approval_approve_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("approve", help="Approve a pending approval record")
    parser.add_argument("approval_id")
    parser.add_argument("--note")


def create_approval_deny_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("deny", help="Deny a pending approval record")
    parser.add_argument("approval_id")
    parser.add_argument("--note")


def create_approval_cancel_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("cancel", help="Cancel a pending approval record")
    parser.add_argument("approval_id")
    parser.add_argument("--note")


def create_artifact_revise_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("revise", help="Create a new artifact version for a task")
    parser.add_argument("task_id")
    parser.add_argument("--artifact-type")
    parser.add_argument("--artifact-text")
    parser.add_argument("--artifact-json")


def create_openclaw_read_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("read", help="Record a completed OpenClaw-backed read")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--risk", required=True, dest="risk_level")
    parser.add_argument("--request", required=True, dest="user_request")
    parser.add_argument("--tool-name", required=True)
    parser.add_argument("--tool-input-json")
    parser.add_argument("--tool-result-json")
    parser.add_argument("--summary", required=True)
    parser.add_argument("--target")
    parser.add_argument("--metadata-json")
    parser.add_argument("--artifact-type")
    parser.add_argument("--artifact-text")
    parser.add_argument("--artifact-json")
    parser.add_argument("--action-source", default="openclaw_tool")


def create_openclaw_draft_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("draft", help="Record an OpenClaw-generated draft artifact")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--risk", required=True, dest="risk_level")
    parser.add_argument("--request", required=True, dest="user_request")
    parser.add_argument("--artifact-type", required=True)
    parser.add_argument("--artifact-text")
    parser.add_argument("--artifact-json")
    parser.add_argument("--tool-name")
    parser.add_argument("--tool-input-json")
    parser.add_argument("--summary")
    parser.add_argument("--target")
    parser.add_argument("--metadata-json")
    parser.add_argument("--action-source", default="openclaw_skill")


def create_openclaw_execution_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "execution",
        help="Record an OpenClaw-mediated approval-backed execution request",
    )
    parser.add_argument("--domain", required=True)
    parser.add_argument("--risk", required=True, dest="risk_level")
    parser.add_argument("--request", required=True, dest="user_request")
    parser.add_argument("--tool-name", required=True)
    parser.add_argument("--operation-key", required=True)
    parser.add_argument("--result-summary", required=True)
    parser.add_argument("--tool-input-json")
    parser.add_argument("--tool-result-json")
    parser.add_argument("--target")
    parser.add_argument("--metadata-json")
    parser.add_argument("--artifact-type")
    parser.add_argument("--artifact-text")
    parser.add_argument("--artifact-json")
    parser.add_argument("--action-source", default="openclaw_tool")


def create_adapter_execute_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("execute", help="Call the future custom-adapter seam")
    parser.add_argument("--adapter-name", required=True)
    parser.add_argument("--action-name", required=True)


def create_execution_show_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("show", help="Show execution detail by operation_key")
    parser.add_argument("operation_key")


def create_audit_tail_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("tail", help="Show recent audit activity")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--domain", choices=DOMAINS)
    parser.add_argument("--target")


def create_recap_today_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("today", help="Summarize what happened today")
    parser.add_argument("--domain", choices=DOMAINS)


def create_recap_approvals_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("approvals", help="Summarize work awaiting approval")
    parser.add_argument("--domain", choices=DOMAINS)


def create_recap_drafts_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("drafts", help="Summarize open drafts")
    parser.add_argument("--domain", choices=DOMAINS)


def create_recap_failures_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("failures", help="Summarize recent failures")
    parser.add_argument("--domain", choices=DOMAINS)
    parser.add_argument("--limit", type=int, default=20)


def create_recap_external_actions_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("external-actions", help="Summarize recorded external actions")
    parser.add_argument("--domain", choices=DOMAINS)
    parser.add_argument("--limit", type=int, default=20)


def parse_artifact(args: argparse.Namespace) -> Optional[Any]:
    if getattr(args, "artifact_json", None) and getattr(args, "artifact_text", None):
        raise SystemExit("provide either --artifact-json or --artifact-text, not both")
    if getattr(args, "artifact_json", None):
        return json.loads(args.artifact_json)
    if getattr(args, "artifact_text", None):
        return args.artifact_text
    return None


def parse_metadata_json(raw_value: Optional[str]) -> Optional[dict[str, Any]]:
    if raw_value is None:
        return None
    value = json.loads(raw_value)
    if not isinstance(value, dict):
        raise SystemExit("--metadata-json must decode to a JSON object")
    return value


def parse_optional_json_object(raw_value: Optional[str], *, flag_name: str) -> Optional[dict[str, Any]]:
    if raw_value is None:
        return None
    value = json.loads(raw_value)
    if not isinstance(value, dict):
        raise SystemExit(f"{flag_name} must decode to a JSON object")
    return value


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    return value


def print_json(payload: Any) -> None:
    print(json.dumps(to_jsonable(payload), indent=2, sort_keys=True))


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = AgenticOSService(default_paths())

    try:
        if args.command == "init":
            service.initialize()
            print_json(
                {
                    "status": "initialized",
                    "db_path": str(service.paths.db_path),
                    "audit_log_path": str(service.paths.audit_log_path),
                    "artifacts_dir": str(service.paths.artifacts_dir),
                    "policy_rules_path": str(service.paths.policy_rules_path),
                }
            )
            return 0

        service.initialize()

        if args.command == "request" and args.request_command == "create":
            classification = RequestClassification(
                domain=args.domain,
                intent_type=args.intent_type,
                risk_level=args.risk_level,
            ).validate()
            artifact_content = parse_artifact(args)
            payload = service.create_request(
                user_request=args.user_request,
                classification=classification,
                target=args.target,
                request_metadata=parse_metadata_json(args.metadata_json),
                external_write=args.external_write,
                operation_key=args.operation_key,
                artifact_type=args.artifact_type,
                artifact_content=artifact_content,
                result_summary=args.result_summary,
                external_ref=args.external_ref,
                action_source="manual",
            )
            print_json(payload)
            return 0

        if args.command == "openclaw" and args.openclaw_command == "read":
            classification = RequestClassification(
                domain=args.domain,
                intent_type="read",
                risk_level=args.risk_level,
            ).validate()
            payload = service.record_openclaw_read(
                user_request=args.user_request,
                classification=classification,
                tool_name=args.tool_name,
                tool_input=parse_optional_json_object(args.tool_input_json, flag_name="--tool-input-json"),
                tool_result=parse_optional_json_object(args.tool_result_json, flag_name="--tool-result-json"),
                summary=args.summary,
                target=args.target,
                request_metadata=parse_metadata_json(args.metadata_json),
                artifact_type=args.artifact_type,
                artifact_content=parse_artifact(args),
                action_source=args.action_source,
            )
            print_json(payload)
            return 0

        if args.command == "openclaw" and args.openclaw_command == "draft":
            artifact_content = parse_artifact(args)
            if artifact_content is None:
                raise SystemExit("provide --artifact-json or --artifact-text")
            classification = RequestClassification(
                domain=args.domain,
                intent_type="draft",
                risk_level=args.risk_level,
            ).validate()
            payload = service.record_openclaw_draft(
                user_request=args.user_request,
                classification=classification,
                draft_artifact=artifact_content,
                artifact_type=args.artifact_type,
                tool_name=args.tool_name,
                tool_input=parse_optional_json_object(args.tool_input_json, flag_name="--tool-input-json"),
                summary=args.summary,
                target=args.target,
                request_metadata=parse_metadata_json(args.metadata_json),
                action_source=args.action_source,
            )
            print_json(payload)
            return 0

        if args.command == "openclaw" and args.openclaw_command == "execution":
            classification = RequestClassification(
                domain=args.domain,
                intent_type="execute",
                risk_level=args.risk_level,
            ).validate()
            payload = service.record_openclaw_execution(
                user_request=args.user_request,
                classification=classification,
                tool_name=args.tool_name,
                operation_key=args.operation_key,
                result_summary=args.result_summary,
                tool_input=parse_optional_json_object(args.tool_input_json, flag_name="--tool-input-json"),
                tool_result=parse_optional_json_object(args.tool_result_json, flag_name="--tool-result-json"),
                target=args.target,
                request_metadata=parse_metadata_json(args.metadata_json),
                artifact_type=args.artifact_type,
                artifact_content=parse_artifact(args),
                action_source=args.action_source,
            )
            print_json(payload)
            return 0

        if args.command == "task" and args.task_command == "list":
            tasks = service.list_tasks(
                limit=args.limit,
                status=args.status,
                domain=args.domain,
                target=args.target,
                action_source=args.action_source,
            )
            payload = {
                "filters": {
                    "limit": args.limit,
                    "status": args.status,
                    "domain": args.domain,
                    "target": args.target,
                    "action_source": args.action_source,
                },
                "tasks": [asdict(task) for task in tasks],
            }
            print_json(payload)
            return 0

        if args.command == "task" and args.task_command == "show":
            print_json(service.get_task_detail(args.task_id))
            return 0

        if args.command == "task" and args.task_command == "trace":
            print_json(service.get_task_detail(args.task_id))
            return 0

        if args.command == "task" and args.task_command == "complete":
            task = service.complete_task(args.task_id, args.result_summary)
            print_json({"task": asdict(task)})
            return 0

        if args.command == "task" and args.task_command == "fail":
            task = service.fail_task(args.task_id, args.reason)
            print_json({"task": asdict(task)})
            return 0

        if args.command == "task" and args.task_command == "execute":
            print_json(
                service.execute_action(
                    args.task_id,
                    args.result_summary,
                    tool_name=args.tool_name,
                    tool_result=parse_optional_json_object(
                        args.tool_result_json,
                        flag_name="--tool-result-json",
                    ),
                )
            )
            return 0

        if args.command == "approval" and args.approval_command == "list":
            payload = {"approvals": [asdict(item) for item in service.list_approvals(task_id=args.task_id)]}
            print_json(payload)
            return 0

        if args.command == "approval" and args.approval_command == "show":
            print_json(service.get_approval_detail(args.approval_id))
            return 0

        if args.command == "approval" and args.approval_command == "approve":
            print_json(service.approve(args.approval_id, decision_note=args.note))
            return 0

        if args.command == "approval" and args.approval_command == "deny":
            print_json(service.deny(args.approval_id, decision_note=args.note))
            return 0

        if args.command == "approval" and args.approval_command == "cancel":
            print_json(service.cancel(args.approval_id, decision_note=args.note))
            return 0

        if args.command == "artifact" and args.artifact_command == "revise":
            artifact_content = parse_artifact(args)
            if artifact_content is None:
                raise SystemExit("provide --artifact-json or --artifact-text")
            print_json(
                service.revise_artifact(
                    args.task_id,
                    artifact_type=args.artifact_type,
                    artifact_content=artifact_content,
                )
            )
            return 0

        if args.command == "execution" and args.execution_command == "show":
            print_json(service.get_execution_detail(args.operation_key))
            return 0

        if args.command == "audit" and args.audit_command == "tail":
            print_json(
                service.list_recent_audit_activity(
                    limit=args.limit,
                    domain=args.domain,
                    target=args.target,
                )
            )
            return 0

        if args.command == "recap" and args.recap_command == "today":
            print_json(service.recap_today(domain=args.domain))
            return 0

        if args.command == "recap" and args.recap_command == "approvals":
            print_json(service.recap_approvals(domain=args.domain))
            return 0

        if args.command == "recap" and args.recap_command == "drafts":
            print_json(service.recap_drafts(domain=args.domain))
            return 0

        if args.command == "recap" and args.recap_command == "failures":
            print_json(service.recap_failures(domain=args.domain, limit=args.limit))
            return 0

        if args.command == "recap" and args.recap_command == "external-actions":
            print_json(service.recap_external_actions(domain=args.domain, limit=args.limit))
            return 0

        if args.command == "adapter" and args.adapter_command == "execute":
            service.execute_custom_adapter_action(
                adapter_name=args.adapter_name,
                action_name=args.action_name,
            )
            print_json({"status": "ok"})
            return 0
    except (
        KeyError,
        sqlite3.IntegrityError,
        sqlite3.OperationalError,
        NotImplementedError,
    ) as exc:
        print_json({"error": str(exc), "error_type": type(exc).__name__})
        return 2
    except (OperatorError, ValueError) as exc:
        payload = {"error": str(exc), "error_type": type(exc).__name__}
        if isinstance(exc, OperatorError):
            payload["error_code"] = exc.code
            payload["details"] = exc.details
        print_json(payload)
        return 2

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
