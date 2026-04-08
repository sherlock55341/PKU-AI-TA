"""
Submit grades back to course.pku.edu.cn.

Two submission paths are supported:

1. PKU homework plugin (bb-homeWorkCheck-BBLEARN)
   POST saveStudentGrade.do

2. Standard Blackboard assignment / gradebook column
   POST /webapps/assignment/gradeAssignment/submit?course_id=...
"""
from __future__ import annotations

import re
from html import unescape

import httpx
from rich.console import Console

from models import ReviewRecord

HW_BASE = "/webapps/bb-homeWorkCheck-BBLEARN/homeWorkCheck"
SUBMIT_ENDPOINT = f"{HW_BASE}/saveStudentGrade.do"
BB_API = "/learn/api/public/v1"
ASSIGNMENT_REDIRECTOR = "/webapps/assignment/gradeAssignmentRedirector"

console = Console()

_GRADE_PK_RE = re.compile(r"gradePk=[^)]*encodeURIComponent\((\d+)\)")
_ATTEMPT_FORM_RE = re.compile(r'<form id="currentAttempt_form".*?</form>', re.DOTALL)
_INPUT_VALUE_RE = r'name=["\']{name}["\'][^>]*value=["\']([^"\']*)["\']'
_SUBMIT_ENDPOINT_RE = re.compile(
    r"attemptInlineGrader = new attemptGrading\.inlineGrader\([^,]+,\s*'[^']+',\s*'[^']+',\s*'([^']+)'",
    re.DOTALL,
)


def _normalize_column_id(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(r"_(\d+)_\d+", value)
    if match:
        return value
    if re.fullmatch(r"\d+", value):
        return f"_{value}_1"
    return value


def _column_matches(column_id: str, assignment_id: str) -> bool:
    return _normalize_column_id(column_id) == _normalize_column_id(assignment_id)


def _fetch_assignment_title(client: httpx.Client, course_id: str, grade_book_pk: str) -> str:
    """Look up the assignment title for a gradeBookPK via getHomeWorkList.do."""
    from crawler.pku_homework import _parse_homework_list

    resp = client.get(
        f"{HW_BASE}/getHomeWorkList.do",
        params={"course_id": course_id},
    )
    resp.raise_for_status()
    assignments = _parse_homework_list(resp.text)
    for a in assignments:
        if a.get("gradeBookPK") == grade_book_pk:
            return a.get("name", "")
    return ""


def _fetch_student_meta(client: httpx.Client, course_id: str, grade_book_pk: str) -> tuple[dict[str, dict], str]:
    """
    Return ({userId: {filePk, attemptPk}}, assignment_title) by parsing getStudentWork.do.
    """
    from crawler.pku_homework import _STUDENT_ONCLICK_PATTERN, _STUDENT_PATTERN

    title = _fetch_assignment_title(client, course_id, grade_book_pk)
    if not title:
        return {}, ""

    resp = client.get(
        f"{HW_BASE}/getStudentWork.do",
        params={"course_id": course_id, "gradeBookPK": grade_book_pk, "title": title, "showAll": "true"},
    )
    resp.raise_for_status()
    html = resp.text

    meta: dict[str, dict] = {}
    for m in _STUDENT_PATTERN.finditer(html):
        user_id, file_pk, attempt_pk, _ = m.groups()
        meta[user_id] = {"filePk": file_pk, "attemptPk": attempt_pk}
    for m in _STUDENT_ONCLICK_PATTERN.finditer(html):
        user_id, file_pk, attempt_pk, _ = m.groups()
        meta.setdefault(user_id, {"filePk": file_pk, "attemptPk": attempt_pk})

    return meta, title


def _fetch_grade_pk(
    client: httpx.Client,
    course_id: str,
    grade_book_pk: str,
    user_id: str,
    file_pk: str,
    attempt_pk: str,
    title: str,
) -> str | None:
    """
    Fetch CheckWork.do for one student and extract gradePk from sendData() JS.
    gradePk is a per-student grade record ID injected server-side.
    """
    resp = client.get(
        f"{HW_BASE}/CheckWork.do",
        params={
            "course_id": course_id,
            "gradeBookPK": grade_book_pk,
            "userId": user_id,
            "filePk": file_pk,
            "title": title,
            "attemptPk": attempt_pk,
        },
    )
    resp.raise_for_status()
    html = resp.text

    m = _GRADE_PK_RE.search(html)
    return m.group(1) if m else None


def _submit_scores_plugin(
    client: httpx.Client,
    course_id: str,
    column_id: str,
    records: list[ReviewRecord],
) -> bool:
    """Submit via PKU homework plugin. Returns True on successful path selection."""
    grade_book_pk = column_id.strip("_").split("_")[0]

    console.print("  Fetching submission metadata…")
    try:
        student_meta, assignment_title = _fetch_student_meta(client, course_id, grade_book_pk)
    except httpx.HTTPStatusError as e:
        console.print(f"[yellow]Plugin metadata fetch failed:[/yellow] {e}")
        return False

    if not student_meta or not assignment_title:
        return False

    console.print(f"  Found metadata for {len(student_meta)} student(s). Assignment: [cyan]{assignment_title}[/cyan]")

    ok = 0
    for r in records:
        uid = r.result.student_id
        score = r.final_score
        notes = (r.reviewer_notes or "").strip()

        if uid not in student_meta:
            console.print(f"[yellow]⚠[/yellow]  {uid} ({r.result.student_name}): no submission metadata — skipping")
            continue

        meta = student_meta[uid]

        try:
            grade_pk = _fetch_grade_pk(
                client, course_id, grade_book_pk,
                uid, meta["filePk"], meta["attemptPk"], assignment_title,
            )
        except httpx.HTTPStatusError as e:
            console.print(f"[red]✗[/red]  {uid} ({r.result.student_name}): failed to load CheckWork.do — {e}")
            continue

        if grade_pk is None:
            console.print(f"[yellow]⚠[/yellow]  {uid} ({r.result.student_name}): gradePk not found in page JS — skipping")
            continue

        score_str = str(int(score)) if score == int(score) else str(score)
        payload = {
            "inputData": score_str,
            "attemptPk": meta["attemptPk"],
            "gradeBookPk": grade_book_pk,
            "course_id": course_id,
            "richContent": notes[:2000],
            "gradePk": grade_pk,
        }

        try:
            resp = client.post(SUBMIT_ENDPOINT, data=payload)
            resp.raise_for_status()
            console.print(f"[green]✓[/green]  {uid} ({r.result.student_name}) → {score_str}/{r.result.total_max}")
            ok += 1
        except httpx.HTTPStatusError as e:
            console.print(
                f"[red]✗[/red]  {uid} ({r.result.student_name}): HTTP {e.response.status_code} — {e.response.text[:300]}"
            )

    result_color = "green" if ok == len(records) else "yellow"
    console.print(f"\n[{result_color}]Done.[/{result_color}] {ok}/{len(records)} grade(s) submitted via PKU homework plugin.")
    return True


def _lookup_bb_user_ids(client: httpx.Client, course_id: str) -> dict[str, str]:
    from crawler.blackboard import BlackboardCrawler

    crawler = BlackboardCrawler(client, course_id, whitelist=set())
    users = crawler._paginate(
        f"{BB_API}/courses/{course_id}/users",
        params={"role": "Student", "fields": "userId,user.studentId,user.userName"},
    )
    mapping: dict[str, str] = {}
    for user in users:
        bb_user_id = user.get("userId", "")
        user_info = user.get("user", {})
        student_id = user_info.get("studentId") or user_info.get("userName") or ""
        if bb_user_id and student_id:
            mapping[student_id] = bb_user_id
    return mapping


def _fetch_attempt_ids(client: httpx.Client, course_id: str, column_id: str) -> dict[str, str]:
    from crawler.blackboard import BlackboardCrawler

    crawler = BlackboardCrawler(client, course_id, whitelist=set())
    attempts = crawler._paginate(
        f"{BB_API}/courses/{course_id}/gradebook/columns/{column_id}/attempts"
    )
    latest_by_user: dict[str, dict] = {}
    for attempt in attempts:
        user_id = attempt.get("userId", "")
        if not user_id:
            continue
        best = latest_by_user.get(user_id)
        if best is None or (attempt.get("created", ""), attempt.get("id", "")) > (best.get("created", ""), best.get("id", "")):
            latest_by_user[user_id] = attempt
    return {user_id: attempt["id"] for user_id, attempt in latest_by_user.items() if attempt.get("id")}


def _extract_form_value(html: str, name: str) -> str:
    pattern = re.compile(_INPUT_VALUE_RE.format(name=re.escape(name)))
    match = pattern.search(html)
    return unescape(match.group(1)) if match else ""


def _fetch_blackboard_attempt_form(
    client: httpx.Client,
    course_id: str,
    column_id: str,
    attempt_id: str,
) -> dict[str, str]:
    resp = client.get(
        ASSIGNMENT_REDIRECTOR,
        params={
            "course_id": course_id,
            "outcomeDefinitionId": column_id,
            "attempt_id": attempt_id,
        },
    )
    resp.raise_for_status()
    html = resp.text

    submit_match = _SUBMIT_ENDPOINT_RE.search(html)
    submit_url = submit_match.group(1) if submit_match else f"/webapps/assignment/gradeAssignment/submit?course_id={course_id}"
    return {
        "submit_url": submit_url,
        "referer_url": str(resp.request.url),
        "attempt_id": _extract_form_value(html, "attempt_id") or attempt_id,
        "course_id": _extract_form_value(html, "course_id") or course_id,
        "feedbacktext_f": _extract_form_value(html, "feedbacktext_f"),
        "feedbacktext_w": _extract_form_value(html, "feedbacktext_w"),
        "feedbacktype": _extract_form_value(html, "feedbacktype") or "H",
        "gradingNotestext_f": _extract_form_value(html, "gradingNotestext_f"),
        "gradingNotestext_w": _extract_form_value(html, "gradingNotestext_w"),
        "gradingNotestype": _extract_form_value(html, "gradingNotestype") or "H",
        "courseMembershipId": _extract_form_value(html, "courseMembershipId"),
        "submitGradeUrl": _extract_form_value(html, "submitGradeUrl"),
        "cancelGradeUrl": _extract_form_value(html, "cancelGradeUrl"),
        "nonce": _extract_form_value(html, "blackboard.platform.security.NonceUtil.nonce"),
    }


def _submit_scores_blackboard_rest(
    client: httpx.Client,
    course_id: str,
    column_id: str,
    records: list[ReviewRecord],
) -> None:
    """Submit via Blackboard assignment web form."""
    needs_user_lookup = any(not r.result.bb_user_id for r in records)

    try:
        user_map = _lookup_bb_user_ids(client, course_id) if needs_user_lookup else {}
        attempt_map = _fetch_attempt_ids(client, course_id, column_id)
    except httpx.HTTPStatusError as e:
        body = e.response.text[:300] if e.response is not None else str(e)
        console.print(
            f"[red]Error fetching Blackboard submission metadata:[/red] "
            f"HTTP {e.response.status_code} — {body}"
        )
        return
    ok = 0

    for r in records:
        bb_user_id = r.result.bb_user_id
        if not bb_user_id:
            bb_user_id = user_map.get(r.result.student_id, "")

        if not bb_user_id:
            console.print(f"[yellow]⚠[/yellow]  {r.result.student_id} ({r.result.student_name}): Blackboard user ID not found — skipping")
            continue

        attempt_id = attempt_map.get(bb_user_id, "")
        if not attempt_id:
            console.print(f"[yellow]⚠[/yellow]  {r.result.student_id} ({r.result.student_name}): Blackboard attempt ID not found — skipping")
            continue

        try:
            form = _fetch_blackboard_attempt_form(client, course_id, column_id, attempt_id)
        except httpx.HTTPStatusError as e:
            console.print(f"[red]✗[/red]  {r.result.student_id} ({r.result.student_name}): failed to load grading form — {e}")
            continue

        score = r.final_score
        score_str = str(int(score)) if score == int(score) else str(score)
        notes = (r.reviewer_notes or "").strip()[:2000]
        payload = {
            "grade": score_str,
            "attempt_id": form["attempt_id"],
            "course_id": form["course_id"],
            "feedbacktext": notes,
            "feedbacktext_f": form["feedbacktext_f"],
            "feedbacktext_w": form["feedbacktext_w"],
            "feedbacktype": form["feedbacktype"],
            "textbox_prefix": "feedbacktext",
            "gradingNotestext": "",
            "gradingNotestext_f": form["gradingNotestext_f"],
            "gradingNotestext_w": form["gradingNotestext_w"],
            "gradingNotestype": form["gradingNotestype"],
            "courseMembershipId": form["courseMembershipId"],
            "submitGradeUrl": form["submitGradeUrl"],
            "cancelGradeUrl": form["cancelGradeUrl"],
            "blackboard.platform.security.NonceUtil.nonce": form["nonce"],
        }
        multipart_payload = {key: (None, value) for key, value in payload.items()}

        try:
            resp = client.post(
                form["submit_url"],
                files=multipart_payload,
                headers={"Referer": form["referer_url"]},
            )
            resp.raise_for_status()
            console.print(f"[green]✓[/green]  {r.result.student_id} ({r.result.student_name}) → {score_str}/{r.result.total_max}")
            ok += 1
        except httpx.HTTPStatusError as e:
            body = e.response.text[:300] if e.response is not None else str(e)
            console.print(
                f"[red]✗[/red]  {r.result.student_id} ({r.result.student_name}): HTTP {e.response.status_code} — {body}"
            )

    result_color = "green" if ok == len(records) else "yellow"
    console.print(f"\n[{result_color}]Done.[/{result_color}] {ok}/{len(records)} grade(s) submitted via Blackboard assignment form.")


def submit_scores(
    client: httpx.Client,
    course_id: str,
    column_id: str,
    records: list[ReviewRecord],
    *,
    dry_run: bool = False,
) -> None:
    """Submit approved grades via the appropriate backend."""
    approved = [r for r in records if r.approved]
    skipped = len(records) - len(approved)
    if skipped:
        console.print(f"[yellow]Skipping {skipped} unapproved record(s).[/yellow]")
    if not approved:
        return

    if dry_run:
        for r in approved:
            console.print(
                f"[dim][DRY RUN][/dim] Would submit: "
                f"{r.result.student_id} ({r.result.student_name})"
                f" → {r.final_score}/{r.result.total_max}"
                f"  notes: {(r.reviewer_notes or '')[:60]}"
            )
        return

    if _submit_scores_plugin(client, course_id, column_id, approved):
        return

    console.print("[dim]PKU homework plugin submission unavailable; falling back to Blackboard assignment form.[/dim]")
    _submit_scores_blackboard_rest(client, course_id, _normalize_column_id(column_id), approved)
