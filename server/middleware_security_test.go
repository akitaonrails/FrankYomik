package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestAuthMiddlewareQueryTokenIsWebSocketOnly(t *testing.T) {
	handler := AuthMiddleware("secret-token", http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	ordinary := httptest.NewRequest(http.MethodGet, "/api/v1/jobs?token=secret-token", nil)
	ordinaryResult := httptest.NewRecorder()
	handler.ServeHTTP(ordinaryResult, ordinary)
	if ordinaryResult.Code != http.StatusUnauthorized {
		t.Fatalf("ordinary query token status = %d, want 401", ordinaryResult.Code)
	}

	websocket := httptest.NewRequest(http.MethodGet, "/api/v1/ws?token=secret-token", nil)
	websocketResult := httptest.NewRecorder()
	handler.ServeHTTP(websocketResult, websocket)
	if websocketResult.Code != http.StatusOK {
		t.Fatalf("WebSocket query token status = %d, want 200", websocketResult.Code)
	}
}
