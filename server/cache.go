package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Cache handles filesystem-based caching of processed images and metadata.
//
// v2 layout (content-addressed, atomic writes):
//
//	<dir>/v2/objects/aa/bb/<sha256>
//	<dir>/v2/pages/by-hash/<pipeline>/<source_hash>/manifest.json
//	<dir>/v2/pages/by-ref/<pipeline>/<title_slug>/<chapter>/<page>.json
//
// Legacy image-only cache is still read for compatibility:
//
//	<dir>/<pipeline>/<title_slug>/<chapter>/<page>.png
//
// v2 is authoritative when present.
type Cache struct {
	dir              string
	pipelineRevision string
}

const (
	cacheManifestSchemaVersion = 4
	legacyPipelineRevision     = "legacy-v1"
)

var pipelineRevisionRe = regexp.MustCompile(`^[A-Za-z0-9._:-]{1,128}$`)

var errPipelineRevisionMismatch = errors.New("pipeline revision mismatch")

// CacheManifest is the authoritative per-page record in cache v2.
type CacheManifest struct {
	SchemaVersion    int    `json:"schema_version"`
	Pipeline         string `json:"pipeline"`
	PipelineRevision string `json:"pipeline_revision,omitempty"`
	SourceHash       string `json:"source_hash"`

	TitleSlug string `json:"title_slug,omitempty"`
	Chapter   string `json:"chapter,omitempty"`
	Page      string `json:"page,omitempty"`

	SourceObjectSHA256 string `json:"source_object_sha256"`
	SourceBytes        int    `json:"source_bytes"`

	ImageObjectSHA256 string `json:"image_object_sha256"`
	ImageBytes        int    `json:"image_bytes"`

	MetadataObjectSHA256  string   `json:"metadata_object_sha256"`
	MetadataBytes         int      `json:"metadata_bytes"`
	ArtifactObjectSHA256s []string `json:"artifact_object_sha256s,omitempty"`

	ContentHash string `json:"content_hash"`
	RenderHash  string `json:"render_hash"`
	ImageStale  bool   `json:"image_stale"`

	CreatedAtUnixMs int64 `json:"created_at_unix_ms"`
	UpdatedAtUnixMs int64 `json:"updated_at_unix_ms"`
}

type cacheRef struct {
	SchemaVersion int    `json:"schema_version"`
	Pipeline      string `json:"pipeline"`
	SourceHash    string `json:"source_hash"`
	TitleSlug     string `json:"title_slug,omitempty"`
	Chapter       string `json:"chapter,omitempty"`
	Page          string `json:"page,omitempty"`
	UpdatedAt     int64  `json:"updated_at_unix_ms"`
}

// NewCache creates a Cache backed by the given directory.
func NewCache(dir string) *Cache {
	return NewCacheWithRevision(dir, configuredPipelineRevision())
}

func configuredPipelineRevision() string {
	revision, err := resolveConfiguredPipelineRevision()
	if err != nil {
		panic(err)
	}
	return revision
}

// NewCacheWithRevision creates a cache scoped to one pipeline implementation.
// A revision change deliberately turns existing entries into cache misses.
func NewCacheWithRevision(dir, pipelineRevision string) *Cache {
	if !pipelineRevisionRe.MatchString(pipelineRevision) {
		panic(fmt.Sprintf("invalid pipeline revision %q", pipelineRevision))
	}
	return &Cache{dir: dir, pipelineRevision: pipelineRevision}
}

var slugRe = regexp.MustCompile(`[^a-z0-9\-]`)
var pathComponentRe = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)

// slugify converts a string to a lowercase, hyphen-separated slug.
func slugify(s string) string {
	s = strings.ToLower(strings.TrimSpace(s))
	s = strings.ReplaceAll(s, " ", "-")
	s = slugRe.ReplaceAllString(s, "")
	for strings.Contains(s, "--") {
		s = strings.ReplaceAll(s, "--", "-")
	}
	return strings.Trim(s, "-")
}

func hashHex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func safePathComponent(s string) (string, bool) {
	s = strings.TrimSpace(s)
	if s == "" || s == "." || s == ".." {
		return "", false
	}
	if strings.ContainsAny(s, `/\`) {
		return "", false
	}
	if !pathComponentRe.MatchString(s) {
		return "", false
	}
	return s, true
}

func isValidSHA256Hex(s string) bool {
	if len(s) != 64 {
		return false
	}
	for _, ch := range s {
		if !(ch >= '0' && ch <= '9' || ch >= 'a' && ch <= 'f') {
			return false
		}
	}
	return true
}

func canonicalJSON(raw []byte) ([]byte, error) {
	if len(raw) == 0 {
		return []byte("{}"), nil
	}
	v, err := decodeJSONValue(raw)
	if err != nil {
		return nil, err
	}
	return marshalCanonicalJSON(v)
}

func decodeJSONValue(raw []byte) (interface{}, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value interface{}
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	var trailing interface{}
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return nil, errors.New("multiple JSON values")
		}
		return nil, err
	}
	return value, nil
}

func marshalCanonicalJSON(value interface{}) ([]byte, error) {
	var buffer bytes.Buffer
	if err := writeCanonicalJSON(&buffer, value); err != nil {
		return nil, err
	}
	return buffer.Bytes(), nil
}

var canonicalNumberRe = regexp.MustCompile(`^(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?$`)

// normalizeJSONNumber defines Frank Yomik's cross-runtime number spelling.
// Equivalent exponent and trailing-zero forms collapse to one representation.
func normalizeJSONNumber(raw string) (string, error) {
	parts := canonicalNumberRe.FindStringSubmatch(raw)
	if parts == nil {
		return "", fmt.Errorf("invalid JSON number: %s", raw)
	}
	sign, integer, fraction := parts[1], parts[2], parts[3]
	digits := strings.TrimLeft(integer+fraction, "0")
	if digits == "" {
		return "0", nil
	}
	exponent := 0
	if parts[4] != "" {
		parsed, err := strconv.Atoi(parts[4])
		if err != nil {
			return "", fmt.Errorf("invalid JSON number exponent: %s", raw)
		}
		exponent = parsed
	}
	exponent -= len(fraction)
	for strings.HasSuffix(digits, "0") {
		digits = strings.TrimSuffix(digits, "0")
		exponent++
	}
	point := len(digits) + exponent
	var rendered string
	switch {
	case point > 21 || point <= -6:
		coefficient := digits[:1]
		if len(digits) > 1 {
			coefficient += "." + digits[1:]
		}
		rendered = coefficient + "e" + strconv.Itoa(point-1)
	case point <= 0:
		rendered = "0." + strings.Repeat("0", -point) + digits
	case point >= len(digits):
		rendered = digits + strings.Repeat("0", point-len(digits))
	default:
		rendered = digits[:point] + "." + digits[point:]
	}
	return sign + rendered, nil
}

func writeCanonicalJSONString(buffer *bytes.Buffer, value string) error {
	var encoded bytes.Buffer
	encoder := json.NewEncoder(&encoded)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return err
	}
	raw := bytes.TrimSuffix(encoded.Bytes(), []byte("\n"))
	raw = bytes.ReplaceAll(raw, []byte(`\u2028`), []byte("\u2028"))
	raw = bytes.ReplaceAll(raw, []byte(`\u2029`), []byte("\u2029"))
	buffer.Write(raw)
	return nil
}

func writeCanonicalJSON(buffer *bytes.Buffer, value interface{}) error {
	switch typed := value.(type) {
	case nil:
		buffer.WriteString("null")
	case bool:
		if typed {
			buffer.WriteString("true")
		} else {
			buffer.WriteString("false")
		}
	case string:
		return writeCanonicalJSONString(buffer, typed)
	case json.Number:
		raw := string(typed)
		// Schema decimals are represented as binary64 by Python dataclasses.
		// Round decimal/exponent spellings through the same value domain.
		if strings.ContainsAny(raw, ".eE") {
			parsed, err := strconv.ParseFloat(raw, 64)
			if err != nil {
				return err
			}
			raw = strconv.FormatFloat(parsed, 'g', -1, 64)
		}
		normalized, err := normalizeJSONNumber(raw)
		if err != nil {
			return err
		}
		buffer.WriteString(normalized)
	case float64:
		if typed != typed || typed > 1.7976931348623157e308 || typed < -1.7976931348623157e308 {
			return errors.New("JSON numbers must be finite")
		}
		normalized, err := normalizeJSONNumber(strconv.FormatFloat(typed, 'g', -1, 64))
		if err != nil {
			return err
		}
		buffer.WriteString(normalized)
	case int:
		buffer.WriteString(strconv.Itoa(typed))
	case int64:
		buffer.WriteString(strconv.FormatInt(typed, 10))
	case []interface{}:
		buffer.WriteByte('[')
		for index, item := range typed {
			if index > 0 {
				buffer.WriteByte(',')
			}
			if err := writeCanonicalJSON(buffer, item); err != nil {
				return err
			}
		}
		buffer.WriteByte(']')
	case map[string]interface{}:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		buffer.WriteByte('{')
		for index, key := range keys {
			if index > 0 {
				buffer.WriteByte(',')
			}
			if err := writeCanonicalJSONString(buffer, key); err != nil {
				return err
			}
			buffer.WriteByte(':')
			if err := writeCanonicalJSON(buffer, typed[key]); err != nil {
				return err
			}
		}
		buffer.WriteByte('}')
	default:
		return fmt.Errorf("unsupported canonical JSON value %T", value)
	}
	return nil
}

func (c *Cache) canonicalMetadata(raw []byte, pipeline, sourceHash string) ([]byte, error) {
	var payload map[string]interface{}
	if len(raw) == 0 {
		payload = make(map[string]interface{})
	} else {
		decoded, err := decodeJSONValue(raw)
		if err != nil {
			return nil, invalidMetadata("invalid JSON: %v", err)
		}
		var ok bool
		payload, ok = decoded.(map[string]interface{})
		if !ok {
			return nil, invalidMetadata("metadata payload must be an object")
		}
	}
	schemaVersion := 1
	if value, ok := payload["schema_version"]; ok {
		number, err := requireJSONInt(value, "metadata schema version")
		if err != nil {
			return nil, err
		}
		schemaVersion = int(number)
	}
	if schemaVersion != 1 && schemaVersion != 2 {
		return nil, invalidMetadata("unsupported metadata schema version: %d", schemaVersion)
	}
	for key, expected := range map[string]string{
		"pipeline": pipeline, "source_hash": sourceHash,
	} {
		if value, exists := payload[key]; exists && value != nil && value != "" {
			actual, ok := value.(string)
			if !ok || actual != expected {
				return nil, invalidMetadata("metadata %s mismatch", key)
			}
		}
	}
	revision, hasRevision := payload["pipeline_revision"]
	if hasRevision && revision != nil && revision != "" {
		actual, ok := revision.(string)
		if !ok || actual != c.pipelineRevision {
			return nil, invalidMetadata("metadata pipeline revision mismatch")
		}
	} else if schemaVersion == 2 {
		return nil, invalidMetadata("metadata pipeline revision missing")
	}
	if schemaVersion == 2 {
		if err := validatePageDocument(payload, pipeline, c.pipelineRevision, sourceHash); err != nil {
			return nil, err
		}
	}
	return marshalCanonicalJSON(payload)
}

func renderHash(contentHash, imageObjectSHA string) string {
	return hashHex([]byte("v2|" + contentHash + "|" + imageObjectSHA))
}

func (c *Cache) v2Root() string {
	return filepath.Join(c.dir, "v2")
}

func (c *Cache) objectPath(sha string) string {
	return filepath.Join(c.v2Root(), "objects", sha[:2], sha[2:4], sha)
}

func (c *Cache) manifestByHashPath(pipeline, sourceHash string) string {
	pipeline, ok := safePathComponent(pipeline)
	if !ok || !isValidSHA256Hex(sourceHash) {
		return ""
	}
	return filepath.Join(c.v2Root(), "pages", "by-hash", pipeline, sourceHash, "manifest.json")
}

func (c *Cache) refPath(pipeline, title, chapter, page string) string {
	if pipeline == "" || title == "" || chapter == "" || page == "" {
		return ""
	}
	pipeline, ok := safePathComponent(pipeline)
	if !ok {
		return ""
	}
	titleSlug, ok := safePathComponent(slugify(title))
	if !ok {
		return ""
	}
	chapter, ok = safePathComponent(chapter)
	if !ok {
		return ""
	}
	page, ok = safePathComponent(page)
	if !ok {
		return ""
	}
	return filepath.Join(c.v2Root(), "pages", "by-ref", pipeline, titleSlug, chapter, page+".json")
}

func (c *Cache) legacyImagePath(pipeline, title, chapter, page string) string {
	if pipeline == "" || title == "" || chapter == "" || page == "" {
		return ""
	}
	pipeline, ok := safePathComponent(pipeline)
	if !ok {
		return ""
	}
	titleSlug, ok := safePathComponent(slugify(title))
	if !ok {
		return ""
	}
	chapter, ok = safePathComponent(chapter)
	if !ok {
		return ""
	}
	page, ok = safePathComponent(page)
	if !ok {
		return ""
	}
	return filepath.Join(c.dir, pipeline, titleSlug, chapter, page+".png")
}

func writeFileAtomic(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp := filepath.Join(dir, fmt.Sprintf(".%s.tmp-%d", filepath.Base(path), time.Now().UnixNano()))
	if err := os.WriteFile(tmp, data, perm); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}

// StoreObject writes data to the content-addressed object store.
// It is idempotent: if the object already exists and is valid, it returns immediately.
func (c *Cache) StoreObject(data []byte) (sha string, size int, err error) {
	sha = hashHex(data)
	size = len(data)
	if !isValidSHA256Hex(sha) {
		return "", 0, fmt.Errorf("invalid object hash")
	}
	path := c.objectPath(sha)
	if existing, err := os.ReadFile(path); err == nil {
		if hashHex(existing) != sha {
			return "", 0, fmt.Errorf("existing object corrupted: %s", sha)
		}
		return sha, size, nil
	}
	if err := writeFileAtomic(path, data, 0o644); err != nil {
		return "", 0, err
	}
	return sha, size, nil
}

func (c *Cache) readObjectVerified(sha string) ([]byte, error) {
	if !isValidSHA256Hex(sha) {
		return nil, fmt.Errorf("invalid sha: %q", sha)
	}
	data, err := os.ReadFile(c.objectPath(sha))
	if err != nil {
		return nil, err
	}
	if hashHex(data) != sha {
		return nil, fmt.Errorf("object hash mismatch: %s", sha)
	}
	return data, nil
}

func (c *Cache) loadManifestByHash(pipeline, sourceHash string) (*CacheManifest, error) {
	path := c.manifestByHashPath(pipeline, sourceHash)
	if path == "" {
		return nil, os.ErrNotExist
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var m CacheManifest
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, err
	}
	if (m.SchemaVersion != 2 && m.SchemaVersion != 3 &&
		m.SchemaVersion != cacheManifestSchemaVersion) ||
		m.Pipeline != pipeline || m.SourceHash != sourceHash {
		return nil, fmt.Errorf("invalid manifest: %s", path)
	}
	manifestRevision := m.PipelineRevision
	if m.SchemaVersion == 2 {
		if manifestRevision != "" {
			return nil, fmt.Errorf("invalid legacy manifest pipeline revision: %s", path)
		}
		manifestRevision = legacyPipelineRevision
	} else if manifestRevision == "" {
		return nil, fmt.Errorf("missing manifest pipeline revision: %s", path)
	}
	if !pipelineRevisionRe.MatchString(manifestRevision) {
		return nil, fmt.Errorf("invalid manifest pipeline revision: %s", path)
	}
	if manifestRevision != c.pipelineRevision {
		return nil, errPipelineRevisionMismatch
	}
	m.PipelineRevision = manifestRevision
	return &m, nil
}

func (c *Cache) writeManifestByHash(m *CacheManifest) error {
	if m == nil {
		return errors.New("nil manifest")
	}
	m.SchemaVersion = cacheManifestSchemaVersion
	m.PipelineRevision = c.pipelineRevision
	if m.Pipeline == "" || !isValidSHA256Hex(m.SourceHash) {
		return errors.New("invalid manifest key")
	}
	if _, ok := safePathComponent(m.Pipeline); !ok {
		return errors.New("invalid manifest pipeline")
	}
	if !isValidSHA256Hex(m.SourceObjectSHA256) || !isValidSHA256Hex(m.ImageObjectSHA256) ||
		!isValidSHA256Hex(m.MetadataObjectSHA256) {
		return errors.New("invalid manifest objects")
	}
	if m.ContentHash == "" {
		return errors.New("missing content hash")
	}
	if m.RenderHash == "" {
		m.RenderHash = renderHash(m.ContentHash, m.ImageObjectSHA256)
	}
	now := time.Now().UnixMilli()
	if m.CreatedAtUnixMs == 0 {
		m.CreatedAtUnixMs = now
	}
	m.UpdatedAtUnixMs = now

	data, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	path := c.manifestByHashPath(m.Pipeline, m.SourceHash)
	if path == "" {
		return errors.New("invalid manifest path")
	}
	return writeFileAtomic(path, data, 0o644)
}

func (c *Cache) writeRef(pipeline, title, chapter, page, sourceHash string) error {
	path := c.refPath(pipeline, title, chapter, page)
	if path == "" || !isValidSHA256Hex(sourceHash) {
		return nil
	}
	r := cacheRef{
		SchemaVersion: 2,
		Pipeline:      pipeline,
		SourceHash:    sourceHash,
		TitleSlug:     slugify(title),
		Chapter:       chapter,
		Page:          page,
		UpdatedAt:     time.Now().UnixMilli(),
	}
	data, err := json.MarshalIndent(&r, "", "  ")
	if err != nil {
		return err
	}
	return writeFileAtomic(path, data, 0o644)
}

func (c *Cache) resolveRef(pipeline, title, chapter, page string) (string, bool) {
	path := c.refPath(pipeline, title, chapter, page)
	if path == "" {
		return "", false
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return "", false
	}
	var r cacheRef
	if err := json.Unmarshal(data, &r); err != nil {
		return "", false
	}
	if r.Pipeline != pipeline || !isValidSHA256Hex(r.SourceHash) {
		return "", false
	}
	return r.SourceHash, true
}

// StoreBySourceHash writes a complete cache v2 entry.
func (c *Cache) StoreBySourceHash(pipeline, sourceHash string, sourceImageBytes, imageBytes, metadataJSON []byte,
	title, chapter, page string) (*CacheManifest, error) {
	if pipeline == "" {
		return nil, errors.New("missing pipeline")
	}
	if !isValidSHA256Hex(sourceHash) {
		return nil, errors.New("invalid source hash")
	}
	if len(sourceImageBytes) == 0 || len(imageBytes) == 0 {
		return nil, errors.New("empty image bytes")
	}
	if hashHex(sourceImageBytes) != sourceHash {
		return nil, errors.New("source hash mismatch")
	}

	documentPipeline := pipeline
	if basePipeline, _, ok := splitCachePipeline(pipeline); ok {
		documentPipeline = basePipeline
	}
	canonMeta, err := c.canonicalMetadata(metadataJSON, documentPipeline, sourceHash)
	if err != nil {
		return nil, fmt.Errorf("canonical metadata: %w", err)
	}
	srcObj, srcSize, err := c.StoreObject(sourceImageBytes)
	if err != nil {
		return nil, err
	}
	imgObj, imgSize, err := c.StoreObject(imageBytes)
	if err != nil {
		return nil, err
	}
	metaObj, metaSize, err := c.StoreObject(canonMeta)
	if err != nil {
		return nil, err
	}
	contentHash := hashHex(canonMeta)

	manifest := &CacheManifest{
		SchemaVersion:        cacheManifestSchemaVersion,
		Pipeline:             pipeline,
		PipelineRevision:     c.pipelineRevision,
		SourceHash:           sourceHash,
		TitleSlug:            slugify(title),
		Chapter:              chapter,
		Page:                 page,
		SourceObjectSHA256:   srcObj,
		SourceBytes:          srcSize,
		ImageObjectSHA256:    imgObj,
		ImageBytes:           imgSize,
		MetadataObjectSHA256: metaObj,
		MetadataBytes:        metaSize,
		ContentHash:          contentHash,
		RenderHash:           renderHash(contentHash, imgObj),
		ImageStale:           false,
	}
	if err := c.writeManifestByHash(manifest); err != nil {
		return nil, err
	}
	if err := c.writeRef(pipeline, title, chapter, page, sourceHash); err != nil {
		return nil, err
	}
	return manifest, nil
}

// LookupBySourceHash returns translated image bytes and manifest if present/valid.
func (c *Cache) LookupBySourceHash(pipeline, sourceHash string) ([]byte, *CacheManifest, bool) {
	m, err := c.loadManifestByHash(pipeline, sourceHash)
	if err != nil {
		return nil, nil, false
	}
	img, err := c.readObjectVerified(m.ImageObjectSHA256)
	if err != nil {
		return nil, nil, false
	}
	if m.ImageStale {
		return nil, m, false
	}
	return img, m, true
}

// LookupMetadataBySourceHash returns metadata JSON and manifest.
func (c *Cache) LookupMetadataBySourceHash(pipeline, sourceHash string) ([]byte, *CacheManifest, bool) {
	m, err := c.loadManifestByHash(pipeline, sourceHash)
	if err != nil {
		return nil, nil, false
	}
	meta, err := c.readObjectVerified(m.MetadataObjectSHA256)
	if err != nil {
		return nil, nil, false
	}
	return meta, m, true
}

// LookupSourceBySourceHash returns original source image bytes and manifest.
func (c *Cache) LookupSourceBySourceHash(pipeline, sourceHash string) ([]byte, *CacheManifest, bool) {
	m, err := c.loadManifestByHash(pipeline, sourceHash)
	if err != nil {
		return nil, nil, false
	}
	src, err := c.readObjectVerified(m.SourceObjectSHA256)
	if err != nil {
		return nil, nil, false
	}
	return src, m, true
}

// UpdateMetadataBySourceHash updates metadata object and marks image stale
// until a rerender job stores a fresh rendered image.
func (c *Cache) UpdateMetadataBySourceHash(pipeline, sourceHash string, metadataJSON []byte,
	baseContentHash string) (*CacheManifest, error) {
	m, err := c.loadManifestByHash(pipeline, sourceHash)
	if err != nil {
		return nil, err
	}
	if baseContentHash != "" && baseContentHash != m.ContentHash {
		return nil, fmt.Errorf("content hash mismatch")
	}
	documentPipeline := pipeline
	if basePipeline, _, ok := splitCachePipeline(pipeline); ok {
		documentPipeline = basePipeline
	}
	canonMeta, err := c.canonicalMetadata(metadataJSON, documentPipeline, sourceHash)
	if err != nil {
		return nil, err
	}
	referenced, err := referencedArtifactHashes(canonMeta)
	if err != nil {
		return nil, err
	}
	permitted := make(map[string]bool, len(m.ArtifactObjectSHA256s))
	for _, digest := range m.ArtifactObjectSHA256s {
		permitted[digest] = true
	}
	for digest := range referenced {
		if !permitted[digest] {
			return nil, invalidMetadata("metadata introduces unknown artifact: %s", digest)
		}
		if _, err := c.readObjectVerified(digest); err != nil {
			return nil, invalidMetadata("referenced artifact unavailable: %s", digest)
		}
	}
	metaObj, metaSize, err := c.StoreObject(canonMeta)
	if err != nil {
		return nil, err
	}
	m.MetadataObjectSHA256 = metaObj
	m.MetadataBytes = metaSize
	m.ContentHash = hashHex(canonMeta)
	m.ImageStale = true
	m.RenderHash = renderHash(m.ContentHash, m.ImageObjectSHA256)
	if err := c.writeManifestByHash(m); err != nil {
		return nil, err
	}
	return m, nil
}

func referencedArtifactHashes(metadataJSON []byte) (map[string]bool, error) {
	value, err := decodeJSONValue(metadataJSON)
	if err != nil {
		return nil, err
	}
	hashes := make(map[string]bool)
	var visit func(interface{}) error
	visit = func(raw interface{}) error {
		switch typed := raw.(type) {
		case map[string]interface{}:
			if reference, ok := typed["reference"].(string); ok && strings.HasPrefix(reference, "objects/") {
				digest, ok := typed["sha256"].(string)
				if !ok || reference != "objects/"+digest || !isValidSHA256Hex(digest) {
					return invalidMetadata("invalid content-addressed artifact reference")
				}
				hashes[digest] = true
			}
			for _, child := range typed {
				if err := visit(child); err != nil {
					return err
				}
			}
		case []interface{}:
			for _, child := range typed {
				if err := visit(child); err != nil {
					return err
				}
			}
		}
		return nil
	}
	return hashes, visit(value)
}

// ResolveSourceHash resolves a metadata key to source hash.
func (c *Cache) ResolveSourceHash(pipeline, title, chapter, page string) (string, bool) {
	if sourceHash, ok := c.resolveRef(pipeline, title, chapter, page); ok {
		return sourceHash, true
	}
	return "", false
}

// LinkRef creates/updates a by-ref pointer to an existing source hash.
func (c *Cache) LinkRef(pipeline, title, chapter, page, sourceHash string) error {
	return c.writeRef(pipeline, title, chapter, page, sourceHash)
}

// GetManifestBySourceHash returns manifest without loading objects.
func (c *Cache) GetManifestBySourceHash(pipeline, sourceHash string) (*CacheManifest, bool) {
	m, err := c.loadManifestByHash(pipeline, sourceHash)
	if err != nil {
		return nil, false
	}
	return m, true
}

// path returns the legacy filesystem path for cached image.
// This is retained for backward compatibility.
func (c *Cache) path(pipeline, title, chapter, page string) string {
	return c.legacyImagePath(pipeline, title, chapter, page)
}

// Lookup checks if a cached image exists and returns its bytes.
// It prefers v2 by-ref manifests and falls back to legacy image files.
func (c *Cache) Lookup(pipeline, title, chapter, page string) ([]byte, bool) {
	if sourceHash, ok := c.resolveRef(pipeline, title, chapter, page); ok {
		img, _, ok2 := c.LookupBySourceHash(pipeline, sourceHash)
		if ok2 {
			return img, true
		}
	}
	p := c.path(pipeline, title, chapter, page)
	if p == "" {
		return nil, false
	}
	data, err := os.ReadFile(p)
	if err != nil {
		return nil, false
	}
	return data, true
}

// Store saves image bytes to the filesystem cache.
// Compatibility method used by legacy tests; writes v2 manifest with empty metadata
// and also writes the legacy image path.
func (c *Cache) Store(pipeline, title, chapter, page string, imageBytes []byte) error {
	if pipeline == "" || len(imageBytes) == 0 {
		return fmt.Errorf("missing cache key fields")
	}
	sourceHash := hashHex(imageBytes)
	_, err := c.StoreBySourceHash(pipeline, sourceHash, imageBytes, imageBytes, []byte("{}"),
		title, chapter, page)
	if err != nil {
		return err
	}
	// Write legacy file for compatibility with existing tests/tooling.
	p := c.path(pipeline, title, chapter, page)
	if p != "" {
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			return fmt.Errorf("creating cache dir: %w", err)
		}
		if err := writeFileAtomic(p, imageBytes, 0o644); err != nil {
			return err
		}
	}
	return nil
}

// ImagePath returns the legacy filesystem image path if it exists.
func (c *Cache) ImagePath(pipeline, title, chapter, page string) (string, bool) {
	p := c.path(pipeline, title, chapter, page)
	if p == "" {
		return "", false
	}
	if _, err := os.Stat(p); err != nil {
		return "", false
	}
	return p, true
}

// CleanupCorruptObject removes an object file when hash validation fails.
func (c *Cache) CleanupCorruptObject(sha string) {
	if !isValidSHA256Hex(sha) {
		return
	}
	_ = os.Remove(c.objectPath(sha))
}

// HasLegacyImage reports whether legacy image-only cache file exists.
func (c *Cache) HasLegacyImage(pipeline, title, chapter, page string) bool {
	p := c.path(pipeline, title, chapter, page)
	if p == "" {
		return false
	}
	_, err := os.Stat(p)
	return err == nil
}

// RemoveLegacyImage removes legacy cache image if present.
func (c *Cache) RemoveLegacyImage(pipeline, title, chapter, page string) error {
	p := c.path(pipeline, title, chapter, page)
	if p == "" {
		return nil
	}
	if err := os.Remove(p); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// EnsureV2Dirs creates base cache directories.
func (c *Cache) EnsureV2Dirs() error {
	dirs := []string{
		filepath.Join(c.v2Root(), "objects"),
		filepath.Join(c.v2Root(), "pages", "by-hash"),
		filepath.Join(c.v2Root(), "pages", "by-ref"),
	}
	for _, d := range dirs {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return err
		}
	}
	return nil
}

// WalkManifests iterates all v2 manifests.
func (c *Cache) WalkManifests(fn func(path string) error) error {
	root := filepath.Join(c.v2Root(), "pages", "by-hash")
	return filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			return nil
		}
		if info.Name() != "manifest.json" {
			return nil
		}
		return fn(path)
	})
}
