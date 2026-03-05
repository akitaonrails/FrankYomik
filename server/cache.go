package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
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
	dir string
}

// CacheManifest is the authoritative per-page record in cache v2.
type CacheManifest struct {
	SchemaVersion int    `json:"schema_version"`
	Pipeline      string `json:"pipeline"`
	SourceHash    string `json:"source_hash"`

	TitleSlug string `json:"title_slug,omitempty"`
	Chapter   string `json:"chapter,omitempty"`
	Page      string `json:"page,omitempty"`

	SourceObjectSHA256 string `json:"source_object_sha256"`
	SourceBytes        int    `json:"source_bytes"`

	ImageObjectSHA256 string `json:"image_object_sha256"`
	ImageBytes        int    `json:"image_bytes"`

	MetadataObjectSHA256 string `json:"metadata_object_sha256"`
	MetadataBytes        int    `json:"metadata_bytes"`

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
	return &Cache{dir: dir}
}

var slugRe = regexp.MustCompile(`[^a-z0-9\-]`)

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
	var v interface{}
	if len(raw) == 0 {
		return []byte("{}"), nil
	}
	if err := json.Unmarshal(raw, &v); err != nil {
		return nil, err
	}
	out, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	return out, nil
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
	return filepath.Join(c.v2Root(), "pages", "by-hash", pipeline, sourceHash, "manifest.json")
}

func (c *Cache) refPath(pipeline, title, chapter, page string) string {
	if pipeline == "" || title == "" || chapter == "" || page == "" {
		return ""
	}
	return filepath.Join(c.v2Root(), "pages", "by-ref", pipeline, slugify(title), chapter, page+".json")
}

func (c *Cache) legacyImagePath(pipeline, title, chapter, page string) string {
	if pipeline == "" || title == "" || chapter == "" || page == "" {
		return ""
	}
	return filepath.Join(c.dir, pipeline, slugify(title), chapter, page+".png")
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
	if pipeline == "" || !isValidSHA256Hex(sourceHash) {
		return nil, os.ErrNotExist
	}
	path := c.manifestByHashPath(pipeline, sourceHash)
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var m CacheManifest
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, err
	}
	if m.SchemaVersion < 2 || m.Pipeline == "" || m.SourceHash == "" {
		return nil, fmt.Errorf("invalid manifest: %s", path)
	}
	return &m, nil
}

func (c *Cache) writeManifestByHash(m *CacheManifest) error {
	if m == nil {
		return errors.New("nil manifest")
	}
	m.SchemaVersion = 2
	if m.Pipeline == "" || !isValidSHA256Hex(m.SourceHash) {
		return errors.New("invalid manifest key")
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
	return writeFileAtomic(c.manifestByHashPath(m.Pipeline, m.SourceHash), data, 0o644)
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

	canonMeta, err := canonicalJSON(metadataJSON)
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
		SchemaVersion:        2,
		Pipeline:             pipeline,
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
	canonMeta, err := canonicalJSON(metadataJSON)
	if err != nil {
		return nil, err
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
