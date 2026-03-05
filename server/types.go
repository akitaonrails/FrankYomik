package main

import "time"

// Job represents a processing job in the system.
type Job struct {
	ID         string    `json:"id"`
	Pipeline   string    `json:"pipeline"`
	Priority   string    `json:"priority"`
	Status     string    `json:"status"`
	ImageKey   string    `json:"-"`
	ImageHash  string    `json:"-"`
	CreatedAt  time.Time `json:"created_at"`
	DedupHit   bool      `json:"dedup_hit,omitempty"`
	Title      string    `json:"title,omitempty"`
	Chapter    string    `json:"chapter,omitempty"`
	PageNumber string    `json:"page_number,omitempty"`
	SourceURL  string    `json:"source_url,omitempty"`
}

// JobResponse is the API response for job creation/status.
type JobResponse struct {
	JobID       string `json:"job_id"`
	Status      string `json:"status"`
	DedupHit    bool   `json:"dedup_hit,omitempty"`
	Cached      bool   `json:"cached,omitempty"`
	ImageURL    string `json:"image_url,omitempty"`
	MetaURL     string `json:"meta_url,omitempty"`
	SourceHash  string `json:"source_hash,omitempty"`
	ContentHash string `json:"content_hash,omitempty"`
	RenderHash  string `json:"render_hash,omitempty"`
}

// JobStatusResponse is the API response for job status queries.
type JobStatusResponse struct {
	JobID            string `json:"job_id"`
	Status           string `json:"status"`
	Pipeline         string `json:"pipeline,omitempty"`
	Error            string `json:"error,omitempty"`
	ProcessingTimeMs int    `json:"processing_time_ms,omitempty"`
	BubbleCount      int    `json:"bubble_count,omitempty"`
	ImageURL         string `json:"image_url,omitempty"`
	MetaURL          string `json:"meta_url,omitempty"`
	SourceHash       string `json:"source_hash,omitempty"`
	ContentHash      string `json:"content_hash,omitempty"`
	RenderHash       string `json:"render_hash,omitempty"`
}

// HealthResponse is the API response for health checks.
type HealthResponse struct {
	Status        string       `json:"status"`
	Redis         string       `json:"redis"`
	QueueHigh     int64        `json:"queue_high"`
	QueueLow      int64        `json:"queue_low"`
	ActiveWorkers int          `json:"active_workers"`
	Workers       []WorkerInfo `json:"workers,omitempty"`
}

// WorkerInfo represents a single worker's health.
type WorkerInfo struct {
	Name          string `json:"name"`
	LastHeartbeat int64  `json:"last_heartbeat"`
}

// WSMessage is a WebSocket message from the client.
type WSMessage struct {
	Type   string   `json:"type"`
	JobIDs []string `json:"job_ids,omitempty"`
}

// WSNotification is a WebSocket message to the client.
type WSNotification struct {
	Type        string `json:"type"`
	JobID       string `json:"job_id"`
	Status      string `json:"status,omitempty"`
	ImageURL    string `json:"image_url,omitempty"`
	MetaURL     string `json:"meta_url,omitempty"`
	Error       string `json:"error,omitempty"`
	Stage       string `json:"stage,omitempty"`
	Detail      string `json:"detail,omitempty"`
	Percent     int    `json:"percent,omitempty"`
	Cached      bool   `json:"cached,omitempty"`
	SourceHash  string `json:"source_hash,omitempty"`
	ContentHash string `json:"content_hash,omitempty"`
	RenderHash  string `json:"render_hash,omitempty"`
}

// Valid pipeline values.
var validPipelines = map[string]bool{
	"manga_translate": true,
	"manga_furigana":  true,
	"webtoon":         true,
}

// Valid priority values.
var validPriorities = map[string]bool{
	"high": true,
	"low":  true,
}

// Valid target language values.
var validTargetLangs = map[string]bool{
	"en":    true,
	"pt-br": true,
}
