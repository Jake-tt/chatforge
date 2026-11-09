"""File parsing runs in a child process that is killed when it overruns.

The mechanism itself is under test here (like the circuit breaker), so these call
`run_isolated` directly; the HTTP tests in test_attachments go through it on every upload.
"""

import os
import time

import pytest

from app.config import Settings
from app.services.attachments import ConversionFailed, run_isolated


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


async def test_a_conversion_runs_in_a_child_process():
    result = await run_isolated("markdown", {"extension": ".md"}, "# 제목".encode(),
                                _settings())

    assert result == {"markdown": "# 제목"}


async def test_a_conversion_that_hangs_is_killed(tmp_path):
    pid_file = tmp_path / "pid"
    started = time.monotonic()

    with pytest.raises(ConversionFailed, match="timed out"):
        await run_isolated("markdown", {"pid_file": str(pid_file)}, b"",
                           _settings(convert_timeout_seconds=1), module="tests.workers.hang")

    assert time.monotonic() - started < 5
    with pytest.raises(ProcessLookupError):  # gone, not left running in the background
        os.kill(int(pid_file.read_text()), 0)


async def test_a_conversion_that_crashes_is_reported_not_raised_as_is():
    with pytest.raises(ConversionFailed, match="exit 1"):
        await run_isolated("markdown", {}, b"", _settings(), module="tests.workers.crash")


async def test_format_problems_come_back_as_results():
    result = await run_isolated("image", {"max_side": 100, "max_pixels": 10}, b"not an image",
                                _settings())

    assert result == {"error": "unreadable"}
