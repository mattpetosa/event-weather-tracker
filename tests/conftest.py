import pytest

from eventweather import sources


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("tests must not touch the network")
    monkeypatch.setattr(sources, "http_get", blocked)
    monkeypatch.setattr(sources, "cffi_json", blocked)
    sources._FORECA_IDS.clear()
    yield
    sources._FORECA_IDS.clear()
