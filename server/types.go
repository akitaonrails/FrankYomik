package main

// JobResponse is the API response for job creation/status.
type JobResponse struct {
	JobID            string `json:"job_id"`
	Status           string `json:"status"`
	DedupHit         bool   `json:"dedup_hit,omitempty"`
	Cached           bool   `json:"cached,omitempty"`
	ImageURL         string `json:"image_url,omitempty"`
	MetaURL          string `json:"meta_url,omitempty"`
	SourceHash       string `json:"source_hash,omitempty"`
	ContentHash      string `json:"content_hash,omitempty"`
	RenderHash       string `json:"render_hash,omitempty"`
	PipelineRevision string `json:"pipeline_revision,omitempty"`
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
	PipelineRevision string `json:"pipeline_revision,omitempty"`
	// PageKind is "prose" or "artwork" when the worker could tell, so a client
	// can correct a book that is on the wrong pipeline.
	PageKind string `json:"page_kind,omitempty"`
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
	Type             string `json:"type"`
	JobID            string `json:"job_id"`
	Status           string `json:"status,omitempty"`
	ImageURL         string `json:"image_url,omitempty"`
	MetaURL          string `json:"meta_url,omitempty"`
	Error            string `json:"error,omitempty"`
	Stage            string `json:"stage,omitempty"`
	Detail           string `json:"detail,omitempty"`
	Percent          int    `json:"percent,omitempty"`
	Cached           bool   `json:"cached,omitempty"`
	SourceHash       string `json:"source_hash,omitempty"`
	ContentHash      string `json:"content_hash,omitempty"`
	RenderHash       string `json:"render_hash,omitempty"`
	PipelineRevision string `json:"pipeline_revision,omitempty"`
	// PageKind mirrors JobStatusResponse.PageKind: a client that listens
	// rather than polls needs the same facts.
	PageKind string `json:"page_kind,omitempty"`
}

// Valid pipeline values.
var validPipelines = map[string]bool{
	"manga_translate": true,
	"manga_furigana":  true,
	"book_furigana":   true,
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

// splitCachePipeline separates a durable cache namespace from the executable
// pipeline name and target language. English keeps the base namespace while
// other languages use <pipeline>_<target-lang>.
func splitCachePipeline(namespace string) (pipeline, targetLang string, ok bool) {
	if validPipelines[namespace] {
		return namespace, "en", true
	}
	for candidate := range validPipelines {
		for language := range validTargetLangs {
			if language != "en" && namespace == candidate+"_"+language {
				return candidate, language, true
			}
		}
	}
	return "", "", false
}
