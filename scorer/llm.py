"""
LLM-based scorer using OpenAI-compatible API (OpenRouter → Qwen).

Scoring pipeline per submission:
  1. Try to extract text from the attachment (pypdf for PDFs, python-docx for Word).
  2. If text extraction yields nothing (scanned/image PDF or image file), convert PDF
     pages to images via pymupdf and send them to the vision model instead.
  3. Ask the model to return a structured JSON scoring result.
  4. Parse the JSON into a ScoringResult.
"""
from __future__ import annotations

import base64
import io
import json
import re
import tarfile
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import httpx
from openai import APIConnectionError, APIStatusError, OpenAI

from config import settings
from models import Attachment, CriterionScore, ScoringResult, Submission, UncertainPart

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

_client: OpenAI | None = None

_UNREADABLE_MARKERS = (
    "no extractable text",
    "image file",
    "image-only",
    "image format",
    "manual review required",
    "Could not extract text",
    "Unknown file format",
)

_RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
_MAX_LLM_RETRIES = 4
_ARCHIVE_MAX_FILES = 20
_ARCHIVE_MAX_TOTAL_BYTES = 25 * 1024 * 1024
_MAX_TEXT_CHARS_PER_GROUP = 12000
_MAX_PDF_PAGES_PER_GROUP = 4
_MAX_REQUEST_CHARS = 350000
_MAX_CHUNK_REQUEST_CHARS = 180000
_SUPPORTED_ARCHIVE_FILE_EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
    ".jpg",
    ".jpeg",
    ".png",
    ".txt",
    ".md",
)

_WORD_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
}


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
        )
    return _client


def _is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, APIConnectionError):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in _RETRYABLE_STATUS_CODES
    if isinstance(exc, httpx.HTTPError):
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return status_code is None or status_code in _RETRYABLE_STATUS_CODES
    text = str(exc).lower()
    return any(token in text for token in ("timeout", "timed out", "bad gateway", "rate limit", "temporarily unavailable"))


def _is_request_too_large_error(exc: Exception) -> bool:
    if isinstance(exc, APIStatusError):
        return exc.status_code == 413
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code == 413:
        return True
    text = str(exc).lower()
    return "413" in text and "too large" in text


def _create_completion_with_retries(**kwargs):
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_LLM_RETRIES + 1):
        try:
            return _get_client().chat.completions.create(**kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt >= _MAX_LLM_RETRIES or not _is_retryable_error(exc):
                raise
            time.sleep(min(2 ** (attempt - 1), 8))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("LLM request failed without an exception")


_DEFAULT_PROMPT = _PROMPTS_DIR / "system_en.md"


def get_system_prompt(path: Path) -> str:
    """Load the system prompt from a file path."""
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"System prompt file not found: {path}\n"
        f"Built-in prompts are in {_PROMPTS_DIR}/: system_en.md, system_zh.md"
    )


# ---------------------------------------------------------------------------
# Attachment handling
# ---------------------------------------------------------------------------

def _extract_text(attachment: Attachment) -> str:
    """Extract text from a PDF, Word doc, or plain-text file."""
    data = attachment.data
    fname = attachment.filename.lower()

    if data[:4] == b'%PDF' or fname.endswith('.pdf'):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages = [page.extract_text() or "" for page in reader.pages]
            text = "\n\n".join(pages).strip()
            return text if text else "(PDF has no extractable text — may be scanned/image-only)"
        except Exception as e:
            return f"(Could not extract text from PDF: {e})"

    if data[:2] == b'PK' or fname.endswith(('.docx', '.doc')):
        try:
            from docx import Document
            doc = Document(io.BytesIO(data))
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
            xml_text = _extract_docx_text_from_xml(data)
            if len(xml_text) > len(text):
                text = xml_text
            return text if text else "(Word document has no extractable text)"
        except Exception as e:
            return f"(Could not extract text from Word document: {e})"

    if data[:3] == b'\xff\xd8\xff' or fname.endswith(('.jpg', '.jpeg', '.png')):
        return "(Submission is an image file — text extraction not supported)"

    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return f"(Unknown file format for {attachment.filename})"


def _extract_docx_text_from_xml(data: bytes) -> str:
    """Extract plain text from DOCX OOXML, including Office Math runs."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile):
        return ""

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""

    lines: list[str] = []
    for para in root.findall(".//w:p", _WORD_NS):
        parts: list[str] = []
        for elem in para.iter():
            if elem.tag in {
                f"{{{_WORD_NS['w']}}}t",
                f"{{{_WORD_NS['m']}}}t",
            }:
                if elem.text:
                    parts.append(elem.text)
            elif elem.tag == f"{{{_WORD_NS['w']}}}tab":
                parts.append("\t")
            elif elem.tag == f"{{{_WORD_NS['w']}}}br":
                parts.append("\n")
            elif elem.tag == f"{{{_WORD_NS['w']}}}sym":
                char = elem.attrib.get(f"{{{_WORD_NS['w']}}}char", "")
                if char == "F0E0":
                    parts.append(" -> ")
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _is_archive_attachment(attachment: Attachment) -> bool:
    fname = attachment.filename.lower()
    return (
        attachment.data[:4] == b"PK\x03\x04"
        or fname.endswith(".zip")
        or fname.endswith(".tar")
        or fname.endswith(".tar.gz")
        or fname.endswith(".tgz")
    )


def _is_supported_archive_member(filename: str) -> bool:
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in _SUPPORTED_ARCHIVE_FILE_EXTENSIONS)


def _extract_archive_attachments(attachment: Attachment) -> list[Attachment]:
    extracted: list[Attachment] = []
    total_bytes = 0

    def add_file(filename: str, data: bytes) -> None:
        nonlocal total_bytes
        if len(extracted) >= _ARCHIVE_MAX_FILES or total_bytes + len(data) > _ARCHIVE_MAX_TOTAL_BYTES:
            return
        extracted.append(
            Attachment(
                filename=f"{attachment.filename}::{filename}",
                content_type="application/octet-stream",
                data=data,
            )
        )
        total_bytes += len(data)

    try:
        if attachment.data[:4] == b"PK\x03\x04" or attachment.filename.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(attachment.data)) as zf:
                for info in zf.infolist():
                    if info.is_dir() or not _is_supported_archive_member(info.filename):
                        continue
                    if len(extracted) >= _ARCHIVE_MAX_FILES or total_bytes + info.file_size > _ARCHIVE_MAX_TOTAL_BYTES:
                        break
                    add_file(info.filename, zf.read(info))
            return extracted

        if (
            attachment.filename.lower().endswith(".tar")
            or attachment.filename.lower().endswith(".tar.gz")
            or attachment.filename.lower().endswith(".tgz")
        ):
            with tarfile.open(fileobj=io.BytesIO(attachment.data), mode="r:*") as tf:
                for member in tf.getmembers():
                    if not member.isfile() or not _is_supported_archive_member(member.name):
                        continue
                    if len(extracted) >= _ARCHIVE_MAX_FILES or total_bytes + member.size > _ARCHIVE_MAX_TOTAL_BYTES:
                        break
                    fileobj = tf.extractfile(member)
                    if fileobj is None:
                        continue
                    add_file(member.name, fileobj.read())
            return extracted
    except (tarfile.TarError, zipfile.BadZipFile, OSError):
        return []

    return extracted


def _pdf_has_embedded_images(data: bytes) -> bool:
    """Check if a PDF has embedded images (indicating it's a scanned PDF)."""
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=data, filetype="pdf")
        for page in doc:
            if page.get_images(full=True):
                return True
        return False
    except Exception:
        return False


def _needs_vision(text: str, attachment: Attachment | None = None) -> bool:
    if any(marker in text for marker in _UNREADABLE_MARKERS):
        return True

    # If PDF has embedded images, use vision mode (avoids OCR text layer issues)
    if attachment and attachment.filename.lower().endswith('.pdf'):
        if _pdf_has_embedded_images(attachment.data):
            return True

    # Very short extracted text almost certainly means watermarks only, not real content
    return len(text.strip()) < 200


def _pdf_to_image_parts(data: bytes) -> list[dict]:
    """Render PDF pages to images for vision model.

    Always renders pages directly rather than trying to extract embedded images,
    because PDFs often contain non-content images (ICC profiles, thumbnails, etc.)
    that aren't student work. Rendering ensures we see exactly what a human sees.
    """
    import fitz  # pymupdf
    doc = fitz.open(stream=data, filetype="pdf")
    parts = []
    for page in doc:
        mat = fitz.Matrix(2.0, 2.0)
        pix = page.get_pixmap(matrix=mat)
        b64 = base64.b64encode(pix.tobytes("png")).decode()
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
        })
    return parts


def _split_text_into_groups(label: str, text: str, max_chars: int = _MAX_TEXT_CHARS_PER_GROUP) -> list[list[dict]]:
    chunks = [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or [""]
    groups: list[list[dict]] = []
    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        suffix = f" (part {idx}/{total})" if total > 1 else ""
        groups.append([{"type": "text", "text": f"**{label}{suffix}**\n\n{chunk}"}])
    return groups


def _estimate_parts_size(parts: list[dict]) -> int:
    total = 0
    for part in parts:
        if part["type"] == "text":
            total += len(part.get("text", ""))
        elif part["type"] == "image_url":
            total += len(part.get("image_url", {}).get("url", ""))
    return total


def _pack_groups(groups: list[list[dict]], max_chars: int) -> list[list[dict]]:
    packed: list[list[dict]] = []
    current: list[dict] = []
    current_size = 0
    for group in groups:
        group_size = _estimate_parts_size(group)
        if current and current_size + group_size > max_chars:
            packed.append(current)
            current = []
            current_size = 0
        current.extend(group)
        current_size += group_size
    if current:
        packed.append(current)
    return packed


def _attachment_content_groups(attachment: Attachment) -> list[list[dict]]:
    """
    Return content groups for one attachment.

    Tries text extraction first. For scanned/image files falls back to sending
    the raw image bytes as image_url so the model can read them visually.
    """
    if _is_archive_attachment(attachment):
        extracted = _extract_archive_attachments(attachment)
        if not extracted:
            return [[{"type": "text", "text": f"**Archive: {attachment.filename}**\n\n(No supported files extracted from archive)"}]]

        groups: list[list[dict]] = [[
            {"type": "text", "text": f"**Archive: {attachment.filename}**\n\nExtracted {len(extracted)} file(s) for grading."}
        ]]
        for inner in extracted:
            groups.extend(_attachment_content_groups(inner))
        return groups

    text = _extract_text(attachment)
    if not _needs_vision(text, attachment):
        return _split_text_into_groups(f"File: {attachment.filename}", text)

    data = attachment.data
    fname = attachment.filename.lower()

    # JPEG submitted directly
    if data[:3] == b'\xff\xd8\xff' or fname.endswith(('.jpg', '.jpeg')):
        b64 = base64.b64encode(data).decode()
        return [[
            {"type": "text", "text": f"**File: {attachment.filename}** (image)"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}},
        ]]

    # PNG submitted directly
    if data[:8] == b'\x89PNG\r\n\x1a\n' or fname.endswith('.png'):
        b64 = base64.b64encode(data).decode()
        return [[
            {"type": "text", "text": f"**File: {attachment.filename}** (image)"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}},
        ]]

    # Scanned PDF — render pages to images
    if data[:4] == b'%PDF' or fname.endswith('.pdf'):
        try:
            img_parts = _pdf_to_image_parts(data)
            if img_parts:
                groups: list[list[dict]] = []
                total_pages = len(img_parts)
                for start in range(0, total_pages, _MAX_PDF_PAGES_PER_GROUP):
                    end = min(start + _MAX_PDF_PAGES_PER_GROUP, total_pages)
                    groups.append(
                        [{"type": "text", "text": f"**File: {attachment.filename}** (scanned pages {start + 1}-{end})"}]
                        + img_parts[start:end]
                    )
                return groups
        except Exception:
            pass

    # Unrenderable — pass the error text so the model knows
    return [[{"type": "text", "text": f"**File: {attachment.filename}**\n\n{text}"}]]


def _submission_content_groups(submission: Submission) -> list[list[dict]]:
    groups: list[list[dict]] = []
    if submission.text_content.strip():
        groups.extend(_split_text_into_groups("Text answer", submission.text_content))
    for att in submission.attachments:
        groups.extend(_attachment_content_groups(att))
    return groups


def _score_from_content(system_prompt: str, rubric: str, content: list[dict], extra: dict) -> dict:
    response = _create_completion_with_retries(
        model=settings.ta_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [{"type": "text", "text": f"## Scoring Rubric\n\n{rubric}\n\n## Student Submission"}] + content},
        ],
        temperature=0.2,
        **extra,
    )
    raw = response.choices[0].message.content or ""
    return _parse_json(raw)


def _score_from_chunk_summaries(system_prompt: str, rubric: str, chunks: list[list[dict]], extra: dict) -> dict:
    summary_prompt = (
        "You are extracting grading evidence from one chunk of a student's submission. "
        "Summarize only what is visible in this chunk. Organize by rubric criterion when possible. "
        "Do not assign scores. Do not infer that something is missing just because it is absent from this chunk."
    )
    summaries: list[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        response = _create_completion_with_retries(
            model=settings.ta_model,
            messages=[
                {"role": "system", "content": summary_prompt},
                {
                    "role": "user",
                    "content": [{"type": "text", "text": f"## Scoring Rubric\n\n{rubric}\n\n## Submission Chunk {idx}/{len(chunks)}"}] + chunk,
                },
            ],
            temperature=0.1,
            **extra,
        )
        summaries.append(response.choices[0].message.content or f"(Chunk {idx}: no summary returned)")

    combined = "\n\n".join(
        f"### Chunk {idx}\n{summary}" for idx, summary in enumerate(summaries, start=1)
    )
    response = _create_completion_with_retries(
        model=settings.ta_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"## Scoring Rubric\n\n{rubric}\n\n"
                            "## Student Submission Evidence Summaries\n\n"
                            "The original submission was split into chunks because it was too large for one API request. "
                            "Grade using the combined evidence summaries below.\n\n"
                            f"{combined}"
                        ),
                    }
                ],
            },
        ],
        temperature=0.2,
        **extra,
    )
    raw = response.choices[0].message.content or ""
    return _parse_json(raw)


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_submission(submission: Submission, rubric: str, prompt: Path = _DEFAULT_PROMPT) -> ScoringResult:
    """Score a single submission against the rubric using the LLM.

    Args:
        submission: The student submission to score.
        rubric: The rubric text.
        prompt: Path to the system prompt file. Defaults to prompts/system_en.md.
    """
    system_prompt = get_system_prompt(prompt)

    groups = _submission_content_groups(submission)
    content = [part for group in groups for part in group]

    extra: dict = {}
    if not settings.enable_thinking:
        # Only send enable_thinking for Qwen models on platforms that support it
        # Skip for Volcengine/other providers that don't support this parameter
        model_name = settings.ta_model.lower()
        if "qwen" in model_name and ("openrouter" in settings.openai_base_url or "dashscope" in settings.openai_base_url):
            extra["extra_body"] = {"enable_thinking": False}

    try:
        if _estimate_parts_size(content) > _MAX_REQUEST_CHARS:
            data = _score_from_chunk_summaries(system_prompt, rubric, _pack_groups(groups, _MAX_CHUNK_REQUEST_CHARS), extra)
        else:
            data = _score_from_content(system_prompt, rubric, content, extra)
    except Exception as exc:
        if not _is_request_too_large_error(exc):
            raise
        data = _score_from_chunk_summaries(system_prompt, rubric, _pack_groups(groups, _MAX_CHUNK_REQUEST_CHARS), extra)

    def _f(val, default: float = 0.0) -> float:
        return float(val) if val is not None else default

    uncertain = [
        UncertainPart(
            description=u.get("description") or u.get("criterion") or u.get("part") or str(u),
            suggested_score=_f(u.get("suggested_score")),
            suggested_max=_f(u.get("suggested_max")),
        )
        for u in data.get("uncertain_parts", [])
    ]
    breakdown = [
        CriterionScore(
            criterion=b.get("criterion", ""),
            points_awarded=_f(b.get("points_awarded")),
            points_max=_f(b.get("points_max")),
            reasoning=b.get("reasoning", ""),
        )
        for b in data.get("breakdown", [])
    ]
    confidence = _f(data.get("confidence"), 1.0)

    return ScoringResult(
        student_id=submission.student_id,
        student_name=submission.student_name,
        bb_user_id=submission.bb_user_id,
        assignment_id=submission.assignment_id,
        total_score=_f(data.get("total_score")),
        total_max=_f(data.get("total_max")),
        breakdown=breakdown,
        uncertain_parts=uncertain,
        confidence=confidence,
        llm_reasoning=data.get("llm_reasoning", ""),
        needs_review=confidence < settings.review_threshold or len(uncertain) > 0,
    )


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------

def _sanitize_json(s: str) -> str:
    """Escape stray backslashes (e.g. LaTeX \\frac) that make JSON invalid."""
    return re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)


def _parse_json(raw: str) -> dict:
    """Extract JSON from model output, tolerating minor formatting issues."""
    cleaned = re.sub(r"```(?:json)?\s*|```", "", raw).strip()
    for candidate in [cleaned, _sanitize_json(cleaned)]:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        block = match.group()
        for candidate in [block, _sanitize_json(block)]:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
    raise ValueError(f"Could not parse LLM response as JSON:\n{raw[:500]}")
