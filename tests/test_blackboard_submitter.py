import httpx

from models import ReviewRecord, ScoringResult
from crawler.blackboard import BlackboardCrawler
from submitter import blackboard as submitter_blackboard
from submitter.blackboard import _fetch_attempt_ids, _lookup_bb_user_ids


def test_lookup_bb_user_ids_uses_v1_and_maps_student_ids(monkeypatch):
    captured: dict[str, object] = {}

    def fake_paginate(self, path, params=None):
        captured["path"] = path
        captured["params"] = params
        return [
            {"userId": "_229082_1", "user": {"studentId": "2501111919", "userName": "2501111919"}},
            {"userId": "_229083_1", "user": {"studentId": "", "userName": "2501111896"}},
        ]

    monkeypatch.setattr(BlackboardCrawler, "_paginate", fake_paginate)

    mapping = _lookup_bb_user_ids(httpx.Client(trust_env=False), "_98532_1")

    assert captured["path"] == "/learn/api/public/v1/courses/_98532_1/users"
    assert captured["params"] == {"role": "Student", "fields": "userId,user.studentId,user.userName"}
    assert mapping == {
        "2501111919": "_229082_1",
        "2501111896": "_229083_1",
    }


def test_fetch_attempt_ids_uses_v1_and_keeps_latest_attempt_per_user(monkeypatch):
    captured: dict[str, object] = {}

    def fake_paginate(self, path, params=None):
        captured["path"] = path
        captured["params"] = params
        return [
            {"userId": "_229082_1", "id": "_3388411_1", "created": "2026-04-08T07:00:00.000Z"},
            {"userId": "_229082_1", "id": "_3388412_1", "created": "2026-04-08T07:10:00.000Z"},
            {"userId": "_229083_1", "id": "_3388413_1", "created": "2026-04-08T06:50:00.000Z"},
        ]

    monkeypatch.setattr(BlackboardCrawler, "_paginate", fake_paginate)

    mapping = _fetch_attempt_ids(httpx.Client(trust_env=False), "_98532_1", "_424504_1")

    assert captured["path"] == "/learn/api/public/v1/courses/_98532_1/gradebook/columns/_424504_1/attempts"
    assert captured["params"] is None
    assert mapping == {
        "_229082_1": "_3388412_1",
        "_229083_1": "_3388413_1",
    }


def test_blackboard_submit_posts_multipart_with_referer(monkeypatch):
    class DummyClient:
        def __init__(self):
            self.calls = []

        def post(self, url, files=None, headers=None):
            self.calls.append({"url": url, "files": files, "headers": headers})
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(submitter_blackboard, "_fetch_attempt_ids", lambda *args, **kwargs: {"_229082_1": "_3388412_1"})
    monkeypatch.setattr(
        submitter_blackboard,
        "_fetch_blackboard_attempt_form",
        lambda *args, **kwargs: {
            "submit_url": "/webapps/assignment/gradeAssignment/submit?course_id=_98532_1",
            "referer_url": "https://course.pku.edu.cn/webapps/assignment/gradeAssignmentRedirector?course_id=_98532_1",
            "attempt_id": "_3388412_1",
            "course_id": "_98532_1",
            "feedbacktext_f": "f-token",
            "feedbacktext_w": "w-url",
            "feedbacktype": "H",
            "gradingNotestext_f": "nf-token",
            "gradingNotestext_w": "nw-url",
            "gradingNotestype": "H",
            "courseMembershipId": "",
            "submitGradeUrl": "",
            "cancelGradeUrl": "",
            "nonce": "nonce-token",
        },
    )

    record = ReviewRecord(
        result=ScoringResult(
            student_id="2501111919",
            student_name="尹奕涵",
            bb_user_id="_229082_1",
            assignment_id="_424504_1",
            total_score=10.0,
            total_max=10.0,
            confidence=0.9,
            breakdown=[],
            uncertain_parts=[],
            llm_reasoning="",
            needs_review=False,
        ),
        approved=True,
        reviewer_notes="Looks good.",
    )

    client = DummyClient()
    submitter_blackboard._submit_scores_blackboard_rest(client, "_98532_1", "_424504_1", [record])

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == "/webapps/assignment/gradeAssignment/submit?course_id=_98532_1"
    assert call["headers"] == {
        "Referer": "https://course.pku.edu.cn/webapps/assignment/gradeAssignmentRedirector?course_id=_98532_1"
    }
    assert call["files"]["grade"] == (None, "10")
    assert call["files"]["feedbacktext"] == (None, "Looks good.")
    assert call["files"]["blackboard.platform.security.NonceUtil.nonce"] == (None, "nonce-token")
