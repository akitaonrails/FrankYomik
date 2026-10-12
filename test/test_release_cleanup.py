import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "release_cleanup.py"
SPEC = importlib.util.spec_from_file_location("release_cleanup", MODULE_PATH)
assert SPEC and SPEC.loader
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


def image(image_id, version, repositories, references=()):
    return {
        "id": image_id,
        "labels": {"org.opencontainers.image.version": version},
        "repositories": list(repositories),
        "references": list(references),
    }


class ReleaseCleanupTest(unittest.TestCase):
    def test_remote_keeps_running_two_stable_and_explicit_rollback(self):
        images = [
            image("current", "v2.0.3", ["host/akitaonrails/frank-yomik-api"]),
            image("previous", "v2.0.1", ["host/akitaonrails/frank-yomik-api"]),
            image("old", "v2.0.0", ["host/akitaonrails/frank-yomik-api"]),
            image("rc", "v2.0.0-rc.12", ["host/akitaonrails/frank-yomik-worker-rocm"]),
            image("running-old", "v1.0.0", ["host/akitaonrails/frank-yomik-api"]),
            image(
                "rollback",
                "v1.0.1",
                ["host/akitaonrails/frank-yomik-api"],
                ["host/akitaonrails/frank-yomik-api:v1.0.1-rollback"],
            ),
            image("other", "v9.0.0", ["other-project"]),
        ]
        self.assertEqual(
            cleanup.plan_image_removal(images, {"running-old"}, preserve_stable=True),
            ["old", "rc"],
        )

    def test_local_removes_all_unused_project_images(self):
        images = [
            image("used", "v2.0.1", ["frank-yomik-api"]),
            image("gate", "", ["frank-yomik-worker-cpu"]),
            image("release", "v2.0.1", ["localhost:34567/akitaonrails/frank-yomik-api"]),
        ]
        self.assertEqual(
            cleanup.plan_image_removal(images, {"used"}, preserve_stable=False),
            ["gate", "release"],
        )

    def test_registry_keeps_only_two_stable_versions_and_rollbacks(self):
        packages = [
            {"type": "container", "name": "frank-yomik-api", "version": version}
            for version in ("v2.0.1", "v2.0.0", "v1.0.1", "v2.0.0-rc.1", "candidate-abc")
        ] + [
            {"type": "container", "name": "frank-yomik-worker-rocm", "version": "v2.0.1"},
            {"type": "container", "name": "frank-yomik-worker-rocm", "version": "v2.0.0"},
            {"type": "container", "name": "frank-yomik-worker-rocm", "version": "v1.0.1"},
            {"type": "container", "name": "frank-yomik-api", "version": "v1-rollback"},
            {"type": "container", "name": "frank-yomik-api", "version": "latest"},
            {"type": "container", "name": "frank-yomik-api", "version": "sha256:abc"},
            {"type": "container", "name": "unrelated", "version": "candidate-abc"},
        ]
        self.assertEqual(
            cleanup.plan_registry_removal(packages),
            [
                ("frank-yomik-api", "candidate-abc"),
                ("frank-yomik-api", "v1.0.1"),
                ("frank-yomik-api", "v2.0.0-rc.1"),
                ("frank-yomik-worker-rocm", "v1.0.1"),
            ],
        )

    def test_manifests_keep_two_stable_and_explicit_rollback(self):
        paths = [Path(name) for name in (
            "v2.0.1.json", "v2.0.0.json", "v1.0.1.json",
            "v2.0.0-rc.1.json", "v1-rollback.json",
        )]
        self.assertEqual(
            [path.name for path in cleanup.plan_manifest_removal(paths)],
            ["v1.0.1.json", "v2.0.0-rc.1.json"],
        )


if __name__ == "__main__":
    unittest.main()
