package main

import (
	"context"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
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
	log.Printf("Connected to Redis: %s", redactURL(redisURL))

	// Configurable limits from environment
	maxImageSizeMB := getEnvInt("MAX_IMAGE_SIZE_MB", 20)
	streamMaxLenHigh := getEnvInt("STREAM_MAXLEN_HIGH", 500)
	streamMaxLenLow := getEnvInt("STREAM_MAXLEN_LOW", 1000)

	// Server
	server := NewServer(rdb, cacheDir)
	server.maxImageSize = int64(maxImageSizeMB) << 20
	server.streamMaxLenHigh = int64(streamMaxLenHigh)
	server.streamMaxLenLow = int64(streamMaxLenLow)
	server.queue.maxLenHigh = server.streamMaxLenHigh
	server.queue.maxLenLow = server.streamMaxLenLow
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

func getEnvInt(key string, defaultVal int) int {
	if val := os.Getenv(key); val != "" {
		if n, err := strconv.Atoi(val); err == nil {
			return n
		}
		log.Printf("WARN: invalid %s=%q, using default %d", key, val, defaultVal)
	}
	return defaultVal
}

// redactURL masks the password in a URL for safe logging.
func redactURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return "<invalid-url>"
	}
	if u.User != nil {
		if _, hasPw := u.User.Password(); hasPw {
			u.User = url.UserPassword(u.User.Username(), "***")
		}
	}
	return u.String()
}
