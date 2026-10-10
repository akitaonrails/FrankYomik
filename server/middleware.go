package main

import (
	"crypto/subtle"
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
		if auth == "" && r.URL.Path == "/api/v1/ws" {
			// Browser WebSocket APIs cannot set Authorization headers. Keep the
			// query fallback scoped to the WebSocket route so tokens on ordinary
			// URLs are never accepted as credentials.
			if qtoken := r.URL.Query().Get("token"); qtoken != "" {
				auth = "Bearer " + qtoken
			}
		}

		if !strings.HasPrefix(auth, "Bearer ") {
			http.Error(w, `{"error":"missing authorization"}`, http.StatusUnauthorized)
			return
		}

		providedToken := strings.TrimPrefix(auth, "Bearer ")
		if subtle.ConstantTimeCompare([]byte(providedToken), []byte(token)) != 1 {
			http.Error(w, `{"error":"invalid token"}`, http.StatusUnauthorized)
			return
		}

		next.ServeHTTP(w, r)
	})
}
