"""Unit tests for dispatcher."""

import pytest

from src.agentic_os.dispatcher import Dispatcher, DispatchPayload
from src.agentic_os.models import RequestClassification


class TestDispatcher:
    """Test suite for Dispatcher."""

    def setup_method(self):
        """Set up dispatcher for each test."""
        self.dispatcher = Dispatcher()

    def test_personal_draft_brief(self):
        """Personal draft should use content writing template."""
        classification = RequestClassification(
            domain="personal",
            intent_type="draft",
            risk_level="medium",
        )
        payload = self.dispatcher.build_payload(
            task_id="task_000001",
            paperclip_issue_id="page_001",
            title="Write a poem about TypeScript",
            classification=classification,
            routing="auto_execute",
            agent="sonnet",
        )
        assert "Dara" in payload.brief
        assert "task_000001" in payload.brief
        assert "RESULT_START" in payload.brief
        assert "RESULT_END" in payload.brief
        assert "TASK_DONE: task_000001" in payload.brief
        assert "/Users/dara/agents/bin/submit-result task_000001" in payload.brief

    def test_technical_execute_brief(self):
        """Technical execute should use code/research template."""
        classification = RequestClassification(
            domain="technical",
            intent_type="execute",
            risk_level="low",
        )
        payload = self.dispatcher.build_payload(
            task_id="task_000002",
            paperclip_issue_id="page_002",
            title="Fix the login form",
            classification=classification,
            routing="auto_execute",
            agent="sonnet",
        )
        assert "technical" in payload.brief.lower()
        assert "task_000002" in payload.brief
        assert "RESULT_START" in payload.brief
        assert "RESULT_END" in payload.brief
        assert "TASK_DONE: task_000002" in payload.brief
        assert "/Users/dara/agents/bin/submit-result task_000002" in payload.brief
        assert "code" in payload.brief.lower() or "write" in payload.brief.lower()

    def test_technical_read_brief(self):
        """Technical read should use code/research template."""
        classification = RequestClassification(
            domain="technical",
            intent_type="read",
            risk_level="low",
        )
        payload = self.dispatcher.build_payload(
            task_id="task_000003",
            paperclip_issue_id="page_003",
            title="Research API patterns",
            classification=classification,
            routing="auto_execute",
            agent="sonnet",
        )
        assert "technical" in payload.brief.lower()
        assert "task_000003" in payload.brief

    def test_sonnet_default_timeout(self):
        """Sonnet agent should get 300s timeout."""
        classification = RequestClassification(
            domain="technical",
            intent_type="read",
            risk_level="low",
        )
        payload = self.dispatcher.build_payload(
            task_id="task_000004",
            paperclip_issue_id="page_004",
            title="Read something",
            classification=classification,
            routing="auto_execute",
            agent="sonnet",
        )
        assert payload.timeout_seconds == 300

    def test_opus_longer_timeout(self):
        """Opus agent should get 600s timeout."""
        classification = RequestClassification(
            domain="technical",
            intent_type="execute",
            risk_level="high",
        )
        payload = self.dispatcher.build_payload(
            task_id="task_000005",
            paperclip_issue_id="page_005",
            title="Complex task",
            classification=classification,
            routing="needs_approval",
            agent="opus",
        )
        assert payload.timeout_seconds == 600

    def test_codex_default_timeout(self):
        """Codex agent should get 300s timeout."""
        classification = RequestClassification(
            domain="technical",
            intent_type="execute",
            risk_level="low",
        )
        payload = self.dispatcher.build_payload(
            task_id="task_000006",
            paperclip_issue_id="page_006",
            title="Code task",
            classification=classification,
            routing="auto_execute",
            agent="codex",
        )
        assert payload.timeout_seconds == 300

    def test_payload_has_all_fields(self):
        """Payload should have all required fields."""
        classification = RequestClassification(
            domain="personal",
            intent_type="draft",
            risk_level="low",
        )
        payload = self.dispatcher.build_payload(
            task_id="task_000007",
            paperclip_issue_id="page_007",
            title="Test task",
            classification=classification,
            routing="auto_execute",
            agent="sonnet",
        )
        assert payload.task_id == "task_000007"
        assert payload.paperclip_issue_id == "page_007"
        assert payload.routing == "auto_execute"
        assert payload.agent == "sonnet"
        assert isinstance(payload.brief, str)
        assert len(payload.brief) > 0
        assert payload.timeout_seconds > 0

    def test_brief_includes_task_title(self):
        """Brief should include the task title."""
        classification = RequestClassification(
            domain="personal",
            intent_type="draft",
            risk_level="low",
        )
        title = "Write a unique poem"
        payload = self.dispatcher.build_payload(
            task_id="task_000008",
            paperclip_issue_id="page_008",
            title=title,
            classification=classification,
            routing="auto_execute",
            agent="sonnet",
        )
        assert title in payload.brief

    def test_brief_includes_task_id(self):
        """Brief should include the task ID for tracking."""
        classification = RequestClassification(
            domain="technical",
            intent_type="execute",
            risk_level="low",
        )
        task_id = "task_000009"
        payload = self.dispatcher.build_payload(
            task_id=task_id,
            paperclip_issue_id="page_009",
            title="Do something",
            classification=classification,
            routing="auto_execute",
            agent="sonnet",
        )
        assert task_id in payload.brief

    def test_generic_template_fallback(self):
        """Unknown domain/intent combos should use generic template."""
        classification = RequestClassification(
            domain="system",
            intent_type="capture",
            risk_level="medium",
        )
        payload = self.dispatcher.build_payload(
            task_id="task_000010",
            paperclip_issue_id="page_010",
            title="Capture data",
            classification=classification,
            routing="needs_approval",
            agent="sonnet",
        )
        # Should still have RESULT_START/RESULT_END
        assert "RESULT_START" in payload.brief
        assert "RESULT_END" in payload.brief
        assert "TASK_DONE" in payload.brief

    def test_dispatch_payload_is_dataclass(self):
        """DispatchPayload should be a properly formed dataclass."""
        classification = RequestClassification(
            domain="personal",
            intent_type="draft",
            risk_level="low",
        )
        payload = self.dispatcher.build_payload(
            task_id="task_000011",
            paperclip_issue_id="page_011",
            title="Test",
            classification=classification,
            routing="auto_execute",
            agent="sonnet",
        )
        # Should be able to convert to dict
        payload_dict = vars(payload)
        assert "task_id" in payload_dict
        assert "paperclip_issue_id" in payload_dict
        assert "routing" in payload_dict
        assert "agent" in payload_dict
        assert "brief" in payload_dict
        assert "timeout_seconds" in payload_dict
