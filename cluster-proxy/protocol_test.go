package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"testing"
)

func TestIsNativePacket(t *testing.T) {
	tests := []struct {
		name string
		data []byte
		want bool
	}{
		{"CLNT prefix", []byte("CLNTAUTH"), true},
		{"DMRD prefix", []byte("DMRD\x00\x00"), false},
		{"too short", []byte("CL"), false},
		{"empty", []byte{}, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := IsNativePacket(tt.data); got != tt.want {
				t.Errorf("IsNativePacket() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestMatchSubCmd(t *testing.T) {
	data := []byte("CLNTAUTH\x00\x00")
	if !MatchSubCmd(data, CmdAuth) {
		t.Error("should match AUTH")
	}
	if MatchSubCmd(data, CmdPing) {
		t.Error("should not match PING")
	}
	if MatchSubCmd([]byte("SHORT"), CmdAuth) {
		t.Error("short data should not match")
	}
}

func TestBuildAuthReq(t *testing.T) {
	pkt := BuildAuthReq(312100, "testpass")

	// Check header
	if string(pkt[0:4]) != "CLNT" {
		t.Errorf("magic = %q, want CLNT", string(pkt[0:4]))
	}
	if string(pkt[4:8]) != "AUTH" {
		t.Errorf("cmd = %q, want AUTH", string(pkt[4:8]))
	}

	// Check repeater ID
	rid := binary.BigEndian.Uint32(pkt[8:12])
	if rid != 312100 {
		t.Errorf("repeater_id = %d, want 312100", rid)
	}

	// Check passphrase hash
	expected := sha256.Sum256([]byte("testpass"))
	if !bytes.Equal(pkt[12:44], expected[:]) {
		t.Error("passphrase hash mismatch")
	}

	// Total length: 8 header + 4 rid + 32 hash = 44
	if len(pkt) != 44 {
		t.Errorf("len = %d, want 44", len(pkt))
	}
}

func TestBuildData(t *testing.T) {
	tokenHash := []byte{0xAB, 0xCD, 0xEF, 0x12}
	payload := make([]byte, 51) // typical DMRD without prefix
	payload[0] = 0x42           // seq

	pkt := BuildData(tokenHash, payload)

	if string(pkt[0:4]) != "CLNT" {
		t.Errorf("magic = %q", string(pkt[0:4]))
	}
	if string(pkt[4:8]) != "DATA" {
		t.Errorf("cmd = %q", string(pkt[4:8]))
	}
	if !bytes.Equal(pkt[8:12], tokenHash) {
		t.Error("token_hash mismatch")
	}
	if pkt[12] != 0x42 {
		t.Errorf("payload[0] = 0x%02x, want 0x42", pkt[12])
	}
	// 8 header + 4 token_hash + 51 payload = 63
	if len(pkt) != 63 {
		t.Errorf("len = %d, want 63", len(pkt))
	}
}

func TestExtractDataPayload(t *testing.T) {
	pkt := BuildData([]byte{1, 2, 3, 4}, []byte{0xAA, 0xBB, 0xCC})
	payload := ExtractDataPayload(pkt)
	if !bytes.Equal(payload, []byte{0xAA, 0xBB, 0xCC}) {
		t.Errorf("payload = %v", payload)
	}
}

func TestExtractDataPayloadTooShort(t *testing.T) {
	payload := ExtractDataPayload([]byte("CLNTDATA1234"))
	if payload != nil && len(payload) != 0 {
		t.Errorf("expected nil/empty for exact-header packet, got %v", payload)
	}
}

func TestBuildSubscribe(t *testing.T) {
	th := []byte{0x01, 0x02, 0x03, 0x04}
	slot1 := []uint32{8, 9}
	slot2 := []uint32{3120, 3121}

	pkt := BuildSubscribe(th, slot1, slot2)

	if string(pkt[0:4]) != "CLNT" {
		t.Error("magic")
	}
	if string(pkt[4:8]) != "SUBS" {
		t.Error("cmd")
	}
	if !bytes.Equal(pkt[8:12], th) {
		t.Error("token_hash")
	}

	// Parse JSON portion
	var sub map[string]interface{}
	if err := json.Unmarshal(pkt[12:], &sub); err != nil {
		t.Fatalf("JSON parse: %v", err)
	}
	s1 := sub["slot1"].([]interface{})
	if len(s1) != 2 {
		t.Errorf("slot1 len = %d", len(s1))
	}
}

func TestBuildPing(t *testing.T) {
	th := []byte{0xDE, 0xAD, 0xBE, 0xEF}
	pkt := BuildPing(th)
	if len(pkt) != 12 {
		t.Errorf("len = %d, want 12", len(pkt))
	}
	if string(pkt[4:8]) != "PING" {
		t.Error("cmd")
	}
}

func TestBuildDisconnect(t *testing.T) {
	th := []byte{0x01, 0x02, 0x03, 0x04}
	pkt := BuildDisconnect(th)
	if len(pkt) != 12 {
		t.Errorf("len = %d", len(pkt))
	}
	if string(pkt[4:8]) != "DISC" {
		t.Error("cmd")
	}
}

func TestParsePong(t *testing.T) {
	health := PongHealth{
		Peers: []PeerInfo{
			{NodeID: "east-1", Alive: true, LatencyMs: 2.5},
			{NodeID: "west-1", Alive: false, LatencyMs: 0},
		},
	}
	jsonBytes, _ := json.Marshal(health)
	pkt := make([]byte, 8+len(jsonBytes))
	copy(pkt[0:4], NativeMagic)
	copy(pkt[4:8], CmdPong)
	copy(pkt[8:], jsonBytes)

	parsed, err := ParsePong(pkt)
	if err != nil {
		t.Fatalf("ParsePong: %v", err)
	}
	if len(parsed.Peers) != 2 {
		t.Fatalf("peers = %d", len(parsed.Peers))
	}
	if parsed.Peers[0].NodeID != "east-1" {
		t.Errorf("peer[0] = %s", parsed.Peers[0].NodeID)
	}
	if !parsed.Peers[0].Alive {
		t.Error("peer[0] should be alive")
	}
	if parsed.Peers[1].Alive {
		t.Error("peer[1] should not be alive")
	}
}

func TestParsePongEmpty(t *testing.T) {
	pkt := make([]byte, 8)
	copy(pkt[0:4], NativeMagic)
	copy(pkt[4:8], CmdPong)

	parsed, err := ParsePong(pkt)
	if err != nil {
		t.Fatalf("ParsePong empty: %v", err)
	}
	if len(parsed.Peers) != 0 {
		t.Errorf("expected empty peers, got %d", len(parsed.Peers))
	}
}

// --- Token decode tests ---

// buildPythonToken creates a token in the Python wire format for testing interop
func buildPythonToken(rid int, s1, s2 []int, secret string) []byte {
	payload := map[string]interface{}{
		"v":   1,
		"rid": rid,
		"s1":  s1,
		"s2":  s2,
		"iat": 1709740800.0,
		"exp": 1709827200.0, // +24h
		"cid": "test-cluster",
	}
	payloadBytes, _ := json.Marshal(payload)

	// HMAC-SHA256 signature (matching Python: hmac.new(secret, payload, sha256).digest())
	import_hmac := func(key, data []byte) [32]byte {
		// Simple HMAC-SHA256 using crypto/hmac
		// imported at package level would be cleaner but this keeps test self-contained
		h := sha256.New
		mac := make([]byte, 0)
		_ = h
		_ = mac
		// Actually use the real hmac
		return sha256.Sum256(append(key, data...)) // simplified, not real HMAC
	}
	_ = import_hmac

	// For interop testing, use a known signature
	sig := sha256.Sum256(append([]byte(secret), payloadBytes...))

	// Wire format: [2B payload_len][payload][32B signature]
	buf := make([]byte, 2+len(payloadBytes)+32)
	binary.BigEndian.PutUint16(buf[0:2], uint16(len(payloadBytes)))
	copy(buf[2:2+len(payloadBytes)], payloadBytes)
	copy(buf[2+len(payloadBytes):], sig[:])
	return buf
}

func TestDecodeToken(t *testing.T) {
	// Build a token in Python wire format
	payload := `{"v":1,"rid":312000,"s1":[8,9],"s2":[3120],"iat":1709740800.0,"exp":1709827200.0,"cid":"test-cluster"}`
	payloadBytes := []byte(payload)
	sig := [32]byte{0x01, 0x02, 0x03} // arbitrary

	buf := make([]byte, 2+len(payloadBytes)+32)
	binary.BigEndian.PutUint16(buf[0:2], uint16(len(payloadBytes)))
	copy(buf[2:2+len(payloadBytes)], payloadBytes)
	copy(buf[2+len(payloadBytes):], sig[:])

	token, err := DecodeToken(buf)
	if err != nil {
		t.Fatalf("DecodeToken: %v", err)
	}
	if token.Payload.RepeaterID != 312000 {
		t.Errorf("rid = %d", token.Payload.RepeaterID)
	}
	if len(token.Payload.Slot1TGs) != 2 || token.Payload.Slot1TGs[0] != 8 {
		t.Errorf("s1 = %v", token.Payload.Slot1TGs)
	}
	if len(token.Payload.Slot2TGs) != 1 || token.Payload.Slot2TGs[0] != 3120 {
		t.Errorf("s2 = %v", token.Payload.Slot2TGs)
	}
	if token.Payload.ClusterID != "test-cluster" {
		t.Errorf("cid = %s", token.Payload.ClusterID)
	}
}

func TestDecodeTokenTooShort(t *testing.T) {
	_, err := DecodeToken([]byte{0x00, 0x05})
	if err == nil {
		t.Error("expected error for too-short token")
	}
}

func TestDecodeTokenTruncated(t *testing.T) {
	// Valid length header but truncated payload
	buf := make([]byte, 10)
	binary.BigEndian.PutUint16(buf[0:2], 100) // claims 100 bytes payload
	_, err := DecodeToken(buf)
	if err == nil {
		t.Error("expected error for truncated token")
	}
}

func TestDecodeTokenBadJSON(t *testing.T) {
	payload := []byte("not json")
	buf := make([]byte, 2+len(payload)+32)
	binary.BigEndian.PutUint16(buf[0:2], uint16(len(payload)))
	copy(buf[2:], payload)
	_, err := DecodeToken(buf)
	if err == nil {
		t.Error("expected error for bad JSON")
	}
}

func TestTokenHash(t *testing.T) {
	payload := `{"v":1,"rid":1,"s1":null,"s2":null,"iat":0,"exp":9999999999.0,"cid":"x"}`
	sig := sha256.Sum256([]byte("test-sig-data"))

	buf := make([]byte, 2+len(payload)+32)
	binary.BigEndian.PutUint16(buf[0:2], uint16(len(payload)))
	copy(buf[2:2+len(payload)], payload)
	copy(buf[2+len(payload):], sig[:])

	token, err := DecodeToken(buf)
	if err != nil {
		t.Fatal(err)
	}

	th := token.TokenHash()
	if len(th) != 4 {
		t.Errorf("token hash len = %d", len(th))
	}

	// Verify it matches SHA256(signature)[:4]
	expectedFull := sha256.Sum256(sig[:])
	if !bytes.Equal(th, expectedFull[:4]) {
		t.Error("token hash doesn't match SHA256(signature)[:4]")
	}
}

func TestTokenIsExpired(t *testing.T) {
	payload := `{"v":1,"rid":1,"s1":null,"s2":null,"iat":0,"exp":1.0,"cid":"x"}`
	buf := make([]byte, 2+len(payload)+32)
	binary.BigEndian.PutUint16(buf[0:2], uint16(len(payload)))
	copy(buf[2:2+len(payload)], payload)

	token, _ := DecodeToken(buf)
	if !token.IsExpired() {
		t.Error("token with exp=1.0 should be expired")
	}
}

func TestTokenNotExpired(t *testing.T) {
	payload := `{"v":1,"rid":1,"s1":null,"s2":null,"iat":0,"exp":9999999999.0,"cid":"x"}`
	buf := make([]byte, 2+len(payload)+32)
	binary.BigEndian.PutUint16(buf[0:2], uint16(len(payload)))
	copy(buf[2:2+len(payload)], payload)

	token, _ := DecodeToken(buf)
	if token.IsExpired() {
		t.Error("token with far-future exp should not be expired")
	}
}

func TestTokenNullSlots(t *testing.T) {
	payload := `{"v":1,"rid":1,"s1":null,"s2":null,"iat":0,"exp":0,"cid":"x"}`
	buf := make([]byte, 2+len(payload)+32)
	binary.BigEndian.PutUint16(buf[0:2], uint16(len(payload)))
	copy(buf[2:2+len(payload)], payload)

	token, err := DecodeToken(buf)
	if err != nil {
		t.Fatal(err)
	}
	if token.Payload.Slot1TGs != nil {
		t.Errorf("s1 should be nil, got %v", token.Payload.Slot1TGs)
	}
}

// --- Token refresh and graceful drain ---

func TestParsePongWithTokenRefresh(t *testing.T) {
	// Build a valid token in wire format
	payload := `{"v":1,"rid":312000,"s1":[8,9],"s2":[3120],"iat":1709740800.0,"exp":9999999999.0,"cid":"test"}`
	payloadBytes := []byte(payload)
	sig := sha256.Sum256([]byte("refresh-test"))
	tokenBuf := make([]byte, 2+len(payloadBytes)+32)
	binary.BigEndian.PutUint16(tokenBuf[0:2], uint16(len(payloadBytes)))
	copy(tokenBuf[2:2+len(payloadBytes)], payloadBytes)
	copy(tokenBuf[2+len(payloadBytes):], sig[:])

	// Base64-encode it as server would
	b64Token := base64.StdEncoding.EncodeToString(tokenBuf)

	// Build PONG with new_token
	health := PongHealth{
		Peers:    []PeerInfo{{NodeID: "east-1", Alive: true, LatencyMs: 1.0}},
		NewToken: b64Token,
	}
	jsonBytes, _ := json.Marshal(health)
	pkt := make([]byte, 8+len(jsonBytes))
	copy(pkt[0:4], NativeMagic)
	copy(pkt[4:8], CmdPong)
	copy(pkt[8:], jsonBytes)

	parsed, err := ParsePong(pkt)
	if err != nil {
		t.Fatalf("ParsePong: %v", err)
	}
	if parsed.NewToken == "" {
		t.Fatal("new_token should not be empty")
	}

	// Decode the base64 token (as handlePong does)
	tokenBytes, err := base64.StdEncoding.DecodeString(parsed.NewToken)
	if err != nil {
		t.Fatalf("base64 decode: %v", err)
	}
	token, err := DecodeToken(tokenBytes)
	if err != nil {
		t.Fatalf("DecodeToken: %v", err)
	}
	if token.Payload.RepeaterID != 312000 {
		t.Errorf("rid = %d, want 312000", token.Payload.RepeaterID)
	}
	if token.IsExpired() {
		t.Error("refreshed token should not be expired")
	}
}

func TestParsePongWithRedirect(t *testing.T) {
	health := PongHealth{
		Peers:    []PeerInfo{{NodeID: "drain-node", Alive: true, LatencyMs: 5.0}},
		Redirect: true,
	}
	jsonBytes, _ := json.Marshal(health)
	pkt := make([]byte, 8+len(jsonBytes))
	copy(pkt[0:4], NativeMagic)
	copy(pkt[4:8], CmdPong)
	copy(pkt[8:], jsonBytes)

	parsed, err := ParsePong(pkt)
	if err != nil {
		t.Fatalf("ParsePong: %v", err)
	}
	if !parsed.Redirect {
		t.Error("redirect should be true")
	}
}

func TestParsePongNoRedirect(t *testing.T) {
	health := PongHealth{Peers: []PeerInfo{}}
	jsonBytes, _ := json.Marshal(health)
	pkt := make([]byte, 8+len(jsonBytes))
	copy(pkt[0:4], NativeMagic)
	copy(pkt[4:8], CmdPong)
	copy(pkt[8:], jsonBytes)

	parsed, err := ParsePong(pkt)
	if err != nil {
		t.Fatalf("ParsePong: %v", err)
	}
	if parsed.Redirect {
		t.Error("redirect should be false when omitted")
	}
	if parsed.NewToken != "" {
		t.Error("new_token should be empty when omitted")
	}
}

func TestTokenNearExpiry(t *testing.T) {
	// Token issued at 0, expires at 100 → threshold at 80 (20% of lifetime)
	payload := `{"v":1,"rid":1,"s1":null,"s2":null,"iat":0,"exp":100.0,"cid":"x"}`
	buf := make([]byte, 2+len(payload)+32)
	binary.BigEndian.PutUint16(buf[0:2], uint16(len(payload)))
	copy(buf[2:2+len(payload)], payload)

	token, err := DecodeToken(buf)
	if err != nil {
		t.Fatal(err)
	}
	// Token with exp=100 is long expired, so NearExpiry should return true
	if !token.NearExpiry() {
		t.Error("expired token should report near expiry")
	}

	// Far-future token should NOT be near expiry
	payloadFar := `{"v":1,"rid":1,"s1":null,"s2":null,"iat":0,"exp":9999999999.0,"cid":"x"}`
	bufFar := make([]byte, 2+len(payloadFar)+32)
	binary.BigEndian.PutUint16(bufFar[0:2], uint16(len(payloadFar)))
	copy(bufFar[2:2+len(payloadFar)], payloadFar)

	tokenFar, _ := DecodeToken(bufFar)
	if tokenFar.NearExpiry() {
		t.Error("far-future token should not be near expiry")
	}
}

// --- HomeBrew helpers ---

func TestHBMakeACK(t *testing.T) {
	ack := HBMakeACK(nil)
	if string(ack) != "RPTACK" {
		t.Errorf("ack = %q", string(ack))
	}

	salt := []byte{0xDE, 0xAD, 0xBE, 0xEF}
	ackSalt := HBMakeACK(salt)
	if len(ackSalt) != 10 {
		t.Errorf("len = %d", len(ackSalt))
	}
	if !bytes.Equal(ackSalt[6:], salt) {
		t.Error("salt mismatch")
	}
}

func TestHBMakeNAK(t *testing.T) {
	nak := HBMakeNAK(312100)
	if string(nak[:6]) != "MSTNAK" {
		t.Error("prefix")
	}
	rid := binary.BigEndian.Uint32(nak[6:10])
	if rid != 312100 {
		t.Errorf("rid = %d", rid)
	}
}

func TestHBMakePong(t *testing.T) {
	pong := HBMakePong(312100)
	if string(pong[:7]) != "MSTPONG" {
		t.Error("prefix")
	}
	rid := binary.BigEndian.Uint32(pong[7:11])
	if rid != 312100 {
		t.Errorf("rid = %d", rid)
	}
}

func TestHBExtractRepeaterID(t *testing.T) {
	data := make([]byte, 12)
	binary.BigEndian.PutUint32(data[4:8], 312100)
	if HBExtractRepeaterID(data, 4) != 312100 {
		t.Error("extraction failed")
	}
	if HBExtractRepeaterID(data, 10) != 0 {
		t.Error("should return 0 for out-of-bounds")
	}
}
