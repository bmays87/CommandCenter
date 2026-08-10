"""Where runtime-installed extensions live, and how they become importable.

Extensions the user installs from the dashboard go to
``<PRODEO_DATA_DIR>/extensions/lib`` rather than into the virtualenv, because
``uv sync`` is exact: it makes ``.venv`` match the lock and deletes everything
else, hand-installed packages included. Anything installed at runtime therefore
has to live outside it to survive.

Putting that directory on ``sys.path`` is all it takes for
``importlib.metadata.entry_points`` to discover the plugins inside it, so the
Plugin Host needs no changes to find them.
"""

import sys
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)


def extension_lib_dir(data_dir: Path) -> Path:
    """The directory ``uv pip install --target`` writes extensions into."""
    return data_dir / "extensions" / "lib"


def local_index_dir(data_dir: Path) -> Path:
    """Where locally built wheels live, for installing unpublished packages.

    First-party extensions are workspace members that are not on any index, and
    they depend on each other, so a registry install can never resolve them.
    Building the workspace into this directory and passing it as
    ``--find-links`` makes installing them work entirely offline.
    """
    return data_dir / "extensions" / "local-index"


def workspace_root(start: Path | None = None) -> Path | None:
    """The repo checkout this server is running from, if it is one.

    Returns ``None`` for an ordinary installed deployment, where extensions
    come from a package index and there is nothing to build.
    """
    here = start or Path(__file__).resolve()
    for candidate in here.parents:
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file() and (candidate / "packages").is_dir():
            try:
                if "[tool.uv.workspace]" in pyproject.read_text(encoding="utf-8"):
                    return candidate
            except OSError:  # unreadable is simply "not a workspace"
                return None
    return None


def activate_extension_path(data_dir: Path) -> Path:
    """Put the extension lib directory on ``sys.path`` (idempotent).

    Call before the Plugin Host loads. Appended rather than prepended so a
    user-installed package can never shadow the core or its pinned
    dependencies - an extension should extend the server, not replace parts
    of it.
    """
    lib = extension_lib_dir(data_dir)
    entry = str(lib)
    if entry not in sys.path:
        sys.path.append(entry)
        _log.debug("extensions.path_activated", path=entry, exists=lib.is_dir())
    return lib
