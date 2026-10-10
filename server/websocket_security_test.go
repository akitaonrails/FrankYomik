package main

import (
	"fmt"
	"strings"
	"testing"
)

func TestValidateWSSubscriptionsBoundsPerConnectionState(t *testing.T) {
	current := make(map[string]struct{}, maxWSJobSubscriptions-1)
	for i := 0; i < maxWSJobSubscriptions-1; i++ {
		current[fmt.Sprintf("job-%d", i)] = struct{}{}
	}

	if err := validateWSSubscriptions(current, []string{"job-final"}); err != nil {
		t.Fatalf("valid final subscription rejected: %v", err)
	}
	if err := validateWSSubscriptions(current, []string{"job-final", "job-extra"}); err == nil {
		t.Fatal("subscription limit was not enforced")
	}
	if err := validateWSSubscriptions(current, []string{"job-1"}); err != nil {
		t.Fatalf("duplicate subscription should not consume capacity: %v", err)
	}
}

func TestValidateWSSubscriptionsRejectsUnboundedOrMalformedIDs(t *testing.T) {
	cases := [][]string{
		make([]string, maxWSJobSubscriptions+1),
		{""},
		{strings.Repeat("a", maxWSJobIDLength+1)},
		{"job/../other"},
	}
	for _, jobIDs := range cases {
		if err := validateWSSubscriptions(map[string]struct{}{}, jobIDs); err == nil {
			t.Fatalf("invalid subscription set accepted: %#v", jobIDs)
		}
	}
}

func TestValidateWSSubscriptionsAcceptsGeneratedJobIDs(t *testing.T) {
	jobIDs := []string{
		"8ce79b35-4db0-437a-a33f-dcd4577f2162",
		"cached-v2-manga_translate-acde1234",
	}
	if err := validateWSSubscriptions(map[string]struct{}{}, jobIDs); err != nil {
		t.Fatalf("valid generated ids rejected: %v", err)
	}
}
