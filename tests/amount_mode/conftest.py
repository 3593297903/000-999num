"""All new tests and temporary files belong to the current QA run."""
import os
import uuid
from pathlib import Path
import pytest

_DEFAULT_RUN = Path(__file__).resolve().parents[2] / "qa" / "amount_mode" / "local-tests"
RUN = Path(os.environ.get("AMOUNT_QA_ROOT", str(_DEFAULT_RUN)))


@pytest.fixture
def tmp_path():
    path = RUN / "tmp" / uuid.uuid4().hex[:10]
    path.mkdir(parents=True)
    return path
