import pytest
import httpx
import ssl
from unittest.mock import MagicMock, patch

from auth.iaaa import get_session
from auth.iaaa import _exchange_token_for_blackboard_session


class TestGetSession:
    def test_raises_without_credentials(self):
        with patch("auth.iaaa.settings") as mock_settings:
            mock_settings.pku_username = ""
            mock_settings.pku_password = ""
            with pytest.raises(RuntimeError, match="PKU_USERNAME"):
                get_session()

    def test_raises_on_iaaa_failure(self):
        with patch("auth.iaaa.settings") as mock_settings:
            mock_settings.pku_username = "user"
            mock_settings.pku_password = "pass"

            with patch("auth.iaaa.httpx.Client") as MockClient:
                mock_client = MagicMock()
                MockClient.return_value = mock_client

                # getPublicKey.do succeeds
                key_resp = MagicMock()
                key_resp.raise_for_status = MagicMock()
                key_resp.json.return_value = {
                    "success": True,
                    "key": (
                        "-----BEGIN PUBLIC KEY-----\n"
                        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqw9PsMk8v9ED/LiLT62I"
                        "DnelyIA/s8blyxqNmbgXT4xtq+Y64Bd+THYPZ4dUIRuFmMvPowQm9wL27W3PEtQy"
                        "C8VN+TzW/nPzc74fy9cRxgaSh1FXNQBqYZtltb6G5YvwBvZlYdKhE3Oo3noUD0FJ"
                        "JC11Nmcy2/x1V2pwXHRy2DHKaWB1EEtQ9dRxuMZolZIpEwWnT4CHfwEvth83kNRp"
                        "E8471KJEqyQqmqJt3JRerH4X4p41zQFIxCsrznAwku3b1qm0vgGLQ8t7XEiCjDX0"
                        "m5yIJEuW5t1YcteutuJX5+5oXxe2Fo04Wkn1pO6+QoJopqHcHJD5C+7GlnPOLB1c"
                        "DQIDAQAB\n"
                        "-----END PUBLIC KEY-----"
                    ),
                }

                # oauthlogin.do fails
                fail_resp = MagicMock()
                fail_resp.raise_for_status = MagicMock()
                fail_resp.json.return_value = {"success": False, "errors": "Invalid password"}

                mock_client.request.side_effect = [key_resp, fail_resp]

                with pytest.raises(RuntimeError, match="IAAA login failed"):
                    get_session()

    def test_returns_client_on_success(self):
        with patch("auth.iaaa.settings") as mock_settings:
            mock_settings.pku_username = "user"
            mock_settings.pku_password = "pass"

            with patch("auth.iaaa.httpx.Client") as MockClient:
                mock_client = MagicMock()
                MockClient.return_value = mock_client

                key_resp = MagicMock()
                key_resp.raise_for_status = MagicMock()
                key_resp.json.return_value = {
                    "success": True,
                    "key": (
                        "-----BEGIN PUBLIC KEY-----\n"
                        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqw9PsMk8v9ED/LiLT62I"
                        "DnelyIA/s8blyxqNmbgXT4xtq+Y64Bd+THYPZ4dUIRuFmMvPowQm9wL27W3PEtQy"
                        "C8VN+TzW/nPzc74fy9cRxgaSh1FXNQBqYZtltb6G5YvwBvZlYdKhE3Oo3noUD0FJ"
                        "JC11Nmcy2/x1V2pwXHRy2DHKaWB1EEtQ9dRxuMZolZIpEwWnT4CHfwEvth83kNRp"
                        "E8471KJEqyQqmqJt3JRerH4X4p41zQFIxCsrznAwku3b1qm0vgGLQ8t7XEiCjDX0"
                        "m5yIJEuW5t1YcteutuJX5+5oXxe2Fo04Wkn1pO6+QoJopqHcHJD5C+7GlnPOLB1c"
                        "DQIDAQAB\n"
                        "-----END PUBLIC KEY-----"
                    ),
                }

                token_resp = MagicMock()
                token_resp.raise_for_status = MagicMock()
                token_resp.json.return_value = {"success": True, "token": "abc123"}

                campus_resp = MagicMock()
                campus_resp.raise_for_status = MagicMock()

                mock_client.request.side_effect = [key_resp, token_resp]
                mock_client.get.return_value = campus_resp

                result = get_session()
                assert result is mock_client

                login_call = mock_client.request.call_args_list[1]
                assert login_call.args[0] == "POST"
                assert login_call.kwargs["data"]["redirUrl"].startswith("http://course.pku.edu.cn/")

                # Verify campusLogin was called with the token
                campus_call = mock_client.get.call_args
                assert "abc123" in str(campus_call)

    def test_retries_with_direct_client_after_ssl_eof(self):
        with patch("auth.iaaa.settings") as mock_settings:
            mock_settings.pku_username = "user"
            mock_settings.pku_password = "pass"

            with patch("auth.iaaa.httpx.Client") as MockClient:
                proxy_client = MagicMock()
                proxy_client.request.side_effect = ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")

                direct_client = MagicMock()
                key_resp = MagicMock()
                key_resp.raise_for_status = MagicMock()
                key_resp.json.return_value = {
                    "success": True,
                    "key": (
                        "-----BEGIN PUBLIC KEY-----\n"
                        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqw9PsMk8v9ED/LiLT62I"
                        "DnelyIA/s8blyxqNmbgXT4xtq+Y64Bd+THYPZ4dUIRuFmMvPowQm9wL27W3PEtQy"
                        "C8VN+TzW/nPzc74fy9cRxgaSh1FXNQBqYZtltb6G5YvwBvZlYdKhE3Oo3noUD0FJ"
                        "JC11Nmcy2/x1V2pwXHRy2DHKaWB1EEtQ9dRxuMZolZIpEwWnT4CHfwEvth83kNRp"
                        "E8471KJEqyQqmqJt3JRerH4X4p41zQFIxCsrznAwku3b1qm0vgGLQ8t7XEiCjDX0"
                        "m5yIJEuW5t1YcteutuJX5+5oXxe2Fo04Wkn1pO6+QoJopqHcHJD5C+7GlnPOLB1c"
                        "DQIDAQAB\n"
                        "-----END PUBLIC KEY-----"
                    ),
                }
                token_resp = MagicMock()
                token_resp.raise_for_status = MagicMock()
                token_resp.json.return_value = {"success": True, "token": "abc123"}
                campus_resp = MagicMock()
                campus_resp.raise_for_status = MagicMock()
                direct_client.request.side_effect = [key_resp, token_resp]
                direct_client.get.return_value = campus_resp

                MockClient.side_effect = [proxy_client, direct_client]

                result = get_session()

                assert result is direct_client
                assert MockClient.call_args_list[0].kwargs["trust_env"] is True
                assert MockClient.call_args_list[1].kwargs["trust_env"] is False


def test_exchange_token_retries_https_then_falls_back_to_http(monkeypatch):
    monkeypatch.setattr("auth.iaaa.time.sleep", lambda _: None)
    calls = []

    class DummyClient:
        def get(self, url, params=None):
            calls.append((url, params))
            if url.startswith("https://"):
                request = httpx.Request("GET", url)
                return httpx.Response(502, request=request)
            return httpx.Response(200, request=httpx.Request("GET", url))

    _exchange_token_for_blackboard_session(DummyClient(), "secret-token")

    assert len(calls) == 4
    assert all(call[0].startswith("https://") for call in calls[:3])
    assert calls[3][0].startswith("http://")
    assert all(call[1] == {"token": "secret-token"} for call in calls)


def test_exchange_token_falls_back_to_http_after_ssl_error(monkeypatch):
    monkeypatch.setattr("auth.iaaa.time.sleep", lambda _: None)
    calls = []

    class DummyClient:
        def get(self, url, params=None):
            calls.append((url, params))
            if url.startswith("https://"):
                raise ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")
            return httpx.Response(200, request=httpx.Request("GET", url))

    _exchange_token_for_blackboard_session(DummyClient(), "secret-token")

    assert len(calls) == 4
    assert all(call[0].startswith("https://") for call in calls[:3])
    assert calls[3][0].startswith("http://")


def test_exchange_token_error_does_not_include_token(monkeypatch):
    monkeypatch.setattr("auth.iaaa.time.sleep", lambda _: None)

    class DummyClient:
        def get(self, url, params=None):
            return httpx.Response(502, request=httpx.Request("GET", url))

    with pytest.raises(RuntimeError) as excinfo:
        _exchange_token_for_blackboard_session(DummyClient(), "secret-token")

    message = str(excinfo.value)
    assert "secret-token" not in message
    assert "campusLogin" in message
