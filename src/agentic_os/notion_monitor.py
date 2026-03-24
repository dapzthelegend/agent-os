"""Notion monitor — polls Notion for unclaimed Inbox tasks, creates backend tasks, dispatches them."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from typing import Any, Optional


def _extract_multi_select(raw_properties: dict[str, Any], property_name: str) -> list[str]:
    """
    Extract multi_select values from Notion raw_properties.
    
    Args:
        raw_properties: dict of Notion properties
        property_name: the property name to look up
    
    Returns:
        list of option names
    """
    prop_value = raw_properties.get(property_name, {})
    multi_select = prop_value.get("multi_select", [])
    return [item.get("name", "") for item in multi_select if item.get("name")]


def _extract_select(raw_properties: dict[str, Any], property_name: str) -> Optional[str]:
    """
    Extract select value from Notion raw_properties.
    
    Args:
        raw_properties: dict of Notion properties
        property_name: the property name to look up
    
    Returns:
        option name or None
    """
    prop_value = raw_properties.get(property_name, {})
    select = prop_value.get("select")
    if select:
        return select.get("name")
    return None

from .config import Paths, load_app_config, default_paths
from .intake_classifier import IntakeClassifier
from .dispatcher import Dispatcher, DispatchPayload
from .notion import NotionAdapter
from .service import AgenticOSService
from .storage import Database
from .audit import AuditLog


def monitor_once(
    service: AgenticOSService,
    classifier: IntakeClassifier,
    dispatcher: Dispatcher,
    *,
    verbose: bool = False,
    agent_override: Optional[str] = None,
    agent_fallback: Optional[str] = None,
) -> dict[str, Any]:
    """
    Poll Notion for unclaimed Inbox tasks once.
    
    Returns:
        {
            "processed": <count>,
            "dispatch_payloads": [<DispatchPayload>, ...],
            "errors": [<error>, ...],
        }
    """
    results = {
        "processed": 0,
        "dispatch_payloads": [],
        "errors": [],
    }

    try:
        notion_adapter = NotionAdapter.for_db(service.config, "tasks")
        
        # Query Notion for unclaimed Inbox tasks
        if verbose:
            print("[INFO] Querying Notion for unclaimed Inbox tasks...", file=sys.stderr)
        
        notion_tasks = notion_adapter.query_tasks(status="Inbox", limit=100)
        
        for notion_task in notion_tasks:
            if notion_task.archived:
                if verbose:
                    print(f"[SKIP] {notion_task.page_id}: archived", file=sys.stderr)
                continue
            
            # Skip if already claimed
            if notion_task.backend_task_id:
                if verbose:
                    print(f"[SKIP] {notion_task.page_id}: already claimed ({notion_task.backend_task_id})", file=sys.stderr)
                continue

            # Build operation key for idempotency
            operation_key = f"notion:{notion_task.page_id}"

            # Check if backend already has this operation key (race condition protection)
            try:
                existing_tasks = service.db.list_tasks_by_operation_key(operation_key)
                if existing_tasks:
                    if verbose:
                        print(f"[SKIP] {notion_task.page_id}: operation_key already exists (task {existing_tasks[0].id})", file=sys.stderr)
                    continue
            except Exception:
                pass

            # Classify the task
            try:
                # Extract Notion properties from raw_properties using config property names
                notion_domain = _extract_multi_select(
                    notion_task.raw_properties,
                    notion_adapter.config.properties.area
                )
                notion_type = _extract_multi_select(
                    notion_task.raw_properties,
                    notion_adapter.config.properties.type
                )
                # Extract risk from raw properties
                # Risk is a select field, typically named "Risk"
                notion_risk = _extract_select(notion_task.raw_properties, "Risk") or "low"

                classifier_result = classifier.classify(
                    title=notion_task.title,
                    notion_domain=notion_domain,
                    notion_type=notion_type,
                    notion_risk=notion_risk,
                )

                if verbose:
                    print(
                        f"[CLASSIFY] {notion_task.page_id}: {classifier_result.classification.domain}/"
                        f"{classifier_result.classification.intent_type} ({classifier_result.classification.risk_level}) "
                        f"→ {classifier_result.routing} ({classifier_result.agent})",
                        file=sys.stderr
                    )
            except Exception as exc:
                results["errors"].append(f"Classification failed for {notion_task.page_id}: {exc}")
                if verbose:
                    print(f"[ERROR] {notion_task.page_id}: classification failed: {exc}", file=sys.stderr)
                continue

            # Create backend task
            try:
                backend_task = service.create_request(
                    user_request=notion_task.title,
                    classification=classifier_result.classification,
                    operation_key=operation_key,
                    external_ref=notion_task.page_id,
                    action_source="openclaw_skill",
                )

                task_id = backend_task["task"].id
                if verbose:
                    print(f"[CREATE] {notion_task.page_id}: backend task {task_id} created", file=sys.stderr)

            except Exception as exc:
                results["errors"].append(f"Failed to create backend task for {notion_task.page_id}: {exc}")
                if verbose:
                    print(f"[ERROR] {notion_task.page_id}: failed to create backend task: {exc}", file=sys.stderr)
                continue

            # Update Notion: set task ID and status
            try:
                now = datetime.utcnow().isoformat() + "Z"
                notion_adapter.update_task_properties(
                    page_id=notion_task.page_id,
                    backend_task_id=task_id,
                    last_agent_update=now,
                )
                notion_adapter.update_task_status(
                    page_id=notion_task.page_id,
                    status="In progress",
                    last_agent_update=now,
                )

                if verbose:
                    print(f"[UPDATE] {notion_task.page_id}: Notion status → 'In progress', task ID set", file=sys.stderr)

            except Exception as exc:
                results["errors"].append(f"Failed to update Notion page {notion_task.page_id}: {exc}")
                if verbose:
                    print(f"[ERROR] {notion_task.page_id}: failed to update Notion: {exc}", file=sys.stderr)
                continue

            # Append note to Notion
            try:
                note = f"Picked up by agent — task {task_id} created"
                notion_adapter.append_note(page_id=notion_task.page_id, note=note)
                if verbose:
                    print(f"[NOTE] {notion_task.page_id}: appended note", file=sys.stderr)
            except Exception as exc:
                if verbose:
                    print(f"[WARN] {notion_task.page_id}: failed to append note: {exc}", file=sys.stderr)
                # Don't fail the whole task if note fails

            # Build dispatch payload
            try:
                effective_agent = agent_override or classifier_result.agent
                payload = dispatcher.build_payload(
                    task_id=task_id,
                    notion_page_id=notion_task.page_id,
                    title=notion_task.title,
                    classification=classifier_result.classification,
                    routing=classifier_result.routing,
                    agent=effective_agent,
                    fallback_agent=agent_fallback,
                )

                if classifier_result.routing == "auto_execute":
                    results["dispatch_payloads"].append(payload)
                    if verbose:
                        override_note = f" (overridden from {classifier_result.agent})" if agent_override else ""
                        print(f"[DISPATCH] {task_id}: queued for {effective_agent}{override_note}", file=sys.stderr)

            except Exception as exc:
                results["errors"].append(f"Failed to build dispatch payload for {task_id}: {exc}")
                if verbose:
                    print(f"[ERROR] {task_id}: failed to build dispatch payload: {exc}", file=sys.stderr)
                continue

            results["processed"] += 1

    except Exception as exc:
        results["errors"].append(f"Monitor loop failed: {exc}")
        if verbose:
            print(f"[ERROR] Monitor failed: {exc}", file=sys.stderr)

    return results


def output_dispatch_payloads(payloads: list[DispatchPayload]) -> None:
    """Output dispatch payloads as JSON lines (one per line)."""
    for payload in payloads:
        payload_dict = {
            "task_id": payload.task_id,
            "notion_page_id": payload.notion_page_id,
            "routing": payload.routing,
            "agent": payload.agent,
            "fallback_agent": payload.fallback_agent,
            "brief": payload.brief,
            "timeout_seconds": payload.timeout_seconds,
        }
        print(json.dumps(payload_dict))


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Monitor Notion for new tasks and dispatch them")
    parser.add_argument("--watch", action="store_true", help="Run in watch mode (poll repeatedly)")
    parser.add_argument("--interval", type=int, default=300, help="Poll interval in seconds (default 300)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output (to stderr)")
    parser.add_argument(
        "--agent-override",
        metavar="MODEL",
        default=None,
        help=(
            "Override the agent/model for all dispatched tasks "
            "(e.g. 'gemini-flash', 'openrouter/google/gemini-2.0-flash-exp:free'). "
            "Overrides both intake_routing.json rules and the agentOverride config key."
        ),
    )
    args = parser.parse_args()

    paths = default_paths()
    config = load_app_config(paths)
    service = AgenticOSService(paths, config)
    service.initialize()

    classifier = IntakeClassifier()
    dispatcher = Dispatcher()

    # CLI flag takes priority; fall back to config-level agentOverride
    agent_override = args.agent_override or config.agent_override
    agent_fallback = config.agent_fallback
    if agent_override and args.verbose:
        fallback_note = f" (fallback: {agent_fallback})" if agent_fallback else ""
        print(f"[INFO] Agent override active: {agent_override}{fallback_note}", file=sys.stderr)

    if args.watch:
        if args.verbose:
            print("[INFO] Starting Notion monitor in watch mode...", file=sys.stderr)

        while True:
            results = monitor_once(service, classifier, dispatcher, verbose=args.verbose, agent_override=agent_override, agent_fallback=agent_fallback)
            output_dispatch_payloads(results["dispatch_payloads"])

            if args.verbose:
                print(f"[MONITOR] Processed {results['processed']}, errors: {len(results['errors'])}", file=sys.stderr)
                for error in results["errors"]:
                    print(f"[ERROR] {error}", file=sys.stderr)

            time.sleep(args.interval)
    else:
        # Run once
        results = monitor_once(service, classifier, dispatcher, verbose=args.verbose, agent_override=agent_override, agent_fallback=agent_fallback)
        output_dispatch_payloads(results["dispatch_payloads"])

        if args.verbose:
            print(f"[MONITOR] Processed {results['processed']}, errors: {len(results['errors'])}", file=sys.stderr)
            for error in results["errors"]:
                print(f"[ERROR] {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
