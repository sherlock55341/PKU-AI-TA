from crawler.blackboard import (
    BlackboardCrawler,
    _parse_assignment_attachment_links,
    _parse_assignment_submission_text,
)


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
