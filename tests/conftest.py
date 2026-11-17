from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def isolated_upload_retry_file(tmp_path, monkeypatch):
    """Never read or write the real outputs/upload_retry.json from tests."""
    from shorts_clipper.publishers import retry_queue

    monkeypatch.setattr(retry_queue, "RETRY_FILE", tmp_path / "upload_retry.json")


@pytest.fixture(autouse=True)
def disable_sleep():
    with mock.patch("time.sleep", return_value=None):
        yield
