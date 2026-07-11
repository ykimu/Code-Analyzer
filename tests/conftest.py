from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "sample_project"


@pytest.fixture
def sample_root() -> Path:
    return FIXTURE
