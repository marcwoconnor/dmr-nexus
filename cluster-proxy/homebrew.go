package main

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/binary"
	"fmt"
	"log"
	"net"
	"strings"
	"sync"
	"time"
)

// RepeaterState tracks a connected HomeBrew client (MMDVMHost or DMRGateway)
type RepeaterState struct {
	ID         uint32
	Addr       *net.UDPAddr
	State      int
	Salt       [4]byte
	LastPing   time.Time
	Callsign   string
	ConfigData []byte // raw RPTC/DMRC config bytes

	// Per-repeater TG filtering. nil = receive all (default for repeaters
	// that haven't sent RPTO). Empty map = receive none.
	Slot1TGs map[uint32]struct{}
	Slot2TGs map[uint32]struct{}
}

// addrKey is a compact, allocation-free key for UDP address lookup.
// 16 bytes IP (v4-in-v6) + 2 bytes port = 18 bytes.
type addrKey [18]byte

func makeAddrKey(addr *net.UDPAddr) addrKey {
	var k addrKey
	ip := addr.IP.To16()
	copy(k[:16], ip)
	k[16] = byte(addr.Port >> 8)
	k[17] = byte(addr.Port)
	return k
}

// HomebrewServer accepts HomeBrew connections from local repeaters
type HomebrewServer struct {
	conn       *net.UDPConn
	passphrase string
	repeaters  map[uint32]*RepeaterState
	addrMap    map[addrKey]uint32 // allocation-free addr -> repeater ID
	mu         sync.RWMutex
	onDMRD     func(repeaterID uint32, data []byte) // callback for DMRD packets
	onOptions  func(repeaterID uint32, options string)
	debug      bool
}

func NewHomebrewServer(address string, port int, passphrase string, debug bool) (*HomebrewServer, error) {
	addr, err := net.ResolveUDPAddr("udp", fmt.Sprintf("%s:%d", address, port))
	if err != nil {
		return nil, fmt.Errorf("resolve: %w", err)
	}
	conn, err := net.ListenUDP("udp", addr)
	if err != nil {
		return nil, fmt.Errorf("listen: %w", err)
	}
	// Set buffers for burst handling — write buffer matters at scale
	conn.SetReadBuffer(1 << 20)
	conn.SetWriteBuffer(1 << 20)

	return &HomebrewServer{
		conn:       conn,
		passphrase: passphrase,
		repeaters:  make(map[uint32]*RepeaterState),
		addrMap:    make(map[addrKey]uint32),
		debug:      debug,
	}, nil
}

func (s *HomebrewServer) SetCallbacks(onDMRD func(uint32, []byte), onOptions func(uint32, string)) {
	s.onDMRD = onDMRD
	s.onOptions = onOptions
}

// Run starts the receive loop (blocking)
func (s *HomebrewServer) Run() {
	buf := make([]byte, 512)
	for {
		n, addr, err := s.conn.ReadFromUDP(buf)
		if err != nil {
			if ne, ok := err.(*net.OpError); ok && ne.Err.Error() == "use of closed network connection" {
				return
			}
			log.Printf("[HB] read error: %v", err)
			continue
		}
		if n < 4 {
			continue
		}
		s.handlePacket(buf[:n], addr)
	}
}

// dmrdPool reuses buffers for DMRD packet assembly (hot path)
var dmrdPool = sync.Pool{
	New: func() interface{} {
		buf := make([]byte, 128) // typical DMRD is 55 bytes
		return &buf
	},
}

// SetSubscription updates per-repeater TG filter. nil slices = receive all.
func (s *HomebrewServer) SetSubscription(repeaterID uint32, slot1, slot2 []uint32) {
	s.mu.Lock()
	rpt, ok := s.repeaters[repeaterID]
	if ok {
		rpt.Slot1TGs = sliceToSet(slot1)
		rpt.Slot2TGs = sliceToSet(slot2)
	}
	s.mu.Unlock()
}

func sliceToSet(tgs []uint32) map[uint32]struct{} {
	if tgs == nil {
		return nil // nil = wildcard (all)
	}
	m := make(map[uint32]struct{}, len(tgs))
	for _, tg := range tgs {
		m[tg] = struct{}{}
	}
	return m
}

// BroadcastDMRD sends a DMRD packet to connected repeaters that are subscribed
// to the packet's talkgroup+timeslot. Assembles the packet once, takes one lock.
// dmrdPayload layout: seq(1)+src(3)+dst(3)+rptr(4)+bits(1)+stream(4)+...
func (s *HomebrewServer) BroadcastDMRD(dmrdPayload []byte) {
	if len(dmrdPayload) < 12 {
		return
	}

	// Extract TG and slot from payload for filtering
	tgid := uint32(dmrdPayload[4])<<16 | uint32(dmrdPayload[5])<<8 | uint32(dmrdPayload[6])
	slot := 1 + int(dmrdPayload[11]>>7) // bit 7: 0=slot1, 1=slot2

	needed := 4 + len(dmrdPayload)
	bufp := dmrdPool.Get().(*[]byte)
	buf := *bufp
	if cap(buf) < needed {
		buf = make([]byte, needed)
	} else {
		buf = buf[:needed]
	}
	copy(buf[0:4], HBHeaderDMRD)
	copy(buf[4:], dmrdPayload)

	s.mu.RLock()
	for _, rpt := range s.repeaters {
		if rpt.State != StateConnected {
			continue
		}
		// Check per-repeater subscription filter
		var tgSet map[uint32]struct{}
		if slot == 1 {
			tgSet = rpt.Slot1TGs
		} else {
			tgSet = rpt.Slot2TGs
		}
		if tgSet != nil {
			// Explicit filter — check membership
			if _, ok := tgSet[tgid]; !ok {
				continue
			}
		}
		// nil tgSet = wildcard, send to this repeater
		s.conn.WriteToUDP(buf, rpt.Addr)
	}
	s.mu.RUnlock()

	*bufp = buf
	dmrdPool.Put(bufp)
}

func (s *HomebrewServer) Close() {
	s.conn.Close()
}

func (s *HomebrewServer) handlePacket(data []byte, addr *net.UDPAddr) {
	// Compare bytes directly to avoid string allocation on hot path
	p0, p1, p2, p3 := data[0], data[1], data[2], data[3]

	switch {
	case p0 == 'D' && p1 == 'M' && p2 == 'R' && p3 == 'D':
		s.handleDMRD(data, addr)
	case p0 == 'R' && p1 == 'P' && p2 == 'T' && p3 == 'L':
		s.handleRPTL(data, addr)
	case p0 == 'R' && p1 == 'P' && p2 == 'T' && p3 == 'K':
		s.handleRPTK(data, addr)
	case p0 == 'R' && p1 == 'P' && p2 == 'T' && p3 == 'C' && len(data) > 8:
		s.handleRPTC(data, addr)
	case p0 == 'R' && p1 == 'P' && p2 == 'T' && p3 == 'O':
		s.handleRPTO(data, addr)
	case p0 == 'R' && p1 == 'P' && p2 == 'T' && p3 == 'P': // RPTPING
		s.handlePing(data, addr)
	case p0 == 'D' && p1 == 'M' && p2 == 'R' && p3 == 'C':
		s.handleDMRC(data, addr)
	case p0 == 'D' && p1 == 'M' && p2 == 'R' && (p3 == 'G' || p3 == 'A'):
		// GPS and talker alias — forward upstream as-is
		s.handleDMRD(data, addr)
	default:
		if s.debug {
			log.Printf("[HB] unknown packet prefix %02x%02x%02x%02x from %s", p0, p1, p2, p3, addr)
		}
	}
}

// handleRPTL processes a login request
func (s *HomebrewServer) handleRPTL(data []byte, addr *net.UDPAddr) {
	if len(data) < 8 {
		return
	}
	repeaterID := HBExtractRepeaterID(data, 4)

	// Generate random salt
	var salt [4]byte
	rand.Read(salt[:])

	rpt := &RepeaterState{
		ID:       repeaterID,
		Addr:     addr,
		State:    StateAuth,
		Salt:     salt,
		LastPing: time.Now(),
	}

	s.mu.Lock()
	s.repeaters[repeaterID] = rpt
	s.addrMap[makeAddrKey(addr)] = repeaterID
	s.mu.Unlock()

	// Send RPTACK with salt
	resp := make([]byte, 10)
	copy(resp[0:6], HBRespACK)
	copy(resp[6:10], salt[:])
	s.conn.WriteToUDP(resp, addr)

	log.Printf("[HB] login from repeater %d @ %s", repeaterID, addr)
}

// handleRPTK processes an auth response
func (s *HomebrewServer) handleRPTK(data []byte, addr *net.UDPAddr) {
	if len(data) < 40 {
		return
	}
	repeaterID := HBExtractRepeaterID(data, 4)

	s.mu.RLock()
	rpt, ok := s.repeaters[repeaterID]
	s.mu.RUnlock()
	if !ok || rpt.State != StateAuth {
		s.conn.WriteToUDP(HBMakeNAK(repeaterID), addr)
		return
	}

	// Verify: SHA256(salt + passphrase)
	h := sha256.New()
	h.Write(rpt.Salt[:])
	h.Write([]byte(s.passphrase))
	expected := h.Sum(nil)

	if len(data) >= 40 {
		clientHash := data[8:40]
		if subtle.ConstantTimeCompare(clientHash, expected) != 1 {
			log.Printf("[HB] auth failed for repeater %d", repeaterID)
			s.conn.WriteToUDP(HBMakeNAK(repeaterID), addr)
			s.mu.Lock()
			delete(s.repeaters, repeaterID)
			delete(s.addrMap, makeAddrKey(addr))
			s.mu.Unlock()
			return
		}
	}

	s.mu.Lock()
	rpt.State = StateConfig
	s.mu.Unlock()

	// Send RPTACK (no payload — just confirms auth)
	resp := make([]byte, 6)
	copy(resp[0:6], HBRespACK)
	s.conn.WriteToUDP(resp, addr)

	log.Printf("[HB] auth OK for repeater %d", repeaterID)
}

// handleRPTC processes configuration from repeater
func (s *HomebrewServer) handleRPTC(data []byte, addr *net.UDPAddr) {
	if len(data) < 12 {
		return
	}
	repeaterID := HBExtractRepeaterID(data, 4)

	s.mu.Lock()
	rpt, ok := s.repeaters[repeaterID]
	if !ok {
		s.mu.Unlock()
		return
	}
	// Accept config in both StateConfig and StateConnected (re-config)
	rpt.State = StateConnected
	rpt.ConfigData = make([]byte, len(data)-8)
	copy(rpt.ConfigData, data[8:])
	rpt.Addr = addr
	rpt.LastPing = time.Now()
	if len(data) >= 16 {
		rpt.Callsign = strings.TrimSpace(string(data[8:16]))
	}
	s.addrMap[makeAddrKey(addr)] = repeaterID
	s.mu.Unlock()

	resp := make([]byte, 6)
	copy(resp[0:6], HBRespACK)
	s.conn.WriteToUDP(resp, addr)

	log.Printf("[HB] config from repeater %d (%s) — connected", repeaterID, rpt.Callsign)
}

// handleRPTO processes options (talkgroup subscriptions)
func (s *HomebrewServer) handleRPTO(data []byte, addr *net.UDPAddr) {
	if len(data) < 12 {
		return
	}
	repeaterID := HBExtractRepeaterID(data, 4)

	s.mu.RLock()
	_, ok := s.repeaters[repeaterID]
	s.mu.RUnlock()
	if !ok {
		return
	}

	// Options string starts at byte 8
	options := strings.TrimSpace(string(data[8:]))
	log.Printf("[HB] options from repeater %d: %s", repeaterID, options)

	resp := make([]byte, 6)
	copy(resp[0:6], HBRespACK)
	s.conn.WriteToUDP(resp, addr)

	if s.onOptions != nil {
		s.onOptions(repeaterID, options)
	}
}

// handlePing processes RPTPING keepalive
func (s *HomebrewServer) handlePing(data []byte, addr *net.UDPAddr) {
	// RPTPING is 11 bytes: "RPTPING" (7) + repeater_id (4)
	if len(data) < 11 {
		return
	}
	repeaterID := binary.BigEndian.Uint32(data[7:11])

	s.mu.Lock()
	rpt, ok := s.repeaters[repeaterID]
	if ok {
		rpt.LastPing = time.Now()
		rpt.Addr = addr
	}
	s.mu.Unlock()

	s.conn.WriteToUDP(HBMakePong(repeaterID), addr)
}

// handleDMRC processes MMDVM-style config push (simplified protocol)
func (s *HomebrewServer) handleDMRC(data []byte, addr *net.UDPAddr) {
	if len(data) < 8 {
		return
	}
	repeaterID := HBExtractRepeaterID(data, 4)

	s.mu.Lock()
	rpt, ok := s.repeaters[repeaterID]
	if !ok {
		// Auto-register for MMDVM simplified protocol (no auth handshake)
		rpt = &RepeaterState{
			ID:    repeaterID,
			Addr:  addr,
			State: StateConnected,
		}
		s.repeaters[repeaterID] = rpt
		s.addrMap[makeAddrKey(addr)] = repeaterID
		log.Printf("[HB] MMDVM-style connect from repeater %d @ %s", repeaterID, addr)
	}
	rpt.LastPing = time.Now()
	rpt.Addr = addr
	if len(data) >= 16 {
		rpt.Callsign = strings.TrimSpace(string(data[8:16]))
	}
	if len(data) > 8 {
		rpt.ConfigData = make([]byte, len(data)-8)
		copy(rpt.ConfigData, data[8:])
	}
	rpt.State = StateConnected
	s.mu.Unlock()

	// Respond with RPTACK
	resp := make([]byte, 6)
	copy(resp[0:6], HBRespACK)
	s.conn.WriteToUDP(resp, addr)
}

// handleDMRD processes DMR data packets (hot path)
func (s *HomebrewServer) handleDMRD(data []byte, addr *net.UDPAddr) {
	if len(data) < 20 {
		return
	}

	// Fast path: look up repeater by address (zero-alloc key)
	key := makeAddrKey(addr)
	s.mu.RLock()
	repeaterID, ok := s.addrMap[key]
	if ok {
		rpt := s.repeaters[repeaterID]
		if rpt != nil {
			ok = rpt.State == StateConnected
		}
	}
	s.mu.RUnlock()

	if !ok {
		return
	}

	if s.onDMRD != nil {
		// Pass everything after the 4-byte prefix (DMRD/DMRG/DMRA)
		s.onDMRD(repeaterID, data[4:])
	}
}

// PruneStale removes repeaters that haven't pinged within timeout.
// Returns IDs of pruned repeaters so caller can clean up subscriptions.
func (s *HomebrewServer) PruneStale(timeout time.Duration) []uint32 {
	now := time.Now()
	var pruned []uint32
	s.mu.Lock()
	for id, rpt := range s.repeaters {
		if now.Sub(rpt.LastPing) > timeout {
			log.Printf("[HB] pruning stale repeater %d (%s)", id, rpt.Callsign)
			delete(s.addrMap, makeAddrKey(rpt.Addr))
			delete(s.repeaters, id)
			pruned = append(pruned, id)
		}
	}
	s.mu.Unlock()
	return pruned
}
