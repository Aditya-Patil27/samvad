"""Shared fixtures.

NO TEST MAY CALL A LIVE API. Not once, not "just to check". Tests run in CI,
CI runs on every push, and a live call in a test is a bill that scales with
your commit rate.
"""
import os

import pytest

os.environ.setdefault("MOCK_LLM", "1")
os.environ.setdefault("SAMVAD_SECRET", "test-secret-not-a-real-one")


@pytest.fixture
def secret() -> str:
    return os.environ["SAMVAD_SECRET"]
