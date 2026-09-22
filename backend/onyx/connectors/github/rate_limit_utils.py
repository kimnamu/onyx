import time
from datetime import datetime, timedelta, timezone
from typing import Any

from github import Github
from github.GithubRetry import GithubRetry
from urllib3.util.retry import Retry

from onyx.utils.logger import setup_logger
from onyx.utils.retry_after import cap_wait_seconds

logger = setup_logger()

# The primary GitHub rate limit resets hourly, so a legitimate wait can be
# close to one hour. Anything larger comes from a broken or hostile server.
MAX_GITHUB_RATE_LIMIT_WAIT_SECONDS = 3660.0


class CappedGithubRetry(GithubRetry):
    """GithubRetry whose rate-limit and Retry-After waits are capped."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("retry_after_max", int(MAX_GITHUB_RATE_LIMIT_WAIT_SECONDS))
        super().__init__(**kwargs)

    def increment(self, *args: Any, **kwargs: Any) -> Retry:
        retry = super().increment(*args, **kwargs)
        # GithubRetry replaces get_backoff_time with an uncapped reset wait.
        backoff = cap_wait_seconds(
            retry.get_backoff_time(), MAX_GITHUB_RATE_LIMIT_WAIT_SECONDS
        )
        retry.get_backoff_time = lambda: backoff  # ty: ignore[invalid-assignment]
        return retry


def sleep_after_rate_limit_exception(github_client: Github) -> None:
    """
    Sleep until the GitHub rate limit resets, but never longer than
    MAX_GITHUB_RATE_LIMIT_WAIT_SECONDS.

    Args:
        github_client: The GitHub client that hit the rate limit
    """
    sleep_time = github_client.get_rate_limit().core.reset.replace(
        tzinfo=timezone.utc
    ) - datetime.now(tz=timezone.utc)
    sleep_time += timedelta(minutes=1)  # add an extra minute just to be safe
    sleep_seconds = cap_wait_seconds(
        sleep_time.total_seconds(), MAX_GITHUB_RATE_LIMIT_WAIT_SECONDS
    )
    logger.notice("Ran into Github rate-limit. Sleeping %s seconds.", sleep_seconds)
    time.sleep(sleep_seconds)
