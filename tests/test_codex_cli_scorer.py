import json
from pathlib import Path
from unittest.mock import MagicMock

from models import Submission
from scorer.codex_cli import build_codex_exec_command, score_submission_with_codex_cli


VALID_CODEX_RESPONSE = {
    "total_score": 10,
    "total_max": 10,
    "confidence": 0.9,
    "breakdown": [
        {
            "criterion": "Correctness",
            "points_awarded": 10,
            "points_max": 10,
            "reasoning": "Full marks.",
        }
    ],
    "uncertain_parts": [],
    "llm_reasoning": "Correct.",
}


def test_build_codex_exec_command_uses_noninteractive_schema_output():
    command = build_codex_exec_command(
        schema_path=Path("/tmp/schema.json"),
        output_path=Path("/tmp/result.json"),
        model="gpt-5.1-codex-mini",
    )

    assert command[:2] == ["codex", "exec"]
    assert "--ephemeral" in command
    assert "--skip-git-repo-check" in command
    assert command[command.index("-s") + 1] == "read-only"
    assert command[command.index("--output-schema") + 1] == "/tmp/schema.json"
    assert command[command.index("-o") + 1] == "/tmp/result.json"
    assert command[command.index("-m") + 1] == "gpt-5.1-codex-mini"
    assert command[-1] == "-"


def test_score_submission_with_codex_cli_inherits_parent_environment(monkeypatch, tmp_path):
    prompt_file = tmp_path / "system.md"
    prompt_file.write_text("Return JSON.", encoding="utf-8")

    def fake_run(command, **kwargs):
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text(json.dumps(VALID_CODEX_RESPONSE), encoding="utf-8")
        assert "env" not in kwargs
        assert kwargs["text"] is True
        assert kwargs["capture_output"] is True
        assert kwargs["input"]
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("scorer.codex_cli.subprocess.run", fake_run)

    submission = Submission(
        student_id="2100012345",
        student_name="Test Student",
        assignment_id="423829",
        assignment_title="HW1",
        text_content="Answer.",
    )

    result = score_submission_with_codex_cli(submission, "Rubric.", prompt_file)

    assert result.total_score == 10
    assert result.needs_review is False
