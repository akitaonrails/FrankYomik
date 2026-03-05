package main

import (
	"net/http"
	"strings"
)

// AuthMiddleware validates Bearer token authentication.
func AuthMiddleware(token string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Health endpoint is public
		if r.URL.Path == "/api/v1/health" {
			next.ServeHTTP(w, r)
			return
		}

		auth := r.Header.Get("Authorization")
		if auth == "" {
			// Also check query param for WebSocket connections
			if qtoken := r.URL.Query().Get("token"); qtoken != "" {
				auth = "Bearer " + qtoken
			}
		}

		if !strings.HasPrefix(auth, "Bearer ") {
			http.Error(w, `{"error":"missing authorization"}`, http.StatusUnauthorized)
			return
		}

		providedToken := strings.TrimPrefix(auth, "Bearer ")
		if providedToken != token {
			http.Error(w, `{"error":"invalid token"}`, http.StatusUnauthorized)
			return
		}

		next.ServeHTTP(w, r)
	})
}
