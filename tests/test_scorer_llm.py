import io
import json
import tarfile
import zipfile
import pytest
from unittest.mock import MagicMock, patch

from models import Attachment, Submission
from scorer.llm import _parse_json, _extract_text, _extract_docx_text_from_xml, score_submission


VALID_LLM_RESPONSE = {
    "total_score": 85.0,
    "total_max": 100.0,
    "confidence": 0.9,
    "breakdown": [
        {"criterion": "Correctness", "points_awarded": 40.0, "points_max": 50.0, "reasoning": "Mostly correct."},
        {"criterion": "Style", "points_awarded": 45.0, "points_max": 50.0, "reasoning": "Clean code."},
    ],
    "uncertain_parts": [],
    "llm_reasoning": "Good overall submission.",
}


class TestParseJson:
    def test_clean_json(self):
        raw = json.dumps(VALID_LLM_RESPONSE)
        result = _parse_json(raw)
        assert result["total_score"] == 85.0

    def test_strips_markdown_fences(self):
        raw = f"```json\n{json.dumps(VALID_LLM_RESPONSE)}\n```"
        result = _parse_json(raw)
        assert result["confidence"] == 0.9

    def test_strips_plain_fences(self):
        raw = f"```\n{json.dumps(VALID_LLM_RESPONSE)}\n```"
        result = _parse_json(raw)
        assert result["total_max"] == 100.0

    def test_extracts_json_from_surrounding_text(self):
        raw = f"Here is the score:\n{json.dumps(VALID_LLM_RESPONSE)}\nEnd."
        result = _parse_json(raw)
        assert result["total_score"] == 85.0

    def test_raises_on_invalid_json(self):
        with pytest.raises((ValueError, Exception)):
            _parse_json("This is not JSON at all.")


class TestExtractText:
    def test_returns_string(self):
        att = Attachment(filename="report.pdf", content_type="application/pdf", data=b"%PDF-fake")
        result = _extract_text(att)
        assert isinstance(result, str)

    def test_handles_unreadable_pdf(self):
        att = Attachment(filename="bad.pdf", content_type="application/pdf", data=b"not a real pdf")
        result = _extract_text(att)
        # Should not raise; returns an error message instead
        assert isinstance(result, str)

    def test_extracts_docx_math_text_from_xml(self):
        xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                    xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">
          <w:body>
            <w:p>
              <w:r><w:t>X_1:</w:t></w:r>
              <m:oMath><m:r><m:t>2</m:t></m:r><m:r><m:t>X</m:t></m:r><m:r><m:t>_2</m:t></m:r></m:oMath>
            </w:p>
          </w:body>
        </w:document>"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", xml)

        result = _extract_docx_text_from_xml(buf.getvalue())
        assert "X_1:" in result
        assert "2X_2" in result


class TestScoreSubmission:
    def _make_submission(self, text="My answer.", attachments=None):
        return Submission(
            student_id="2100012345",
            student_name="Test Student",
            assignment_id="_1_1",
            assignment_title="HW1",
            text_content=text,
            attachments=attachments or [],
        )

    def _mock_response(self, content: str):
        choice = MagicMock()
        choice.message.content = content
        resp = MagicMock()
        resp.choices = [choice]
        return resp

    @patch("scorer.llm._get_client")
    def test_returns_scoring_result(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_response(
            json.dumps(VALID_LLM_RESPONSE)
        )
        mock_get_client.return_value = mock_client

        result = score_submission(self._make_submission(), rubric="Grade on correctness (50pts) and style (50pts).")

        assert result.student_id == "2100012345"
        assert result.total_score == 85.0
        assert result.total_max == 100.0
        assert len(result.breakdown) == 2
        assert result.needs_review is False  # confidence=0.9 > threshold=0.75

    @patch("scorer.llm._get_client")
    def test_flags_low_confidence_for_review(self, mock_get_client):
        low_conf = {**VALID_LLM_RESPONSE, "confidence": 0.5}
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_response(json.dumps(low_conf))
        mock_get_client.return_value = mock_client

        result = score_submission(self._make_submission(), rubric="Rubric.")
        assert result.needs_review is True

    @patch("scorer.llm._get_client")
    def test_flags_uncertain_parts_for_review(self, mock_get_client):
        with_uncertain = {
            **VALID_LLM_RESPONSE,
            "confidence": 0.9,
            "uncertain_parts": [{"description": "Unclear argument", "suggested_score": 5.0, "suggested_max": 10.0}],
        }
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_response(json.dumps(with_uncertain))
        mock_get_client.return_value = mock_client

        result = score_submission(self._make_submission(), rubric="Rubric.")
        assert result.needs_review is True
        assert len(result.uncertain_parts) == 1

    @patch("scorer.llm._get_client")
    def test_includes_attachment_in_message(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_response(json.dumps(VALID_LLM_RESPONSE))
        mock_get_client.return_value = mock_client

        att = Attachment(filename="essay.pdf", content_type="application/pdf", data=b"%PDF")
        sub = self._make_submission(attachments=[att])
        score_submission(sub, rubric="Rubric.")

        call_args = mock_client.chat.completions.create.call_args
        messages = call_args.kwargs["messages"]
        user_content = messages[1]["content"]

        # Attachment text is merged into a "text" content part
        types = [p["type"] for p in user_content]
        assert "text" in types
        # The combined text part should mention the filename
        texts = [p["text"] for p in user_content if p["type"] == "text"]
        assert any("essay.pdf" in t for t in texts)

    @patch("scorer.llm._get_client")
    def test_extracts_zip_attachment_contents(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_response(json.dumps(VALID_LLM_RESPONSE))
        mock_get_client.return_value = mock_client

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("answer.txt", "Problem 1 solution")
        att = Attachment(filename="submission.zip", content_type="application/zip", data=buf.getvalue())

        score_submission(self._make_submission(attachments=[att]), rubric="Rubric.")

        user_content = mock_client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        texts = [p["text"] for p in user_content if p["type"] == "text"]
        assert any("Archive: submission.zip" in t for t in texts)
        assert any("answer.txt" in t for t in texts)
        assert any("Problem 1 solution" in t for t in texts)

    @patch("scorer.llm._get_client")
    def test_extracts_tar_gz_attachment_contents(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_response(json.dumps(VALID_LLM_RESPONSE))
        mock_get_client.return_value = mock_client

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b"Problem 2 solution"
            info = tarfile.TarInfo(name="work/answer.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        att = Attachment(filename="submission.tar.gz", content_type="application/gzip", data=buf.getvalue())

        score_submission(self._make_submission(attachments=[att]), rubric="Rubric.")

        user_content = mock_client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        texts = [p["text"] for p in user_content if p["type"] == "text"]
        assert any("Archive: submission.tar.gz" in t for t in texts)
        assert any("work/answer.txt" in t for t in texts)
        assert any("Problem 2 solution" in t for t in texts)

    @patch("scorer.llm._score_from_chunk_summaries")
    @patch("scorer.llm._estimate_parts_size")
    def test_uses_chunked_scoring_for_large_requests(self, mock_estimate_parts_size, mock_score_from_chunk_summaries):
        mock_estimate_parts_size.return_value = 10**9
        mock_score_from_chunk_summaries.return_value = VALID_LLM_RESPONSE

        result = score_submission(self._make_submission(text="Large answer"), rubric="Rubric.")

        assert result.total_score == 85.0
        assert mock_score_from_chunk_summaries.called
