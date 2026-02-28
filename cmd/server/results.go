package main

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	resultKeyPrefix    = "frank:results:"
	resultImgKeyPrefix = "frank:results:img:"
	heartbeatPrefix    = "frank:worker:"
	resultTTL          = 1 * time.Hour
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
		if resp.MetaURL == "" && resp.Pipeline != "" && resp.SourceHash != "" {
			resp.MetaURL = fmt.Sprintf("/api/v1/cache/by-hash/%s/%s/meta", resp.Pipeline, resp.SourceHash)
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
func (r *Results) GetActiveWorkers(ctx context.Context) ([]WorkerInfo, error) {
	keys, err := r.rdb.Keys(ctx, heartbeatPrefix+"*:heartbeat").Result()
	if err != nil {
		return nil, err
	}

	now := time.Now().Unix()
	var workers []WorkerInfo

	for _, key := range keys {
		ts, err := r.rdb.Get(ctx, key).Int64()
		if err != nil {
			continue
		}
		if now-ts < 90 {
			// Extract name: frank:worker:<name>:heartbeat
			name := key[len(heartbeatPrefix) : len(key)-len(":heartbeat")]
			workers = append(workers, WorkerInfo{
				Name:          name,
				LastHeartbeat: ts,
			})
		}
	}

	return workers, nil
}
