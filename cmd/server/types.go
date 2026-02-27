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
}

// JobResponse is the API response for job creation/status.
type JobResponse struct {
	JobID    string `json:"job_id"`
	Status   string `json:"status"`
	DedupHit bool   `json:"dedup_hit,omitempty"`
}

// JobStatusResponse is the API response for job status queries.
type JobStatusResponse struct {
	JobID            string `json:"job_id"`
	Status           string `json:"status"`
	Error            string `json:"error,omitempty"`
	ProcessingTimeMs int    `json:"processing_time_ms,omitempty"`
	BubbleCount      int    `json:"bubble_count,omitempty"`
	ImageURL         string `json:"image_url,omitempty"`
}

// HealthResponse is the API response for health checks.
type HealthResponse struct {
	Status        string         `json:"status"`
	Redis         string         `json:"redis"`
	QueueHigh     int64          `json:"queue_high"`
	QueueLow      int64          `json:"queue_low"`
	ActiveWorkers int            `json:"active_workers"`
	Workers       []WorkerInfo   `json:"workers,omitempty"`
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
	Type     string `json:"type"`
	JobID    string `json:"job_id"`
	Status   string `json:"status,omitempty"`
	ImageURL string `json:"image_url,omitempty"`
	Error    string `json:"error,omitempty"`
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
