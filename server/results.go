package main

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

type workerCapabilities struct {
	SchemaVersion    int      `json:"schema_version"`
	Pipelines        []string `json:"pipelines"`
	PipelineRevision string   `json:"pipeline_revision"`
	Models           []string `json:"models"`
	Preflight        string   `json:"preflight"`
}

func supportsAllPipelines(pipelines []string) bool {
	required := map[string]bool{
		"book_furigana":   false,
		"manga_furigana":  false,
		"manga_translate": false,
		"webtoon":         false,
	}
	if len(pipelines) != len(required) {
		return false
	}
	for _, pipeline := range pipelines {
		if _, ok := required[pipeline]; !ok || required[pipeline] {
			return false
		}
		required[pipeline] = true
	}
	for _, present := range required {
		if !present {
			return false
		}
	}
	return true
}

const (
	resultKeyPrefix    = "frank:results:"
	resultImgKeyPrefix = "frank:results:img:"
	heartbeatPrefix    = "frank:worker:"
	activeWorkerWindow = 90 * time.Second
)

// Results handles job result storage and retrieval.
type Results struct {
	rdb *redis.Client
}

// NewResults creates a new Results instance.
func NewResults(rdb *redis.Client) *Results {
	return &Results{rdb: rdb}
}

// GetJobStatus retrieves the status of a job.
func (r *Results) GetJobStatus(ctx context.Context, jobID string) (*JobStatusResponse, error) {
	key := resultKeyPrefix + jobID
	data, err := r.rdb.Get(ctx, key).Result()
	if err == redis.Nil {
		// Check if the job is still queued (image key exists)
		// If we can't find it, it's either pending or unknown
		return &JobStatusResponse{
			JobID:  jobID,
			Status: "queued",
		}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("getting result: %w", err)
	}

	var resp JobStatusResponse
	if err := json.Unmarshal([]byte(data), &resp); err != nil {
		return nil, fmt.Errorf("parsing result: %w", err)
	}
	resp.JobID = jobID

	if resp.Status == "completed" {
		resp.ImageURL = fmt.Sprintf("/api/v1/jobs/%s/image", jobID)
		cachePipeline := resp.CachePipeline
		if cachePipeline == "" {
			cachePipeline = resp.Pipeline
		}
		if resp.MetaURL == "" && cachePipeline != "" && resp.SourceHash != "" {
			resp.MetaURL = fmt.Sprintf("/api/v1/cache/by-hash/%s/%s/meta", cachePipeline, resp.SourceHash)
		}
	}

	return &resp, nil
}

// GetJobImage retrieves the processed image bytes.
func (r *Results) GetJobImage(ctx context.Context, jobID string) ([]byte, error) {
	key := resultImgKeyPrefix + jobID
	data, err := r.rdb.Get(ctx, key).Bytes()
	if err == redis.Nil {
		return nil, fmt.Errorf("image not found for job %s", jobID)
	}
	if err != nil {
		return nil, fmt.Errorf("getting image: %w", err)
	}
	return data, nil
}

// DeleteJob removes all data associated with a job.
func (r *Results) DeleteJob(ctx context.Context, jobID string) error {
	keys := []string{
		resultKeyPrefix + jobID,
		resultImgKeyPrefix + jobID,
	}
	return r.rdb.Del(ctx, keys...).Err()
}

// GetActiveWorkers returns a list of workers with recent heartbeats.
func (r *Results) GetActiveWorkers(ctx context.Context, pipelineRevision string) ([]WorkerInfo, error) {
	keys, err := r.rdb.Keys(ctx, heartbeatPrefix+"*:heartbeat").Result()
	if err != nil {
		return nil, err
	}

	now := time.Now()
	var workers []WorkerInfo

	for _, key := range keys {
		ts, err := r.rdb.Get(ctx, key).Int64()
		if err != nil {
			continue
		}
		if now.Sub(time.Unix(ts, 0)) < activeWorkerWindow {
			// Extract name: frank:worker:<name>:heartbeat
			name := key[len(heartbeatPrefix) : len(key)-len(":heartbeat")]
			worker := WorkerInfo{
				Name:          name,
				LastHeartbeat: ts,
			}
			data, err := r.rdb.Get(ctx, heartbeatPrefix+name+":capabilities").Bytes()
			if err == nil {
				var capabilities workerCapabilities
				if json.Unmarshal(data, &capabilities) == nil && capabilities.SchemaVersion == 1 {
					worker.Pipelines = capabilities.Pipelines
					worker.PipelineRevision = capabilities.PipelineRevision
					worker.Models = capabilities.Models
					worker.Preflight = capabilities.Preflight
					worker.Ready = capabilities.Preflight == "passed" &&
						capabilities.PipelineRevision == pipelineRevision &&
						supportsAllPipelines(capabilities.Pipelines)
				}
			}
			workers = append(workers, worker)
		}
	}

	return workers, nil
}
