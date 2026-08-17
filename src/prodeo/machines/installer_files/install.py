"""Prodeo CCAN installer — sets up the Command Center Agent Node.

Run from the unpacked installer directory:

    python install.py [--dir PATH]

Needs Python 3.12+ and network access to PyPI (for third-party
dependencies; the Prodeo wheels themselves are bundled). The config baked
into this installer pairs the node to exactly one Command Center — the one
it was downloaded from.
"""

import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path


def default_dir() -> Path:
    # Mirrors prodeo_ccan.config.default_data_dir(); keep the two in sync.
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / (
            "prodeo-ccan"
        )
    return Path.home() / ".local" / "share" / "prodeo-ccan"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        type=Path,
        default=default_dir(),
        help="install target (default: %(default)s)",
    )
    target: Path = parser.parse_args().dir

    # Not dead code: this script must *run* on old Pythons to say why it won't.
    if sys.version_info < (3, 12):  # noqa: UP036
        print(f"error: Python 3.12+ required, this is {sys.version.split()[0]}")
        return 1
    here = Path(__file__).resolve().parent
    wheels = sorted((here / "wheels").glob("*.whl"))
    config = here / "ccan.json"
    if not any(w.name.startswith("prodeo_ccan-") for w in wheels) or not config.is_file():
        print("error: run install.py from the unpacked installer directory")
        return 1

    target.mkdir(parents=True, exist_ok=True)
    venv_dir = target / "venv"
    print(f"creating environment in {venv_dir} ...")
    venv.EnvBuilder(with_pip=True, upgrade_deps=False).create(venv_dir)
    python = (
        venv_dir
        / ("Scripts" if sys.platform == "win32" else "bin")
        / ("python.exe" if sys.platform == "win32" else "python")
    )

    print("installing prodeo-ccan ...")
    result = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--find-links",
            str(here / "wheels"),
            *(str(w) for w in wheels),
        ],
        check=False,
    )
    if result.returncode != 0:
        print("error: pip install failed (see output above)")
        return result.returncode

    shutil.copy2(config, target / "ccan.json")

    exe = (
        venv_dir
        / ("Scripts" if sys.platform == "win32" else "bin")
        / ("prodeo-ccan.exe" if sys.platform == "win32" else "prodeo-ccan")
    )
    print()
    print("installed. Start the node with:")
    print(f"  {exe}")
    print()
    print("then add this machine in the Command Center dashboard by its")
    print("FQDN or IP address. The node answers only that Command Center.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
