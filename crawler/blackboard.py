"""
Crawler for course.pku.edu.cn (Blackboard Learn).

NOTE: Blackboard's REST API requires an instructor/TA role.
      The endpoints below follow Blackboard Learn REST API v1.
      Base path: /learn/api/public/v1/

      If the PKU instance is older and doesn't expose REST, update
      _fetch_submission_text() to scrape HTML instead.

Relevant endpoints used:
  GET /learn/api/public/v1/courses/{courseId}/gradebook/columns
      → list gradable columns (assignments)
  GET /learn/api/public/v1/courses/{courseId}/gradebook/columns/{columnId}/attempts
      → list student attempts with studentId, status, text
  GET /learn/api/public/v1/courses/{courseId}/gradebook/columns/{columnId}/attempts/{attemptId}/files
      → list attached files for an attempt
  GET /learn/api/public/v1/courses/{courseId}/users  (role=Student)
      → map userId → studentId + name
"""
from __future__ import annotations

import re
from html import unescape

import httpx

from models import Attachment, Submission

BB_API = "/learn/api/public/v1"
ASSIGNMENT_REDIRECTOR = "/webapps/assignment/gradeAssignmentRedirector"

_ATTACHMENT_LINK_RE = re.compile(
    r'<li>\s*<a[^>]*class="attachment[^"]*"[^>]*>\s*(?P<filename>.*?)\s*</a>.*?'
    r'<a[^>]*class="dwnldBtn"[^>]*href="(?P<href>[^"]+)"',
    re.DOTALL | re.IGNORECASE,
)
_SUBMISSION_TEXT_BLOCK_RE = re.compile(
    r'<div[^>]*id="currentAttempt_submissionText"[^>]*>(?P<body>.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


class BlackboardCrawler:
    def __init__(self, client: httpx.Client, course_id: str, whitelist: set[str]):
        self.client = client
        self.course_id = course_id
        self.whitelist = whitelist  # empty set = all students
        self._user_map: dict[str, tuple[str, str]] = {}  # bb_user_id → (student_id, name)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch_assignments(self) -> list[dict]:
        """Return list of gradable column metadata dicts."""
        return self._paginate(f"{BB_API}/courses/{self.course_id}/gradebook/columns")

    def fetch_submissions(self, column_id: str, assignment_title: str) -> list[Submission]:
        """Fetch all student submissions for a given gradebook column."""
        self._ensure_user_map()
        attempts = self._latest_attempts(
            self._paginate(
            f"{BB_API}/courses/{self.course_id}/gradebook/columns/{column_id}/attempts"
            )
        )
        submissions = []
        for attempt in attempts:
            bb_uid = attempt.get("userId", "")
            student_id, student_name = self._user_map.get(bb_uid, (bb_uid, "Unknown"))

            if self.whitelist and student_id not in self.whitelist:
                continue

            text = attempt.get("text") or ""
            attachments = self._fetch_attachments(column_id, attempt["id"])
            if not text.strip() and not attachments:
                text, attachments = self._fetch_submission_from_assignment_page(column_id, attempt["id"])

            submissions.append(
                Submission(
                    student_id=student_id,
                    student_name=student_name,
                    assignment_id=column_id,
                    assignment_title=assignment_title,
                    bb_user_id=bb_uid,
                    text_content=text,
                    attachments=attachments,
                    submitted_at=attempt.get("created", ""),
                )
            )
        return submissions

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_user_map(self) -> None:
        if self._user_map:
            return
        users = self._paginate(
            f"{BB_API}/courses/{self.course_id}/users",
            params={"role": "Student", "fields": "userId,user.studentId,user.userName,user.name.full,user.name.given"},
        )
        for u in users:
            bb_uid = u.get("userId", "")
            user_info = u.get("user", {})
            name_info = user_info.get("name", {})
            student_id = user_info.get("studentId") or user_info.get("userName") or bb_uid
            name = name_info.get("full") or name_info.get("given") or "Unknown"
            self._user_map[bb_uid] = (student_id, name)

    def _fetch_attachments(self, column_id: str, attempt_id: str) -> list[Attachment]:
        try:
            files = self._paginate(
                f"{BB_API}/courses/{self.course_id}/gradebook/columns"
                f"/{column_id}/attempts/{attempt_id}/files"
            )
        except httpx.HTTPStatusError:
            return []

        attachments = []
        for f in files:
            file_url = f.get("downloadUri") or f.get("viewUrl", "")
            if not file_url:
                continue
            try:
                resp = self.client.get(file_url)
                resp.raise_for_status()
                attachments.append(
                    Attachment(
                        filename=f.get("fileName", "attachment"),
                        content_type=resp.headers.get("content-type", "application/octet-stream"),
                        data=resp.content,
                    )
                )
            except httpx.HTTPStatusError:
                continue
        return attachments

    def _fetch_submission_from_assignment_page(
        self, column_id: str, attempt_id: str
    ) -> tuple[str, list[Attachment]]:
        try:
            resp = self.client.get(
                ASSIGNMENT_REDIRECTOR,
                params={
                    "course_id": self.course_id,
                    "outcomeDefinitionId": column_id,
                    "attempt_id": attempt_id,
                },
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            return "", []

        html = resp.text
        text = _parse_assignment_submission_text(html)
        attachments = self._download_assignment_page_attachments(html)
        return text, attachments

    def _latest_attempts(self, attempts: list[dict]) -> list[dict]:
        """Keep only the latest attempt for each Blackboard user."""
        latest_by_user: dict[str, dict] = {}
        for attempt in attempts:
            user_id = attempt.get("userId", "")
            if not user_id:
                continue
            best = latest_by_user.get(user_id)
            if best is None or _attempt_sort_key(attempt) > _attempt_sort_key(best):
                latest_by_user[user_id] = attempt
        return list(latest_by_user.values())

    def _download_assignment_page_attachments(self, html: str) -> list[Attachment]:
        attachments: list[Attachment] = []
        for filename, href in _parse_assignment_attachment_links(html):
            try:
                resp = self.client.get(href)
                resp.raise_for_status()
                attachments.append(
                    Attachment(
                        filename=filename,
                        content_type=resp.headers.get("content-type", "application/octet-stream"),
                        data=resp.content,
                    )
                )
            except httpx.HTTPStatusError:
                continue
        return attachments

    def _paginate(self, path: str, params: dict | None = None) -> list[dict]:
        """Follow Blackboard's cursor-based pagination."""
        params = dict(params or {})
        params.setdefault("limit", "100")
        results: list[dict] = []
        url: str | None = path

        while url:
            resp = self.client.get(url, params=params if url == path else None)
            resp.raise_for_status()
            body = resp.json()
            results.extend(body.get("results", []))
            paging = body.get("paging", {})
            url = paging.get("nextPage")  # None when last page

        return results


def _parse_assignment_attachment_links(html: str) -> list[tuple[str, str]]:
    attachments: list[tuple[str, str]] = []
    for match in _ATTACHMENT_LINK_RE.finditer(html):
        filename = _normalize_html_text(match.group("filename"))
        href = unescape(match.group("href")).strip()
        if not filename or not href:
            continue
        attachments.append((filename, href))
    return attachments


def _parse_assignment_submission_text(html: str) -> str:
    match = _SUBMISSION_TEXT_BLOCK_RE.search(html)
    if not match:
        return ""
    body = match.group("body")
    body = body.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    return _normalize_html_text(body)


def _normalize_html_text(value: str) -> str:
    text = unescape(_TAG_RE.sub(" ", value))
    return _WHITESPACE_RE.sub(" ", text).strip()


def _attempt_sort_key(attempt: dict) -> tuple[str, int]:
    created = str(attempt.get("created") or "")
    attempt_id = str(attempt.get("id") or "")
    match = re.search(r"_(\d+)_", attempt_id)
    numeric_id = int(match.group(1)) if match else -1
    return created, numeric_id
