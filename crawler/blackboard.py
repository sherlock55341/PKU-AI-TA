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

import io
from typing import Iterator

import httpx

from models import Attachment, Submission

BB_API = "/learn/api/public/v1"


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
        attempts = self._paginate(
            f"{BB_API}/courses/{self.course_id}/gradebook/columns/{column_id}/attempts"
        )
        submissions = []
        for attempt in attempts:
            bb_uid = attempt.get("userId", "")
            student_id, student_name = self._user_map.get(bb_uid, (bb_uid, "Unknown"))

            if self.whitelist and student_id not in self.whitelist:
                continue

            text = attempt.get("text") or ""
            attachments = self._fetch_attachments(column_id, attempt["id"])

            submissions.append(
                Submission(
                    student_id=student_id,
                    student_name=student_name,
                    assignment_id=column_id,
                    assignment_title=assignment_title,
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
            params={"role": "Student", "fields": "userId,user.studentId,user.name.full"},
        )
        for u in users:
            bb_uid = u.get("userId", "")
            student_id = u.get("user", {}).get("studentId", bb_uid)
            name = u.get("user", {}).get("name", {}).get("full", "Unknown")
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
