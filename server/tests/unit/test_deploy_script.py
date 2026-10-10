import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = "1" * 40
PIPELINE = "sha256:" + "d" * 64
API_DIGEST = "sha256:" + "a" * 64
WORKER_DIGEST = "sha256:" + "b" * 64
CONFLICT_DIGEST = "sha256:" + "c" * 64


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _fake_tools(
    tmp_path: Path, *, conflict: bool = False, existing_match: bool = False
) -> tuple[dict[str, str], Path]:
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "calls.log"
    state = tmp_path / "state"
    manifest_dir = tmp_path / "manifests"
    _write_executable(
        tools / "git",
        f"""#!/bin/sh
printf 'git %s\\n' "$*" >> '{log}'
case "$*" in
  *status*) exit 0 ;;
  *rev-parse*) printf '%s\\n' '{SOURCE}' ;;
esac
""",
    )
    _write_executable(
        tools / "revision-python",
        f"#!/bin/sh\nprintf '%s\\n' '{PIPELINE}'\n",
    )
    _write_executable(
        tools / "docker",
        f"""#!/bin/sh
printf 'docker %s\\n' "$*" >> '{log}'
case "$*" in
  *\"manifest inspect --insecure --verbose\"*)
    ref="$5"
    case "$ref" in *worker*) digest='{WORKER_DIGEST}' ;; *) digest='{API_DIGEST}' ;; esac
    case "$ref" in
      *:v-test)
        if [ "${{FAKE_CONFLICT:-0}}" = 1 ]; then digest='{CONFLICT_DIGEST}'
        elif [ "${{FAKE_EXISTING_MATCH:-0}}" = 1 ]; then :
        elif [ ! -f '{state}.'"$digest" ]; then exit 1
        fi
        ;;
    esac
    printf '[{{"Descriptor":{{"digest":"%s","platform":{{"architecture":"amd64","os":"linux"}}}}}}]\\n' "$digest"
    ;;
  *\"image inspect\"*)
    case "$*" in
      *org.opencontainers.image.revision*) printf '%s\\n' '{SOURCE}'; exit 0 ;;
      *io.frank-yomik.pipeline-revision*) printf '%s\\n' '{PIPELINE}'; exit 0 ;;
    esac
    ;;
  *\"push "*":v-test\"*)
    case "$*" in *worker*) touch '{state}.{WORKER_DIGEST}' ;; *) touch '{state}.{API_DIGEST}' ;; esac
    ;;
esac
""",
    )
    _write_executable(
        tools / "ssh",
        f"""#!/bin/sh
printf 'ssh %s\\n' "$*" >> '{log}'
cat >/dev/null || true
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tools}:{env['PATH']}",
            "REVISION_PYTHON": str(tools / "revision-python"),
            "RELEASE_MANIFEST_DIR": str(manifest_dir),
            "REGISTRY": "registry.test/project",
            "REMOTE_REGISTRY": "registry.remote/project",
            "FAKE_CONFLICT": "1" if conflict else "0",
            "FAKE_EXISTING_MATCH": "1" if existing_match else "0",
        }
    )
    return env, log


def test_deploy_records_and_uses_verified_digests(tmp_path: Path) -> None:
    env, log = _fake_tools(tmp_path)
    result = subprocess.run(
        [str(ROOT / "bin/deploy"), "v-test", "rocm"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert "manifest inspect --insecure --verbose" in calls
    assert f"SOURCE_REVISION={SOURCE}" in calls
    assert f"PIPELINE_REVISION={PIPELINE}" in calls
    assert f"API_IMAGE='registry.remote/project/frank-yomik-api@{API_DIGEST}'" in calls
    assert f"WORKER_IMAGE='registry.remote/project/frank-yomik-worker-rocm@{WORKER_DIGEST}'" in calls
    assert "--no-deps --force-recreate --scale worker=2 api worker" in calls
    assert "compose down" not in calls
    record = json.loads((tmp_path / "manifests/v-test.json").read_text())
    assert record["source_revision"] == SOURCE
    assert record["pipeline_revision"] == PIPELINE
    assert record["images"]["api"]["digest"] == API_DIGEST
    assert record["images"]["worker"]["digest"] == WORKER_DIGEST


def test_deploy_refuses_to_replace_conflicting_release_tag(tmp_path: Path) -> None:
    env, log = _fake_tools(tmp_path, conflict=True)
    result = subprocess.run(
        [str(ROOT / "bin/deploy"), "v-test", "rocm"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Refusing to overwrite" in result.stderr
    assert "ssh " not in log.read_text()
    assert not (tmp_path / "manifests/v-test.json").exists()


def test_deploy_accepts_existing_tag_only_when_digest_matches(tmp_path: Path) -> None:
    env, log = _fake_tools(tmp_path, existing_match=True)
    result = subprocess.run(
        [str(ROOT / "bin/deploy"), "v-test", "rocm"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "docker tag" not in log.read_text()


def test_deploy_rejects_moving_latest_tag_before_build(tmp_path: Path) -> None:
    env, log = _fake_tools(tmp_path)
    result = subprocess.run(
        [str(ROOT / "bin/deploy"), "latest", "rocm"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "moving tag 'latest'" in result.stderr
    assert not log.exists()
