"""Server-supplied rate-limit waits must never exceed the per-site cap."""

import io
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response as HttplibResponse
from slack_sdk.http_retry.request import HttpRequest
from slack_sdk.http_retry.response import HttpResponse
from slack_sdk.http_retry.state import RetryState
from urllib3 import HTTPResponse

from onyx.connectors.github import connector as github_connector
from onyx.connectors.github import rate_limit_utils as github_rl
from onyx.connectors.github.connector import GithubConnector
from onyx.connectors.github.rate_limit_utils import (
    MAX_GITHUB_RATE_LIMIT_WAIT_SECONDS,
    CappedGithubRetry,
)
from onyx.connectors.google_utils import google_utils
from onyx.connectors.google_utils.google_utils import (
    MAX_GOOGLE_RATE_LIMIT_WAIT_SECONDS,
)
from onyx.connectors.hubspot.rate_limit import get_rate_limit_retry_delay_seconds
from onyx.connectors.microsoft_utils.graph_client import backoff_seconds
from onyx.connectors.slack import source_operations as slack_ops
from onyx.connectors.slack.source_operations import (
    OnyxRedisSlackRetryHandler,
    OnyxSlackWebClient,
)
from onyx.connectors.zulip import utils as zulip_utils
from onyx.connectors.zulip.utils import ZulipAPIError, call_api
from onyx.utils.retry_after import MAX_RETRY_AFTER_SECONDS

_HUGE = "999999999"
_FAR_FUTURE_EPOCH = int((datetime.now(timezone.utc) + timedelta(days=365)).timestamp())


def test_github_reset_sleep_is_capped() -> None:
    client = MagicMock()
    client.get_rate_limit.return_value.core.reset = datetime.now(
        timezone.utc
    ) + timedelta(days=365)
    with patch.object(github_rl.time, "sleep") as sleep:
        github_rl.sleep_after_rate_limit_exception(client)
    assert sleep.call_args.args[0] == MAX_GITHUB_RATE_LIMIT_WAIT_SECONDS


def test_github_reset_sleep_keeps_normal_hourly_wait() -> None:
    client = MagicMock()
    client.get_rate_limit.return_value.core.reset = datetime.now(
        timezone.utc
    ) + timedelta(minutes=30)
    with patch.object(github_rl.time, "sleep") as sleep:
        github_rl.sleep_after_rate_limit_exception(client)
    assert 1790 <= sleep.call_args.args[0] <= 1860


def _github_403(headers: dict[str, str]) -> HTTPResponse:
    body = json.dumps({"message": "API rate limit exceeded for user."}).encode()
    return HTTPResponse(
        body=io.BytesIO(body), status=403, headers=headers, preload_content=False
    )


def test_github_retry_primary_reset_backoff_is_capped() -> None:
    response = _github_403(
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(_FAR_FUTURE_EPOCH)}
    )
    retry = CappedGithubRetry().increment(method="GET", url="/x", response=response)
    assert retry.get_backoff_time() == MAX_GITHUB_RATE_LIMIT_WAIT_SECONDS


def test_github_retry_after_header_is_capped() -> None:
    retry = CappedGithubRetry()
    assert (
        retry.get_retry_after(_github_403({"Retry-After": _HUGE}))
        == MAX_GITHUB_RATE_LIMIT_WAIT_SECONDS
    )


@pytest.mark.parametrize(
    "env_base_url", [None, "https://ghes.example.com/api/v3"], ids=["dotcom", "ghes"]
)
def test_github_client_uses_capped_retry(
    monkeypatch: pytest.MonkeyPatch, env_base_url: str | None
) -> None:
    monkeypatch.setattr(github_connector, "GITHUB_CONNECTOR_BASE_URL", env_base_url)
    with patch.object(github_connector, "Github") as github_cls:
        connector = GithubConnector(repo_owner="o", repositories="r")
        connector.load_credentials({"github_access_token": "t"})
    assert isinstance(github_cls.call_args.kwargs["retry"], CappedGithubRetry)


class _HubSpotRateLimitError(Exception):
    def __init__(self, headers: dict[str, str]) -> None:
        super().__init__("Too Many Requests")
        self.headers = headers


def test_hubspot_retry_after_is_capped() -> None:
    exc = _HubSpotRateLimitError({"Retry-After": _HUGE})
    assert get_rate_limit_retry_delay_seconds(exc) == MAX_RETRY_AFTER_SECONDS


def test_graph_retry_after_is_capped() -> None:
    assert backoff_seconds(0, _HUGE) == MAX_RETRY_AFTER_SECONDS
    assert backoff_seconds(0, "7") == 7.0


def test_zulip_retry_is_capped_bounded_and_keeps_kwargs() -> None:
    fun = MagicMock(
        return_value={
            "result": "error",
            "code": "RATE_LIMIT_HIT",
            "msg": "slow down",
            "retry-after": _HUGE,
        }
    )
    with patch.object(zulip_utils.time, "sleep") as sleep:
        with pytest.raises(ZulipAPIError):
            call_api(fun, "a", anchor="newest")
    assert sleep.call_count == zulip_utils._MAX_RATE_LIMIT_RETRIES
    assert all(c.args[0] == MAX_RETRY_AFTER_SECONDS for c in sleep.call_args_list)
    assert fun.call_count == zulip_utils._MAX_RATE_LIMIT_RETRIES + 1
    assert all(c.kwargs == {"anchor": "newest"} for c in fun.call_args_list)


def test_zulip_retry_returns_after_rate_limit_clears() -> None:
    fun = MagicMock(
        side_effect=[
            {"result": "error", "code": "RATE_LIMIT_HIT", "retry-after": "2"},
            {"result": "success"},
        ]
    )
    with patch.object(zulip_utils.time, "sleep") as sleep:
        assert call_api(fun, x=1) == {"result": "success"}
    sleep.assert_called_once_with(3.0)


def test_slack_shared_delay_ttl_is_capped() -> None:
    redis = MagicMock()
    redis.pttl.return_value = int(MAX_RETRY_AFTER_SECONDS * 1000) - 1
    handler = OnyxRedisSlackRetryHandler(max_retry_count=5, delay_key="d", r=redis)
    handler.prepare_for_next_attempt(
        state=RetryState(),
        request=MagicMock(spec=HttpRequest),
        response=HttpResponse(status_code=429, headers={"Retry-After": [_HUGE]}),
    )
    assert redis.set.call_args.kwargs["px"] == int(MAX_RETRY_AFTER_SECONDS * 1000)


def test_slack_sleep_on_shared_delay_is_capped() -> None:
    redis = MagicMock()
    redis.pttl.return_value = 10**12  # TTL written by another process
    client = OnyxSlackWebClient(delay_lock="l", delay_key="d", r=redis)
    with (
        patch.object(slack_ops.time, "sleep") as sleep,
        patch.object(
            slack_ops.WebClient, "_perform_urllib_http_request_internal"
        ) as parent,
    ):
        parent.return_value = {}
        client._perform_urllib_http_request_internal("https://x", MagicMock())
    sleep.assert_called_once_with(MAX_RETRY_AFTER_SECONDS)


def _google_429(headers: dict[str, str], message: str) -> HttpError:
    resp = HttplibResponse({"status": "429", **headers})
    resp.reason = "Too Many Requests"
    content = json.dumps(
        {
            "error": {
                "code": 429,
                "message": message,
                "errors": [{"reason": "rateLimitExceeded"}],
            }
        }
    ).encode()
    return HttpError(resp, content)


def test_google_retry_wait_is_capped() -> None:
    error = _google_429({}, "Quota exceeded. Retry after 2999-01-01T00:00:00.000Z")
    request = MagicMock()
    request.execute.side_effect = [error, "ok"]
    with patch.object(google_utils.time, "sleep") as sleep:
        assert google_utils._execute_with_retry(request) == "ok"
    sleep.assert_called_once_with(MAX_GOOGLE_RATE_LIMIT_WAIT_SECONDS)
