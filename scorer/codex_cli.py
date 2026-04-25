"""
Scoring backend that delegates one submission to the local Codex CLI.

The subprocess intentionally inherits the parent process environment. This keeps
VPN/proxy settings identical to the terminal that launched the PKU-AI-TA command.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from models import ScoringResult, Submission
from scorer.llm import (
    _parse_json,
    get_system_prompt,
    scoring_result_from_data,
    submission_text_for_prompt,
)

_DEFAULT_PROMPT = Path(__file__).parent.parent / "prompts" / "system_en.md"


def build_codex_exec_command(
    *,
    schema_path: Path,
    output_path: Path,
    model: str = "",
) -> list[str]:
    """Build the non-interactive Codex CLI command used for grading."""
    command = [
        "codex",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-s",
        "read-only",
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
    ]
    if model:
        command.extend(["-m", model])
    command.append("-")
    return command


def score_submission_with_codex_cli(
    submission: Submission,
    rubric: str,
    prompt: Path = _DEFAULT_PROMPT,
    *,
    model: str = "",
    timeout: int = 900,
) -> ScoringResult:
    """Score a single submission by invoking `codex exec` non-interactively."""
    system_prompt = get_system_prompt(prompt)
    prompt_text = _build_grading_prompt(system_prompt, rubric, submission)

    with tempfile.TemporaryDirectory(prefix="pku-ai-ta-codex-") as tmp:
        tmpdir = Path(tmp)
        schema_path = tmpdir / "grading_schema.json"
        output_path = tmpdir / "result.json"
        schema_path.write_text(json.dumps(_grading_schema(), ensure_ascii=False), encoding="utf-8")

        command = build_codex_exec_command(
            schema_path=schema_path,
            output_path=output_path,
            model=model,
        )
        result = subprocess.run(
            command,
            input=prompt_text,
            text=True,
            capture_output=True,
            cwd=tmpdir,
            timeout=timeout,
            check=False,
        )

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Codex CLI scoring failed with exit code {result.returncode}: {detail[:1000]}")

        raw = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
        if not raw:
            raw = result.stdout.strip()

    data = _parse_json(raw)
    return scoring_result_from_data(submission, data)


def _build_grading_prompt(system_prompt: str, rubric: str, submission: Submission) -> str:
    submission_text = submission_text_for_prompt(submission)
    return f"""
You are running as a non-interactive grading backend. Do not edit files. Do not run commands unless absolutely necessary.
Return only the JSON object requested by the grading instructions and JSON schema.

## Grading Instructions

{system_prompt}

## Scoring Rubric

{rubric}

## Student Metadata

- student_id: {submission.student_id}
- student_name: {submission.student_name}
- assignment_id: {submission.assignment_id}
- assignment_title: {submission.assignment_title}

## Student Submission

{submission_text}
""".strip()


def _grading_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "total_score",
            "total_max",
            "confidence",
            "breakdown",
            "uncertain_parts",
            "llm_reasoning",
        ],
        "properties": {
            "total_score": {"type": "number"},
            "total_max": {"type": "number"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "breakdown": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["criterion", "points_awarded", "points_max", "reasoning"],
                    "properties": {
                        "criterion": {"type": "string"},
                        "points_awarded": {"type": "number"},
                        "points_max": {"type": "number"},
                        "reasoning": {"type": "string"},
                    },
                },
            },
            "uncertain_parts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["description", "suggested_score", "suggested_max"],
                    "properties": {
                        "description": {"type": "string"},
                        "suggested_score": {"type": "number"},
                        "suggested_max": {"type": "number"},
                    },
                },
            },
            "llm_reasoning": {"type": "string"},
        },
    }
