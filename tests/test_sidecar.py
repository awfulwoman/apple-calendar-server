"""Direct tests for the sidecar. Everywhere else it is exercised only through the
store, which always hands it the configured default path."""
from apple_calendar_server.sidecar import Sidecar


def test_accepts_a_db_path_with_no_directory_component(tmp_path, monkeypatch):
    """`CALENDAR_SERVER_DB_PATH=meta.db` is a reasonable thing to set. It reaches
    `os.makedirs(os.path.dirname(path))` as `makedirs("")` and raises."""
    monkeypatch.chdir(tmp_path)
    Sidecar("meta.db")
    assert (tmp_path / "meta.db").exists()
