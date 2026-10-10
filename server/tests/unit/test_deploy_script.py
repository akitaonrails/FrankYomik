import json
import os
import socket
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
    tmp_path: Path,
    *,
    conflict: bool = False,
    existing_match: bool = False,
    build_failure: bool = False,
    use_forward: bool = False,
    lookup_failure: bool = False,
    sync_failure: bool = False,
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
if [ -n "${{CAPTURE_DOCKER_CONFIG:-}}" ] && [ -f "${{DOCKER_CONFIG:-}}/config.json" ]; then
  cp "${{DOCKER_CONFIG}}/config.json" "${{CAPTURE_DOCKER_CONFIG}}"
fi
case "$*" in
  build*)
    if [ "${{FAKE_BUILD_FAILURE:-0}}" = 1 ]; then exit 42; fi
    exit 0
    ;;
  *\"manifest inspect --insecure --verbose\"*)
    ref="$5"
    case "$ref" in *worker*) digest='{WORKER_DIGEST}' ;; *) digest='{API_DIGEST}' ;; esac
    case "$ref" in
      *:v-test)
        if [ "${{FAKE_LOOKUP_FAILURE:-0}}" = 1 ]; then echo 'registry unavailable' >&2; exit 1
        elif [ "${{FAKE_CONFLICT:-0}}" = 1 ]; then digest='{CONFLICT_DIGEST}'
        elif [ "${{FAKE_EXISTING_MATCH:-0}}" = 1 ]; then :
        elif [ ! -f '{state}.'"$digest" ]; then echo 'manifest unknown' >&2; exit 1
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
case "$*" in
  *.release-*)
    cat >/dev/null || true
    if [ "${{FAKE_SYNC_FAILURE:-0}}" = 1 ]; then exit 55; fi
    exit 0
    ;;
esac
case "$*" in
  *\" -O exit \"*)
    while [ "$#" -gt 0 ]; do
      if [ "$1" = -S ]; then control="$2"; shift 2; continue; fi
      shift
    done
    if [ -f "$control.pid" ]; then kill "$(cat "$control.pid")" 2>/dev/null || true; fi
    rm -f "$control" "$control.pid"
    exit 0
    ;;
  *\" -f \"*\" -L \"*)
    while [ "$#" -gt 0 ]; do
      case "$1" in
        -S) control="$2"; shift 2 ;;
        -L) spec="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    port="${{spec#127.0.0.1:}}"; port="${{port%%:*}}"
    python3 -c 'import socket,sys,time; s=socket.socket(); s.bind(("127.0.0.1",int(sys.argv[1]))); s.listen(); time.sleep(60)' "$port" &
    pid=$!
    sleep 0.05
    kill -0 "$pid" 2>/dev/null || exit 1
    touch "$control"; printf '%s\\n' "$pid" > "$control.pid"
    exit 0
    ;;
esac
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
            "FAKE_BUILD_FAILURE": "1" if build_failure else "0",
            "FAKE_LOOKUP_FAILURE": "1" if lookup_failure else "0",
            "FAKE_SYNC_FAILURE": "1" if sync_failure else "0",
        }
    )
    if use_forward:
        env.pop("REGISTRY", None)
        env.pop("REMOTE_REGISTRY", None)
        source_config = tmp_path / "source-docker-config"
        source_config.mkdir()
        (source_config / "config.json").write_text(
            json.dumps({"auths": {"192.168.0.90:3007": {"auth": "test"}}})
        )
        env["DOCKER_CONFIG"] = str(source_config)
        env["CAPTURE_DOCKER_CONFIG"] = str(tmp_path / "forwarded-config.json")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            env["LOCAL_REGISTRY_PORT"] = str(probe.getsockname()[1])
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
    assert calls.count(".release-") == 3
    assert "docker compose -f docker-compose.prod.yml" in calls
    assert "docker compose up" not in calls
    assert "docker compose ps" not in calls
    assert f"PIPELINE_REVISION='{PIPELINE}'" in calls
    assert calls.index("docker-compose.prod.yml.release-") < calls.index(
        "docker compose -f docker-compose.prod.yml"
    )
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


def test_deploy_stops_immediately_when_build_fails(tmp_path: Path) -> None:
    env, log = _fake_tools(tmp_path, build_failure=True)
    result = subprocess.run(
        [str(ROOT / "bin/deploy"), "v-test", "rocm"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    calls = log.read_text()
    assert "docker build " in calls
    assert "docker image inspect" not in calls
    assert "docker push" not in calls
    assert "ssh " not in calls


def test_deploy_uses_and_cleans_up_temporary_registry_forward(tmp_path: Path) -> None:
    env, log = _fake_tools(tmp_path, use_forward=True)
    port = int(env["LOCAL_REGISTRY_PORT"])
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
    assert f"localhost:{port}/akitaonrails/frank-yomik-api" in calls
    assert (
        f"API_IMAGE='192.168.0.90:3007/akitaonrails/frank-yomik-api@{API_DIGEST}'"
        in calls
    )
    assert "Opening temporary SSH forward" in result.stdout
    assert json.loads((tmp_path / "forwarded-config.json").read_text()) == {
        "auths": {f"localhost:{port}": {"auth": "test"}}
    }
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def test_deploy_stops_if_registry_forward_port_collides(tmp_path: Path) -> None:
    env, log = _fake_tools(tmp_path, use_forward=True)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", int(env["LOCAL_REGISTRY_PORT"])))
        occupied.listen()
        result = subprocess.run(
            [str(ROOT / "bin/deploy"), "v-test", "rocm"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    assert result.returncode != 0
    assert "Registry SSH forward failed" in result.stderr
    assert "docker build" not in log.read_text()


def test_deploy_fails_closed_on_transient_release_tag_lookup(tmp_path: Path) -> None:
    env, log = _fake_tools(tmp_path, lookup_failure=True)
    result = subprocess.run(
        [str(ROOT / "bin/deploy"), "v-test", "rocm"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Registry manifest lookup failed" in result.stderr
    calls = log.read_text()
    assert "docker tag" not in calls
    assert "docker push registry.test/project/frank-yomik-api:v-test" not in calls
    assert "ssh " not in calls


def test_deploy_sync_failure_stops_before_remote_pull_or_recreation(tmp_path: Path) -> None:
    env, log = _fake_tools(tmp_path, sync_failure=True)
    result = subprocess.run(
        [str(ROOT / "bin/deploy"), "v-test", "rocm"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    calls = log.read_text()
    assert calls.count(".release-") == 1
    assert "docker pull" not in calls
    assert "docker compose" not in calls
