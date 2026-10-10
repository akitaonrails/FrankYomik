package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

type canonicalFixture struct {
	Input     json.RawMessage `json:"input"`
	Canonical string          `json:"canonical"`
	SHA256    string          `json:"sha256"`
}

type normalFormFixture struct {
	Base            map[string]interface{} `json:"base"`
	NullableFields  []string               `json:"nullable_fields"`
	DefaultedFields []struct {
		Path    string      `json:"path"`
		Default interface{} `json:"default"`
	} `json:"defaulted_fields"`
}

func loadNormalFormFixture(t *testing.T) normalFormFixture {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("tests", "fixtures", "page_document_v2_normal_form.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixture normalFormFixture
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	if err := decoder.Decode(&fixture); err != nil {
		t.Fatal(err)
	}
	return fixture
}

func cloneJSONObject(t *testing.T, value map[string]interface{}) map[string]interface{} {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := decodeJSONValue(raw)
	if err != nil {
		t.Fatal(err)
	}
	return decoded.(map[string]interface{})
}

func setJSONPointer(t *testing.T, payload map[string]interface{}, pointer string, value interface{}, remove bool) {
	t.Helper()
	parts := strings.Split(strings.TrimPrefix(pointer, "/"), "/")
	var target interface{} = payload
	for _, part := range parts[:len(parts)-1] {
		switch typed := target.(type) {
		case map[string]interface{}:
			target = typed[part]
		case []interface{}:
			index, err := strconv.Atoi(part)
			if err != nil {
				t.Fatal(err)
			}
			target = typed[index]
		default:
			t.Fatalf("pointer %s traversed %T", pointer, target)
		}
	}
	object, ok := target.(map[string]interface{})
	if !ok {
		t.Fatalf("pointer %s ends in %T", pointer, target)
	}
	field := parts[len(parts)-1]
	if remove {
		delete(object, field)
	} else {
		object[field] = value
	}
}

func canonicalFixtureDocument(t *testing.T, payload map[string]interface{}) string {
	t.Helper()
	cache := NewCacheWithRevision(t.TempDir(), "pipeline-v2")
	raw, err := marshalCanonicalJSON(payload)
	if err != nil {
		t.Fatal(err)
	}
	canonical, err := cache.canonicalMetadata(raw, "manga_translate", strings.Repeat("0123456789abcdef", 4))
	if err != nil {
		t.Fatal(err)
	}
	return string(canonical)
}

func loadCanonicalFixture(t *testing.T) canonicalFixture {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("tests", "fixtures", "page_document_v2_canonical.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixture canonicalFixture
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatal(err)
	}
	return fixture
}

func TestPageDocumentCanonicalFixtureMatchesPython(t *testing.T) {
	fixture := loadCanonicalFixture(t)
	cache := NewCacheWithRevision(t.TempDir(), "pipeline-v2")
	sourceHash := strings.Repeat("0123456789abcdef", 4)

	canonical, err := cache.canonicalMetadata(
		fixture.Input, "manga_translate", sourceHash,
	)
	if err != nil {
		t.Fatalf("canonical metadata: %v", err)
	}
	if string(canonical) != fixture.Canonical {
		t.Fatalf("canonical mismatch\n got: %s\nwant: %s", canonical, fixture.Canonical)
	}
	digest := sha256.Sum256(canonical)
	if got := hex.EncodeToString(digest[:]); got != fixture.SHA256 {
		t.Fatalf("hash = %s, want %s", got, fixture.SHA256)
	}
}

func TestCanonicalJSONNormalizationMatchesPython(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("tests", "fixtures", "canonical_json_normalization.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Cases []struct {
			Input     string `json:"input"`
			Canonical string `json:"canonical"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatal(err)
	}
	for _, test := range fixture.Cases {
		canonical, err := canonicalJSON([]byte(test.Input))
		if err != nil {
			t.Fatalf("canonicalize %q: %v", test.Input, err)
		}
		if string(canonical) != test.Canonical {
			t.Fatalf("canonicalize %q = %q, want %q", test.Input, canonical, test.Canonical)
		}
	}
}

func TestPageDocumentNullableFieldsHaveOneNormalForm(t *testing.T) {
	fixture := loadNormalFormFixture(t)
	for _, pointer := range fixture.NullableFields {
		t.Run(pointer, func(t *testing.T) {
			missing := cloneJSONObject(t, fixture.Base)
			explicitNull := cloneJSONObject(t, fixture.Base)
			setJSONPointer(t, missing, pointer, nil, true)
			setJSONPointer(t, explicitNull, pointer, nil, false)
			if got, want := canonicalFixtureDocument(t, explicitNull), canonicalFixtureDocument(t, missing); got != want {
				t.Fatalf("explicit null and missing differ\nnull: %s\nmissing: %s", got, want)
			}
		})
	}
}

func TestPageDocumentDefaultedFieldsHaveOneNormalForm(t *testing.T) {
	fixture := loadNormalFormFixture(t)
	for _, test := range fixture.DefaultedFields {
		t.Run(test.Path, func(t *testing.T) {
			missing := cloneJSONObject(t, fixture.Base)
			explicitNull := cloneJSONObject(t, fixture.Base)
			explicitDefault := cloneJSONObject(t, fixture.Base)
			setJSONPointer(t, missing, test.Path, nil, true)
			setJSONPointer(t, explicitNull, test.Path, nil, false)
			setJSONPointer(t, explicitDefault, test.Path, test.Default, false)
			want := canonicalFixtureDocument(t, missing)
			if got := canonicalFixtureDocument(t, explicitNull); got != want {
				t.Fatalf("explicit null differs from missing\nnull: %s\nmissing: %s", got, want)
			}
			if got := canonicalFixtureDocument(t, explicitDefault); got != want {
				t.Fatalf("explicit default differs from missing\ndefault: %s\nmissing: %s", got, want)
			}
		})
	}
}

func TestHTTPPatchLoadsRerendersAndKeepsHashInPython(t *testing.T) {
	python := filepath.Join(".venv", "bin", "python")
	if _, err := os.Stat(python); err != nil {
		t.Skip("Python virtual environment is not available")
	}
	fixture := loadCanonicalFixture(t)
	decoded, err := decodeJSONValue(fixture.Input)
	if err != nil {
		t.Fatal(err)
	}
	payload := decoded.(map[string]interface{})
	imageBytes := makePNGBytes()
	sourceHash := hashHex(imageBytes)
	payload["source_hash"] = sourceHash
	payload["image"] = map[string]interface{}{
		"width": json.Number("100"), "height": json.Number("80"),
	}
	region := payload["regions"].([]interface{})[0].(map[string]interface{})
	region["parent_id"] = nil
	region["user"] = nil
	region["ocr"].(map[string]interface{})["alternatives"] = nil
	region["ocr"].(map[string]interface{})["routing_reason"] = nil
	region["render"].(map[string]interface{})["vertical"] = nil
	region["transform"].(map[string]interface{})["value"] = nil
	raw, err := marshalCanonicalJSON(payload)
	if err != nil {
		t.Fatal(err)
	}

	cacheRoot := t.TempDir()
	cache := NewCacheWithRevision(cacheRoot, "pipeline-v2")
	manifest, err := cache.StoreBySourceHash(
		"manga_translate", sourceHash, imageBytes, imageBytes,
		[]byte(`{"schema_version":1,"regions":[]}`), "Test", "1", "1",
	)
	if err != nil {
		t.Fatal(err)
	}
	srv := &Server{
		cache:            cache,
		pipelineRevision: "pipeline-v2",
		submitRerenderJob: func(_ context.Context, _ []byte, _, _ string, metadata *JobMetadata) (string, bool, error) {
			if metadata == nil || !metadata.RerenderFromMetadata {
				t.Fatal("PATCH did not request metadata rerender")
			}
			return "rerender-job", false, nil
		},
	}
	mux := http.NewServeMux()
	mux.HandleFunc("PATCH /api/v1/cache/by-hash/{pipeline}/{source_hash}/meta", srv.handlePatchCacheMetaByHash)
	patchBody, err := json.Marshal(map[string]interface{}{
		"base_content_hash": manifest.ContentHash,
		"metadata":          json.RawMessage(raw),
	})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPatch,
		"/api/v1/cache/by-hash/manga_translate/"+sourceHash+"/meta", bytes.NewReader(patchBody))
	recorder := httptest.NewRecorder()
	mux.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusAccepted {
		t.Fatalf("PATCH status = %d: %s", recorder.Code, recorder.Body.String())
	}
	var response JobResponse
	if err := json.NewDecoder(recorder.Body).Decode(&response); err != nil {
		t.Fatal(err)
	}

	script := `
import hashlib, sys
from dataclasses import replace
from io import BytesIO
import numpy as np
from PIL import Image
from pipeline.document import PageDocument, canonical_json_bytes
from worker.page_cache import PageCache
from worker.job import ProcessingJob, process_job, production_dependencies
root, source_hash, revision = sys.argv[1:]
cache = PageCache(root, pipeline_revision=revision)
payload = cache.load_metadata_by_hash(
    "manga_translate", source_hash, document_pipeline="manga_translate")
document = PageDocument.from_dict(payload)
source = cache.load_source_image_by_hash("manga_translate", source_hash)
def decode(data):
    image = Image.open(BytesIO(data)).convert("RGB")
    return np.asarray(image)[:, :, ::-1].copy(), image
def encode(image):
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
deps = replace(
    production_dependencies(),
    decode_image=decode,
    encode_image=encode,
    extract_manga_mask=lambda *_args, **_kwargs: None,
    clear_region=lambda *_args, **_kwargs: None,
    clear_strokes=lambda *_args, **_kwargs: None,
    render_translation=lambda *_args, **_kwargs: None,
    furigana_font_limits=lambda _values: (None, None, None),
    config_value=lambda name: {"MIN_FONT_SIZE": 10, "EN_PAGE_FONT_DIVISOR": 30}[name],
)
result = process_job(ProcessingJob(
    job_id="cross-runtime-rerender",
    pipeline="manga_translate",
    image_bytes=source,
    source_hash=source_hash,
    rerender_from_metadata=True,
    metadata_payload=payload,
    pipeline_revision=revision,
), dependencies=deps)
assert result.status == "completed", result.error
assert result.image_bytes.startswith(b"\x89PNG")
assert result.metadata_payload == payload
print(hashlib.sha256(canonical_json_bytes(document.to_dict())).hexdigest())
`
	command := exec.Command(python, "-c", script, cacheRoot, sourceHash, "pipeline-v2")
	command.Env = append(os.Environ(), "PYTHONPATH=.")
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("Python cache load failed: %v\n%s", err, output)
	}
	if got := strings.TrimSpace(string(output)); got != response.ContentHash {
		t.Fatalf("Python content hash = %s, HTTP content hash = %s", got, response.ContentHash)
	}
}

func TestPatchRejectsInvalidPageDocumentBeforeMutation(t *testing.T) {
	cache := NewCacheWithRevision(t.TempDir(), "pipeline-v2")
	srv := &Server{cache: cache, pipelineRevision: "pipeline-v2"}
	mux := http.NewServeMux()
	mux.HandleFunc(
		"PATCH /api/v1/cache/by-hash/{pipeline}/{source_hash}/meta",
		srv.handlePatchCacheMetaByHash,
	)
	imageBytes := makePNGBytes()
	sourceHash := hashHex(imageBytes)
	manifest, err := srv.cache.StoreBySourceHash(
		"manga_translate", sourceHash, imageBytes, imageBytes,
		[]byte(`{"schema_version":1,"regions":[]}`), "Test", "1", "1",
	)
	if err != nil {
		t.Fatal(err)
	}
	body, err := json.Marshal(map[string]interface{}{
		"base_content_hash": manifest.ContentHash,
		"metadata": map[string]interface{}{
			"schema_version":    2,
			"page_id":           "page-1",
			"pipeline":          "manga_translate",
			"pipeline_revision": srv.pipelineRevision,
			"source_hash":       sourceHash,
			"image":             map[string]interface{}{"width": 100.5, "height": 100},
			"regions":           []interface{}{},
			"stage_provenance":  map[string]interface{}{},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(
		http.MethodPatch,
		"/api/v1/cache/by-hash/manga_translate/"+sourceHash+"/meta",
		bytes.NewReader(body),
	)
	recorder := httptest.NewRecorder()
	mux.ServeHTTP(recorder, req)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400: %s", recorder.Code, recorder.Body.String())
	}
	after, ok := srv.cache.GetManifestBySourceHash("manga_translate", sourceHash)
	if !ok {
		t.Fatal("manifest disappeared after rejected patch")
	}
	if after.ContentHash != manifest.ContentHash || after.ImageStale {
		t.Fatal("rejected patch mutated the cache manifest")
	}
}

func TestPageDocumentValidatorRejectsAdversarialPayloads(t *testing.T) {
	fixture := loadCanonicalFixture(t)
	sourceHash := strings.Repeat("0123456789abcdef", 4)
	tests := []struct {
		name   string
		mutate func(map[string]interface{})
	}{
		{"boolean schema", func(payload map[string]interface{}) {
			payload["schema_version"] = true
		}},
		{"fractional dimension", func(payload map[string]interface{}) {
			payload["image"].(map[string]interface{})["width"] = json.Number("100.5")
		}},
		{"boolean coordinate", func(payload map[string]interface{}) {
			region := payload["regions"].([]interface{})[0].(map[string]interface{})
			region["bbox"].([]interface{})[0] = true
		}},
		{"invalid source hash", func(payload map[string]interface{}) {
			payload["source_hash"] = strings.Repeat("A", 64)
		}},
		{"unknown pipeline", func(payload map[string]interface{}) {
			payload["pipeline"] = "manga"
		}},
		{"parent cycle", func(payload map[string]interface{}) {
			region := payload["regions"].([]interface{})[0].(map[string]interface{})
			region["parent_id"] = region["id"]
		}},
		{"polygon outside page", func(payload map[string]interface{}) {
			region := payload["regions"].([]interface{})[0].(map[string]interface{})
			region["polygon"] = []interface{}{
				[]interface{}{json.Number("0"), json.Number("0")},
				[]interface{}{json.Number("100"), json.Number("1")},
				[]interface{}{json.Number("1"), json.Number("1")},
			}
		}},
		{"render box outside page", func(payload map[string]interface{}) {
			region := payload["regions"].([]interface{})[0].(map[string]interface{})
			region["render"] = map[string]interface{}{
				"bbox": []interface{}{
					json.Number("0"), json.Number("0"),
					json.Number("101"), json.Number("10"),
				},
				"provenance": map[string]interface{}{
					"backend": "renderer", "revision": "v1",
				},
				"vertical": false,
			}
		}},
		{"mask dimension mismatch", func(payload map[string]interface{}) {
			region := payload["regions"].([]interface{})[0].(map[string]interface{})
			region["mask"] = map[string]interface{}{
				"reference": "mask.rle", "encoding": "coco-rle",
				"width": json.Number("99"), "height": json.Number("200"),
			}
		}},
		{"unknown top-level field", func(payload map[string]interface{}) {
			payload["future_field"] = "must not be ignored"
		}},
		{"unknown nested field", func(payload map[string]interface{}) {
			region := payload["regions"].([]interface{})[0].(map[string]interface{})
			region["future_field"] = "must not be ignored"
		}},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			decoded, err := decodeJSONValue(fixture.Input)
			if err != nil {
				t.Fatal(err)
			}
			payload := decoded.(map[string]interface{})
			test.mutate(payload)
			raw, err := marshalCanonicalJSON(payload)
			if err != nil {
				t.Fatal(err)
			}
			cache := NewCacheWithRevision(t.TempDir(), "pipeline-v2")
			_, err = cache.canonicalMetadata(raw, "manga_translate", sourceHash)
			if !errors.Is(err, errInvalidMetadata) {
				t.Fatalf("expected invalid metadata error, got %v", err)
			}
		})
	}
}
