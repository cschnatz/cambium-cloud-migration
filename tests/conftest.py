import pytest


@pytest.fixture(autouse=True)
def no_cloud_delay(monkeypatch):
    monkeypatch.setattr("cnmaestro_migrate.cloud.DELAY", 0)
