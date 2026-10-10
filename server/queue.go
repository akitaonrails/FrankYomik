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
	streamHigh      = "frank:jobs:high"
	streamLow       = "frank:jobs:low"
	imageKeyPrefix  = "frank:images:"
	dedupKey        = "frank:dedup"
	latestKeyPrefix = "frank:latest:"
	imageTTL        = 0 // no expiry — v2 cache is authoritative, Redis is fallback
	dedupTTL        = 1 * time.Hour
	latestTTL       = 10 * time.Minute

	// Dedup can point at jobs whose Redis result or stream entry is gone.  Use a
	// shorter threshold after a completed-result lookup miss and a longer one for
	// queued duplicate submits, where the original worker may simply be slow.
	completedDedupStaleThreshold = 60 * time.Second
	queuedDedupStaleThreshold    = 2 * time.Minute
)

// Queue handles Redis stream operations for job submission.
type Queue struct {
	rdb              *redis.Client
	maxLenHigh       int64
	maxLenLow        int64
	pipelineRevision string
}

// NewQueue creates a new Queue connected to Redis.
func NewQueue(rdb *redis.Client) *Queue {
	return NewQueueWithRevision(rdb, configuredPipelineRevision())
}

// NewQueueWithRevision creates a queue whose deduplication and messages are
// scoped to one pipeline implementation.
func NewQueueWithRevision(rdb *redis.Client, pipelineRevision string) *Queue {
	if !pipelineRevisionRe.MatchString(pipelineRevision) {
		panic(fmt.Sprintf("invalid pipeline revision %q", pipelineRevision))
	}
	return &Queue{rdb: rdb, maxLenHigh: 500, maxLenLow: 1000, pipelineRevision: pipelineRevision}
}

// JobMetadata holds optional metadata for a job submission.
type JobMetadata struct {
	Title                string
	Chapter              string
	PageNumber           string
	SourceURL            string
	SourceSite           string
	LatestGroup          string
	LatestToken          string
	LatestSeq            string
	RerenderFromMetadata bool
	ForceReprocess       bool
	TargetLang           string
}

// SubmitJob stores the image, deduplicates, and enqueues a job.
// Returns (job_id, dedup_hit, error).
func (q *Queue) SubmitJob(ctx context.Context, imageBytes []byte, pipeline, priority string, meta *JobMetadata) (string, bool, error) {
	// Compute SHA256 for dedup
	hash := fmt.Sprintf("%x", sha256.Sum256(imageBytes))
	latest := latestMarkerFor(priority, meta)
	forceNew := (meta != nil && (meta.RerenderFromMetadata || meta.ForceReprocess)) || latest.ok
	targetLang := "en"
	if meta != nil && meta.TargetLang != "" {
		targetLang = meta.TargetLang
	}

	// Include the pipeline revision so a deployment never deduplicates against
	// work produced by an older implementation.
	dedupField := hash + ":" + pipeline + ":" + targetLang + ":" + q.pipelineRevision
	if !forceNew {
		existingJobID, err := q.rdb.HGet(ctx, dedupKey, dedupField).Result()
		if err == nil && existingJobID != "" {
			// Even when dedup returns an existing queued job, update the interactive
			// latest marker so older high-priority Kindle jobs can be demoted.
			q.updateLatestMarker(ctx, priority, meta)
			// Check if the existing job is stale — if it was created more than
			// 2 minutes ago and never completed, the stream entry was likely
			// consumed or trimmed by a previous worker session. Clear the dedup
			// entry and re-enqueue.
			if jobIsStale(existingJobID, queuedDedupStaleThreshold) {
				log.Printf("INFO: stale dedup hit for %s (job %s), re-enqueuing", dedupField, existingJobID)
				q.rdb.HDel(ctx, dedupKey, dedupField)
			} else {
				return existingJobID, true, nil
			}
		}
	}

	// Include a short revision digest so rolling deployments cannot write two
	// revisions into the same result key when submissions share a millisecond.
	identityHash := sha256.Sum256([]byte(q.pipelineRevision + "\x00" + targetLang))
	jobID := fmt.Sprintf("job-%s-%x-%d", hash[:12], identityHash[:4], time.Now().UnixMilli())

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
		"job_id":            jobID,
		"pipeline":          pipeline,
		"pipeline_revision": q.pipelineRevision,
		"image_key":         imageKey,
		"source_hash":       hash,
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
		if latest.ok {
			values["source_site"] = "kindle"
			values["latest_group"] = latest.group
			values["latest_token"] = latest.token
			values["latest_seq"] = strconv.FormatInt(latest.seq, 10)
			values["latest_key"] = latest.key
		}
		if meta.RerenderFromMetadata {
			values["rerender_from_metadata"] = "1"
		}
	}
	addArgs := &redis.XAddArgs{
		Stream: stream,
		MaxLen: maxLen,
		Approx: true,
		Values: values,
	}

	var err error
	if latest.ok {
		pipe := q.rdb.TxPipeline()
		pipe.Eval(ctx, latestMarkerUpdateLua, []string{latest.key}, latest.seq, latest.token, int64(latestTTL/time.Millisecond))
		pipe.XAdd(ctx, addArgs)
		_, err = pipe.Exec(ctx)
	} else {
		err = q.rdb.XAdd(ctx, addArgs).Err()
	}
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

type latestMarker struct {
	key   string
	group string
	token string
	seq   int64
	ok    bool
}

const latestMarkerUpdateLua = `
local current = redis.call("GET", KEYS[1])
local next_seq = tonumber(ARGV[1])
if current then
  local sep = string.find(current, "\n", 1, true)
  local current_seq = tonumber(sep and string.sub(current, 1, sep - 1) or current)
  if current_seq and current_seq > next_seq then
    return 0
  end
end
redis.call("SET", KEYS[1], ARGV[1] .. "\n" .. ARGV[2], "PX", ARGV[3])
return 1
`

func latestMarkerFor(priority string, meta *JobMetadata) latestMarker {
	if priority != "high" || meta == nil || strings.ToLower(strings.TrimSpace(meta.SourceSite)) != "kindle" {
		return latestMarker{}
	}
	group := cleanOpaqueField(meta.LatestGroup, 160)
	token := cleanOpaqueField(meta.LatestToken, 160)
	seq, err := strconv.ParseInt(strings.TrimSpace(meta.LatestSeq), 10, 64)
	if group == "" || token == "" || err != nil || seq <= 0 {
		return latestMarker{}
	}
	return latestMarker{key: latestKeyForGroup(group), group: group, token: token, seq: seq, ok: true}
}

func (q *Queue) updateLatestMarker(ctx context.Context, priority string, meta *JobMetadata) {
	latest := latestMarkerFor(priority, meta)
	if !latest.ok {
		return
	}
	if err := q.updateLatestMarkerAtomic(ctx, latest); err != nil {
		log.Printf("WARN: latest marker SET: %v", err)
	}
}

func (q *Queue) updateLatestMarkerAtomic(ctx context.Context, latest latestMarker) error {
	return q.rdb.Eval(ctx, latestMarkerUpdateLua, []string{latest.key}, latest.seq, latest.token, int64(latestTTL/time.Millisecond)).Err()
}

func latestKeyForGroup(group string) string {
	sum := sha256.Sum256([]byte(group))
	return fmt.Sprintf("%s%x", latestKeyPrefix, sum)
}

func cleanOpaqueField(value string, maxBytes int) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	if len(value) > maxBytes {
		value = value[:maxBytes]
	}
	for _, r := range value {
		if r < 32 || r == 127 {
			return ""
		}
	}
	return value
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

// CancelJob removes transient result keys for a job.
// Stream messages and dedup entries are intentionally left alone; real
// cancellation would require scanning Redis Streams and the dedup hash.
func (q *Queue) CancelJob(ctx context.Context, jobID string) error {
	resultKey := "frank:results:" + jobID
	resultImgKey := "frank:results:img:" + jobID
	q.rdb.Del(ctx, resultKey, resultImgKey)
	return nil
}
