from pathlib import Path

import pytest

from rbs.catalog import sample_instance
from rbs.emit import dumps


@pytest.fixture
def instance():
    return sample_instance()


@pytest.fixture
def sample_input_path(tmp_path: Path) -> Path:
    path = tmp_path / "sample_input.json"
    path.write_text(dumps(sample_instance()), encoding="utf-8")
    return path
