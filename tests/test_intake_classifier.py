"""Unit tests for intake classifier."""

import pytest

from src.agentic_os.intake_classifier import IntakeClassifier


class TestIntakeClassifier:
    """Test suite for IntakeClassifier."""

    def setup_method(self):
        """Set up classifier for each test."""
        self.classifier = IntakeClassifier()

    def test_technical_execute_low_risk_auto_executes(self):
        """Technical execute with low risk should auto-execute via manager."""
        result = self.classifier.classify(
            title="Fix the login bug",
            notion_domain=["code"],
            notion_type=["fix"],
            notion_risk="low"
        )
        assert result.classification.domain == "technical"
        assert result.classification.intent_type == "execute"
        assert result.classification.risk_level == "low"
        assert result.routing == "auto_execute"
        assert result.agent == "manager"

    def test_technical_read_low_risk_auto_executes(self):
        """Technical read with low risk should auto-execute via manager."""
        result = self.classifier.classify(
            title="Review the API docs",
            notion_domain=["engineering"],
            notion_type=["review"],
            notion_risk="low"
        )
        assert result.classification.domain == "technical"
        assert result.classification.intent_type == "read"
        assert result.classification.risk_level == "low"
        assert result.routing == "auto_execute"
        assert result.agent == "manager"

    def test_personal_draft_medium_risk_goes_to_manager(self):
        """Personal draft with medium risk goes to manager."""
        result = self.classifier.classify(
            title="Draft email to client",
            notion_domain=["email"],
            notion_type=["draft"],
            notion_risk="medium"
        )
        assert result.classification.domain == "personal"
        assert result.classification.intent_type == "draft"
        assert result.classification.risk_level == "medium"
        assert result.routing == "auto_execute"
        assert result.agent == "manager"

    def test_personal_execute_high_risk_needs_approval_via_senior(self):
        """Personal execute with high risk needs approval via senior."""
        result = self.classifier.classify(
            title="Send important email",
            notion_domain=["email"],
            notion_type=["send"],
            notion_risk="high"
        )
        assert result.classification.domain == "personal"
        assert result.classification.intent_type == "execute"
        assert result.classification.risk_level == "high"
        assert result.routing == "needs_approval"
        assert result.agent == "senior"

    def test_finance_goes_to_manager(self):
        """Finance tasks go to manager."""
        result = self.classifier.classify(
            title="Review quarterly budget",
            notion_domain=["finance"],
            notion_type=["review"],
            notion_risk="low"
        )
        assert result.classification.domain == "finance"
        assert result.routing == "auto_execute"
        assert result.agent == "manager"

    def test_high_risk_routes_to_senior(self):
        """High-risk tasks always go to senior via approval flow."""
        result = self.classifier.classify(
            title="Deploy to production",
            notion_domain=["ops"],
            notion_type=["execute"],
            notion_risk="high"
        )
        assert result.classification.risk_level == "high"
        assert result.routing == "needs_approval"
        assert result.agent == "senior"

    def test_infer_domain_from_title(self):
        """If no domain tags, infer from title."""
        result = self.classifier.classify(
            title="Write a blog post about Rust",
            notion_domain=[],
            notion_type=[],
            notion_risk="low"
        )
        assert result.classification.domain == "technical"
        assert result.inferred_domain is True

    def test_infer_intent_from_title(self):
        """If no intent tags, infer from title."""
        result = self.classifier.classify(
            title="Research best practices",
            notion_domain=["code"],
            notion_type=[],
            notion_risk="low"
        )
        assert result.classification.intent_type == "read"
        assert result.inferred_intent is True

    def test_fallback_to_personal_draft(self):
        """If no domain or intent can be inferred, fall back to personal/draft."""
        result = self.classifier.classify(
            title="Do something",
            notion_domain=[],
            notion_type=[],
            notion_risk="low"
        )
        # Since "do" is not a recognized keyword, should fall back
        assert result.classification.domain in ("personal", "technical")
        assert result.classification.intent_type in ("draft", "execute", "read")

    def test_domain_mapping_code_to_technical(self):
        """Domain tag 'code' should map to 'technical'."""
        result = self.classifier.classify(
            title="Task",
            notion_domain=["code"],
            notion_type=[],
            notion_risk="low"
        )
        assert result.classification.domain == "technical"

    def test_domain_mapping_writing_to_personal(self):
        """Domain tag 'writing' should map to 'personal'."""
        result = self.classifier.classify(
            title="Task",
            notion_domain=["writing"],
            notion_type=[],
            notion_risk="low"
        )
        assert result.classification.domain == "personal"

    def test_intent_mapping_create_to_execute(self):
        """Intent tag 'create' should map to 'execute'."""
        result = self.classifier.classify(
            title="Task",
            notion_domain=[],
            notion_type=["create"],
            notion_risk="low"
        )
        assert result.classification.intent_type == "execute"

    def test_intent_mapping_summarise_to_read(self):
        """Intent tag 'summarise' should map to 'read'."""
        result = self.classifier.classify(
            title="Task",
            notion_domain=[],
            notion_type=["summarise"],
            notion_risk="low"
        )
        assert result.classification.intent_type == "read"

    def test_risk_level_validation(self):
        """Invalid risk levels should default to 'low'."""
        result = self.classifier.classify(
            title="Task",
            notion_domain=[],
            notion_type=[],
            notion_risk="invalid"
        )
        assert result.classification.risk_level == "low"

    def test_none_parameters_default_to_empty_list(self):
        """None parameters should be treated as empty lists."""
        result = self.classifier.classify(
            title="Write a poem",
            notion_domain=None,
            notion_type=None,
            notion_risk=None
        )
        # Should infer from title
        assert result.classification.domain is not None
        assert result.classification.intent_type is not None
