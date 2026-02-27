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
	"mime/multipart"
	"net/http"
	"net/http/httptest"
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

	return NewServer(rdb), rdb
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
	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	if pipeline != "" {
		writer.WriteField("pipeline", pipeline)
	}
	if priority != "" {
		writer.WriteField("priority", priority)
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

	if w.Code != http.StatusOK {
		t.Fatalf("got %d, want 200", w.Code)
	}

	var resp HealthResponse
	json.NewDecoder(w.Body).Decode(&resp)
	if resp.Status != "healthy" {
		t.Errorf("got %q, want 'healthy'", resp.Status)
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
