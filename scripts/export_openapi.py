"""Dump the server's OpenAPI schema (used by the dashboard type generation).

Usage: ``uv run python scripts/export_openapi.py [out_path]``
"""

import json
import sys
from pathlib import Path

from prodeo import __version__
from prodeo.api import create_app
from prodeo.bus import InProcessEventBus
from prodeo.persistence import SqliteEventStore
from prodeo.sessions import SessionRegistry


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dashboard") / "openapi.json"
    bus = InProcessEventBus()
    app = create_app(
        registry=SessionRegistry(bus),
        store=SqliteEventStore(Path("unused.db")),  # never opened; schema only
        bus=bus,
        node="schema",
        version=__version__,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
