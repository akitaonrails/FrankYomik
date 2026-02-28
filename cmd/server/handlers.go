package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"sync"

	"github.com/redis/go-redis/v9"
)

const maxImageSize = 20 << 20 // 20 MB

// Server holds the HTTP handlers and dependencies.
type Server struct {
	queue   *Queue
	results *Results
	cache   *Cache
	rdb     *redis.Client

	// WebSocket subscriptions
	mu          sync.Mutex
	subscribers map[string]map[chan WSNotification]struct{} // jobID -> set of channels
}

// NewServer creates a new Server instance.
func NewServer(rdb *redis.Client, cacheDir string) *Server {
	return &Server{
		queue:       NewQueue(rdb),
		results:     NewResults(rdb),
		cache:       NewCache(cacheDir),
		rdb:         rdb,
		subscribers: make(map[string]map[chan WSNotification]struct{}),
	}
}

// RegisterRoutes sets up all API routes.
func (s *Server) RegisterRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/v1/jobs", s.handleCreateJob)
	mux.HandleFunc("GET /api/v1/jobs/{id}", s.handleGetJob)
	mux.HandleFunc("GET /api/v1/jobs/{id}/image", s.handleGetJobImage)
	mux.HandleFunc("DELETE /api/v1/jobs/{id}", s.handleDeleteJob)
	mux.HandleFunc("GET /api/v1/cache/{pipeline}/{title}/{chapter}/{page}/image", s.handleCacheImage)
	mux.HandleFunc("GET /api/v1/health", s.handleHealth)
	mux.HandleFunc("GET /api/v1/ws", s.handleWebSocket)
}

// handleCreateJob handles POST /api/v1/jobs
func (s *Server) handleCreateJob(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, maxImageSize)
	if err := r.ParseMultipartForm(maxImageSize); err != nil {
		jsonError(w, "invalid multipart form", http.StatusBadRequest)
		return
	}

	pipeline := r.FormValue("pipeline")
	if !validPipelines[pipeline] {
		jsonError(w, fmt.Sprintf("invalid pipeline: %s (valid: manga_translate, manga_furigana, webtoon)", pipeline),
			http.StatusBadRequest)
		return
	}

	priority := r.FormValue("priority")
	if priority == "" {
		priority = "high"
	}
	if !validPriorities[priority] {
		jsonError(w, "invalid priority: must be 'high' or 'low'", http.StatusBadRequest)
		return
	}

	// Parse optional metadata
	meta := &JobMetadata{
		Title:      r.FormValue("title"),
		Chapter:    r.FormValue("chapter"),
		PageNumber: r.FormValue("page_number"),
		SourceURL:  r.FormValue("source_url"),
	}

	// Check filesystem cache if metadata is provided
	if meta.Title != "" && meta.Chapter != "" && meta.PageNumber != "" {
		if _, ok := s.cache.Lookup(pipeline, meta.Title, meta.Chapter, meta.PageNumber); ok {
			cacheJobID := fmt.Sprintf("cached-%s-%s-%s-%s", pipeline, slugify(meta.Title), meta.Chapter, meta.PageNumber)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusCreated)
			json.NewEncoder(w).Encode(JobResponse{
				JobID:    cacheJobID,
				Status:   "completed",
				Cached:   true,
				ImageURL: fmt.Sprintf("/api/v1/cache/%s/%s/%s/%s/image", pipeline, slugify(meta.Title), meta.Chapter, meta.PageNumber),
			})
			return
		}
	}

	file, _, err := r.FormFile("image")
	if err != nil {
		jsonError(w, "missing 'image' field", http.StatusBadRequest)
		return
	}
	defer file.Close()

	imageBytes, err := io.ReadAll(file)
	if err != nil {
		jsonError(w, "reading image", http.StatusBadRequest)
		return
	}
	if len(imageBytes) == 0 {
		jsonError(w, "empty image", http.StatusBadRequest)
		return
	}

	jobID, dedupHit, err := s.queue.SubmitJob(r.Context(), imageBytes, pipeline, priority, meta)
	if err != nil {
		log.Printf("ERROR submitting job: %v", err)
		jsonError(w, "internal error", http.StatusInternalServerError)
		return
	}

	// If dedup hit, check if the job already completed so the client
	// gets the result immediately instead of waiting for a poll cycle.
	if dedupHit {
		status, err := s.results.GetJobStatus(r.Context(), jobID)
		if err == nil && status.Status == "completed" {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusCreated)
			json.NewEncoder(w).Encode(JobResponse{
				JobID:    jobID,
				Status:   "completed",
				Cached:   true,
				DedupHit: true,
				ImageURL: status.ImageURL,
			})
			return
		}
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(JobResponse{
		JobID:    jobID,
		Status:   "queued",
		DedupHit: dedupHit,
	})
}

// handleGetJob handles GET /api/v1/jobs/{id}
func (s *Server) handleGetJob(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("id")
	if jobID == "" {
		jsonError(w, "missing job id", http.StatusBadRequest)
		return
	}

	status, err := s.results.GetJobStatus(r.Context(), jobID)
	if err != nil {
		log.Printf("ERROR getting job status: %v", err)
		jsonError(w, "internal error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(status)
}

// handleGetJobImage handles GET /api/v1/jobs/{id}/image
func (s *Server) handleGetJobImage(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("id")
	if jobID == "" {
		jsonError(w, "missing job id", http.StatusBadRequest)
		return
	}

	// For cached job IDs, serve from filesystem cache
	if strings.HasPrefix(jobID, "cached-") {
		s.serveCachedJobImage(w, jobID)
		return
	}

	imageBytes, err := s.results.GetJobImage(r.Context(), jobID)
	if err != nil {
		if strings.Contains(err.Error(), "not found") {
			jsonError(w, "image not found", http.StatusNotFound)
		} else {
			jsonError(w, "internal error", http.StatusInternalServerError)
		}
		return
	}

	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Content-Length", fmt.Sprintf("%d", len(imageBytes)))
	w.Write(imageBytes)
}

// serveCachedJobImage serves an image from the filesystem cache for a cached-* job ID.
// cached-{pipeline}-{title}-{chapter}-{page} format.
func (s *Server) serveCachedJobImage(w http.ResponseWriter, jobID string) {
	// Parse: cached-{pipeline}-{title}-{chapter}-{page}
	// Pipeline names contain underscores, not hyphens, so split carefully.
	// Format: "cached-manga_translate-one-piece-1084-003"
	rest := strings.TrimPrefix(jobID, "cached-")

	// Find pipeline by checking known prefixes
	var pipeline, remainder string
	for p := range validPipelines {
		prefix := p + "-"
		if strings.HasPrefix(rest, prefix) {
			pipeline = p
			remainder = strings.TrimPrefix(rest, prefix)
			break
		}
	}
	if pipeline == "" {
		jsonError(w, "invalid cached job id", http.StatusBadRequest)
		return
	}

	// remainder = "one-piece-1084-003" — last segment is page, second-to-last is chapter
	parts := strings.Split(remainder, "-")
	if len(parts) < 3 {
		jsonError(w, "invalid cached job id format", http.StatusBadRequest)
		return
	}

	page := parts[len(parts)-1]
	chapter := parts[len(parts)-2]
	title := strings.Join(parts[:len(parts)-2], "-")

	imageBytes, ok := s.cache.Lookup(pipeline, title, chapter, page)
	if !ok {
		jsonError(w, "cached image not found", http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Content-Length", fmt.Sprintf("%d", len(imageBytes)))
	w.Write(imageBytes)
}

// handleCacheImage handles GET /api/v1/cache/{pipeline}/{title}/{chapter}/{page}/image
func (s *Server) handleCacheImage(w http.ResponseWriter, r *http.Request) {
	pipeline := r.PathValue("pipeline")
	title := r.PathValue("title")
	chapter := r.PathValue("chapter")
	page := r.PathValue("page")

	if pipeline == "" || title == "" || chapter == "" || page == "" {
		jsonError(w, "missing path parameters", http.StatusBadRequest)
		return
	}

	imageBytes, ok := s.cache.Lookup(pipeline, title, chapter, page)
	if !ok {
		jsonError(w, "cached image not found", http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Content-Length", fmt.Sprintf("%d", len(imageBytes)))
	w.Write(imageBytes)
}

// handleDeleteJob handles DELETE /api/v1/jobs/{id}
func (s *Server) handleDeleteJob(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("id")
	if jobID == "" {
		jsonError(w, "missing job id", http.StatusBadRequest)
		return
	}

	if err := s.results.DeleteJob(r.Context(), jobID); err != nil {
		log.Printf("ERROR deleting job: %v", err)
		jsonError(w, "internal error", http.StatusInternalServerError)
		return
	}

	if err := s.queue.CancelJob(r.Context(), jobID); err != nil {
		log.Printf("ERROR cancelling job: %v", err)
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "deleted"})
}

// handleHealth handles GET /api/v1/health
func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	resp := HealthResponse{
		Status: "healthy",
		Redis:  "connected",
	}

	// Check Redis
	if err := s.rdb.Ping(ctx).Err(); err != nil {
		resp.Status = "unhealthy"
		resp.Redis = "disconnected"
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		json.NewEncoder(w).Encode(resp)
		return
	}

	// Queue lengths
	resp.QueueHigh, _ = s.rdb.XLen(ctx, streamHigh).Result()
	resp.QueueLow, _ = s.rdb.XLen(ctx, streamLow).Result()

	// Active workers
	workers, _ := s.results.GetActiveWorkers(ctx)
	resp.Workers = workers
	resp.ActiveWorkers = len(workers)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// subscribe registers a channel to receive notifications for a job.
func (s *Server) subscribe(jobID string, ch chan WSNotification) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.subscribers[jobID] == nil {
		s.subscribers[jobID] = make(map[chan WSNotification]struct{})
	}
	s.subscribers[jobID][ch] = struct{}{}
}

// unsubscribe removes a channel from all job subscriptions.
func (s *Server) unsubscribe(ch chan WSNotification) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for jobID, subs := range s.subscribers {
		delete(subs, ch)
		if len(subs) == 0 {
			delete(s.subscribers, jobID)
		}
	}
}

// notify sends a notification to all subscribers of a job.
func (s *Server) notify(jobID string, notif WSNotification) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for ch := range s.subscribers[jobID] {
		select {
		case ch <- notif:
		default:
			// Channel full, skip
		}
	}
}

// StartRedisSubscriber listens on Redis Pub/Sub for job notifications
// and forwards them to WebSocket subscribers.
func (s *Server) StartRedisSubscriber(ctx context.Context) {
	pubsub := s.rdb.PSubscribe(ctx, "frank:notify:*")
	defer pubsub.Close()

	ch := pubsub.Channel()
	for {
		select {
		case <-ctx.Done():
			return
		case msg, ok := <-ch:
			if !ok {
				return
			}
			// Extract job ID from channel: frank:notify:<job_id>
			parts := strings.SplitN(msg.Channel, ":", 3)
			if len(parts) < 3 {
				continue
			}
			jobID := parts[2]

			var meta map[string]interface{}
			if err := json.Unmarshal([]byte(msg.Payload), &meta); err != nil {
				continue
			}

			msgType, _ := meta["type"].(string)

			// Progress events
			if msgType == "progress" {
				stage, _ := meta["stage"].(string)
				detail, _ := meta["detail"].(string)
				percent := 0
				if p, ok := meta["percent"].(float64); ok {
					percent = int(p)
				}
				notif := WSNotification{
					Type:    "job_progress",
					JobID:   jobID,
					Stage:   stage,
					Detail:  detail,
					Percent: percent,
				}
				s.notify(jobID, notif)
				continue
			}

			// Completion events
			status, _ := meta["status"].(string)
			errMsg, _ := meta["error"].(string)

			notif := WSNotification{
				Type:   "job_complete",
				JobID:  jobID,
				Status: status,
				Error:  errMsg,
			}
			if status == "completed" {
				notif.ImageURL = fmt.Sprintf("/api/v1/jobs/%s/image", jobID)
			}

			s.notify(jobID, notif)
		}
	}
}

func jsonError(w http.ResponseWriter, msg string, code int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(map[string]string{"error": msg})
}
