import os
import subprocess
from pathlib import Path


ENTRYPOINT = Path(__file__).parents[3] / "docker" / "worker-entrypoint.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _run_entrypoint(tmp_path: Path, groups: str) -> list[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "id", f"printf '%s\\n' '{groups}'")
    _write_executable(bin_dir / "chown", "exit 0")
    _write_executable(bin_dir / "python3.12", "exit 99")
    _write_executable(bin_dir / "setpriv", "printf '%s\\n' \"$@\"")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "APP_UID": "1026",
        "APP_GID": "1026",
    }
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), "--pipeline", "both"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.splitlines()


def test_entrypoint_preserves_only_non_root_supplemental_groups(tmp_path: Path) -> None:
    args = _run_entrypoint(tmp_path, "0 1026 485 488")

    assert args[:6] == [
        "--reuid",
        "1026",
        "--regid",
        "1026",
        "--groups",
        "485,488",
    ]
    assert args[-4:] == ["-m", "worker", "--pipeline", "both"]


def test_entrypoint_clears_groups_when_none_are_granted(tmp_path: Path) -> None:
    args = _run_entrypoint(tmp_path, "0 1026")

    assert args[:5] == ["--reuid", "1026", "--regid", "1026", "--clear-groups"]
    assert args[-4:] == ["-m", "worker", "--pipeline", "both"]
