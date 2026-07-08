"""Tests for polymarket module loader."""

from pathlib import Path

from arb import polymarket_data as pd


def test_load_polymarket_module_from_repo():
    mod = pd.load_polymarket_module()
    assert hasattr(mod, "_get")
    assert mod.GAMMA.startswith("https://")


def test_repo_polymarket_script_exists():
    path = Path(pd._repo_polymarket_script())
    assert path.is_file()
