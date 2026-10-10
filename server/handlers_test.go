package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"io"
	"io/fs"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func newTestServer(t *testing.T) (*Server, *redis.Client) {
	t.Helper()

	opt, err := redis.ParseURL("redis://localhost:6379/15") // Use DB 15 for tests
	if err != nil {
		t.Fatalf("parse redis url: %v", err)
	}
	rdb := redis.NewClient(opt)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	if err := rdb.Ping(ctx).Err(); err != nil {
		t.Skipf("Redis not available: %v", err)
	}

	// Clean test DB
	rdb.FlushDB(ctx)

	t.Cleanup(func() {
		rdb.FlushDB(context.Background())
		rdb.Close()
	})

	cacheDir := t.TempDir()
	return NewServer(rdb, cacheDir), rdb
}

func makePNGBytes() []byte {
	img := image.NewRGBA(image.Rect(0, 0, 100, 80))
	for x := range 100 {
		for y := range 80 {
			img.Set(x, y, color.White)
		}
	}
	var buf bytes.Buffer
	png.Encode(&buf, img)
	return buf.Bytes()
}

func makeJobRequest(t *testing.T, pipeline, priority string, imgBytes []byte) (*http.Request, *httptest.ResponseRecorder) {
	t.Helper()
	fields := map[string]string{}
	if pipeline != "" {
		fields["pipeline"] = pipeline
	}
	if priority != "" {
		fields["priority"] = priority
	}
	return makeJobRequestWithFields(t, fields, imgBytes)
}

func makeJobRequestWithFields(t *testing.T, fields map[string]string, imgBytes []byte) (*http.Request, *httptest.ResponseRecorder) {
	t.Helper()
	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	for key, value := range fields {
		writer.WriteField(key, value)
	}
	if imgBytes != nil {
		part, _ := writer.CreateFormFile("image", "test.png")
		part.Write(imgBytes)
	}
	writer.Close()

	req := httptest.NewRequest("POST", "/api/v1/jobs", body)
	req.Header.Set("Content-Type", writer.FormDataContentType())
	return req, httptest.NewRecorder()
}

// ==================================
// Auth Middleware Tests
// ==================================

func TestAuthMiddleware(t *testing.T) {
	handler := AuthMiddleware("secret-token", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	tests := []struct {
		name   string
		path   string
		auth   string
		status int
	}{
		{"health is public", "/api/v1/health", "", http.StatusOK},
		{"missing auth", "/api/v1/jobs", "", http.StatusUnauthorized},
		{"wrong token", "/api/v1/jobs", "Bearer wrong", http.StatusUnauthorized},
		{"valid token", "/api/v1/jobs", "Bearer secret-token", http.StatusOK},
		{"basic auth rejected", "/api/v1/jobs", "Basic dXNlcjpwYXNz", http.StatusUnauthorized},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			req := httptest.NewRequest("GET", tt.path, nil)
			if tt.auth != "" {
				req.Header.Set("Authorization", tt.auth)
			}
			w := httptest.NewRecorder()
			handler.ServeHTTP(w, req)
			if w.Code != tt.status {
				t.Errorf("got %d, want %d", w.Code, tt.status)
			}
		})
	}
}

func TestAuthMiddlewareQueryParam(t *testing.T) {
	handler := AuthMiddleware("ws-token", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	t.Run("valid query param token", func(t *testing.T) {
		req := httptest.NewRequest("GET", "/api/v1/ws?token=ws-token", nil)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Errorf("got %d, want 200", w.Code)
		}
	})

	t.Run("wrong query param token", func(t *testing.T) {
		req := httptest.NewRequest("GET", "/api/v1/ws?token=wrong", nil)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		if w.Code != http.StatusUnauthorized {
			t.Errorf("got %d, want 401", w.Code)
		}
	})

	t.Run("header takes precedence over query param", func(t *testing.T) {
		req := httptest.NewRequest("GET", "/api/v1/ws?token=ignored", nil)
		req.Header.Set("Authorization", "Bearer ws-token")
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Errorf("got %d, want 200", w.Code)
		}
	})
}

func TestAuthMiddlewareErrorFormat(t *testing.T) {
	handler := AuthMiddleware("tok", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))

	req := httptest.NewRequest("GET", "/api/v1/jobs", nil)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)

	body := w.Body.String()
	if !strings.Contains(body, "missing authorization") {
		t.Errorf("expected JSON error message, got: %s", body)
	}
}

func TestCacheRejectsUnsafePathComponents(t *testing.T) {
	root := t.TempDir()
	cache := NewCache(root)
	hash := strings.Repeat("a", 64)

	if got := cache.refPath("manga_translate", "One Piece", "../../etc", "003"); got != "" {
		t.Fatalf("refPath should reject unsafe chapter, got %q", got)
	}
	if got := cache.legacyImagePath("manga_translate", "One Piece", "1084", "../003"); got != "" {
		t.Fatalf("legacyImagePath should reject unsafe page, got %q", got)
	}
	if got := cache.manifestByHashPath("../escape", hash); got != "" {
		t.Fatalf("manifestByHashPath should reject unsafe pipeline, got %q", got)
	}

	if err := cache.LinkRef("manga_translate", "One Piece", "../../etc", "003", hash); err != nil {
		t.Fatalf("LinkRef should fail closed without writing, got %v", err)
	}

	var found bool
	_ = filepath.Walk(root, func(path string, info fs.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if path != root {
			found = true
		}
		return nil
	})
	if found {
		t.Fatal("unexpected write outside cache path guard")
	}
}

// ==================================
// POST /api/v1/jobs Tests
// ==================================

func TestCreateJobValidation(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	t.Run("missing pipeline", func(t *testing.T) {
		req, w := makeJobRequest(t, "", "", makePNGBytes())
		mux.ServeHTTP(w, req)
		if w.Code != http.StatusBadRequest {
			t.Errorf("got %d, want 400", w.Code)
		}
		assertJSONError(t, w.Body, "invalid pipeline")
	})

	t.Run("invalid pipeline", func(t *testing.T) {
		req, w := makeJobRequest(t, "nonexistent", "", makePNGBytes())
		mux.ServeHTTP(w, req)
		if w.Code != http.StatusBadRequest {
			t.Errorf("got %d, want 400", w.Code)
		}
	})

	t.Run("invalid priority", func(t *testing.T) {
		req, w := makeJobRequest(t, "manga_translate", "urgent", makePNGBytes())
		mux.ServeHTTP(w, req)
		if w.Code != http.StatusBadRequest {
			t.Errorf("got %d, want 400", w.Code)
		}
		assertJSONError(t, w.Body, "invalid priority")
	})

	t.Run("missing image field", func(t *testing.T) {
		req, w := makeJobRequest(t, "manga_translate", "high", nil)
		mux.ServeHTTP(w, req)
		if w.Code != http.StatusBadRequest {
			t.Errorf("got %d, want 400", w.Code)
		}
	})

	t.Run("empty image", func(t *testing.T) {
		req, w := makeJobRequest(t, "manga_translate", "high", []byte{})
		mux.ServeHTTP(w, req)
		if w.Code != http.StatusBadRequest {
			t.Errorf("got %d, want 400", w.Code)
		}
	})
}

func TestCreateJobInvalidTargetLang(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	writer.WriteField("pipeline", "manga_translate")
	writer.WriteField("target_lang", "invalid")
	part, _ := writer.CreateFormFile("image", "test.png")
	part.Write(makePNGBytes())
	writer.Close()

	req := httptest.NewRequest("POST", "/api/v1/jobs", body)
	req.Header.Set("Content-Type", writer.FormDataContentType())
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("got %d, want 400", w.Code)
	}
	assertJSONError(t, w.Body, "invalid target_lang")
}

func TestCreateJobTargetLangForwarded(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	writer.WriteField("pipeline", "manga_translate")
	writer.WriteField("target_lang", "pt-br")
	part, _ := writer.CreateFormFile("image", "test.png")
	part.Write(makePNGBytes())
	writer.Close()

	req := httptest.NewRequest("POST", "/api/v1/jobs", body)
	req.Header.Set("Content-Type", writer.FormDataContentType())
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusCreated {
		t.Fatalf("got %d, want 201: %s", w.Code, w.Body.String())
	}

	// Verify target_lang was passed to the stream
	ctx := context.Background()
	msgs, err := rdb.XRange(ctx, streamHigh, "-", "+").Result()
	if err != nil {
		t.Fatalf("xrange: %v", err)
	}
	if len(msgs) == 0 {
		t.Fatal("expected at least one stream message")
	}
	last := msgs[len(msgs)-1]
	if last.Values["target_lang"] != "pt-br" {
		t.Errorf("target_lang: got %q, want 'pt-br'", last.Values["target_lang"])
	}
}

func TestCreateJobDefaultTargetLang(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	req, w := makeJobRequest(t, "manga_translate", "", makePNGBytes())
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusCreated {
		t.Fatalf("got %d, want 201", w.Code)
	}

	// When target_lang is "en" (default), it should NOT be in the stream
	ctx := context.Background()
	msgs, err := rdb.XRange(ctx, streamHigh, "-", "+").Result()
	if err != nil {
		t.Fatalf("xrange: %v", err)
	}
	if len(msgs) == 0 {
		t.Fatal("expected at least one stream message")
	}
	last := msgs[len(msgs)-1]
	if _, exists := last.Values["target_lang"]; exists {
		t.Errorf("target_lang should not be in stream for default 'en'")
	}
}

func TestCreateJobSuccess(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	t.Run("manga_translate high priority", func(t *testing.T) {
		req, w := makeJobRequest(t, "manga_translate", "high", makePNGBytes())
		mux.ServeHTTP(w, req)

		if w.Code != http.StatusCreated {
			body, _ := io.ReadAll(w.Body)
			t.Fatalf("got %d, want 201: %s", w.Code, string(body))
		}

		var resp JobResponse
		json.NewDecoder(w.Body).Decode(&resp)
		if resp.JobID == "" {
			t.Error("expected non-empty job_id")
		}
		if resp.Status != "queued" {
			t.Errorf("got status %q, want 'queued'", resp.Status)
		}
		if resp.DedupHit {
			t.Error("first submission should not be dedup_hit")
		}
	})

	t.Run("manga_furigana pipeline", func(t *testing.T) {
		req, w := makeJobRequest(t, "manga_furigana", "", makePNGBytes())
		mux.ServeHTTP(w, req)
		if w.Code != http.StatusCreated {
			t.Errorf("got %d, want 201", w.Code)
		}
	})

	t.Run("webtoon pipeline", func(t *testing.T) {
		req, w := makeJobRequest(t, "webtoon", "low", makePNGBytes())
		mux.ServeHTTP(w, req)
		if w.Code != http.StatusCreated {
			t.Errorf("got %d, want 201", w.Code)
		}
	})

	t.Run("default priority is high", func(t *testing.T) {
		req, w := makeJobRequest(t, "manga_translate", "", makePNGBytes())
		mux.ServeHTTP(w, req)
		if w.Code != http.StatusCreated {
			t.Errorf("got %d, want 201", w.Code)
		}
	})
}

func TestCreateJobKindleLatestMarker(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)
	ctx := context.Background()

	fields := map[string]string{
		"pipeline":     "manga_translate",
		"priority":     "high",
		"source_site":  "kindle",
		"latest_group": "kindle:B000000001:session-a",
		"latest_token": "kindle-session-a-1",
		"latest_seq":   "1",
	}
	req, w := makeJobRequestWithFields(t, fields, append(makePNGBytes(), byte(1)))
	mux.ServeHTTP(w, req)
	if w.Code != http.StatusCreated {
		body, _ := io.ReadAll(w.Body)
		t.Fatalf("got %d, want 201: %s", w.Code, string(body))
	}

	msgs, err := rdb.XRange(ctx, streamHigh, "-", "+").Result()
	if err != nil {
		t.Fatalf("xrange: %v", err)
	}
	if len(msgs) != 1 {
		t.Fatalf("got %d stream messages, want 1", len(msgs))
	}
	values := msgs[0].Values
	if values["source_site"] != "kindle" {
		t.Fatalf("source_site = %v, want kindle", values["source_site"])
	}
	if values["latest_group"] != fields["latest_group"] {
		t.Fatalf("latest_group = %v, want %s", values["latest_group"], fields["latest_group"])
	}
	if values["latest_token"] != fields["latest_token"] {
		t.Fatalf("latest_token = %v, want %s", values["latest_token"], fields["latest_token"])
	}
	if values["latest_seq"] != fields["latest_seq"] {
		t.Fatalf("latest_seq = %v, want %s", values["latest_seq"], fields["latest_seq"])
	}
	latestKey, ok := values["latest_key"].(string)
	if !ok || latestKey == "" {
		t.Fatalf("latest_key missing from stream values: %#v", values["latest_key"])
	}
	if latestKey != latestKeyForGroup(fields["latest_group"]) {
		t.Fatalf("latest_key = %s, want %s", latestKey, latestKeyForGroup(fields["latest_group"]))
	}
	stored, err := rdb.Get(ctx, latestKey).Result()
	if err != nil {
		t.Fatalf("get latest key: %v", err)
	}
	if latestTokenFromStoredValue(stored) != fields["latest_token"] {
		t.Fatalf("latest marker = %s, want %s", stored, fields["latest_token"])
	}

	fields["latest_token"] = "kindle-session-a-2"
	fields["latest_seq"] = "2"
	req2, w2 := makeJobRequestWithFields(t, fields, append(makePNGBytes(), byte(2)))
	mux.ServeHTTP(w2, req2)
	if w2.Code != http.StatusCreated {
		body, _ := io.ReadAll(w2.Body)
		t.Fatalf("second got %d, want 201: %s", w2.Code, string(body))
	}
	stored, err = rdb.Get(ctx, latestKey).Result()
	if err != nil {
		t.Fatalf("get latest key after second submit: %v", err)
	}
	if latestTokenFromStoredValue(stored) != fields["latest_token"] {
		t.Fatalf("latest marker after second submit = %s, want %s", stored, fields["latest_token"])
	}

	fields["latest_token"] = "kindle-session-a-older"
	fields["latest_seq"] = "1"
	req3, w3 := makeJobRequestWithFields(t, fields, append(makePNGBytes(), byte(3)))
	mux.ServeHTTP(w3, req3)
	if w3.Code != http.StatusCreated {
		body, _ := io.ReadAll(w3.Body)
		t.Fatalf("third got %d, want 201: %s", w3.Code, string(body))
	}
	stored, err = rdb.Get(ctx, latestKey).Result()
	if err != nil {
		t.Fatalf("get latest key after older submit: %v", err)
	}
	if latestTokenFromStoredValue(stored) != "kindle-session-a-2" {
		t.Fatalf("older seq regressed latest marker to %q", stored)
	}
}

func TestCreateJobKindleLatestMarkerBypassesDedup(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)
	ctx := context.Background()
	img := append(makePNGBytes(), byte(9))

	fields := map[string]string{
		"pipeline":     "manga_translate",
		"priority":     "high",
		"source_site":  "kindle",
		"latest_group": "kindle:B000000001:session-a",
		"latest_token": "kindle-session-a-1",
		"latest_seq":   "1",
	}
	req1, w1 := makeJobRequestWithFields(t, fields, img)
	mux.ServeHTTP(w1, req1)
	if w1.Code != http.StatusCreated {
		body, _ := io.ReadAll(w1.Body)
		t.Fatalf("first got %d, want 201: %s", w1.Code, string(body))
	}
	var resp1 JobResponse
	if err := json.NewDecoder(w1.Body).Decode(&resp1); err != nil {
		t.Fatalf("decode first response: %v", err)
	}
	if resp1.DedupHit {
		t.Fatal("first latest submission should not be a dedup hit")
	}

	// Job IDs include a millisecond timestamp. Make the duplicate submit happen in
	// a later tick so this regression test checks dedup behavior, not clock
	// granularity.
	time.Sleep(2 * time.Millisecond)

	fields["latest_token"] = "kindle-session-a-2"
	fields["latest_seq"] = "2"
	req2, w2 := makeJobRequestWithFields(t, fields, img)
	mux.ServeHTTP(w2, req2)
	if w2.Code != http.StatusCreated {
		body, _ := io.ReadAll(w2.Body)
		t.Fatalf("second got %d, want 201: %s", w2.Code, string(body))
	}
	var resp2 JobResponse
	if err := json.NewDecoder(w2.Body).Decode(&resp2); err != nil {
		t.Fatalf("decode second response: %v", err)
	}
	if resp2.DedupHit {
		t.Fatal("duplicate Kindle latest submission should bypass dedup")
	}
	if resp2.JobID == resp1.JobID {
		t.Fatalf("duplicate Kindle latest submission reused job_id %s", resp2.JobID)
	}

	msgs, err := rdb.XRange(ctx, streamHigh, "-", "+").Result()
	if err != nil {
		t.Fatalf("xrange: %v", err)
	}
	if len(msgs) != 2 {
		t.Fatalf("got %d stream messages, want 2", len(msgs))
	}
	if msgs[0].Values["latest_token"] != "kindle-session-a-1" {
		t.Fatalf("first stream latest_token = %v", msgs[0].Values["latest_token"])
	}
	if msgs[1].Values["latest_token"] != "kindle-session-a-2" {
		t.Fatalf("second stream latest_token = %v", msgs[1].Values["latest_token"])
	}

	latestKey := latestKeyForGroup(fields["latest_group"])
	stored, err := rdb.Get(ctx, latestKey).Result()
	if err != nil {
		t.Fatalf("get latest key: %v", err)
	}
	if latestTokenFromStoredValue(stored) != "kindle-session-a-2" {
		t.Fatalf("latest marker = %s, want kindle-session-a-2", stored)
	}
}

func latestTokenFromStoredValue(value string) string {
	parts := strings.SplitN(value, "\n", 2)
	if len(parts) == 2 {
		return parts[1]
	}
	return value
}

func TestCreateJobWebtoonDoesNotWriteLatestMarker(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)
	ctx := context.Background()

	fields := map[string]string{
		"pipeline":     "webtoon",
		"priority":     "high",
		"source_site":  "webtoon",
		"latest_group": "webtoon:episode",
		"latest_token": "wt-1",
	}
	req, w := makeJobRequestWithFields(t, fields, makePNGBytes())
	mux.ServeHTTP(w, req)
	if w.Code != http.StatusCreated {
		body, _ := io.ReadAll(w.Body)
		t.Fatalf("got %d, want 201: %s", w.Code, string(body))
	}

	msgs, err := rdb.XRange(ctx, streamHigh, "-", "+").Result()
	if err != nil {
		t.Fatalf("xrange: %v", err)
	}
	if len(msgs) != 1 {
		t.Fatalf("got %d stream messages, want 1", len(msgs))
	}
	if _, exists := msgs[0].Values["latest_key"]; exists {
		t.Fatalf("webtoon job should not contain latest_key: %#v", msgs[0].Values)
	}
	if exists, err := rdb.Exists(ctx, latestKeyForGroup(fields["latest_group"])).Result(); err != nil || exists != 0 {
		t.Fatalf("webtoon latest marker exists=%d err=%v, want none", exists, err)
	}
}

func TestCreateJobDedup(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	imgBytes := makePNGBytes()

	// First submission
	req1, rec1 := makeJobRequest(t, "manga_translate", "", imgBytes)
	mux.ServeHTTP(rec1, req1)

	var resp1 JobResponse
	json.NewDecoder(rec1.Body).Decode(&resp1)

	// Second submission (same image)
	req2, rec2 := makeJobRequest(t, "manga_translate", "", imgBytes)
	mux.ServeHTTP(rec2, req2)

	var resp2 JobResponse
	json.NewDecoder(rec2.Body).Decode(&resp2)

	if resp2.JobID != resp1.JobID {
		t.Errorf("dedup should return same job_id: %s != %s", resp2.JobID, resp1.JobID)
	}
	if !resp2.DedupHit {
		t.Error("expected dedup_hit to be true")
	}
}

func TestDedupDifferentImages(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	img1 := makePNGBytes()
	// Create a different image
	img2Buf := image.NewRGBA(image.Rect(0, 0, 50, 50))
	for x := range 50 {
		for y := range 50 {
			img2Buf.Set(x, y, color.Black)
		}
	}
	var buf bytes.Buffer
	png.Encode(&buf, img2Buf)
	img2 := buf.Bytes()

	req1, rec1 := makeJobRequest(t, "manga_translate", "", img1)
	mux.ServeHTTP(rec1, req1)
	var resp1 JobResponse
	json.NewDecoder(rec1.Body).Decode(&resp1)

	req2, rec2 := makeJobRequest(t, "manga_translate", "", img2)
	mux.ServeHTTP(rec2, req2)
	var resp2 JobResponse
	json.NewDecoder(rec2.Body).Decode(&resp2)

	if resp1.JobID == resp2.JobID {
		t.Error("different images should get different job IDs")
	}
}

// ==================================
// GET /api/v1/jobs/{id} Tests
// ==================================

func TestGetJobStatusQueued(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	// Unknown job — should return "queued" (pending)
	req := httptest.NewRequest("GET", "/api/v1/jobs/unknown-123", nil)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("got %d, want 200", w.Code)
	}

	var resp JobStatusResponse
	json.NewDecoder(w.Body).Decode(&resp)
	if resp.Status != "queued" {
		t.Errorf("got status %q, want 'queued'", resp.Status)
	}
}

func TestGetJobStatusCompleted(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	// Pre-store a completed result
	meta := map[string]interface{}{
		"job_id":             "done-1",
		"status":             "completed",
		"processing_time_ms": 1500,
		"bubble_count":       5,
		"error":              "",
	}
	metaJSON, _ := json.Marshal(meta)
	rdb.Set(context.Background(), "frank:results:done-1", metaJSON, time.Hour)
	rdb.Set(context.Background(), "frank:results:img:done-1", []byte("fake-png"), time.Hour)

	req := httptest.NewRequest("GET", "/api/v1/jobs/done-1", nil)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("got %d, want 200", w.Code)
	}

	var resp JobStatusResponse
	json.NewDecoder(w.Body).Decode(&resp)
	if resp.Status != "completed" {
		t.Errorf("got status %q, want 'completed'", resp.Status)
	}
	if resp.ImageURL == "" {
		t.Error("completed job should have image_url")
	}
	if !strings.Contains(resp.ImageURL, "done-1") {
		t.Errorf("image_url should contain job id: %s", resp.ImageURL)
	}
}

func TestGetJobStatusUsesLanguageScopedCachePipeline(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	meta := map[string]interface{}{
		"status":         "completed",
		"pipeline":       "manga_translate",
		"cache_pipeline": "manga_translate_pt-br",
		"source_hash":    strings.Repeat("a", 64),
	}
	metaJSON, _ := json.Marshal(meta)
	rdb.Set(context.Background(), "frank:results:translated-1", metaJSON, time.Hour)

	req := httptest.NewRequest("GET", "/api/v1/jobs/translated-1", nil)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	var resp JobStatusResponse
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(resp.MetaURL, "/manga_translate_pt-br/") {
		t.Fatalf("metadata URL is not language scoped: %q", resp.MetaURL)
	}
}

func TestGetJobStatusFailed(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	meta := map[string]interface{}{
		"status": "failed",
		"error":  "decode failed",
	}
	metaJSON, _ := json.Marshal(meta)
	rdb.Set(context.Background(), "frank:results:fail-1", metaJSON, time.Hour)

	req := httptest.NewRequest("GET", "/api/v1/jobs/fail-1", nil)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	var resp JobStatusResponse
	json.NewDecoder(w.Body).Decode(&resp)
	if resp.Status != "failed" {
		t.Errorf("got status %q, want 'failed'", resp.Status)
	}
	if resp.ImageURL != "" {
		t.Error("failed job should not have image_url")
	}
}

// ==================================
// GET /api/v1/jobs/{id}/image Tests
// ==================================

func TestGetJobImage(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	imgData := makePNGBytes()
	rdb.Set(context.Background(), "frank:results:img:img-1", imgData, time.Hour)

	req := httptest.NewRequest("GET", "/api/v1/jobs/img-1/image", nil)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("got %d, want 200", w.Code)
	}
	if w.Header().Get("Content-Type") != "image/png" {
		t.Errorf("got content-type %q, want image/png", w.Header().Get("Content-Type"))
	}
	if w.Header().Get("Content-Length") != fmt.Sprintf("%d", len(imgData)) {
		t.Errorf("content-length mismatch")
	}
	if !bytes.Equal(w.Body.Bytes(), imgData) {
		t.Error("response body does not match stored image")
	}
}

func TestGetJobImageNotFound(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	req := httptest.NewRequest("GET", "/api/v1/jobs/nonexistent/image", nil)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("got %d, want 404", w.Code)
	}
}

// ==================================
// DELETE /api/v1/jobs/{id} Tests
// ==================================

func TestDeleteJob(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	// Pre-store result data
	ctx := context.Background()
	rdb.Set(ctx, "frank:results:del-1", "meta", time.Hour)
	rdb.Set(ctx, "frank:results:img:del-1", "img", time.Hour)

	req := httptest.NewRequest("DELETE", "/api/v1/jobs/del-1", nil)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("got %d, want 200", w.Code)
	}

	var resp map[string]string
	json.NewDecoder(w.Body).Decode(&resp)
	if resp["status"] != "deleted" {
		t.Errorf("got status %q, want 'deleted'", resp["status"])
	}

	// Verify data was removed
	exists, _ := rdb.Exists(ctx, "frank:results:del-1").Result()
	if exists > 0 {
		t.Error("result key should have been deleted")
	}
	exists, _ = rdb.Exists(ctx, "frank:results:img:del-1").Result()
	if exists > 0 {
		t.Error("result image key should have been deleted")
	}
}

func TestDeleteNonexistentJob(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	req := httptest.NewRequest("DELETE", "/api/v1/jobs/ghost-1", nil)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	// Should succeed (idempotent delete)
	if w.Code != http.StatusOK {
		t.Errorf("got %d, want 200", w.Code)
	}
}

// ==================================
// GET /api/v1/health Tests
// ==================================

func TestHealthEndpoint(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	req := httptest.NewRequest("GET", "/api/v1/health", nil)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("got %d, want 503", w.Code)
	}

	var resp HealthResponse
	json.NewDecoder(w.Body).Decode(&resp)
	if resp.Status != "unhealthy" {
		t.Errorf("got %q, want 'unhealthy'", resp.Status)
	}
	if resp.Redis != "connected" {
		t.Errorf("got redis %q, want 'connected'", resp.Redis)
	}
}

func TestHealthReportsQueueLengths(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	// Add some items to streams
	ctx := context.Background()
	rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: streamHigh,
		Values: map[string]interface{}{"test": "1"},
	})
	rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: streamLow,
		Values: map[string]interface{}{"test": "1"},
	})
	rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: streamLow,
		Values: map[string]interface{}{"test": "2"},
	})

	req := httptest.NewRequest("GET", "/api/v1/health", nil)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	var resp HealthResponse
	json.NewDecoder(w.Body).Decode(&resp)
	if resp.QueueHigh != 1 {
		t.Errorf("queue_high: got %d, want 1", resp.QueueHigh)
	}
	if resp.QueueLow != 2 {
		t.Errorf("queue_low: got %d, want 2", resp.QueueLow)
	}
}

func TestHealthReportsWorkers(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	// Simulate active worker heartbeat
	ctx := context.Background()
	now := time.Now().Unix()
	rdb.Set(ctx, "frank:worker:test-worker-1:heartbeat", fmt.Sprintf("%d", now), time.Minute)
	rdb.Set(ctx, "frank:worker:test-worker-1:capabilities", `{
		"schema_version":1,
		"pipelines":["book_furigana","manga_furigana","manga_translate","webtoon"],
		"pipeline_revision":"`+srv.pipelineRevision+`",
		"models":["manga-ocr","easyocr-ko"],
		"preflight":"passed"
	}`, time.Minute)

	req := httptest.NewRequest("GET", "/api/v1/health", nil)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	var resp HealthResponse
	json.NewDecoder(w.Body).Decode(&resp)
	if resp.ActiveWorkers != 1 {
		t.Errorf("active_workers: got %d, want 1", resp.ActiveWorkers)
	}
	if len(resp.Workers) != 1 || resp.Workers[0].Name != "test-worker-1" {
		t.Errorf("unexpected workers: %+v", resp.Workers)
	}
	if !resp.Workers[0].Ready || resp.Workers[0].PipelineRevision != srv.pipelineRevision {
		t.Errorf("worker capabilities not exposed: %+v", resp.Workers[0])
	}
	if w.Code != http.StatusOK || resp.ReadyWorkers != 1 || resp.Status != "healthy" {
		t.Errorf("ready health response: code=%d response=%+v", w.Code, resp)
	}
	if resp.PipelineRevision != srv.pipelineRevision {
		t.Errorf("health revision: got %q want %q", resp.PipelineRevision, srv.pipelineRevision)
	}
}

func TestHealthMarksMismatchedWorkerNotReady(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)
	ctx := context.Background()
	rdb.Set(ctx, "frank:worker:old:heartbeat", fmt.Sprintf("%d", time.Now().Unix()), time.Minute)
	rdb.Set(ctx, "frank:worker:old:capabilities", `{
		"schema_version":1,"pipelines":["webtoon"],
		"pipeline_revision":"old-revision","models":[],"preflight":"passed"
	}`, time.Minute)

	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest("GET", "/api/v1/health", nil))
	var resp HealthResponse
	json.NewDecoder(w.Body).Decode(&resp)
	if len(resp.Workers) != 1 || resp.Workers[0].Ready {
		t.Fatalf("mismatched worker must not be ready: %+v", resp.Workers)
	}
	if w.Code != http.StatusServiceUnavailable || resp.Status != "unhealthy" {
		t.Fatalf("mismatched worker health must fail closed: code=%d response=%+v", w.Code, resp)
	}
}

func TestHealthRejectsPartialCapabilityHeartbeat(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)
	ctx := context.Background()
	rdb.Set(ctx, "frank:worker:partial:heartbeat", fmt.Sprintf("%d", time.Now().Unix()), time.Minute)
	rdb.Set(ctx, "frank:worker:partial:capabilities", `{
		"schema_version":1,"pipelines":["webtoon"],
		"pipeline_revision":"`+srv.pipelineRevision+`","models":[],"preflight":"passed"
	}`, time.Minute)

	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest("GET", "/api/v1/health", nil))
	var resp HealthResponse
	json.NewDecoder(w.Body).Decode(&resp)
	if w.Code != http.StatusServiceUnavailable || resp.Workers[0].Ready {
		t.Fatalf("partial worker must not satisfy health: code=%d response=%+v", w.Code, resp)
	}
}

// ==================================
// Subscribe/Notify Tests (unit, no Redis)
// ==================================

func TestSubscribeAndNotify(t *testing.T) {
	srv := &Server{
		subscribers: make(map[string]map[chan WSNotification]struct{}),
	}

	ch := make(chan WSNotification, 10)
	srv.subscribe("job-1", ch)

	notif := WSNotification{Type: "job_complete", JobID: "job-1", Status: "completed"}
	srv.notify("job-1", notif)

	select {
	case received := <-ch:
		if received.JobID != "job-1" {
			t.Errorf("got job_id %q, want 'job-1'", received.JobID)
		}
		if received.Status != "completed" {
			t.Errorf("got status %q, want 'completed'", received.Status)
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for notification")
	}
}

func TestNotifyNoSubscribers(t *testing.T) {
	srv := &Server{
		subscribers: make(map[string]map[chan WSNotification]struct{}),
	}

	// Should not panic
	srv.notify("nobody-listening", WSNotification{Type: "test"})
}

func TestSubscribeMultipleJobs(t *testing.T) {
	srv := &Server{
		subscribers: make(map[string]map[chan WSNotification]struct{}),
	}

	ch := make(chan WSNotification, 10)
	srv.subscribe("job-a", ch)
	srv.subscribe("job-b", ch)

	srv.notify("job-a", WSNotification{JobID: "job-a"})
	srv.notify("job-b", WSNotification{JobID: "job-b"})

	got := make(map[string]bool)
	for i := 0; i < 2; i++ {
		select {
		case n := <-ch:
			got[n.JobID] = true
		case <-time.After(time.Second):
			t.Fatal("timeout")
		}
	}

	if !got["job-a"] || !got["job-b"] {
		t.Errorf("expected both jobs, got: %v", got)
	}
}

func TestUnsubscribeRemovesFromAllJobs(t *testing.T) {
	srv := &Server{
		subscribers: make(map[string]map[chan WSNotification]struct{}),
	}

	ch := make(chan WSNotification, 10)
	srv.subscribe("job-x", ch)
	srv.subscribe("job-y", ch)

	srv.unsubscribe(ch)

	// After unsubscribe, notifications should not be delivered
	srv.notify("job-x", WSNotification{JobID: "job-x"})

	select {
	case <-ch:
		t.Fatal("should not receive after unsubscribe")
	case <-time.After(50 * time.Millisecond):
		// Expected
	}

	// Maps should be cleaned up
	srv.mu.Lock()
	defer srv.mu.Unlock()
	if len(srv.subscribers) != 0 {
		t.Errorf("expected empty subscribers map, got %d entries", len(srv.subscribers))
	}
}

func TestNotifyFullChannelDoesNotBlock(t *testing.T) {
	srv := &Server{
		subscribers: make(map[string]map[chan WSNotification]struct{}),
	}

	ch := make(chan WSNotification, 1) // Small buffer
	srv.subscribe("job-full", ch)

	// Fill the channel
	ch <- WSNotification{JobID: "filling"}

	// This should not block (drops the notification)
	done := make(chan struct{})
	go func() {
		srv.notify("job-full", WSNotification{JobID: "job-full"})
		close(done)
	}()

	select {
	case <-done:
		// Notify returned without blocking
	case <-time.After(time.Second):
		t.Fatal("notify blocked on full channel")
	}
}

func TestMultipleSubscribersSameJob(t *testing.T) {
	srv := &Server{
		subscribers: make(map[string]map[chan WSNotification]struct{}),
	}

	ch1 := make(chan WSNotification, 10)
	ch2 := make(chan WSNotification, 10)
	srv.subscribe("shared-job", ch1)
	srv.subscribe("shared-job", ch2)

	srv.notify("shared-job", WSNotification{JobID: "shared-job"})

	// Both channels should receive the notification
	for _, ch := range []chan WSNotification{ch1, ch2} {
		select {
		case n := <-ch:
			if n.JobID != "shared-job" {
				t.Errorf("unexpected job_id: %s", n.JobID)
			}
		case <-time.After(time.Second):
			t.Fatal("timeout waiting for notification")
		}
	}
}

// ==================================
// JSON error helper
// ==================================

func TestJsonErrorFormat(t *testing.T) {
	w := httptest.NewRecorder()
	jsonError(w, "test error", http.StatusBadRequest)

	if w.Code != http.StatusBadRequest {
		t.Errorf("got %d, want 400", w.Code)
	}
	if w.Header().Get("Content-Type") != "application/json" {
		t.Errorf("got content-type %q", w.Header().Get("Content-Type"))
	}

	var resp map[string]string
	json.NewDecoder(w.Body).Decode(&resp)
	if resp["error"] != "test error" {
		t.Errorf("got error %q, want 'test error'", resp["error"])
	}
}

// ==================================
// Helpers
// ==================================

// ==================================
// Cache Tests
// ==================================

func TestCreateJobCacheHit(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	// Pre-populate cache
	imgData := makePNGBytes()
	if err := srv.cache.Store("manga_translate", "one-piece", "1084", "003", imgData); err != nil {
		t.Fatalf("cache store: %v", err)
	}

	// Submit with metadata that matches cache
	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	writer.WriteField("pipeline", "manga_translate")
	writer.WriteField("title", "One Piece")
	writer.WriteField("chapter", "1084")
	writer.WriteField("page_number", "003")
	part, _ := writer.CreateFormFile("image", "test.png")
	part.Write(imgData)
	writer.Close()

	req := httptest.NewRequest("POST", "/api/v1/jobs", body)
	req.Header.Set("Content-Type", writer.FormDataContentType())
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusCreated {
		t.Fatalf("got %d, want 201: %s", w.Code, w.Body.String())
	}

	var resp JobResponse
	json.NewDecoder(w.Body).Decode(&resp)
	if !resp.Cached {
		t.Error("expected cached=true")
	}
	if resp.Status != "completed" {
		t.Errorf("got status %q, want 'completed'", resp.Status)
	}
	if !strings.HasPrefix(resp.JobID, "cached-") {
		t.Errorf("expected cached- prefix, got %s", resp.JobID)
	}
	if resp.ImageURL == "" {
		t.Error("expected image_url for cached response")
	}
}

func TestCreateJobCacheMiss(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	// Submit with metadata but no cache entry
	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	writer.WriteField("pipeline", "manga_translate")
	writer.WriteField("title", "One Piece")
	writer.WriteField("chapter", "1084")
	writer.WriteField("page_number", "003")
	part, _ := writer.CreateFormFile("image", "test.png")
	part.Write(makePNGBytes())
	writer.Close()

	req := httptest.NewRequest("POST", "/api/v1/jobs", body)
	req.Header.Set("Content-Type", writer.FormDataContentType())
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusCreated {
		t.Fatalf("got %d, want 201", w.Code)
	}

	var resp JobResponse
	json.NewDecoder(w.Body).Decode(&resp)
	if resp.Cached {
		t.Error("expected cached=false on cache miss")
	}
	if resp.Status != "queued" {
		t.Errorf("got status %q, want 'queued'", resp.Status)
	}
}

func TestCreateJobMetadataPassthrough(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	writer.WriteField("pipeline", "manga_translate")
	writer.WriteField("title", "test-manga")
	writer.WriteField("chapter", "5")
	writer.WriteField("page_number", "2")
	writer.WriteField("source_url", "https://example.com")
	part, _ := writer.CreateFormFile("image", "test.png")
	part.Write(makePNGBytes())
	writer.Close()

	req := httptest.NewRequest("POST", "/api/v1/jobs", body)
	req.Header.Set("Content-Type", writer.FormDataContentType())
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusCreated {
		t.Fatalf("got %d, want 201", w.Code)
	}

	// Verify metadata was passed to the stream
	ctx := context.Background()
	msgs, err := rdb.XRange(ctx, streamHigh, "-", "+").Result()
	if err != nil {
		t.Fatalf("xrange: %v", err)
	}
	if len(msgs) == 0 {
		t.Fatal("expected at least one stream message")
	}

	last := msgs[len(msgs)-1]
	if last.Values["title"] != "test-manga" {
		t.Errorf("title: got %q, want 'test-manga'", last.Values["title"])
	}
	if last.Values["chapter"] != "5" {
		t.Errorf("chapter: got %q, want '5'", last.Values["chapter"])
	}
	if last.Values["page_number"] != "2" {
		t.Errorf("page_number: got %q, want '2'", last.Values["page_number"])
	}
	if last.Values["source_url"] != "https://example.com" {
		t.Errorf("source_url: got %q", last.Values["source_url"])
	}
}

func TestCacheImageEndpoint(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	imgData := makePNGBytes()
	srv.cache.Store("manga_translate", "one-piece", "1", "001", imgData)

	req := httptest.NewRequest("GET", "/api/v1/cache/manga_translate/one-piece/1/001/image", nil)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("got %d, want 200", w.Code)
	}
	if w.Header().Get("Content-Type") != "image/png" {
		t.Errorf("content-type: %q", w.Header().Get("Content-Type"))
	}
	if !bytes.Equal(w.Body.Bytes(), imgData) {
		t.Error("image bytes mismatch")
	}
}

func TestCacheByHashImageAndMetaEndpoints(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	src := makePNGBytes()
	sourceHash := hashHex(src)
	meta := []byte(`{"schema_version":1,"regions":[{"id":"r1"}]}`)
	_, err := srv.cache.StoreBySourceHash(
		"manga_translate",
		sourceHash,
		src,
		src,
		meta,
		"one-piece",
		"1",
		"001",
	)
	if err != nil {
		t.Fatalf("store by hash: %v", err)
	}

	t.Run("image", func(t *testing.T) {
		req := httptest.NewRequest(
			"GET",
			fmt.Sprintf("/api/v1/cache/by-hash/manga_translate/%s/image", sourceHash),
			nil,
		)
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("got %d, want 200", w.Code)
		}
		if !bytes.Equal(w.Body.Bytes(), src) {
			t.Fatal("image mismatch")
		}
	})

	t.Run("meta", func(t *testing.T) {
		req := httptest.NewRequest(
			"GET",
			fmt.Sprintf("/api/v1/cache/by-hash/manga_translate/%s/meta", sourceHash),
			nil,
		)
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("got %d, want 200 body=%s", w.Code, w.Body.String())
		}
		var resp map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
			t.Fatalf("json: %v", err)
		}
		if resp["source_hash"] != sourceHash {
			t.Fatalf("source_hash mismatch")
		}
		if resp["content_hash"] == "" {
			t.Fatalf("missing content_hash")
		}
	})
}

func TestPatchCacheMetaByHashQueuesRerender(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	src := makePNGBytes()
	sourceHash := hashHex(src)
	initialMeta := []byte(`{"schema_version":1,"regions":[{"id":"r1","user":{"manual_translation":""}}]}`)
	manifest, err := srv.cache.StoreBySourceHash(
		"manga_translate",
		sourceHash,
		src,
		src,
		initialMeta,
		"one-piece",
		"1",
		"001",
	)
	if err != nil {
		t.Fatalf("store by hash: %v", err)
	}

	patchBody := map[string]any{
		"base_content_hash": manifest.ContentHash,
		"metadata": map[string]any{
			"schema_version": 1,
			"regions": []any{
				map[string]any{
					"id":   "r1",
					"user": map[string]any{"manual_translation": "edited"},
				},
			},
		},
	}
	raw, _ := json.Marshal(patchBody)
	req := httptest.NewRequest(
		"PATCH",
		fmt.Sprintf("/api/v1/cache/by-hash/manga_translate/%s/meta", sourceHash),
		bytes.NewReader(raw),
	)
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)
	if w.Code != http.StatusAccepted {
		t.Fatalf("got %d, want 202 body=%s", w.Code, w.Body.String())
	}

	var resp JobResponse
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.JobID == "" || resp.Status != "queued" {
		t.Fatalf("unexpected response: %+v", resp)
	}

	// Confirm rerender flag got enqueued.
	ctx := context.Background()
	msgs, err := rdb.XRange(ctx, streamHigh, "-", "+").Result()
	if err != nil {
		t.Fatalf("xrange: %v", err)
	}
	if len(msgs) == 0 {
		t.Fatalf("no stream messages")
	}
	last := msgs[len(msgs)-1]
	if last.Values["rerender_from_metadata"] != "1" {
		t.Fatalf("expected rerender_from_metadata=1, got %v", last.Values["rerender_from_metadata"])
	}
}

func TestPatchCacheMetaByHashConflict409(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	src := makePNGBytes()
	sourceHash := hashHex(src)
	initialMeta := []byte(`{"schema_version":1,"regions":[{"id":"r1"}]}`)
	_, err := srv.cache.StoreBySourceHash(
		"manga_translate",
		sourceHash,
		src,
		src,
		initialMeta,
		"test-title",
		"1",
		"001",
	)
	if err != nil {
		t.Fatalf("store by hash: %v", err)
	}

	// Send PATCH with a wrong base_content_hash to trigger 409
	patchBody := map[string]any{
		"base_content_hash": "stale-hash-that-does-not-match",
		"metadata": map[string]any{
			"schema_version": 1,
			"regions": []any{
				map[string]any{
					"id":   "r1",
					"user": map[string]any{"manual_translation": "edited"},
				},
			},
		},
	}
	raw, _ := json.Marshal(patchBody)
	req := httptest.NewRequest(
		"PATCH",
		fmt.Sprintf("/api/v1/cache/by-hash/manga_translate/%s/meta", sourceHash),
		bytes.NewReader(raw),
	)
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusConflict {
		t.Fatalf("got %d, want 409 body=%s", w.Code, w.Body.String())
	}

	// Verify error message
	var errResp map[string]string
	if err := json.NewDecoder(w.Body).Decode(&errResp); err != nil {
		t.Fatalf("decode error: %v", err)
	}
	if errResp["error"] != "content hash mismatch" {
		t.Fatalf("unexpected error: %s", errResp["error"])
	}
}

func TestPatchCacheMetaByHashNotFound(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	// PATCH a non-existent source hash
	patchBody := map[string]any{
		"metadata": map[string]any{"regions": []any{}},
	}
	raw, _ := json.Marshal(patchBody)
	req := httptest.NewRequest(
		"PATCH",
		"/api/v1/cache/by-hash/manga_translate/nonexistent-hash/meta",
		bytes.NewReader(raw),
	)
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusNotFound {
		t.Fatalf("got %d, want 404 body=%s", w.Code, w.Body.String())
	}
}

func TestCacheImageNotFound(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	req := httptest.NewRequest("GET", "/api/v1/cache/manga_translate/ghost/1/001/image", nil)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("got %d, want 404", w.Code)
	}
}

func TestCachedJobImageServing(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	imgData := makePNGBytes()
	srv.cache.Store("manga_translate", "test", "1", "001", imgData)

	// Request using cached-{pipeline}-{title}-{chapter}-{page} format
	req := httptest.NewRequest("GET", "/api/v1/jobs/cached-manga_translate-test-1-001/image", nil)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("got %d, want 200: %s", w.Code, w.Body.String())
	}
	if !bytes.Equal(w.Body.Bytes(), imgData) {
		t.Error("image bytes mismatch")
	}
}

// ==================================
// Progress Notification Tests
// ==================================

func TestProgressNotificationForwarded(t *testing.T) {
	srv := &Server{
		subscribers: make(map[string]map[chan WSNotification]struct{}),
	}

	ch := make(chan WSNotification, 10)
	srv.subscribe("job-p1", ch)

	// Simulate a progress event from Redis
	progressPayload := `{"type":"progress","job_id":"job-p1","stage":"translating","detail":"3/7 bubbles","percent":43}`
	var meta map[string]any
	json.Unmarshal([]byte(progressPayload), &meta)

	msgType, _ := meta["type"].(string)
	if msgType != "progress" {
		t.Fatal("expected progress type")
	}

	stage, _ := meta["stage"].(string)
	detail, _ := meta["detail"].(string)
	percent := 0
	if p, ok := meta["percent"].(float64); ok {
		percent = int(p)
	}

	notif := WSNotification{
		Type:    "job_progress",
		JobID:   "job-p1",
		Stage:   stage,
		Detail:  detail,
		Percent: percent,
	}
	srv.notify("job-p1", notif)

	select {
	case received := <-ch:
		if received.Type != "job_progress" {
			t.Errorf("type: got %q, want 'job_progress'", received.Type)
		}
		if received.Stage != "translating" {
			t.Errorf("stage: got %q, want 'translating'", received.Stage)
		}
		if received.Detail != "3/7 bubbles" {
			t.Errorf("detail: got %q", received.Detail)
		}
		if received.Percent != 43 {
			t.Errorf("percent: got %d, want 43", received.Percent)
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for notification")
	}
}

// ==================================
// Cache Utility Tests
// ==================================

func TestSlugify(t *testing.T) {
	tests := []struct {
		in, out string
	}{
		{"One Piece", "one-piece"},
		{"tower-of-god", "tower-of-god"},
		{"Test Manga!@#", "test-manga"},
		{"  spaces  ", "spaces"},
		{"UPPER CASE", "upper-case"},
	}
	for _, tt := range tests {
		got := slugify(tt.in)
		if got != tt.out {
			t.Errorf("slugify(%q) = %q, want %q", tt.in, got, tt.out)
		}
	}
}

// ==================================
// JSON error helper
// ==================================

func TestCreateJobForceBypassesCache(t *testing.T) {
	srv, _ := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	// Pre-populate v2 cache so a normal submit would return cached
	imgData := makePNGBytes()
	sourceHash := hashHex(imgData)
	metaJSON := []byte(`{"regions":[]}`)
	if _, err := srv.cache.StoreBySourceHash("manga_translate", sourceHash, imgData, imgData,
		metaJSON, "Test", "1", "1"); err != nil {
		t.Fatalf("cache store: %v", err)
	}

	// Verify cache is hit without force
	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	writer.WriteField("pipeline", "manga_translate")
	part, _ := writer.CreateFormFile("image", "test.png")
	part.Write(imgData)
	writer.Close()

	req := httptest.NewRequest("POST", "/api/v1/jobs", body)
	req.Header.Set("Content-Type", writer.FormDataContentType())
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)

	var normalResp JobResponse
	json.NewDecoder(w.Body).Decode(&normalResp)
	if !normalResp.Cached {
		t.Fatal("expected cache hit without force")
	}

	// Now submit with force=true — should bypass cache and queue a new job
	body2 := &bytes.Buffer{}
	writer2 := multipart.NewWriter(body2)
	writer2.WriteField("pipeline", "manga_translate")
	writer2.WriteField("force", "true")
	part2, _ := writer2.CreateFormFile("image", "test.png")
	part2.Write(imgData)
	writer2.Close()

	req2 := httptest.NewRequest("POST", "/api/v1/jobs", body2)
	req2.Header.Set("Content-Type", writer2.FormDataContentType())
	w2 := httptest.NewRecorder()
	mux.ServeHTTP(w2, req2)

	if w2.Code != http.StatusCreated {
		t.Fatalf("got %d, want 201: %s", w2.Code, w2.Body.String())
	}

	var forceResp JobResponse
	json.NewDecoder(w2.Body).Decode(&forceResp)
	if forceResp.Cached {
		t.Error("expected cached=false with force=true")
	}
	if forceResp.Status != "queued" {
		t.Errorf("got status %q, want 'queued'", forceResp.Status)
	}
	if !strings.HasPrefix(forceResp.JobID, "job-") {
		t.Errorf("expected job- prefix (new job), got %s", forceResp.JobID)
	}
}

func assertJSONError(t *testing.T, body *bytes.Buffer, contains string) {
	t.Helper()
	var resp map[string]string
	if err := json.Unmarshal(body.Bytes(), &resp); err != nil {
		t.Fatalf("response is not valid JSON: %v", err)
	}
	if errMsg, ok := resp["error"]; !ok {
		t.Error("expected 'error' field in JSON response")
	} else if !strings.Contains(errMsg, contains) {
		t.Errorf("error %q does not contain %q", errMsg, contains)
	}
}

func TestCachePipelineRevisionInvalidatesEntries(t *testing.T) {
	root := t.TempDir()
	imageBytes := makePNGBytes()
	sourceHash := hashHex(imageBytes)
	oldCache := NewCacheWithRevision(root, "detector-v1")
	manifest, err := oldCache.StoreBySourceHash(
		"manga_translate", sourceHash, imageBytes, imageBytes,
		[]byte(`{"schema_version":1,"regions":[]}`), "Test", "1", "1",
	)
	if err != nil {
		t.Fatalf("store old revision: %v", err)
	}
	if manifest.SchemaVersion != cacheManifestSchemaVersion {
		t.Fatalf("schema version = %d, want %d", manifest.SchemaVersion, cacheManifestSchemaVersion)
	}
	if manifest.PipelineRevision != "detector-v1" {
		t.Fatalf("pipeline revision = %q", manifest.PipelineRevision)
	}
	if _, _, ok := oldCache.LookupBySourceHash("manga_translate", sourceHash); !ok {
		t.Fatal("same revision should hit cache")
	}
	newCache := NewCacheWithRevision(root, "detector-v2")
	if _, _, ok := newCache.LookupBySourceHash("manga_translate", sourceHash); ok {
		t.Fatal("changed revision should be a cache miss")
	}
}

func TestCacheRejectsMetadataFromAnotherPipelineRevision(t *testing.T) {
	cache := NewCacheWithRevision(t.TempDir(), "pipeline-b")
	imageBytes := makePNGBytes()
	sourceHash := hashHex(imageBytes)
	_, err := cache.StoreBySourceHash(
		"manga_translate", sourceHash, imageBytes, imageBytes,
		[]byte(`{"schema_version":2,"pipeline":"manga_translate","pipeline_revision":"pipeline-a","source_hash":"`+sourceHash+`"}`),
		"Test", "1", "1",
	)
	if err == nil || !strings.Contains(err.Error(), "pipeline revision mismatch") {
		t.Fatalf("expected metadata revision mismatch, got %v", err)
	}
}

func TestLegacyManifestIsReadableOnlyByLegacyRevision(t *testing.T) {
	root := t.TempDir()
	imageBytes := makePNGBytes()
	sourceHash := hashHex(imageBytes)
	legacyCache := NewCacheWithRevision(root, legacyPipelineRevision)
	if _, err := legacyCache.StoreBySourceHash(
		"manga_translate", sourceHash, imageBytes, imageBytes,
		[]byte(`{"schema_version":1,"regions":[]}`), "Test", "1", "1",
	); err != nil {
		t.Fatalf("store legacy entry: %v", err)
	}
	manifestPath := legacyCache.manifestByHashPath("manga_translate", sourceHash)
	data, err := os.ReadFile(manifestPath)
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]interface{}
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatal(err)
	}
	raw["schema_version"] = float64(2)
	delete(raw, "pipeline_revision")
	data, _ = json.Marshal(raw)
	if err := os.WriteFile(manifestPath, data, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, _, ok := legacyCache.LookupBySourceHash("manga_translate", sourceHash); !ok {
		t.Fatal("manifest v2 without a revision should map to legacy-v1")
	}
	if _, _, ok := NewCacheWithRevision(root, "current-v2").LookupBySourceHash("manga_translate", sourceHash); ok {
		t.Fatal("non-legacy revision must reject a legacy manifest")
	}
}

func TestCacheManifestIdentityMustMatchLookupKey(t *testing.T) {
	cache := NewCacheWithRevision(t.TempDir(), "pipeline-v2")
	imageBytes := makePNGBytes()
	sourceHash := hashHex(imageBytes)
	if _, err := cache.StoreBySourceHash(
		"manga_translate", sourceHash, imageBytes, imageBytes,
		[]byte(`{"schema_version":1,"regions":[]}`), "Test", "1", "1",
	); err != nil {
		t.Fatal(err)
	}
	path := cache.manifestByHashPath("manga_translate", sourceHash)
	data, _ := os.ReadFile(path)
	var manifest map[string]interface{}
	_ = json.Unmarshal(data, &manifest)
	manifest["pipeline"] = "webtoon"
	data, _ = json.Marshal(manifest)
	_ = os.WriteFile(path, data, 0o644)
	if _, _, ok := cache.LookupBySourceHash("manga_translate", sourceHash); ok {
		t.Fatal("manifest whose pipeline differs from its lookup path was accepted")
	}
}

func TestQueuePipelineRevisionScopesDedupAndStream(t *testing.T) {
	_, rdb := newTestServer(t)
	ctx := context.Background()
	imageBytes := makePNGBytes()
	meta := &JobMetadata{TargetLang: "en"}

	queueA := NewQueueWithRevision(rdb, "pipeline-a")
	jobA, hit, err := queueA.SubmitJob(ctx, imageBytes, "manga_translate", "high", meta)
	if err != nil || hit {
		t.Fatalf("submit revision a: hit=%v err=%v", hit, err)
	}
	jobA2, hit, err := queueA.SubmitJob(ctx, imageBytes, "manga_translate", "high", meta)
	if err != nil || !hit || jobA2 != jobA {
		t.Fatalf("same revision should deduplicate: job=%q hit=%v err=%v", jobA2, hit, err)
	}

	queueB := NewQueueWithRevision(rdb, "pipeline-b")
	jobB, hit, err := queueB.SubmitJob(ctx, imageBytes, "manga_translate", "high", meta)
	if err != nil || hit || jobB == jobA {
		t.Fatalf("changed revision should enqueue independently: job=%q hit=%v err=%v", jobB, hit, err)
	}
	msgs, err := rdb.XRange(ctx, streamHigh, "-", "+").Result()
	if err != nil {
		t.Fatal(err)
	}
	if len(msgs) != 2 {
		t.Fatalf("stream messages = %d, want 2", len(msgs))
	}
	if got := msgs[0].Values["pipeline_revision"]; got != "pipeline-a" {
		t.Fatalf("first stream revision = %v", got)
	}
	if got := msgs[1].Values["pipeline_revision"]; got != "pipeline-b" {
		t.Fatalf("second stream revision = %v", got)
	}
}

func TestCreateJobTreatsOtherPipelineRevisionAsCacheMiss(t *testing.T) {
	srv, rdb := newTestServer(t)
	root := t.TempDir()
	imageBytes := makePNGBytes()
	sourceHash := hashHex(imageBytes)
	oldCache := NewCacheWithRevision(root, "pipeline-a")
	if _, err := oldCache.StoreBySourceHash(
		"manga_translate", sourceHash, imageBytes, imageBytes,
		[]byte(`{"schema_version":1,"regions":[]}`), "Test", "1", "1",
	); err != nil {
		t.Fatalf("seed old cache: %v", err)
	}
	srv.cache = NewCacheWithRevision(root, "pipeline-b")
	srv.queue = NewQueueWithRevision(rdb, "pipeline-b")
	srv.pipelineRevision = "pipeline-b"
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	req, recorder := makeJobRequest(t, "manga_translate", "high", imageBytes)
	mux.ServeHTTP(recorder, req)
	if recorder.Code != http.StatusCreated {
		t.Fatalf("status = %d: %s", recorder.Code, recorder.Body.String())
	}
	var response JobResponse
	if err := json.NewDecoder(recorder.Body).Decode(&response); err != nil {
		t.Fatal(err)
	}
	if response.Cached || response.Status != "queued" {
		t.Fatalf("old revision was served: %+v", response)
	}
	if response.PipelineRevision != "pipeline-b" {
		t.Fatalf("response revision = %q", response.PipelineRevision)
	}
}

func TestNonEnglishCacheNamespaceSubmitGetAndPatch(t *testing.T) {
	srv, rdb := newTestServer(t)
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)
	imageBytes := makePNGBytes()
	sourceHash := hashHex(imageBytes)
	metadata, _ := json.Marshal(map[string]interface{}{
		"schema_version":    2,
		"page_id":           "manga_translate:" + sourceHash,
		"pipeline":          "manga_translate",
		"pipeline_revision": srv.pipelineRevision,
		"source_hash":       sourceHash,
		"image":             map[string]interface{}{"width": 100, "height": 80},
		"regions":           []interface{}{},
		"stage_provenance":  map[string]interface{}{},
	})
	manifest, err := srv.cache.StoreBySourceHash(
		"manga_translate_pt-br", sourceHash, imageBytes, imageBytes,
		metadata, "Test", "1", "1",
	)
	if err != nil {
		t.Fatalf("seed pt-br cache: %v", err)
	}

	request, recorder := makeJobRequestWithFields(t, map[string]string{
		"pipeline": "manga_translate", "target_lang": "pt-br",
	}, imageBytes)
	mux.ServeHTTP(recorder, request)
	var submit JobResponse
	if err := json.NewDecoder(recorder.Body).Decode(&submit); err != nil {
		t.Fatal(err)
	}
	if recorder.Code != http.StatusCreated || !submit.Cached {
		t.Fatalf("non-English durable cache miss: code=%d response=%+v", recorder.Code, submit)
	}
	if !strings.Contains(submit.MetaURL, "manga_translate_pt-br") {
		t.Fatalf("wrong metadata namespace: %q", submit.MetaURL)
	}

	getRequest := httptest.NewRequest(
		"GET", fmt.Sprintf("/api/v1/cache/by-hash/manga_translate_pt-br/%s/meta", sourceHash), nil)
	getRecorder := httptest.NewRecorder()
	mux.ServeHTTP(getRecorder, getRequest)
	if getRecorder.Code != http.StatusOK {
		t.Fatalf("GET metadata = %d: %s", getRecorder.Code, getRecorder.Body.String())
	}
	var getResponse map[string]interface{}
	if err := json.NewDecoder(getRecorder.Body).Decode(&getResponse); err != nil {
		t.Fatal(err)
	}
	storedMetadata := getResponse["metadata"].(map[string]interface{})
	if storedMetadata["pipeline"] != "manga_translate" {
		t.Fatalf("document pipeline = %v", storedMetadata["pipeline"])
	}

	patchBody, _ := json.Marshal(map[string]interface{}{
		"base_content_hash": manifest.ContentHash,
		"metadata": map[string]interface{}{
			"schema_version":    2,
			"page_id":           "manga_translate:" + sourceHash,
			"pipeline":          "manga_translate",
			"pipeline_revision": srv.pipelineRevision,
			"source_hash":       sourceHash,
			"image":             map[string]interface{}{"width": 100, "height": 80},
			"regions":           []interface{}{},
			"stage_provenance":  map[string]interface{}{},
		},
	})
	patchRequest := httptest.NewRequest(
		"PATCH", fmt.Sprintf("/api/v1/cache/by-hash/manga_translate_pt-br/%s/meta", sourceHash),
		bytes.NewReader(patchBody))
	patchRecorder := httptest.NewRecorder()
	mux.ServeHTTP(patchRecorder, patchRequest)
	if patchRecorder.Code != http.StatusAccepted {
		t.Fatalf("PATCH metadata = %d: %s", patchRecorder.Code, patchRecorder.Body.String())
	}
	messages, err := rdb.XRange(context.Background(), streamHigh, "-", "+").Result()
	if err != nil || len(messages) != 1 {
		t.Fatalf("rerender stream messages=%d err=%v", len(messages), err)
	}
	values := messages[0].Values
	if values["pipeline"] != "manga_translate" || values["target_lang"] != "pt-br" ||
		values["rerender_from_metadata"] != "1" {
		t.Fatalf("wrong rerender namespace fields: %#v", values)
	}
}
