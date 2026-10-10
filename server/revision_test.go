package main

import (
	"encoding/json"
	"strings"
	"testing"
	"testing/fstest"
)

func TestDerivedPipelineRevisionMatchesPinnedManifest(t *testing.T) {
	const expected = "sha256:9bbd95ce6d976dc3eb995711ee4fce03f6a6e42fef6f9b4d5b8cceff698cc9f6"
	if got := derivedPipelineRevision(); got != expected {
		t.Fatalf("derived revision = %q, want %q", got, expected)
	}
}

func TestConfiguredPipelineRevisionRejectsMismatch(t *testing.T) {
	t.Setenv("PIPELINE_REVISION", "sha256:"+strings.Repeat("0", 64))
	if _, err := resolveConfiguredPipelineRevision(); err == nil {
		t.Fatal("expected configured revision mismatch")
	}
}

func TestLegacyPipelineRevisionRequiresExplicitOptIn(t *testing.T) {
	t.Setenv("PIPELINE_REVISION", legacyPipelineRevision)
	t.Setenv("ALLOW_LEGACY_PIPELINE_REVISION", "0")
	if _, err := resolveConfiguredPipelineRevision(); err == nil {
		t.Fatal("expected legacy revision opt-in error")
	}
	t.Setenv("ALLOW_LEGACY_PIPELINE_REVISION", "1")
	if got, err := resolveConfiguredPipelineRevision(); err != nil || got != legacyPipelineRevision {
		t.Fatalf("legacy opt-in = %q, %v", got, err)
	}
}

func TestManifestIdentityMutationChangesFingerprint(t *testing.T) {
	var manifest map[string]interface{}
	if err := json.Unmarshal(stageManifestJSON, &manifest); err != nil {
		t.Fatal(err)
	}
	before, err := fingerprintStageManifest(stageManifestJSON)
	if err != nil {
		t.Fatal(err)
	}
	stages := manifest["stages"].(map[string]interface{})
	translation := stages["manga.translation"].(map[string]interface{})
	model := translation["model"].(map[string]interface{})
	model["weights_revision"] = "replacement-weights"
	changed, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	after, err := fingerprintStageManifest(changed)
	if err != nil {
		t.Fatal(err)
	}
	if before == after {
		t.Fatal("model weights identity did not change pipeline fingerprint")
	}
}

func TestManifestFingerprintRejectsNonCanonicalNumber(t *testing.T) {
	if _, err := fingerprintStageManifest([]byte(`{"value":9.50e-1}`)); err == nil {
		t.Fatal("expected non-canonical manifest number to be rejected")
	}
}

func TestStageArtifactDriftStopsRevisionDerivation(t *testing.T) {
	files := fstest.MapFS{}
	var manifest stageManifestArtifacts
	if err := json.Unmarshal(stageManifestJSON, &manifest); err != nil {
		t.Fatal(err)
	}
	for _, artifact := range manifest.Artifacts {
		data, err := stageIdentityFS.ReadFile(artifact.Path)
		if err != nil {
			t.Fatal(err)
		}
		files[artifact.Path] = &fstest.MapFile{Data: data}
	}
	files[manifest.Artifacts["config"].Path] = &fstest.MapFile{Data: []byte("drift")}

	if err := verifyStageManifestArtifacts(stageManifestJSON, files); err == nil {
		t.Fatal("expected artifact drift to be rejected")
	}
}
