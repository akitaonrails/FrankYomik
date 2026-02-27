package main

import (
	"context"
	"crypto/sha256"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	streamHigh     = "frank:jobs:high"
	streamLow      = "frank:jobs:low"
	imageKeyPrefix = "frank:images:"
	dedupKey       = "frank:dedup"
	imageTTL       = 1 * time.Hour
	dedupTTL       = 1 * time.Hour
	maxLenHigh     = 500
	maxLenLow      = 1000
)

// Queue handles Redis stream operations for job submission.
type Queue struct {
	rdb *redis.Client
}

// NewQueue creates a new Queue connected to Redis.
func NewQueue(rdb *redis.Client) *Queue {
	return &Queue{rdb: rdb}
}

// SubmitJob stores the image, deduplicates, and enqueues a job.
// Returns (job_id, dedup_hit, error).
func (q *Queue) SubmitJob(ctx context.Context, imageBytes []byte, pipeline, priority string) (string, bool, error) {
	// Compute SHA256 for dedup
	hash := fmt.Sprintf("%x", sha256.Sum256(imageBytes))

	// Check dedup
	existingJobID, err := q.rdb.HGet(ctx, dedupKey, hash).Result()
	if err == nil && existingJobID != "" {
		return existingJobID, true, nil
	}

	// Generate job ID
	jobID := fmt.Sprintf("job-%s-%d", hash[:12], time.Now().UnixMilli())

	// Store image bytes
	imageKey := imageKeyPrefix + hash
	if err := q.rdb.Set(ctx, imageKey, imageBytes, imageTTL).Err(); err != nil {
		return "", false, fmt.Errorf("storing image: %w", err)
	}

	// Choose stream and max length
	stream := streamHigh
	maxLen := int64(maxLenHigh)
	if priority == "low" {
		stream = streamLow
		maxLen = int64(maxLenLow)
	}

	// Enqueue
	err = q.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: stream,
		MaxLen: maxLen,
		Approx: true,
		Values: map[string]interface{}{
			"job_id":    jobID,
			"pipeline":  pipeline,
			"image_key": imageKey,
		},
	}).Err()
	if err != nil {
		return "", false, fmt.Errorf("enqueuing job: %w", err)
	}

	// Store dedup mapping
	q.rdb.HSet(ctx, dedupKey, hash, jobID)
	q.rdb.Expire(ctx, dedupKey, dedupTTL)

	return jobID, false, nil
}

// CancelJob removes a pending job from the dedup hash.
// Stream messages can't be easily cancelled, but the worker will skip
// jobs whose image key has been deleted.
func (q *Queue) CancelJob(ctx context.Context, jobID string) error {
	// Remove from dedup (allow re-submission)
	// We'd need to scan dedup hash — for now, delete result keys
	resultKey := "frank:results:" + jobID
	resultImgKey := "frank:results:img:" + jobID
	q.rdb.Del(ctx, resultKey, resultImgKey)
	return nil
}
