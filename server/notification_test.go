package main

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

// completedMeta is what the worker stores for a finished job.
func completedMeta() map[string]any {
	return map[string]any{
		"job_id":             "job-1",
		"status":             "completed",
		"pipeline":           "manga_furigana",
		"source_hash":        "abc123",
		"content_hash":       "def456",
		"render_hash":        "789xyz",
		"page_kind":          "prose",
		"bubble_count":       2,
		"processing_time_ms": 1500,
	}
}

// A client either polls or listens, and picks for reasons of its own. A fact
// carried by only one path is worse than a missing one: it works in testing
// and vanishes in use, which is exactly how page_kind was first shipped.
func TestBothDeliveryPathsCarryTheSameFacts(t *testing.T) {
	meta := completedMeta()

	notif := completionNotification("job-1", meta)

	raw, err := json.Marshal(meta)
	if err != nil {
		t.Fatalf("marshalling meta: %v", err)
	}
	var polled JobStatusResponse
	if err := json.Unmarshal(raw, &polled); err != nil {
		t.Fatalf("parsing meta as a status response: %v", err)
	}

	for _, field := range []struct {
		name   string
		pushed string
		polled string
	}{
		{"page_kind", notif.PageKind, polled.PageKind},
		{"source_hash", notif.SourceHash, polled.SourceHash},
		{"content_hash", notif.ContentHash, polled.ContentHash},
		{"render_hash", notif.RenderHash, polled.RenderHash},
		{"status", notif.Status, polled.Status},
	} {
		if field.pushed != field.polled {
			t.Errorf("%s differs between paths: pushed %q, polled %q",
				field.name, field.pushed, field.polled)
		}
		if field.pushed == "" {
			t.Errorf("%s is empty on both paths; the worker reported it", field.name)
		}
	}
}

// The two responses are separate structs, so a field added to one is silently
// absent from the other. This names the fields that must exist in both.
func TestNotificationDeclaresEveryResultField(t *testing.T) {
	shared := []string{"source_hash", "content_hash", "render_hash", "page_kind", "status"}

	notif := jsonTags(reflect.TypeOf(WSNotification{}))
	status := jsonTags(reflect.TypeOf(JobStatusResponse{}))

	for _, field := range shared {
		if !notif[field] {
			t.Errorf("WSNotification is missing %q; clients that listen would never see it", field)
		}
		if !status[field] {
			t.Errorf("JobStatusResponse is missing %q; clients that poll would never see it", field)
		}
	}
}

func TestFailedJobsStillReportWhatWasSeen(t *testing.T) {
	meta := map[string]any{
		"status":    "failed",
		"error":     "page does not look like typeset prose",
		"page_kind": "artwork",
	}

	notif := completionNotification("job-2", meta)

	if notif.Status != "failed" || notif.Error == "" {
		t.Fatalf("failure not reported: %+v", notif)
	}
	// The verdict is how a client corrects a book on the wrong pipeline, and a
	// refusal is exactly when it needs it.
	if notif.PageKind != "artwork" {
		t.Errorf("page_kind dropped on failure: %q", notif.PageKind)
	}
	if notif.ImageURL != "" {
		t.Errorf("failed job should not advertise an image: %q", notif.ImageURL)
	}
}

func jsonTags(t reflect.Type) map[string]bool {
	tags := map[string]bool{}
	for i := 0; i < t.NumField(); i++ {
		name := strings.Split(t.Field(i).Tag.Get("json"), ",")[0]
		if name != "" && name != "-" {
			tags[name] = true
		}
	}
	return tags
}
