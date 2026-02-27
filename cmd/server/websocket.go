package main

import (
	"encoding/json"
	"log"
	"net/http"
	"time"

	"github.com/gorilla/websocket"
)

var upgrader = websocket.Upgrader{
	ReadBufferSize:  1024,
	WriteBufferSize: 1024,
	CheckOrigin: func(r *http.Request) bool {
		return true // Auth is handled by middleware
	},
}

const (
	writeWait  = 10 * time.Second
	pongWait   = 60 * time.Second
	pingPeriod = 50 * time.Second // Must be less than pongWait
)

// handleWebSocket upgrades to WebSocket and manages subscriptions.
func (s *Server) handleWebSocket(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("WebSocket upgrade error: %v", err)
		return
	}
	defer conn.Close()

	notifCh := make(chan WSNotification, 64)
	defer s.unsubscribe(notifCh)

	// Write pump: sends notifications and pings
	done := make(chan struct{})
	go func() {
		defer close(done)
		ticker := time.NewTicker(pingPeriod)
		defer ticker.Stop()

		for {
			select {
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
			for _, jobID := range msg.JobIDs {
				s.subscribe(jobID, notifCh)
				log.Printf("WS subscribed to job %s", jobID)
			}
		case "unsubscribe":
			// Unsubscribe specific jobs
			s.mu.Lock()
			for _, jobID := range msg.JobIDs {
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

	// Wait for write pump to finish
	<-done
}
