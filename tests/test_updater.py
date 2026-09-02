from __future__ import annotations

from pathlib import Path

import pytest

from codelite import updater


def test_version_tuple_accepts_release_tags():
    assert updater._version_tuple("v1.4.2") == (1, 4, 2)


def test_version_tuple_rejects_invalid_values():
    with pytest.raises(updater.UpdateError):
        updater._version_tuple("latest")


def test_check_update_is_hidden_for_source_runs(monkeypatch):
    monkeypatch.setattr(updater, "_installed_version", lambda: (None, None))
    monkeypatch.setattr(updater, "_release", lambda: pytest.fail("must not fetch a release"))

    assert updater.check_update() == {"supported": False}


def test_check_update_finds_debian_asset(monkeypatch):
    monkeypatch.setattr(updater, "_installed_version", lambda: ("linux-deb", "1.4.1"))
    monkeypatch.setattr(
        updater,
        "_release",
        lambda: {
            "tag_name": "v1.5.0",
            "assets": [
                {"name": "code-lite_1.5.0_all.deb"},
                {"name": "SHA256SUMS.txt"},
            ],
        },
    )

    status = updater.check_update()
    assert status["available"] is True
    assert status["asset_available"] is True
    assert status["current_version"] == "1.4.1"
    assert status["latest_version"] == "1.5.0"


def test_sha256_streams_file(tmp_path: Path):
    path = tmp_path / "installer"
    path.write_bytes(b"Code Lite")
    assert updater._sha256(path) == "820c904b18f4fee68ca8117530d9463ac083a9aed60a315aff3e8452e5347241"
