import pytest

from nix_fod_swh_checker import cache as cache_module
from nix_fod_swh_checker import checkpoint as checkpoint_module
from nix_fod_swh_checker import cli as cli_module


@pytest.fixture(autouse=True)
def isolate_default_paths(tmp_path, monkeypatch):
    """Redirect default checkpoint and cache paths into the per-test tmp dir."""
    monkeypatch.setattr(
        checkpoint_module,
        "default_checkpoint_path",
        lambda installable: tmp_path / "checkpoint.json",
    )
    monkeypatch.setattr(
        cache_module,
        "default_cache_path",
        lambda: tmp_path / "cache.json",
    )
    # ``cli`` imports these defaults, so patch its module-level references too.
    monkeypatch.setattr(
        cli_module,
        "default_checkpoint_path",
        lambda installable: tmp_path / "checkpoint.json",
    )
    monkeypatch.setattr(
        cli_module,
        "default_cache_path",
        lambda: tmp_path / "cache.json",
    )
