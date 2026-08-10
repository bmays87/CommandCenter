"""Host detection: the Python floor comparison and what detect() reports."""

import sys

from prodeo.environment import Environment, detect


def _env(**overrides: object) -> Environment:
    base: dict[str, object] = {"platform": "linux", "python": (3, 12)}
    base.update(overrides)
    return Environment.model_validate(base)


def test_python_floor_accepts_equal_and_newer() -> None:
    env = _env(python=(3, 12))
    assert env.python_at_least("3.12") is True
    assert env.python_at_least("3.10") is True
    assert env.python_at_least("3.13") is False


def test_python_floor_handles_a_bare_major() -> None:
    assert _env(python=(3, 12)).python_at_least("3") is True
    assert _env(python=(3, 12)).python_at_least("4") is False


def test_unparsable_floor_gates_nothing() -> None:
    # A malformed requirement must not lock a user out of an extension; the
    # failure direction here should always be permissive.
    assert _env().python_at_least("not-a-version") is True
    assert _env().python_at_least("") is True


def test_detect_reports_this_interpreter() -> None:
    env = detect()
    assert env.platform == sys.platform
    assert env.python == (sys.version_info.major, sys.version_info.minor)
    assert env.python_at_least("3.12") is True  # the project's own floor
    assert "machine" in env.details


def test_gpu_defaults_to_absent() -> None:
    # Requirements are evaluated against an injected Environment, so a test
    # never depends on whether the host running it has a GPU.
    assert _env().nvidia_gpu is False
