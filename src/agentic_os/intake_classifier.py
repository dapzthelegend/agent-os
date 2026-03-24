"""Intake classifier — maps Notion task properties to backend classification + routing decision."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .models import RequestClassification


@dataclass(frozen=True)
class ClassifierResult:
    """Result of classifying a task for dispatch."""
    classification: RequestClassification
    routing: str          # auto_execute | needs_approval | escalate
    agent: str            # sonnet | opus | codex
    inferred_domain: bool = False
    inferred_intent: bool = False


class IntakeClassifier:
    """Classifies Notion tasks into backend domain/intent/risk + routing decision."""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        """
        Initialize classifier with routing rules.
        
        Args:
            config_path: path to intake_routing.json. If None, looks for it in the
                        agentic-os repo root or falls back to defaults.
        """
        if config_path is None:
            # Try to find intake_routing.json in the agentic-os repo root
            candidates = [
                Path(__file__).parent.parent.parent / "intake_routing.json",
                Path.cwd() / "intake_routing.json",
            ]
            for candidate in candidates:
                if candidate.exists():
                    config_path = candidate
                    break
            
            if config_path is None:
                # Use built-in defaults
                config_path = None

        if config_path and config_path.exists():
            with open(config_path) as f:
                config = json.load(f)
        else:
            # Built-in defaults matching the brief
            config = {
                "rules": [
                    {"domain": "technical", "intent_type": "execute", "risk": "low",    "routing": "auto_execute",   "agent": "sonnet"},
                    {"domain": "technical", "intent_type": "read",    "risk": "low",    "routing": "auto_execute",   "agent": "sonnet"},
                    {"domain": "personal",  "intent_type": "draft",   "risk": "medium", "routing": "needs_approval", "agent": "sonnet"},
                    {"domain": "personal",  "intent_type": "execute", "risk": "high",   "routing": "needs_approval", "agent": "opus"},
                    {"domain": "finance",   "intent_type": "*",       "risk": "*",      "routing": "needs_approval", "agent": "sonnet"},
                    {"domain": "*",         "intent_type": "*",       "risk": "high",   "routing": "needs_approval", "agent": "opus"},
                    {"domain": "*",         "intent_type": "*",       "risk": "*",      "routing": "auto_execute",   "agent": "sonnet"}
                ],
                "domain_map": {
                    "content": "personal",
                    "writing": "personal",
                    "research": "personal",
                    "code": "technical",
                    "engineering": "technical",
                    "email": "personal",
                    "ops": "system",
                    "system": "system",
                    "finance": "finance",
                    "money": "finance"
                },
                "intent_map": {
                    "write": "draft",
                    "draft": "draft",
                    "build": "execute",
                    "create": "execute",
                    "fix": "execute",
                    "research": "read",
                    "summarise": "read",
                    "review": "read",
                    "send": "execute",
                    "analyse": "read",
                    "article": "content",
                    "blog": "content",
                    "document": "content",
                    "report": "content",
                    "summary": "content",
                    "writeup": "content",
                    "explainer": "content"
                }
            }
        
        self.rules = config.get("rules", [])
        self.domain_map = config.get("domain_map", {})
        self.intent_map = config.get("intent_map", {})

    def classify(
        self,
        title: str,
        notion_domain: Optional[list[str]] = None,
        notion_type: Optional[list[str]] = None,
        notion_risk: Optional[str] = None,
    ) -> ClassifierResult:
        """
        Classify a Notion task into backend domain/intent/risk + routing.
        
        Args:
            title: Notion page title / task description
            notion_domain: list of Domain tags from Notion (e.g. ["code", "writing"])
            notion_type: list of Type tags from Notion (e.g. ["draft", "execute"])
            notion_risk: Risk property from Notion (e.g. "low", "medium", "high")
        
        Returns:
            ClassifierResult with classification, routing, agent
        """
        notion_domain = notion_domain or []
        notion_type = notion_type or []
        notion_risk = notion_risk or "low"

        inferred_domain = False
        inferred_intent = False

        # Map Notion domain tags → backend domain
        domain = None
        if notion_domain:
            # Use first mapped domain tag
            for tag in notion_domain:
                if tag.lower() in self.domain_map:
                    domain = self.domain_map[tag.lower()]
                    break
        
        # If no domain from tags, infer from title
        if not domain:
            domain = self._infer_domain_from_title(title)
            inferred_domain = bool(domain)
        
        if not domain:
            domain = "personal"  # fallback
            inferred_domain = True

        # Map Notion type tags → backend intent_type
        intent_type = None
        if notion_type:
            # Use first mapped intent type
            for tag in notion_type:
                if tag.lower() in self.intent_map:
                    intent_type = self.intent_map[tag.lower()]
                    break
        
        # If no intent from tags, infer from title
        if not intent_type:
            intent_type = self._infer_intent_from_title(title)
            inferred_intent = bool(intent_type)
        
        if not intent_type:
            intent_type = "draft"  # fallback
            inferred_intent = True

        # Map Notion risk → backend risk_level
        risk_level = notion_risk.lower() if notion_risk else "low"
        if risk_level not in ("low", "medium", "high"):
            risk_level = "low"

        # Build classification
        classification = RequestClassification(
            domain=domain,
            intent_type=intent_type,
            risk_level=risk_level,
        ).validate()

        # Look up routing rule
        routing, agent = self._lookup_routing(domain, intent_type, risk_level)

        return ClassifierResult(
            classification=classification,
            routing=routing,
            agent=agent,
            inferred_domain=inferred_domain,
            inferred_intent=inferred_intent,
        )

    def _infer_domain_from_title(self, title: str) -> Optional[str]:
        """Infer domain from task title using keyword matching."""
        title_lower = title.lower()
        
        # Check keywords in order of specificity
        keywords = {
            "technical": ["code", "python", "javascript", "rust", "build", "deploy", "bug", "fix", "refactor", "test"],
            "finance": ["budget", "expense", "invoice", "payment", "money", "financial", "accounting"],
            "system": ["ops", "devops", "infrastructure", "docker", "kubernetes", "aws", "gcp"],
            "personal": ["email", "write", "draft", "content", "article", "note", "reminder"],
        }
        
        for domain, keywords_list in keywords.items():
            for keyword in keywords_list:
                if keyword in title_lower:
                    return domain
        
        return None

    def _infer_intent_from_title(self, title: str) -> Optional[str]:
        """Infer intent type from task title using keyword matching."""
        title_lower = title.lower()

        # Content intent is highest priority — check before draft/execute
        content_keywords = [
            "article", "blog post", "blog", "write a summary", "write up",
            "writeup", "document", "report", "explainer", "write a report",
            "research summary", "content piece",
        ]
        for keyword in content_keywords:
            if keyword in title_lower:
                return "content"

        keywords = {
            "execute": ["build", "create", "deploy", "fix", "run", "make", "send"],
            "draft": ["draft", "write", "compose", "outline", "sketch"],
            "read": ["research", "review", "analyse", "analyze", "summarize", "summarise", "read", "investigate"],
        }

        for intent, keywords_list in keywords.items():
            for keyword in keywords_list:
                if keyword in title_lower:
                    return intent

        return None

    def _lookup_routing(self, domain: str, intent_type: str, risk_level: str) -> tuple[str, str]:
        """
        Look up routing rule. Rules are evaluated top-to-bottom; first match wins.
        
        Returns:
            (routing, agent) tuple
        """
        for rule in self.rules:
            rule_domain = rule["domain"]
            rule_intent = rule["intent_type"]
            rule_risk = rule["risk"]
            
            # Check if rule matches
            domain_match = (rule_domain == "*" or rule_domain == domain)
            intent_match = (rule_intent == "*" or rule_intent == intent_type)
            risk_match = (rule_risk == "*" or rule_risk == risk_level)
            
            if domain_match and intent_match and risk_match:
                return (rule["routing"], rule["agent"])
        
        # Fallback (should not reach if rules are complete)
        return ("auto_execute", "sonnet")
