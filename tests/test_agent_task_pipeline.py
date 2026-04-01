import json

import ouroboros.agent_task_pipeline as pipeline


def test_task_summary_prefers_direct_model_when_openrouter_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "openai::gpt-5.4-mini")
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACK", "openai::gpt-5.4-mini")
    monkeypatch.setenv("OUROBOROS_MODEL", "openai::gpt-5.4")
    monkeypatch.setenv("OUROBOROS_MODEL_CODE", "openai::gpt-5.4")

    captured = {}

    class FakeLlm:
        def chat(self, *, messages, model, reasoning_effort, max_tokens):
            captured["messages"] = messages
            captured["model"] = model
            captured["reasoning_effort"] = reasoning_effort
            captured["max_tokens"] = max_tokens
            return {"content": "direct summary ok"}, {"cost": 0}

    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True)

    pipeline._run_task_summary(
        env=None,
        llm=FakeLlm(),
        task={"id": "task-123", "type": "task", "text": "Reply with exactly OK."},
        usage={"rounds": 1, "cost": 0.01},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        drive_logs=drive_logs,
    )

    assert captured["model"] == "openai::gpt-5.4-mini"
    chat_lines = (drive_logs / "chat.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(chat_lines) == 1
    payload = json.loads(chat_lines[0])
    assert payload["type"] == "task_summary"
    assert payload["text"] == "direct summary ok"


def test_task_summary_keeps_openrouter_model_when_key_present(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "openai::gpt-5.4-mini")

    assert (
        pipeline._resolve_task_summary_model("google/gemini-3-flash-preview")
        == "google/gemini-3-flash-preview"
    )
