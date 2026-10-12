#!/usr/bin/env python3
"""Remove Frank Yomik release artifacts without pruning shared Docker state."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any, Iterable
import urllib.error
import urllib.parse
import urllib.request


STABLE_VERSION = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
PROJECT_REPOSITORY = re.compile(
    r"(?:^|/)frank-yomik-(?:api|worker|worker-cpu|worker-rocm)$"
)


def is_project_repository(repository: str) -> bool:
    return bool(PROJECT_REPOSITORY.search(repository)) or repository == "frank-yomik-api-revision-test"


def stable_versions(versions: Iterable[str], keep: int = 2) -> set[str]:
    parsed: dict[str, tuple[int, int, int]] = {}
    for version in versions:
        match = STABLE_VERSION.fullmatch(version)
        if match:
            parsed[version] = tuple(map(int, match.groups()))
    ordered = sorted(parsed, key=parsed.get, reverse=True)
    return set(ordered[:keep])


def plan_image_removal(
    images: list[dict[str, Any]], running_ids: set[str], preserve_stable: bool
) -> list[str]:
    versions = {
        str(image.get("labels", {}).get("org.opencontainers.image.version", ""))
        for image in images
        if any(
            is_project_repository(str(repo))
            for repo in image.get("repositories", [])
        )
    }
    retained_versions = stable_versions(versions) if preserve_stable else set()
    removals: list[str] = []
    for image in images:
        image_id = str(image["id"])
        references = [str(ref) for ref in image.get("references", [])]
        repositories = [str(repo) for repo in image.get("repositories", [])]
        if not any(is_project_repository(repo) for repo in repositories):
            continue
        version = str(image.get("labels", {}).get("org.opencontainers.image.version", ""))
        if image_id in running_ids:
            continue
        if any("rollback" in ref.lower() for ref in references):
            continue
        if preserve_stable and version in retained_versions:
            continue
        removals.append(image_id)
    return sorted(set(removals))


def plan_registry_removal(packages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    project = [
        package
        for package in packages
        if package.get("type") == "container"
        and is_project_repository(str(package.get("name", "")))
    ]
    retained: dict[str, set[str]] = {}
    for name in {str(package.get("name", "")) for package in project}:
        retained[name] = stable_versions(
            str(package.get("version", ""))
            for package in project
            if str(package.get("name", "")) == name
        )
    removals = []
    for package in project:
        name = str(package["name"])
        version = str(package.get("version", ""))
        if "rollback" in version.lower():
            continue
        is_stable = bool(STABLE_VERSION.fullmatch(version))
        is_temporary = version.startswith("candidate-") or bool(
            re.fullmatch(r"v\d+\.\d+\.\d+-.+", version)
        )
        if is_temporary or (is_stable and version not in retained[name]):
            removals.append((name, version))
    return sorted(removals)


def plan_manifest_removal(paths: Iterable[Path]) -> list[Path]:
    paths = list(paths)
    retained = stable_versions(path.stem for path in paths)
    return sorted(
        path
        for path in paths
        if path.stem not in retained and "rollback" not in path.stem.lower()
    )


class DockerHost:
    def __init__(self, ssh_target: str | None = None, ssh_options: list[str] | None = None):
        self.ssh_target = ssh_target
        self.ssh_options = ssh_options or []

    def run(self, arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        if self.ssh_target:
            command = ["ssh", *self.ssh_options, self.ssh_target, shlex.join(arguments)]
        else:
            command = arguments
        return subprocess.run(command, check=check, text=True, capture_output=True)

    def inventory(self) -> tuple[list[dict[str, Any]], set[str]]:
        rows = []
        listing = self.run(
            ["docker", "image", "ls", "--no-trunc", "--format", "{{json .}}"]
        ).stdout
        for line in listing.splitlines():
            row = json.loads(line)
            repository = str(row.get("Repository", ""))
            if is_project_repository(repository):
                rows.append(row)

        by_id: dict[str, set[str]] = {}
        repositories: dict[str, set[str]] = {}
        for row in rows:
            image_id = str(row["ID"])
            repository = str(row["Repository"])
            tag = str(row.get("Tag", "<none>"))
            repositories.setdefault(image_id, set()).add(repository)
            if tag != "<none>":
                by_id.setdefault(image_id, set()).add(f"{repository}:{tag}")

        image_ids = sorted(repositories)
        details: dict[str, dict[str, Any]] = {}
        if image_ids:
            inspected = json.loads(self.run(["docker", "image", "inspect", *image_ids]).stdout)
            for image in inspected:
                image_id = str(image["Id"])
                refs = set(image.get("RepoTags") or []) | set(image.get("RepoDigests") or [])
                details[image_id] = {
                    "id": image_id,
                    "references": sorted(refs | by_id.get(image_id, set())),
                    "repositories": sorted(repositories.get(image_id, set())),
                    "labels": (image.get("Config") or {}).get("Labels") or {},
                }

        running_ids: set[str] = set()
        container_ids = self.run(["docker", "ps", "-q"]).stdout.split()
        if container_ids:
            output = self.run(
                ["docker", "inspect", "--format", "{{.Image}}", *container_ids]
            ).stdout
            running_ids.update(output.split())
        return list(details.values()), running_ids

    def remove_images(self, image_ids: list[str], apply: bool) -> None:
        for image_id in image_ids:
            print(f"{'remove' if apply else 'would remove'} image {image_id}"
                  f" on {self.ssh_target or 'local Docker'}")
            if apply:
                # The selected ID may have both a build tag and a registry tag.
                # Selection already excludes running and retained images.
                self.run(["docker", "image", "rm", "--force", image_id])

    def remove_stopped_project_containers(self, removable_images: set[str], apply: bool) -> None:
        container_ids = self.run(["docker", "ps", "-aq"]).stdout.split()
        if not container_ids:
            return
        inspected = json.loads(self.run(["docker", "container", "inspect", *container_ids]).stdout)
        for container in inspected:
            state = str((container.get("State") or {}).get("Status", ""))
            image_id = str(container.get("Image", ""))
            if state == "running" or image_id not in removable_images:
                continue
            container_id = str(container["Id"])
            name = str(container.get("Name", "")).lstrip("/")
            print(f"{'remove' if apply else 'would remove'} stopped container {name}"
                  f" on {self.ssh_target or 'local Docker'}")
            if apply:
                self.run(["docker", "container", "rm", container_id])


def docker_auth(registry: str, docker_config: Path) -> str | None:
    try:
        config = json.loads((docker_config / "config.json").read_text())
    except (OSError, ValueError):
        return None
    auth = (config.get("auths", {}).get(registry) or {}).get("auth")
    if not auth:
        return None
    # Validate before putting it in an HTTP header; never print the decoded value.
    base64.b64decode(auth, validate=True)
    return str(auth)


def registry_request(
    base_url: str, path: str, auth: str | None, method: str = "GET"
) -> bytes:
    headers = {"Authorization": f"Basic {auth}"} if auth else {}
    request = urllib.request.Request(base_url.rstrip("/") + path, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def clean_registry(
    base_url: str, registry_host: str, owner: str, docker_config: Path, apply: bool
) -> None:
    auth = docker_auth(registry_host, docker_config)
    packages: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for page in range(1, 101):
        query = urllib.parse.urlencode(
            {"type": "container", "q": "frank-yomik", "limit": 100, "page": page}
        )
        path = f"/api/v1/packages/{urllib.parse.quote(owner, safe='')}?{query}"
        batch = json.loads(registry_request(base_url, path, auth))
        new = []
        for package in batch:
            key = (
                str(package.get("type", "")),
                str(package.get("name", "")),
                str(package.get("version", "")),
            )
            if key not in seen:
                seen.add(key)
                new.append(package)
        packages.extend(new)
        if not batch or not new:
            break
    else:
        raise RuntimeError("registry package pagination exceeded 100 pages")
    for name, version in plan_registry_removal(packages):
        print(f"{'remove' if apply else 'would remove'} registry package {name}:{version}")
        if apply:
            delete_path = "/api/v1/packages/{}/{}/{}/{}".format(
                urllib.parse.quote(owner, safe=""),
                "container",
                urllib.parse.quote(name, safe=""),
                urllib.parse.quote(version, safe=""),
            )
            registry_request(base_url, delete_path, auth, method="DELETE")


def clean_manifests(host: DockerHost, directory: str, apply: bool) -> None:
    if host.ssh_target:
        output = host.run(
            ["find", directory, "-maxdepth", "1", "-type", "f", "-name", "*.json", "-print"]
        ).stdout
        paths = [Path(line) for line in output.splitlines()]
        for path in plan_manifest_removal(paths):
            print(f"{'remove' if apply else 'would remove'} remote manifest {path.name}")
            if apply:
                host.run(["rm", "-f", "--", str(path)])
    else:
        root = Path(directory)
        paths = list(root.glob("*.json")) if root.exists() else []
        for path in plan_manifest_removal(paths):
            print(f"{'remove' if apply else 'would remove'} local manifest {path.name}")
            if apply:
                path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform cleanup; default is a dry run")
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--remote-only", action="store_true")
    parser.add_argument("--registry-url")
    parser.add_argument("--registry-host")
    parser.add_argument("--registry-owner", default="akitaonrails")
    parser.add_argument("--skip-registry", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.local_only and args.remote_only:
        raise SystemExit("--local-only and --remote-only are mutually exclusive")

    root = Path(__file__).resolve().parent.parent
    server = os.environ.get("SERVER", "192.168.0.90")
    ssh_user = os.environ.get("SSH_USER", "akitaonrails")
    remote_dir = os.environ.get("REMOTE_DIR", "/var/opt/docker/frank/yomik")
    ssh_options = ["-F", "/dev/null"]
    if os.environ.get("SSH_KNOWN_HOSTS"):
        ssh_options += ["-o", f"UserKnownHostsFile={os.environ['SSH_KNOWN_HOSTS']}"]
    if os.environ.get("SSH_STRICT_HOST_KEY_CHECKING", "yes") == "no":
        ssh_options += ["-o", "StrictHostKeyChecking=no"]

    if not args.remote_only:
        local = DockerHost()
        images, running = local.inventory()
        removals = plan_image_removal(images, running, preserve_stable=False)
        local.remove_stopped_project_containers(set(removals), args.apply)
        local.remove_images(removals, args.apply)
        clean_manifests(local, str(root / "release-manifests"), args.apply)

    if not args.local_only:
        remote = DockerHost(f"{ssh_user}@{server}", ssh_options)
        images, running = remote.inventory()
        removals = plan_image_removal(images, running, preserve_stable=True)
        remote.remove_stopped_project_containers(set(removals), args.apply)
        remote.remove_images(removals, args.apply)
        clean_manifests(remote, f"{remote_dir}/release-manifests", args.apply)

        if not args.skip_registry:
            registry_host = args.registry_host or os.environ.get("REGISTRY_HOST", f"{server}:3007")
            registry_url = args.registry_url or os.environ.get("REGISTRY_URL", f"http://{registry_host}")
            docker_config = Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
            clean_registry(
                registry_url,
                registry_host,
                args.registry_owner,
                docker_config,
                args.apply,
            )

    if not args.apply:
        print("dry run only; pass --apply to remove the listed artifacts")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (subprocess.CalledProcessError, urllib.error.URLError, RuntimeError, ValueError) as exc:
        print(f"release cleanup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
