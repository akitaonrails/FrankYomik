package main

import (
	"bytes"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"strings"
)

// Embed the manifest and every artifact it names. The API image does not run
// the Python pipeline, but it must still prove that the revision it advertises
// describes the exact checked-in worker sources and assets.
//
//go:embed pipeline/stage_manifest.json config.yaml requirements.txt requirements.worker.lock pipeline/*.py pipeline/adapters/*.py worker/*.py kindle/*.py webtoon/*.py fonts/*.otf fonts/genei_antique/*.ttf
var stageIdentityFS embed.FS

var stageManifestJSON = mustReadStageIdentityFile("pipeline/stage_manifest.json")

func mustReadStageIdentityFile(name string) []byte {
	data, err := stageIdentityFS.ReadFile(name)
	if err != nil {
		panic(err)
	}
	return data
}

type stageArtifact struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type stageManifestArtifacts struct {
	Artifacts map[string]stageArtifact `json:"artifacts"`
}

func verifyStageManifestArtifacts(raw []byte, artifacts fs.FS) error {
	var manifest stageManifestArtifacts
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return fmt.Errorf("decode stage manifest artifacts: %w", err)
	}
	if len(manifest.Artifacts) == 0 {
		return errors.New("pipeline stage manifest has no artifacts")
	}
	for name, artifact := range manifest.Artifacts {
		if artifact.Path == "" || artifact.SHA256 == "" ||
			!fs.ValidPath(artifact.Path) {
			return fmt.Errorf("invalid pipeline artifact %s", name)
		}
		data, err := fs.ReadFile(artifacts, artifact.Path)
		if err != nil {
			return fmt.Errorf("pipeline artifact unavailable: %s: %w", name, err)
		}
		sum := sha256.Sum256(data)
		if hex.EncodeToString(sum[:]) != artifact.SHA256 {
			return fmt.Errorf("pipeline artifact fingerprint mismatch: %s", name)
		}
	}
	return nil
}

func fingerprintStageManifest(raw []byte) (string, error) {
	var manifest interface{}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&manifest); err != nil {
		return "", fmt.Errorf("decode stage manifest: %w", err)
	}
	if err := validateStageManifestNormalForm(manifest); err != nil {
		return "", err
	}
	data, err := canonicalJSON(raw)
	if err != nil {
		return "", fmt.Errorf("canonicalize stage manifest: %w", err)
	}
	sum := sha256.Sum256(data)
	return "sha256:" + hex.EncodeToString(sum[:]), nil
}

func validateStageManifestNormalForm(value interface{}) error {
	switch typed := value.(type) {
	case map[string]interface{}:
		for key, child := range typed {
			if strings.ContainsAny(key, "\u2028\u2029") {
				return errors.New("stage manifest contains a non-canonical string")
			}
			if err := validateStageManifestNormalForm(child); err != nil {
				return err
			}
		}
	case []interface{}:
		for _, child := range typed {
			if err := validateStageManifestNormalForm(child); err != nil {
				return err
			}
		}
	case string:
		if strings.ContainsAny(typed, "\u2028\u2029") {
			return errors.New("stage manifest contains a non-canonical string")
		}
	case json.Number:
		normalized, err := normalizeJSONNumber(typed.String())
		if err != nil || normalized != typed.String() {
			return fmt.Errorf("stage manifest contains non-canonical number %s", typed)
		}
	}
	return nil
}

func derivedPipelineRevision() string {
	if err := verifyStageManifestArtifacts(stageManifestJSON, stageIdentityFS); err != nil {
		panic(err)
	}
	revision, err := fingerprintStageManifest(stageManifestJSON)
	if err != nil {
		panic(err)
	}
	return revision
}

func resolveConfiguredPipelineRevision() (string, error) {
	derived := derivedPipelineRevision()
	expected := strings.TrimSpace(os.Getenv("PIPELINE_REVISION"))
	if expected == "" {
		return derived, nil
	}
	if !pipelineRevisionRe.MatchString(expected) {
		return "", fmt.Errorf("invalid PIPELINE_REVISION %q", expected)
	}
	if expected == legacyPipelineRevision {
		if os.Getenv("ALLOW_LEGACY_PIPELINE_REVISION") == "1" {
			return expected, nil
		}
		return "", errors.New(
			"legacy pipeline revision requires ALLOW_LEGACY_PIPELINE_REVISION=1")
	}
	if expected != derived {
		return "", fmt.Errorf(
			"PIPELINE_REVISION mismatch: expected %s, derived %s",
			expected, derived)
	}
	return derived, nil
}
