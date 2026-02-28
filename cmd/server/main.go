package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"
)

func main() {
	// Configuration from environment
	addr := getEnv("LISTEN_ADDR", ":8080")
	redisURL := getEnv("REDIS_URL", "redis://localhost:6379")
	authToken := getEnv("AUTH_TOKEN", "")
	cacheDir := getEnv("CACHE_DIR", "./cache")

	if authToken == "" {
		log.Fatal("AUTH_TOKEN environment variable is required")
	}

	// Redis connection
	opt, err := redis.ParseURL(redisURL)
	if err != nil {
		log.Fatalf("Invalid REDIS_URL: %v", err)
	}
	rdb := redis.NewClient(opt)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	if err := rdb.Ping(ctx).Err(); err != nil {
		log.Fatalf("Cannot connect to Redis: %v", err)
	}
	log.Printf("Connected to Redis: %s", redisURL)

	// Server
	server := NewServer(rdb, cacheDir)
	absCacheDir, _ := filepath.Abs(cacheDir)
	log.Printf("Cache directory: %s (absolute: %s)", cacheDir, absCacheDir)

	// Start Redis Pub/Sub subscriber for WebSocket notifications
	go server.StartRedisSubscriber(ctx)

	// Routes
	mux := http.NewServeMux()
	server.RegisterRoutes(mux)

	// Apply auth middleware
	handler := AuthMiddleware(authToken, mux)

	httpServer := &http.Server{
		Addr:         addr,
		Handler:      handler,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 60 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	// Graceful shutdown
	go func() {
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		<-sigCh

		log.Println("Shutting down...")
		cancel()

		shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer shutdownCancel()

		if err := httpServer.Shutdown(shutdownCtx); err != nil {
			log.Printf("HTTP shutdown error: %v", err)
		}
	}()

	log.Printf("Server listening on %s", addr)
	if err := httpServer.ListenAndServe(); err != http.ErrServerClosed {
		log.Fatalf("Server error: %v", err)
	}
	log.Println("Server stopped")
}

func getEnv(key, defaultVal string) string {
	if val := os.Getenv(key); val != "" {
		return val
	}
	return defaultVal
}
