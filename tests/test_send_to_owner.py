import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d


def test_send_to_owner_success(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_ROUTER_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123456")
    
    dummy_file = tmp_path / "test.md"
    dummy_file.write_text("Hello, world!")

    mock_post = MagicMock()
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"ok": True, "result": {"message_id": 999}}
    monkeypatch.setattr("httpx.Client.post", mock_post)

    res = d.send_to_owner([str(dummy_file)], "Test Doc")
    assert res == "message_id=999"
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert kwargs["data"]["chat_id"] == "123456"
    assert "document" in kwargs["files"]


def test_send_to_owner_missing_env(monkeypatch, tmp_path):
    monkeypatch.delenv("AI_ROUTER_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    
    dummy_file = tmp_path / "test.md"
    dummy_file.write_text("Hello")

    with pytest.raises(ValueError, match="AI_ROUTER_BOT_TOKEN or TELEGRAM_OWNER_CHAT_ID not in env"):
        d.send_to_owner([str(dummy_file)], "Test Doc")


def test_send_to_owner_file_not_found(monkeypatch):
    monkeypatch.setenv("AI_ROUTER_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123456")
    
    with pytest.raises(ValueError, match="File not found"):
        d.send_to_owner(["/non/existent/file.md"], "Test")


def test_send_to_owner_api_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_ROUTER_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123456")

    dummy_file = tmp_path / "test.md"
    dummy_file.write_text("Hello")

    mock_post = MagicMock()
    mock_post.side_effect = httpx.HTTPError("Bad Request")
    monkeypatch.setattr("httpx.Client.post", mock_post)

    with pytest.raises(RuntimeError, match="Telegram API error: Bad Request"):
        d.send_to_owner([str(dummy_file)], "Test Doc")


def test_send_to_owner_ok_false_is_loud(monkeypatch, tmp_path):
    # A 200 response with ok=false (or missing message_id) must raise, never
    # return "message_id=None" with exit 0 (anti-hallucination gate).
    monkeypatch.setenv("AI_ROUTER_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123456")

    dummy_file = tmp_path / "test.md"
    dummy_file.write_text("Hello")

    mock_post = MagicMock()
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"ok": False, "description": "chat not found"}
    monkeypatch.setattr("httpx.Client.post", mock_post)

    with pytest.raises(RuntimeError, match="Telegram refused test.md: chat not found"):
        d.send_to_owner([str(dummy_file)], "Test Doc")
