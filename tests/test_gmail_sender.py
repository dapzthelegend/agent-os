"""
Unit tests for gmail_sender: OAuth2 refresh, MIME building, and send_email flow.

No real HTTP or filesystem calls are made.
"""
from __future__ import annotations

import base64
import json
import tempfile
from email import message_from_bytes
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.agentic_os import gmail_sender as gs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _creds_file(tmp_path: Path, *, missing: list[str] | None = None) -> str:
    data = {
        "web": {
            "client_id": "cid",
            "client_secret": "csec",
            "refresh_token": "rtok",
        }
    }
    for k in (missing or []):
        data["web"].pop(k, None)
    p = tmp_path / "creds.json"
    p.write_text(json.dumps(data))
    return str(p)


def _mock_token_response(access_token: str = "access_tok"):
    resp = MagicMock()
    resp.read.return_value = json.dumps({"access_token": access_token}).encode()
    resp.status = 200
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _mock_send_response(status: int = 200):
    resp = MagicMock()
    resp.read.return_value = b'{"id": "msg_123"}'
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# _load_credentials
# ---------------------------------------------------------------------------

class TestLoadCredentials:
    def test_loads_from_explicit_path(self, tmp_path):
        path = _creds_file(tmp_path)
        creds = gs._load_credentials(path)
        assert creds["client_id"] == "cid"
        assert creds["client_secret"] == "csec"
        assert creds["refresh_token"] == "rtok"

    def test_loads_from_env_var(self, tmp_path, monkeypatch):
        path = _creds_file(tmp_path)
        monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", path)
        creds = gs._load_credentials(None)
        assert creds["refresh_token"] == "rtok"

    def test_raises_on_missing_key(self, tmp_path):
        path = _creds_file(tmp_path, missing=["refresh_token"])
        with pytest.raises(ValueError, match="refresh_token"):
            gs._load_credentials(path)

    def test_raises_on_missing_file(self):
        with pytest.raises(FileNotFoundError):
            gs._load_credentials("/nonexistent/path/creds.json")


# ---------------------------------------------------------------------------
# _refresh_access_token
# ---------------------------------------------------------------------------

class TestRefreshAccessToken:
    def test_returns_access_token(self):
        with patch("src.agentic_os.gmail_sender.urllib_request.urlopen",
                   return_value=_mock_token_response("tok_abc")) as mock_open:
            token = gs._refresh_access_token("cid", "csec", "rtok")
        assert token == "tok_abc"

    def test_sends_correct_grant_type(self):
        captured = []

        def fake_open(req, timeout=None):
            captured.append(req)
            return _mock_token_response()

        with patch("src.agentic_os.gmail_sender.urllib_request.urlopen", side_effect=fake_open):
            gs._refresh_access_token("cid", "csec", "rtok")

        req = captured[0]
        body = req.data.decode()
        assert "grant_type=refresh_token" in body
        assert "client_id=cid" in body
        assert "refresh_token=rtok" in body

    def test_raises_when_no_access_token_in_response(self):
        bad_resp = MagicMock()
        bad_resp.read.return_value = json.dumps({"error": "invalid_grant"}).encode()
        bad_resp.__enter__ = lambda s: s
        bad_resp.__exit__ = MagicMock(return_value=False)

        with patch("src.agentic_os.gmail_sender.urllib_request.urlopen",
                   return_value=bad_resp):
            with pytest.raises(RuntimeError, match="Token refresh failed"):
                gs._refresh_access_token("cid", "csec", "rtok")


# ---------------------------------------------------------------------------
# _build_raw_message
# ---------------------------------------------------------------------------

class TestBuildRawMessage:
    def test_output_is_valid_base64url(self):
        raw = gs._build_raw_message(
            to="user@example.com",
            sender="from@example.com",
            subject="Test",
            body="Hello",
        )
        decoded = base64.urlsafe_b64decode(raw + "==")
        assert len(decoded) > 0

    def test_decoded_contains_headers(self):
        raw = gs._build_raw_message(
            to="to@example.com",
            sender="from@example.com",
            subject="My Subject",
            body="Body text",
        )
        decoded = base64.urlsafe_b64decode(raw + "==")
        msg = message_from_bytes(decoded)
        assert msg["To"] == "to@example.com"
        assert msg["From"] == "from@example.com"
        assert msg["Subject"] == "My Subject"

    def test_decoded_contains_body(self):
        raw = gs._build_raw_message(
            to="a@b.com", sender="s@b.com", subject="S", body="Hello world"
        )
        decoded = base64.urlsafe_b64decode(raw + "==")
        msg = message_from_bytes(decoded)
        payload = msg.get_payload(decode=True)
        assert b"Hello world" in payload


# ---------------------------------------------------------------------------
# send_email (integration of all steps)
# ---------------------------------------------------------------------------

class TestSendEmail:
    def test_returns_true_on_success(self, tmp_path):
        path = _creds_file(tmp_path)

        with patch("src.agentic_os.gmail_sender.urllib_request.urlopen",
                   side_effect=[_mock_token_response(), _mock_send_response()]):
            result = gs.send_email(
                to="user@example.com",
                subject="Hello",
                body="World",
                credentials_path=path,
            )
        assert result is True

    def test_returns_false_on_http_error(self, tmp_path):
        from urllib.error import HTTPError
        path = _creds_file(tmp_path)

        http_err = HTTPError(
            url=gs.GMAIL_SEND_URL,
            code=403,
            msg="Forbidden",
            hdrs={},  # type: ignore[arg-type]
            fp=None,
        )
        http_err.read = lambda: b"forbidden"

        with patch("src.agentic_os.gmail_sender.urllib_request.urlopen",
                   side_effect=[_mock_token_response(), http_err]):
            result = gs.send_email(
                to="user@example.com",
                subject="Hello",
                body="World",
                credentials_path=path,
            )
        assert result is False

    def test_uses_franchieinc_sender_by_default(self, tmp_path, capsys):
        path = _creds_file(tmp_path)
        sent_raw = []

        def fake_open(req, timeout=None):
            # first call is token refresh
            if "oauth2" in req.full_url:
                return _mock_token_response()
            # second call is Gmail send
            body = json.loads(req.data.decode())
            sent_raw.append(body["raw"])
            return _mock_send_response()

        with patch("src.agentic_os.gmail_sender.urllib_request.urlopen",
                   side_effect=fake_open):
            gs.send_email(to="x@y.com", subject="S", body="B", credentials_path=path)

        decoded = base64.urlsafe_b64decode(sent_raw[0] + "==")
        msg = message_from_bytes(decoded)
        assert msg["From"] == "franchieinc@gmail.com"

    def test_bearer_token_in_gmail_request(self, tmp_path):
        path = _creds_file(tmp_path)
        captured_reqs = []

        def fake_open(req, timeout=None):
            captured_reqs.append(req)
            if "oauth2" in req.full_url:
                return _mock_token_response("my_access_token")
            return _mock_send_response()

        with patch("src.agentic_os.gmail_sender.urllib_request.urlopen",
                   side_effect=fake_open):
            gs.send_email(to="x@y.com", subject="S", body="B", credentials_path=path)

        gmail_req = captured_reqs[1]
        assert gmail_req.get_header("Authorization") == "Bearer my_access_token"
