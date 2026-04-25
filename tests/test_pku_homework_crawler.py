import ssl

from crawler.pku_homework import _get_with_retries


def test_pku_homework_get_with_retries_handles_ssl_eof(monkeypatch):
    monkeypatch.setattr("crawler.pku_homework.time.sleep", lambda _: None)

    class DummyClient:
        def __init__(self):
            self.calls = 0

        def get(self, url, params=None):
            self.calls += 1
            if self.calls < 3:
                raise ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")
            return "ok"

    client = DummyClient()

    assert _get_with_retries(client, "/x", params={"course_id": "_98532_1"}) == "ok"
    assert client.calls == 3
