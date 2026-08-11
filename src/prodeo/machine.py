"""Machine-local actions, behind the seam CCAN will occupy (ADR-0020).

Command Center's core is platform-independent and containerizable; anything
that touches the machine an agent runs on — today, opening an editor — goes
through this Protocol. The composition root wires the local implementation;
when the Command Center Agent Node becomes a separate per-machine process, a
remote implementation replaces it *here* and nothing above the seam moves.
"""

import asyncio
import shutil
from pathlib import Path
from typing import Protocol

import structlog

from prodeo.errors import MachineActionError

_log = structlog.get_logger(__name__)


class MachineActions(Protocol):
    """What the hub may ask the machine hosting the agents to do."""

    async def open_editor(self, path: str) -> None:
        """Open ``path`` in the user's code editor, in a new window.

        Raises :class:`MachineActionError` when the path is not a directory or
        no editor is available.
        """
        ...


class LocalMachineActions:
    """The colocated implementation: hub and agent machine are one host."""

    #: Editor launchers tried in order; each opens a *new* window so several
    #: projects can be open side by side (one agent per project).
    _EDITORS = ("code", "code-insiders")

    async def open_editor(self, path: str) -> None:
        target = Path(path).expanduser()
        if not target.is_dir():
            raise MachineActionError(f"{path!r} is not a directory on this machine")
        editor = next((found for name in self._EDITORS if (found := shutil.which(name))), None)
        if editor is None:
            raise MachineActionError(
                "VS Code is not on PATH ('code' or 'code-insiders'); "
                "install it or add its bin directory to PATH"
            )
        # Fire and forget, argv only - never a shell. The editor process
        # outlives this request by design.
        process = await asyncio.create_subprocess_exec(
            editor,
            "--new-window",
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        _log.info("machine.editor_opened", path=str(target), editor=editor, pid=process.pid)
