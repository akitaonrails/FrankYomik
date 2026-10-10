import copy
from pathlib import Path

import pytest
import yaml

from pipeline.revision import (
    DEFAULT_PIPELINE_REVISION,
    LEGACY_PIPELINE_REVISION,
    composite_implementation_revision,
    derived_pipeline_revision,
    fingerprint_stage_manifest,
    get_pipeline_revision,
    load_stage_manifest,
    stage_config_hash,
    stage_identity,
    validate_pipeline_revision,
    verify_stage_manifest_artifacts,
)


def test_checked_in_manifest_fingerprint_is_local_default(monkeypatch):
    monkeypatch.delenv("PIPELINE_REVISION", raising=False)
    monkeypatch.delenv("ALLOW_LEGACY_PIPELINE_REVISION", raising=False)

    revision = get_pipeline_revision()

    assert revision == DEFAULT_PIPELINE_REVISION
    assert revision == derived_pipeline_revision()
    assert revision.startswith("sha256:")
    assert len(revision) == 71


def test_matching_expected_revision_is_accepted(monkeypatch):
    monkeypatch.setenv("PIPELINE_REVISION", DEFAULT_PIPELINE_REVISION)

    assert get_pipeline_revision() == DEFAULT_PIPELINE_REVISION


def test_mismatched_expected_revision_fails(monkeypatch):
    monkeypatch.setenv("PIPELINE_REVISION", "sha256:" + "0" * 64)

    with pytest.raises(ValueError, match="PIPELINE_REVISION mismatch"):
        get_pipeline_revision()


def test_legacy_revision_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("PIPELINE_REVISION", LEGACY_PIPELINE_REVISION)
    monkeypatch.delenv("ALLOW_LEGACY_PIPELINE_REVISION", raising=False)

    with pytest.raises(ValueError, match="requires ALLOW_LEGACY"):
        get_pipeline_revision()

    monkeypatch.setenv("ALLOW_LEGACY_PIPELINE_REVISION", "1")
    assert get_pipeline_revision() == LEGACY_PIPELINE_REVISION


@pytest.mark.parametrize(
    ("path", "new_value"),
    [
        (("implementation", "revision"), "sha256:" + "1" * 64),
        (("model", "id"), "replacement-model"),
        (("model", "weights_revision"), "replacement-weights"),
        (("prompt", "sha256"), "2" * 64),
        (("output_config", "temperature"), 0.9),
    ],
)
def test_each_output_identity_changes_fingerprint(path, new_value):
    manifest = load_stage_manifest()
    changed = copy.deepcopy(manifest)
    target = changed["stages"]["manga.translation"]
    target[path[0]][path[1]] = new_value

    assert fingerprint_stage_manifest(changed) != fingerprint_stage_manifest(manifest)


def test_stage_provenance_identity_is_not_global_revision():
    identity = stage_identity("manga_translate", "translation")

    assert identity["implementation"]["revision"] != DEFAULT_PIPELINE_REVISION
    assert identity["model"]["weights_revision"].startswith("sha256:")
    assert stage_config_hash(identity).startswith("sha256:")


def test_shared_semantic_source_changes_both_detection_stage_revisions():
    manifest = load_stage_manifest()
    changed = copy.deepcopy(manifest)
    changed["artifacts"]["python_pipeline_detection"]["sha256"] = "0" * 64

    for stage_name in ("manga.detection", "webtoon.detection"):
        implementation = manifest["stages"][stage_name]["implementation"]
        changed_implementation = changed["stages"][stage_name]["implementation"]
        assert "python_pipeline_detection" in implementation["component_artifacts"]
        assert composite_implementation_revision(
            changed, changed_implementation["component_artifacts"],
        ) != implementation["revision"]


def test_manga_ocr_revision_covers_router_engines_and_persistence_policy():
    manifest = load_stage_manifest()
    implementation = manifest["stages"]["manga.ocr"]["implementation"]
    required = {
        "config",
        "manga_ocr",
        "manga_pipeline",
        "python_pipeline_ocr",
        "hayai_ocr",
        "hayai_model",
        "hayai_configuration",
        "python_kindle_config",
        "python_pipeline_adapters_current",
        "python_pipeline_current_backend",
        "python_worker_job",
        "python_worker_consumer",
    }

    assert required <= set(implementation["component_artifacts"])
    for component in required:
        changed = copy.deepcopy(manifest)
        changed["artifacts"][component]["sha256"] = "0" * 64
        assert composite_implementation_revision(
            changed, implementation["component_artifacts"],
        ) != implementation["revision"]


def test_artifact_drift_is_rejected():
    manifest = load_stage_manifest()
    manifest["artifacts"]["config"]["sha256"] = "0" * 64

    with pytest.raises(ValueError, match="artifact fingerprint mismatch"):
        verify_stage_manifest_artifacts(manifest)


def test_output_affecting_python_source_set_is_exhaustive():
    server_root = Path(__file__).parents[2]
    expected = {
        path.relative_to(server_root).as_posix()
        for package in ("pipeline", "worker", "kindle", "webtoon")
        for path in (server_root / package).glob("*.py")
    }
    manifest = load_stage_manifest()
    recorded = {
        artifact["path"] for artifact in manifest["artifacts"].values()
    }

    assert expected <= recorded


def test_configured_font_assets_are_exhaustively_fingerprinted():
    server_root = Path(__file__).parents[2]
    config = yaml.safe_load((server_root / "config.yaml").read_text())
    configured = set(config["fonts"].values())
    manifest = load_stage_manifest()
    recorded = {
        artifact["path"] for artifact in manifest["artifacts"].values()
    }

    assert configured <= recorded


def test_manifest_rejects_non_canonical_number():
    manifest = load_stage_manifest()
    manifest["stages"]["manga.translation"]["output_config"]["temperature"] = 1e-7

    with pytest.raises(ValueError, match="non-canonical number"):
        fingerprint_stage_manifest(manifest)


@pytest.mark.parametrize(
    "revision", [None, True, 2, "", "../escape", "has a space", "x" * 129]
)
def test_revision_validation_rejects_wrong_types_and_unsafe_values(revision):
    with pytest.raises(ValueError, match="invalid pipeline revision"):
        validate_pipeline_revision(revision)
