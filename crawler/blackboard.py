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
import ssl
import time
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
_ANCHOR_RE = re.compile(r'<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>', re.DOTALL | re.IGNORECASE)
_HREF_RE = re.compile(r'''href\s*=\s*["'](?P<href>[^"']+)["']''', re.IGNORECASE)
_COURSE_ID_RE = re.compile(r"course_id=(_\d+_\d+)")
_PKID_COURSE_ID_RE = re.compile(r"PkId\{key=(_\d+_\d+),\s*dataType=blackboard\.data\.course\.Course")
_TITLE_RE = re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.DOTALL | re.IGNORECASE)
_TOKEN_VALUE_RE = re.compile(r"(?i)(token=)[^&\"'\s<>]+")
_SESSION_VALUE_RE = re.compile(r"(?i)(session|nonce|auth)[^\"'\s<>]*")

COURSE_DISCOVERY_PATHS = (
    "/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_1_1",
    "/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_2_1",
    "/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_3_1",
    "/webapps/blackboard/execute/courseManager?sourceType=COURSES",
    "/webapps/blackboard/execute/launcher?type=Course",
    "/",
)


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
        return _paginate(self.client, path, params)


def fetch_courses(client: httpx.Client) -> list[dict]:
    """Return courses visible to the authenticated Blackboard user."""
    web_courses = fetch_courses_from_web(client)
    if web_courses:
        return web_courses
    return _paginate(
        client,
        f"{BB_API}/courses",
        params={"fields": "id,courseId,name,availability.available"},
        timeout=8,
    )


def fetch_courses_from_web(client: httpx.Client) -> list[dict]:
    """Parse Blackboard web pages for course links with course_id query values."""
    courses: list[dict] = []
    seen: set[str] = set()
    for path in COURSE_DISCOVERY_PATHS:
        try:
            resp = client.get(path, timeout=8)
            resp.raise_for_status()
        except httpx.HTTPError:
            continue
        for course in _parse_course_links(resp.text):
            course_id = course["id"]
            if course_id in seen:
                continue
            seen.add(course_id)
            courses.append(course)
    return courses


def debug_course_discovery(client: httpx.Client, output: Path) -> list[dict]:
    """Fetch candidate Blackboard course pages and save sanitized diagnostics."""
    diagnostics: list[dict] = []
    html_parts: list[str] = []
    for path in COURSE_DISCOVERY_PATHS:
        entry = {
            "path": path,
            "status": None,
            "title": "",
            "course_links": [],
            "error": "",
        }
        try:
            resp = client.get(path, timeout=8)
            entry["status"] = resp.status_code
            entry["title"] = _parse_html_title(resp.text)
            links = _parse_course_links(resp.text)
            entry["course_links"] = links
            html_parts.append(
                f"\n<!-- PATH: {path} STATUS: {resp.status_code} TITLE: {entry['title']} -->\n"
                + sanitize_debug_html(resp.text[:200000])
            )
        except Exception as exc:
            entry["error"] = f"{exc.__class__.__name__}: {exc}"
        diagnostics.append(entry)

    output.write_text("\n\n".join(html_parts), encoding="utf-8")
    return diagnostics


def _paginate(
    client: httpx.Client,
    path: str,
    params: dict | None = None,
    timeout: float | None = None,
) -> list[dict]:
    """Follow Blackboard's cursor-based pagination for a client and API path."""
    params = dict(params or {})
    params.setdefault("limit", "100")
    results: list[dict] = []
    url: str | None = path

    while url:
        kwargs = {"params": params if url == path else None}
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = _get_with_retries(client, url, **kwargs)
        resp.raise_for_status()
        body = resp.json()
        results.extend(body.get("results", []))
        paging = body.get("paging", {})
        url = paging.get("nextPage")  # None when last page

    return results


def _parse_course_links(html: str) -> list[dict]:
    """Extract course metadata from Blackboard HTML links containing course_id."""
    courses: list[dict] = []
    seen: set[str] = set()

    for match in _ANCHOR_RE.finditer(html):
        href_match = _HREF_RE.search(match.group("attrs"))
        if not href_match:
            continue
        href = unescape(href_match.group("href"))
        course_id = _course_id_from_href(href)
        if not course_id or course_id in seen:
            continue
        title = _normalize_html_text(match.group("body"))
        if not title:
            title = course_id
        seen.add(course_id)
        courses.append({"id": course_id, "name": title, "courseId": "", "source": "web"})

    return courses


def _parse_html_title(html: str) -> str:
    match = _TITLE_RE.search(html)
    if not match:
        return ""
    return _normalize_html_text(match.group("title"))


def sanitize_debug_html(html: str) -> str:
    """Redact likely sensitive values from saved debug HTML."""
    redacted = _TOKEN_VALUE_RE.sub(r"\1<redacted>", html)
    redacted = re.sub(r'(?i)(password|passwd|pwd)\s*=\s*["\'][^"\']+["\']', r'\1="<redacted>"', redacted)
    redacted = re.sub(r'(?i)(value\s*=\s*["\'])([^"\']{24,})(["\'])', r'\1<redacted>\3', redacted)
    redacted = _SESSION_VALUE_RE.sub("<redacted>", redacted)
    return redacted


def _course_id_from_href(href: str) -> str:
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    values = query.get("course_id") or query.get("courseId") or []
    for value in values:
        if re.fullmatch(r"_\d+_\d+", value):
            return value

    match = _COURSE_ID_RE.search(href)
    if match:
        return match.group(1)

    match = _PKID_COURSE_ID_RE.search(unescape(href))
    return match.group(1) if match else ""


def _get_with_retries(client: httpx.Client, url: str, attempts: int = 3, **kwargs) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return client.get(url, **kwargs)
        except (httpx.HTTPError, ssl.SSLError, OSError) as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(attempt)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("GET request failed without an exception")


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
