from crawler.blackboard import (
    BlackboardCrawler,
    debug_course_discovery,
    fetch_courses,
    fetch_courses_from_web,
    sanitize_debug_html,
    _get_with_retries,
    _parse_course_links,
    _parse_assignment_attachment_links,
    _parse_assignment_submission_text,
)
import ssl


def test_parse_assignment_attachment_links():
    html = """
    <div id="currentAttempt_submission" class="segment">
      <h4>提交</h4>
      <ul id="currentAttempt_submissionList" class="filesList">
        <li>
          <a id="currentAttempt_attemptFile_3367621_1" class="attachment genericFile" href="#"
              onclick="gradeAssignment.inlineView( event, '_3367621_1', '_3388412_1' );" >
            Ln 是单调函数.docx
          </a>
          <div class="downloadFile">
            <a class="dwnldBtn" href="/webapps/assignment/download?course_id=_98532_1&amp;attempt_id=_3388412_1&amp;file_id=_3367621_1&amp;fileName=Ln%20%E6%98%AF%E5%8D%95%E8%B0%83%E5%87%BD%E6%95%B0.docx" role="button"></a>
          </div>
        </li>
      </ul>
    </div>
    """

    assert _parse_assignment_attachment_links(html) == [
        (
            "Ln 是单调函数.docx",
            "/webapps/assignment/download?course_id=_98532_1&attempt_id=_3388412_1&file_id=_3367621_1&fileName=Ln%20%E6%98%AF%E5%8D%95%E8%B0%83%E5%87%BD%E6%95%B0.docx",
        )
    ]


def test_parse_assignment_submission_text():
    html = """
    <div id="currentAttempt_submissionText" class="vtbegenerated">
      Problem 1:<br />
      Prove the claim.
    </div>
    """

    assert _parse_assignment_submission_text(html) == "Problem 1: Prove the claim."


def test_latest_attempts_keeps_only_newest_per_user():
    crawler = BlackboardCrawler(client=None, course_id="_1_1", whitelist=set())
    attempts = [
        {"userId": "_u1_1", "id": "_100_1", "created": "2026-03-20T10:00:00.000Z"},
        {"userId": "_u1_1", "id": "_101_1", "created": "2026-03-20T11:00:00.000Z"},
        {"userId": "_u2_1", "id": "_200_1", "created": "2026-03-20T09:00:00.000Z"},
    ]

    latest = crawler._latest_attempts(attempts)

    assert len(latest) == 2
    by_user = {a["userId"]: a for a in latest}
    assert by_user["_u1_1"]["id"] == "_101_1"
    assert by_user["_u2_1"]["id"] == "_200_1"


def test_fetch_courses_uses_short_timeout(monkeypatch):
    captured = {}

    def fake_paginate(client, path, params=None, timeout=None):
        captured["path"] = path
        captured["params"] = params
        captured["timeout"] = timeout
        return [{"id": "_98024_1", "name": "Algorithms"}]

    monkeypatch.setattr("crawler.blackboard.fetch_courses_from_web", lambda client: [])
    monkeypatch.setattr("crawler.blackboard._paginate", fake_paginate)

    courses = fetch_courses(client=object())

    assert courses == [{"id": "_98024_1", "name": "Algorithms"}]
    assert captured["path"] == "/learn/api/public/v1/courses"
    assert captured["timeout"] == 8


def test_parse_course_links_from_blackboard_html():
    html = """
    <ul>
      <li><a href="/webapps/blackboard/execute/launcher?type=Course&id=_111_1&url=">Ignore id param</a></li>
      <li><a href="/webapps/blackboard/execute/courseMain?course_id=_98024_1">算法设计与分析</a></li>
      <li><a href="/webapps/blackboard/execute/launcher?type=Course&id=PkId{key=_98532_1, dataType=blackboard.data.course.Course, container=blackboard.persist.DatabaseContainer@123}&url=">集成电路工程算法</a></li>
      <li><a href="/webapps/blackboard/execute/courseMain?course_id=_98024_1">Duplicate</a></li>
      <li><a href="/webapps/bb-homeWorkCheck-BBLEARN/homeWorkCheck/getHomeWorkList.do?course_id=_99000_1">机器学习</a></li>
    </ul>
    """

    courses = _parse_course_links(html)

    assert courses == [
        {"id": "_98024_1", "name": "算法设计与分析", "courseId": "", "source": "web"},
        {"id": "_98532_1", "name": "集成电路工程算法", "courseId": "", "source": "web"},
        {"id": "_99000_1", "name": "机器学习", "courseId": "", "source": "web"},
    ]


def test_fetch_courses_prefers_web_courses(monkeypatch):
    monkeypatch.setattr(
        "crawler.blackboard.fetch_courses_from_web",
        lambda client: [{"id": "_98024_1", "name": "Algorithms", "courseId": "", "source": "web"}],
    )

    courses = fetch_courses(client=object())

    assert courses == [{"id": "_98024_1", "name": "Algorithms", "courseId": "", "source": "web"}]


def test_fetch_courses_from_web_requests_known_pages():
    class DummyResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

    class DummyClient:
        def __init__(self):
            self.calls = []

        def get(self, path, timeout=None):
            self.calls.append((path, timeout))
            html = '<a href="/webapps/blackboard/execute/courseMain?course_id=_98024_1">Algorithms</a>'
            return DummyResponse(html)

    client = DummyClient()
    courses = fetch_courses_from_web(client)

    assert courses == [{"id": "_98024_1", "name": "Algorithms", "courseId": "", "source": "web"}]
    assert client.calls[0] == ("/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_1_1", 8)


def test_debug_course_discovery_saves_sanitized_html(tmp_path):
    class DummyResponse:
        status_code = 200
        text = """
        <html><head><title>Courses</title></head>
        <body>
          <a href="/webapps/blackboard/execute/courseMain?course_id=_98024_1&token=secret">Algorithms</a>
          <input value="abcdefghijklmnopqrstuvwxyz1234567890" />
        </body></html>
        """

        def raise_for_status(self):
            return None

    class DummyClient:
        def get(self, path, timeout=None):
            return DummyResponse()

    output = tmp_path / "debug.html"
    diagnostics = debug_course_discovery(DummyClient(), output)

    assert diagnostics[0]["title"] == "Courses"
    assert diagnostics[0]["course_links"][0]["id"] == "_98024_1"
    saved = output.read_text(encoding="utf-8")
    assert "secret" not in saved
    assert "abcdefghijklmnopqrstuvwxyz1234567890" not in saved


def test_sanitize_debug_html_redacts_tokens_and_long_values():
    html = '<a href="/x?token=secret-token">x</a><input value="abcdefghijklmnopqrstuvwxyz1234567890">'

    redacted = sanitize_debug_html(html)

    assert "secret-token" not in redacted
    assert "abcdefghijklmnopqrstuvwxyz1234567890" not in redacted


def test_blackboard_get_with_retries_handles_ssl_eof(monkeypatch):
    monkeypatch.setattr("crawler.blackboard.time.sleep", lambda _: None)

    class DummyClient:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")
            return "ok"

    client = DummyClient()

    assert _get_with_retries(client, "/x") == "ok"
    assert client.calls == 3
