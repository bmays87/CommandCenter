"""Restarting the server from the API (ADR-0016).

The re-exec itself belongs to ``main()`` and is not exercised here - replacing
the process image mid-test-run would take pytest with it. What is exercised is
everything that decides *whether* to re-exec, and the argv that would be used.
"""

import sys
import types
from pathlib import Path

import pytest

from prodeo.config import Settings
from prodeo.server import Server, reexec_argv, run

pytestmark = pytest.mark.integration


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        node_name="test-node",
        data_dir=tmp_path,
        api_port=0,  # ephemeral port
        adapters={"claude-code": {"projects_dir": str(tmp_path / "no-projects")}},
        discovery_interval_s=0,
        dashboard_dir=tmp_path / "no-dashboard",
    )


def test_request_restart_releases_the_wait_and_records_the_intent(tmp_path: Path) -> None:
    server = Server(_settings(tmp_path))

    server.request_restart()  # what the API endpoint's background task calls

    assert server.restart_wanted is True
    assert server.shutdown.is_set() is True


@pytest.mark.asyncio
async def test_run_returns_false_on_an_ordinary_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Server.start

    async def start_then_stop(self: Server) -> None:
        await original(self)
        self.shutdown.set()  # stands in for SIGINT

    monkeypatch.setattr(Server, "start", start_then_stop)

    assert await run(_settings(tmp_path)) is False


@pytest.mark.asyncio
async def test_run_returns_true_when_the_api_asked_for_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Server.start

    async def start_then_restart(self: Server) -> None:
        await original(self)
        self.request_restart()

    monkeypatch.setattr(Server, "start", start_then_restart)

    assert await run(_settings(tmp_path)) is True


# --- reexec_argv -------------------------------------------------------------


def test_reexec_argv_for_a_console_script(monkeypatch: pytest.MonkeyPatch) -> None:
    # prodeo-server.exe on Windows, a shebang script on POSIX: both are
    # directly executable, so argv[0] is re-run as-is.
    monkeypatch.setattr(sys, "argv", ["/opt/venv/bin/prodeo-server", "--flag"])
    monkeypatch.setitem(sys.modules, "__main__", types.SimpleNamespace(__spec__=None))

    assert reexec_argv()[1:] == ["--flag"]
    assert reexec_argv()[0].endswith("prodeo-server")


def test_reexec_argv_for_a_console_script_launched_through_runpy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: ``uv run prodeo-server`` sets a __spec__ named "__main__".

    The first cut treated any __spec__ as "-m was used", so this produced
    ``python -m ""`` and the server never came back from a restart.
    """
    spec = types.SimpleNamespace(name="__main__", parent="")
    monkeypatch.setattr(sys, "argv", ["/opt/venv/bin/prodeo-server"])
    monkeypatch.setitem(sys.modules, "__main__", types.SimpleNamespace(__spec__=spec))

    argv = reexec_argv()

    assert "-m" not in argv
    assert argv[0].endswith("prodeo-server")


def test_reexec_argv_for_python_dash_m(monkeypatch: pytest.MonkeyPatch) -> None:
    # sys.argv[0] here is the resolved module file, which cannot be run alone.
    spec = types.SimpleNamespace(name="prodeo.server", parent="prodeo")
    monkeypatch.setattr(sys, "argv", ["/src/prodeo/server.py", "--flag"])
    monkeypatch.setitem(sys.modules, "__main__", types.SimpleNamespace(__spec__=spec))

    assert reexec_argv() == [sys.executable, "-m", "prodeo.server", "--flag"]


def test_reexec_argv_for_a_package_main(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = types.SimpleNamespace(name="prodeo.server.__main__", parent="prodeo.server")
    monkeypatch.setattr(sys, "argv", ["/src/prodeo/server/__main__.py"])
    monkeypatch.setitem(sys.modules, "__main__", types.SimpleNamespace(__spec__=spec))

    assert reexec_argv() == [sys.executable, "-m", "prodeo.server"]


def test_reexec_argv_for_a_plain_script(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_server.py", "--flag"])
    monkeypatch.setitem(sys.modules, "__main__", types.SimpleNamespace(__spec__=None))

    assert reexec_argv() == [sys.executable, "run_server.py", "--flag"]
