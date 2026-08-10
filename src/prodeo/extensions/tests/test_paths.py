"""The extension lib directory and how it joins sys.path."""

import sys
from pathlib import Path

from prodeo.extensions import (
    activate_extension_path,
    extension_lib_dir,
    local_index_dir,
    workspace_root,
)


def test_lib_dir_is_under_the_data_dir_not_the_venv(tmp_path: Path) -> None:
    # The whole point: outside .venv, so `uv sync` cannot delete what the user
    # installed.
    lib = extension_lib_dir(tmp_path)
    assert lib == tmp_path / "extensions" / "lib"
    assert ".venv" not in str(lib)


def test_activate_appends_once_and_is_idempotent(tmp_path: Path) -> None:
    before = list(sys.path)
    try:
        lib = activate_extension_path(tmp_path)
        assert str(lib) in sys.path
        # Appended, never prepended: an installed extension must not shadow the
        # core or its pinned dependencies.
        assert sys.path[-1] == str(lib)

        activate_extension_path(tmp_path)
        assert sys.path.count(str(lib)) == 1
    finally:
        sys.path[:] = before


def test_workspace_root_finds_this_checkout() -> None:
    # Running from the repo, so the workspace must be discoverable - that is
    # what lets unpublished first-party extensions be built and installed.
    root = workspace_root()
    assert root is not None
    assert (root / "packages").is_dir()
    assert "[tool.uv.workspace]" in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_workspace_root_is_none_outside_a_checkout(tmp_path: Path) -> None:
    # An ordinary installed deployment has nothing to build; extensions come
    # from a package index instead.
    (tmp_path / "deep").mkdir()
    assert workspace_root(tmp_path / "deep" / "module.py") is None


def test_workspace_root_ignores_a_pyproject_without_a_workspace(tmp_path: Path) -> None:
    (tmp_path / "packages").mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    (tmp_path / "sub").mkdir()
    assert workspace_root(tmp_path / "sub" / "module.py") is None


def test_local_index_is_beside_the_lib_dir(tmp_path: Path) -> None:
    assert local_index_dir(tmp_path) == tmp_path / "extensions" / "local-index"


def test_activate_works_before_the_directory_exists(tmp_path: Path) -> None:
    # First boot has nothing installed yet; joining sys.path must still be safe.
    before = list(sys.path)
    try:
        lib = activate_extension_path(tmp_path / "fresh")
        assert not lib.exists()
        assert str(lib) in sys.path
    finally:
        sys.path[:] = before
