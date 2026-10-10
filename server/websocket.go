package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

var upgrader = websocket.Upgrader{
	ReadBufferSize:  1024,
	WriteBufferSize: 1024,
	CheckOrigin: func(r *http.Request) bool {
		// If CORS_ORIGINS is set, only allow those origins (defense-in-depth).
		// Default: accept all (auth middleware already validates the token).
		allowed := os.Getenv("CORS_ORIGINS")
		if allowed == "" {
			return true
		}
		origin := r.Header.Get("Origin")
		for _, o := range strings.Split(allowed, ",") {
			if strings.TrimSpace(o) == origin {
				return true
			}
		}
		log.Printf("WARN: WS origin %q rejected (allowed: %s)", origin, allowed)
		return false
	},
}

const (
	writeWait             = 10 * time.Second
	pongWait              = 60 * time.Second
	pingPeriod            = 50 * time.Second // Must be less than pongWait
	maxWSMessageBytes     = 64 << 10
	maxWSJobSubscriptions = 256
	maxWSJobIDLength      = 200
)

var websocketJobIDRe = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

func validateWSSubscriptions(current map[string]struct{}, jobIDs []string) error {
	if len(jobIDs) > maxWSJobSubscriptions {
		return fmt.Errorf("too many job subscriptions")
	}
	next := len(current)
	for _, jobID := range jobIDs {
		if len(jobID) == 0 || len(jobID) > maxWSJobIDLength || !websocketJobIDRe.MatchString(jobID) {
			return fmt.Errorf("invalid job id")
		}
		if _, exists := current[jobID]; !exists {
			next++
		}
	}
	if next > maxWSJobSubscriptions {
		return fmt.Errorf("too many job subscriptions")
	}
	return nil
}

// handleWebSocket upgrades to WebSocket and manages subscriptions.
func (s *Server) handleWebSocket(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("WebSocket upgrade error: %v", err)
		return
	}
	defer conn.Close()
	conn.SetReadLimit(maxWSMessageBytes)

	notifCh := make(chan WSNotification, 64)
	defer s.unsubscribe(notifCh)
	subscriptions := make(map[string]struct{})
	stopWriter := make(chan struct{})
	var stopWriterOnce sync.Once
	stopWritePump := func() { stopWriterOnce.Do(func() { close(stopWriter) }) }
	defer stopWritePump()

	// Write pump: sends notifications and pings
	done := make(chan struct{})
	go func() {
		defer close(done)
		ticker := time.NewTicker(pingPeriod)
		defer ticker.Stop()

		for {
			select {
			case <-stopWriter:
				return
			case notif, ok := <-notifCh:
				if !ok {
					return
				}
				conn.SetWriteDeadline(time.Now().Add(writeWait))
				data, err := json.Marshal(notif)
				if err != nil {
					log.Printf("WS marshal error: %v", err)
					continue
				}
				if err := conn.WriteMessage(websocket.TextMessage, data); err != nil {
					log.Printf("WS write error: %v", err)
					return
				}
			case <-ticker.C:
				conn.SetWriteDeadline(time.Now().Add(writeWait))
				if err := conn.WriteMessage(websocket.PingMessage, nil); err != nil {
					return
				}
			}
		}
	}()

	// Read pump: processes subscribe/unsubscribe messages
	conn.SetReadDeadline(time.Now().Add(pongWait))
	conn.SetPongHandler(func(string) error {
		conn.SetReadDeadline(time.Now().Add(pongWait))
		return nil
	})

	for {
		_, message, err := conn.ReadMessage()
		if err != nil {
			if websocket.IsUnexpectedCloseError(err,
				websocket.CloseGoingAway, websocket.CloseNormalClosure) {
				log.Printf("WS read error: %v", err)
			}
			break
		}

		var msg WSMessage
		if err := json.Unmarshal(message, &msg); err != nil {
			log.Printf("WS invalid message: %v", err)
			continue
		}

		switch msg.Type {
		case "subscribe":
			if err := validateWSSubscriptions(subscriptions, msg.JobIDs); err != nil {
				_ = conn.WriteControl(
					websocket.CloseMessage,
					websocket.FormatCloseMessage(websocket.ClosePolicyViolation, err.Error()),
					time.Now().Add(writeWait),
				)
				return
			}
			for _, jobID := range msg.JobIDs {
				s.subscribe(jobID, notifCh)
				subscriptions[jobID] = struct{}{}
			}
		case "unsubscribe":
			// Unsubscribe specific jobs
			s.mu.Lock()
			for _, jobID := range msg.JobIDs {
				delete(subscriptions, jobID)
				if subs, ok := s.subscribers[jobID]; ok {
					delete(subs, notifCh)
					if len(subs) == 0 {
						delete(s.subscribers, jobID)
					}
				}
			}
			s.mu.Unlock()
		}
	}

	// Stop the write pump immediately after a normal read-side disconnect.
	// Otherwise it can retain the connection and its subscription channel until
	// the next 50-second ping discovers the closed peer.
	stopWritePump()
	<-done
}
