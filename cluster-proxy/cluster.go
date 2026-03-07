package main

import (
	"encoding/base64"
	"fmt"
	"log"
	"net"
	"sync"
	"sync/atomic"
	"time"
)

// ServerConn represents a connection to one cluster server
type ServerConn struct {
	addr       *net.UDPAddr
	conn       *net.UDPConn
	alive      atomic.Bool
	missedPong atomic.Int32
	lastPong   time.Time
	mu         sync.Mutex
}

// ClusterClient manages connections to upstream cluster-native servers
type ClusterClient struct {
	servers      []*ServerConn
	primary      atomic.Int32 // index into servers
	passphrase   string
	repeaterID   uint32
	token        *Token
	tokenMu      sync.RWMutex
	pingInterval time.Duration
	pingTimeout  int

	// Subscription
	slot1TGs []uint32
	slot2TGs []uint32
	subMu    sync.RWMutex

	// Dedup: stream_id -> last seen time
	seenStreams sync.Map

	// Callback for inbound DMRD from cluster
	onDMRD func(dmrdPayload []byte)

	// Cluster health
	health     *PongHealth
	healthMu   sync.RWMutex

	debug  bool
	stopCh chan struct{}
}

func NewClusterClient(cfg *Config) (*ClusterClient, error) {
	cc := &ClusterClient{
		passphrase:   cfg.Cluster.Passphrase,
		repeaterID:   cfg.Local.RepeaterID,
		pingInterval: time.Duration(cfg.Cluster.PingInterval) * time.Second,
		pingTimeout:  cfg.Cluster.PingTimeout,
		slot1TGs:     cfg.Subscription.Slot1,
		slot2TGs:     cfg.Subscription.Slot2,
		debug:        cfg.LogLevel == "debug",
		stopCh:       make(chan struct{}),
	}

	for _, sc := range cfg.Cluster.Servers {
		addr, err := net.ResolveUDPAddr("udp", fmt.Sprintf("%s:%d", sc.Address, sc.Port))
		if err != nil {
			return nil, fmt.Errorf("resolve %s:%d: %w", sc.Address, sc.Port, err)
		}
		conn, err := net.DialUDP("udp", nil, addr)
		if err != nil {
			return nil, fmt.Errorf("dial %s:%d: %w", sc.Address, sc.Port, err)
		}
		conn.SetReadBuffer(1 << 20)
		srv := &ServerConn{addr: addr, conn: conn}
		cc.servers = append(cc.servers, srv)
	}

	cc.primary.Store(0)
	return cc, nil
}

func (cc *ClusterClient) SetDMRDCallback(fn func(dmrdPayload []byte)) {
	cc.onDMRD = fn
}

// UpdateSubscription changes the talkgroup subscription and re-subscribes
func (cc *ClusterClient) UpdateSubscription(slot1, slot2 []uint32) {
	cc.subMu.Lock()
	cc.slot1TGs = slot1
	cc.slot2TGs = slot2
	cc.subMu.Unlock()
	cc.sendSubscribe()
}

// SendDMRD forwards a DMRD payload to the primary cluster server
// dmrdPayload is everything after the "DMRD" prefix (51 bytes typical)
func (cc *ClusterClient) SendDMRD(dmrdPayload []byte) {
	cc.tokenMu.RLock()
	t := cc.token
	cc.tokenMu.RUnlock()
	if t == nil {
		return
	}

	pkt := BuildData(t.TokenHash(), dmrdPayload)
	pri := int(cc.primary.Load())
	if pri < len(cc.servers) {
		cc.servers[pri].conn.Write(pkt)
	}
}

// Run starts auth, receive loops, and keepalive (blocking)
func (cc *ClusterClient) Run() {
	// Authenticate to all servers
	for i, srv := range cc.servers {
		if err := cc.authenticate(i, srv); err != nil {
			log.Printf("[CN] auth failed to server %d (%s): %v", i, srv.addr, err)
		} else {
			srv.alive.Store(true)
			log.Printf("[CN] authenticated to server %d (%s)", i, srv.addr)
		}
	}

	// Send initial subscription
	cc.sendSubscribe()

	// Start receive loops for all servers
	for i := range cc.servers {
		go cc.receiveLoop(i)
	}

	// Keepalive + dedup cleanup loop (blocking)
	cc.keepaliveLoop()
}

func (cc *ClusterClient) Close() {
	// Send disconnect to all servers
	cc.tokenMu.RLock()
	t := cc.token
	cc.tokenMu.RUnlock()
	if t != nil {
		disc := BuildDisconnect(t.TokenHash())
		for _, srv := range cc.servers {
			srv.conn.Write(disc)
		}
	}

	close(cc.stopCh)
	for _, srv := range cc.servers {
		srv.conn.Close()
	}
}

func (cc *ClusterClient) authenticate(idx int, srv *ServerConn) error {
	authReq := BuildAuthReq(cc.repeaterID, cc.passphrase)

	srv.conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
	if _, err := srv.conn.Write(authReq); err != nil {
		return fmt.Errorf("send AUTH: %w", err)
	}

	// Wait for AUTH_ACK or AUTH_NAK
	buf := make([]byte, 2048) // tokens are variable-length JSON
	srv.conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	n, err := srv.conn.Read(buf)
	if err != nil {
		return fmt.Errorf("read AUTH response: %w", err)
	}

	if n < NativeHeaderSize {
		return fmt.Errorf("response too short (%d bytes)", n)
	}

	// Check for NAK
	if MatchSubCmd(buf[:n], CmdAuthNak) {
		reason := ""
		if n > NativeHeaderSize {
			reason = string(buf[NativeHeaderSize:n])
		}
		return fmt.Errorf("auth rejected: %s", reason)
	}

	// Expect AUTH_ACK
	if !MatchSubCmd(buf[:n], CmdAuthAck) {
		return fmt.Errorf("unexpected response: %s", string(buf[4:8]))
	}

	// Decode token from bytes after CLNT+AACK header
	tokenData := buf[NativeHeaderSize:n]
	token, err := DecodeToken(tokenData)
	if err != nil {
		return fmt.Errorf("decode token: %w", err)
	}

	if token.IsExpired() {
		return fmt.Errorf("received expired token")
	}

	cc.tokenMu.Lock()
	cc.token = token
	cc.tokenMu.Unlock()

	// Clear deadlines for normal operation
	srv.conn.SetReadDeadline(time.Time{})
	srv.conn.SetWriteDeadline(time.Time{})

	srv.mu.Lock()
	srv.lastPong = time.Now()
	srv.mu.Unlock()

	log.Printf("[CN] token received: repeater=%d expires_in=%.0fs cluster=%s",
		token.Payload.RepeaterID,
		token.Payload.ExpiresAt-float64(time.Now().Unix()),
		token.Payload.ClusterID)

	return nil
}

func (cc *ClusterClient) sendSubscribe() {
	cc.tokenMu.RLock()
	t := cc.token
	cc.tokenMu.RUnlock()
	if t == nil {
		return
	}

	cc.subMu.RLock()
	pkt := BuildSubscribe(t.TokenHash(), cc.slot1TGs, cc.slot2TGs)
	cc.subMu.RUnlock()

	for _, srv := range cc.servers {
		if srv.alive.Load() {
			srv.conn.Write(pkt)
		}
	}
	log.Printf("[CN] subscription sent")
}

func (cc *ClusterClient) receiveLoop(idx int) {
	srv := cc.servers[idx]
	buf := make([]byte, 2048)

	for {
		select {
		case <-cc.stopCh:
			return
		default:
		}

		srv.conn.SetReadDeadline(time.Now().Add(cc.pingInterval * 2))
		n, err := srv.conn.Read(buf)
		if err != nil {
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				continue
			}
			select {
			case <-cc.stopCh:
				return
			default:
				log.Printf("[CN] server %d read error: %v", idx, err)
				continue
			}
		}

		if n < NativeHeaderSize {
			continue
		}

		data := buf[:n]

		switch {
		case MatchSubCmd(data, CmdData):
			cc.handleInboundDMRD(idx, data)
		case MatchSubCmd(data, CmdPong):
			cc.handlePong(idx, data)
		case MatchSubCmd(data, CmdSubAck):
			if cc.debug {
				log.Printf("[CN] subscription ACK from server %d", idx)
			}
		case MatchSubCmd(data, CmdAuthNak):
			log.Printf("[CN] server %d sent AUTH_NAK — re-authenticating", idx)
			go func() {
				if err := cc.authenticate(idx, srv); err != nil {
					log.Printf("[CN] re-auth failed: %v", err)
				} else {
					cc.sendSubscribe()
				}
			}()
		}
	}
}

func (cc *ClusterClient) handleInboundDMRD(serverIdx int, data []byte) {
	dmrdPayload := ExtractDataPayload(data)
	if dmrdPayload == nil || len(dmrdPayload) < 16 {
		return
	}

	// Dedup by stream_id (offset 12-15 in DMRD payload: seq(1)+src(3)+dst(3)+rptr(4)+bits(1)=12, then stream_id(4))
	if len(cc.servers) > 1 && len(dmrdPayload) >= 16 {
		streamID := uint32(dmrdPayload[12])<<24 | uint32(dmrdPayload[13])<<16 |
			uint32(dmrdPayload[14])<<8 | uint32(dmrdPayload[15])

		now := time.Now()
		if prev, loaded := cc.seenStreams.LoadOrStore(streamID, now); loaded {
			prevTime := prev.(time.Time)
			if now.Sub(prevTime) < 200*time.Millisecond {
				return // duplicate
			}
			cc.seenStreams.Store(streamID, now)
		}
	}

	if cc.onDMRD != nil {
		cc.onDMRD(dmrdPayload)
	}
}

func (cc *ClusterClient) handlePong(serverIdx int, data []byte) {
	health, err := ParsePong(data)
	if err != nil {
		if cc.debug {
			log.Printf("[CN] pong parse error from server %d: %v", serverIdx, err)
		}
		return
	}

	srv := cc.servers[serverIdx]
	srv.missedPong.Store(0)
	srv.mu.Lock()
	srv.lastPong = time.Now()
	srv.mu.Unlock()
	srv.alive.Store(true)

	if health != nil {
		cc.healthMu.Lock()
		cc.health = health
		cc.healthMu.Unlock()

		// Token refresh: server piggybacks a new token when current is near expiry
		if health.NewToken != "" {
			tokenBytes, err := base64.StdEncoding.DecodeString(health.NewToken)
			if err != nil {
				log.Printf("[CN] token refresh base64 error: %v", err)
			} else {
				newToken, err := DecodeToken(tokenBytes)
				if err != nil {
					log.Printf("[CN] token refresh decode error: %v", err)
				} else if !newToken.IsExpired() {
					cc.tokenMu.Lock()
					cc.token = newToken
					cc.tokenMu.Unlock()
					log.Printf("[CN] token refreshed from server %d, expires_in=%.0fs",
						serverIdx, newToken.Payload.ExpiresAt-float64(time.Now().Unix()))
				}
			}
		}

		// Graceful drain: server is shutting down, failover to another
		if health.Redirect {
			log.Printf("[CN] server %d requesting redirect (draining)", serverIdx)
			srv.alive.Store(false)
			cc.tryFailover(serverIdx)
		}
	}
}

func (cc *ClusterClient) keepaliveLoop() {
	ticker := time.NewTicker(cc.pingInterval)
	defer ticker.Stop()

	cleanupTicker := time.NewTicker(30 * time.Second)
	defer cleanupTicker.Stop()

	for {
		select {
		case <-cc.stopCh:
			return

		case <-ticker.C:
			cc.tokenMu.RLock()
			t := cc.token
			cc.tokenMu.RUnlock()
			if t == nil {
				continue
			}

			ping := BuildPing(t.TokenHash())

			for i, srv := range cc.servers {
				if !srv.alive.Load() {
					// Try to recover dead non-primary servers
					go func(idx int, s *ServerConn) {
						if err := cc.authenticate(idx, s); err != nil {
							if cc.debug {
								log.Printf("[CN] server %d recovery failed: %v", idx, err)
							}
							return
						}
						s.alive.Store(true)
						s.missedPong.Store(0)
						log.Printf("[CN] server %d recovered", idx)
						cc.sendSubscribe()
					}(i, srv)
					continue
				}

				if _, err := srv.conn.Write(ping); err != nil {
					log.Printf("[CN] ping send failed to server %d: %v", i, err)
				}

				missed := srv.missedPong.Add(1)
				if int(missed) > cc.pingTimeout {
					if srv.alive.Load() {
						log.Printf("[CN] server %d dead (%d missed pongs)", i, missed)
						srv.alive.Store(false)
						cc.tryFailover(i)
					}
				} else if missed > 1 {
					log.Printf("[CN] server %d: %d missed pong(s)", i, missed)
				}
			}

		case <-cleanupTicker.C:
			cutoff := time.Now().Add(-5 * time.Second)
			cc.seenStreams.Range(func(key, value any) bool {
				if value.(time.Time).Before(cutoff) {
					cc.seenStreams.Delete(key)
				}
				return true
			})
		}
	}
}

func (cc *ClusterClient) tryFailover(deadIdx int) {
	pri := int(cc.primary.Load())
	if deadIdx != pri {
		return
	}

	// Find first alive server
	for i, srv := range cc.servers {
		if i != deadIdx && srv.alive.Load() {
			cc.primary.Store(int32(i))
			log.Printf("[CN] failover: primary → server %d (%s)", i, srv.addr)
			go func(idx int, s *ServerConn) {
				if err := cc.authenticate(idx, s); err != nil {
					log.Printf("[CN] re-auth to server %d failed: %v", idx, err)
				} else {
					cc.sendSubscribe()
				}
			}(i, srv)
			return
		}
	}

	log.Printf("[CN] CRITICAL: no alive servers")

	// Reconnect loop
	go func() {
		for attempt := 1; ; attempt++ {
			select {
			case <-cc.stopCh:
				return
			default:
			}
			time.Sleep(5 * time.Second)
			log.Printf("[CN] reconnect attempt %d to server %d", attempt, deadIdx)

			if err := cc.authenticate(deadIdx, cc.servers[deadIdx]); err != nil {
				log.Printf("[CN] reconnect failed: %v", err)
				continue
			}

			cc.servers[deadIdx].alive.Store(true)
			cc.servers[deadIdx].missedPong.Store(0)
			cc.primary.Store(int32(deadIdx))
			cc.sendSubscribe()
			log.Printf("[CN] reconnected to server %d", deadIdx)
			return
		}
	}()
}
