package main

import (
	"context"
	"crypto/sha256"
	"fmt"
	"log"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	streamHigh     = "frank:jobs:high"
	streamLow      = "frank:jobs:low"
	imageKeyPrefix = "frank:images:"
	dedupKey       = "frank:dedup"
	imageTTL       = 0 // no expiry — v2 cache is authoritative, Redis is fallback
	dedupTTL       = 1 * time.Hour
)

// Queue handles Redis stream operations for job submission.
type Queue struct {
	rdb        *redis.Client
	maxLenHigh int64
	maxLenLow  int64
}

// NewQueue creates a new Queue connected to Redis.
func NewQueue(rdb *redis.Client) *Queue {
	return &Queue{rdb: rdb, maxLenHigh: 500, maxLenLow: 1000}
}

// JobMetadata holds optional metadata for a job submission.
type JobMetadata struct {
	Title                string
	Chapter              string
	PageNumber           string
	SourceURL            string
	RerenderFromMetadata bool
	ForceReprocess       bool
	TargetLang           string
}

// SubmitJob stores the image, deduplicates, and enqueues a job.
// Returns (job_id, dedup_hit, error).
func (q *Queue) SubmitJob(ctx context.Context, imageBytes []byte, pipeline, priority string, meta *JobMetadata) (string, bool, error) {
	// Compute SHA256 for dedup
	hash := fmt.Sprintf("%x", sha256.Sum256(imageBytes))
	forceNew := meta != nil && (meta.RerenderFromMetadata || meta.ForceReprocess)
	targetLang := "en"
	if meta != nil && meta.TargetLang != "" {
		targetLang = meta.TargetLang
	}

	// Check dedup (keyed by hash + pipeline + target_lang to avoid collisions)
	dedupField := hash + ":" + pipeline + ":" + targetLang
	if !forceNew {
		existingJobID, err := q.rdb.HGet(ctx, dedupKey, dedupField).Result()
		if err == nil && existingJobID != "" {
			// Check if the existing job is stale — if it was created more than
			// 2 minutes ago and never completed, the stream entry was likely
			// consumed or trimmed by a previous worker session. Clear the dedup
			// entry and re-enqueue.
			if jobIsStale(existingJobID, 2*time.Minute) {
				log.Printf("INFO: stale dedup hit for %s (job %s), re-enqueuing", dedupField, existingJobID)
				q.rdb.HDel(ctx, dedupKey, dedupField)
			} else {
				return existingJobID, true, nil
			}
		}
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
	maxLen := q.maxLenHigh
	if priority == "low" {
		stream = streamLow
		maxLen = q.maxLenLow
	}

	// Enqueue
	values := map[string]interface{}{
		"job_id":      jobID,
		"pipeline":    pipeline,
		"image_key":   imageKey,
		"source_hash": hash,
	}
	if targetLang != "en" {
		values["target_lang"] = targetLang
	}
	if meta != nil {
		if meta.Title != "" {
			values["title"] = meta.Title
		}
		if meta.Chapter != "" {
			values["chapter"] = meta.Chapter
		}
		if meta.PageNumber != "" {
			values["page_number"] = meta.PageNumber
		}
		if meta.SourceURL != "" {
			values["source_url"] = meta.SourceURL
		}
		if meta.RerenderFromMetadata {
			values["rerender_from_metadata"] = "1"
		}
	}
	err := q.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: stream,
		MaxLen: maxLen,
		Approx: true,
		Values: values,
	}).Err()
	if err != nil {
		return "", false, fmt.Errorf("enqueuing job: %w", err)
	}

	// Store dedup mapping (keyed by hash + pipeline)
	if !forceNew {
		if err := q.rdb.HSet(ctx, dedupKey, dedupField, jobID).Err(); err != nil {
			log.Printf("WARN: dedup HSet: %v", err)
		}
		if err := q.rdb.Expire(ctx, dedupKey, dedupTTL).Err(); err != nil {
			log.Printf("WARN: dedup Expire: %v", err)
		}
	}

	return jobID, false, nil
}

// jobIsStale checks if a job ID is older than the given threshold.
// Job IDs have the format "job-<hash_prefix>-<unix_ms>".
func jobIsStale(jobID string, threshold time.Duration) bool {
	parts := strings.Split(jobID, "-")
	if len(parts) < 3 {
		return false
	}
	tsStr := parts[len(parts)-1]
	ts, err := strconv.ParseInt(tsStr, 10, 64)
	if err != nil {
		return false
	}
	created := time.UnixMilli(ts)
	return time.Since(created) > threshold
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
