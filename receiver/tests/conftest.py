import pytest


@pytest.fixture(autouse=True)
def _isolated_transfer_log(tmp_path, monkeypatch):
    """Receiver tests must not write into the real ~/.local/state/droptouch/transfers.json."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("DROPTOUCH_NO_PROMPT", "1")   # no real "Connect phone?" notifications from tests
