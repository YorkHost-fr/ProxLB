from collections.abc import Iterator

import pytest

from proxlb.models import storage_balancing


@pytest.fixture(autouse=True)
def _reset_failed_moves() -> Iterator[None]:
    """Each test starts without remembered disk move failures."""
    storage_balancing._failed_moves.clear()
    yield
    storage_balancing._failed_moves.clear()
