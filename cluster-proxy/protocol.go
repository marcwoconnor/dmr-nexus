package main

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"time"
)

// Cluster-native protocol constants — matches hblink4/cluster_protocol.py
var (
	// 4-byte magic prefix distinguishes native from HomeBrew
	NativeMagic = []byte("CLNT")

	// Sub-commands (4 bytes each, appended after magic)
	CmdAuth      = []byte("AUTH")
	CmdAuthAck   = []byte("AACK")
	CmdAuthNak   = []byte("ANAK")
	CmdSubscribe = []byte("SUBS")
	CmdSubAck    = []byte("SACK")
	CmdPing      = []byte("PING")
	CmdPong      = []byte("PONG")
	CmdData      = []byte("DATA")
	CmdDisc      = []byte("DISC")
)

const (
	// Packet structure: CLNT(4) + CMD(4) + ... = 8 byte header minimum
	NativeHeaderSize = 8
	TokenHashSize    = 4

	// HomeBrew constants
	HBHeaderDMRD = "DMRD"
	HBHeaderRPTL = "RPTL"
	HBHeaderRPTK = "RPTK"
	HBHeaderRPTC = "RPTC"
	HBHeaderRPTO = "RPTO"
	HBHeaderRPTP = "RPTP" // prefix of RPTPING
	HBHeaderDMRC = "DMRC" // MMDVM simplified config push
	HBHeaderDMRG = "DMRG"
	HBHeaderDMRA = "DMRA"

	HBRespACK  = "RPTACK"
	HBRespNAK  = "MSTNAK"
	HBRespPONG = "MSTPONG"

	// HomeBrew repeater auth states
	StateLogin     = iota
	StateAuth
	StateConfig
	StateConnected
)

// --- Token (JSON-based, matches Python TokenManager) ---

// TokenPayload matches the Python Token.to_bytes() JSON structure
type TokenPayload struct {
	Version    int     `json:"v"`
	RepeaterID int     `json:"rid"`
	Slot1TGs   []int   `json:"s1"`
	Slot2TGs   []int   `json:"s2"`
	IssuedAt   float64 `json:"iat"`
	ExpiresAt  float64 `json:"exp"`
	ClusterID  string  `json:"cid"`
}

// Token holds a decoded cluster session token
type Token struct {
	Payload   TokenPayload
	Signature [32]byte
	RawBytes  []byte // full serialized form for re-use
}

// TokenHash returns the 4-byte hash used for per-packet validation
// Matches Python: hashlib.sha256(self.signature).digest()[:4]
func (t *Token) TokenHash() []byte {
	h := sha256.Sum256(t.Signature[:])
	return h[:TokenHashSize]
}

// IsExpired checks if the token has expired
func (t *Token) IsExpired() bool {
	return float64(time.Now().Unix()) > t.Payload.ExpiresAt
}

// NearExpiry returns true if token is within 20% of its lifetime of expiring
func (t *Token) NearExpiry() bool {
	now := float64(time.Now().Unix())
	lifetime := t.Payload.ExpiresAt - t.Payload.IssuedAt
	threshold := t.Payload.ExpiresAt - (lifetime / 5)
	return now >= threshold
}

// DecodeToken deserializes a token from wire format: [2B payload_len][payload][32B signature]
func DecodeToken(data []byte) (*Token, error) {
	if len(data) < 34 { // 2B len + min payload + 32B sig
		return nil, fmt.Errorf("token too short (%d bytes)", len(data))
	}
	payloadLen := int(binary.BigEndian.Uint16(data[:2]))
	totalLen := 2 + payloadLen + 32
	if len(data) < totalLen {
		return nil, fmt.Errorf("token truncated: need %d, have %d", totalLen, len(data))
	}

	payloadBytes := data[2 : 2+payloadLen]
	var payload TokenPayload
	if err := json.Unmarshal(payloadBytes, &payload); err != nil {
		return nil, fmt.Errorf("token JSON: %w", err)
	}

	t := &Token{Payload: payload}
	copy(t.Signature[:], data[2+payloadLen:totalLen])
	t.RawBytes = make([]byte, totalLen)
	copy(t.RawBytes, data[:totalLen])
	return t, nil
}

// --- Cluster-native packet builders ---

// BuildAuthReq creates: CLNT + AUTH + 4B repeater_id + SHA256(passphrase)
func BuildAuthReq(repeaterID uint32, passphrase string) []byte {
	hash := sha256.Sum256([]byte(passphrase))
	buf := make([]byte, 8+4+32) // header + rid + hash = 44
	copy(buf[0:4], NativeMagic)
	copy(buf[4:8], CmdAuth)
	binary.BigEndian.PutUint32(buf[8:12], repeaterID)
	copy(buf[12:44], hash[:])
	return buf
}

// BuildSubscribe creates: CLNT + SUBS + 4B token_hash + subscription_json
func BuildSubscribe(tokenHash []byte, slot1, slot2 []uint32) []byte {
	sub := map[string]interface{}{}
	if slot1 != nil {
		s1 := make([]int, len(slot1))
		for i, v := range slot1 {
			s1[i] = int(v)
		}
		sub["slot1"] = s1
	}
	if slot2 != nil {
		s2 := make([]int, len(slot2))
		for i, v := range slot2 {
			s2[i] = int(v)
		}
		sub["slot2"] = s2
	}
	jsonBytes, _ := json.Marshal(sub)

	buf := make([]byte, 8+TokenHashSize+len(jsonBytes))
	copy(buf[0:4], NativeMagic)
	copy(buf[4:8], CmdSubscribe)
	copy(buf[8:8+TokenHashSize], tokenHash)
	copy(buf[8+TokenHashSize:], jsonBytes)
	return buf
}

// BuildPing creates: CLNT + PING + 4B token_hash
func BuildPing(tokenHash []byte) []byte {
	buf := make([]byte, 8+TokenHashSize)
	copy(buf[0:4], NativeMagic)
	copy(buf[4:8], CmdPing)
	copy(buf[8:8+TokenHashSize], tokenHash)
	return buf
}

// BuildData creates: CLNT + DATA + 4B token_hash + DMRD_payload
// dmrdPayload is everything after the 4-byte "DMRD" prefix (51 bytes typical)
func BuildData(tokenHash []byte, dmrdPayload []byte) []byte {
	buf := make([]byte, 8+TokenHashSize+len(dmrdPayload))
	copy(buf[0:4], NativeMagic)
	copy(buf[4:8], CmdData)
	copy(buf[8:8+TokenHashSize], tokenHash)
	copy(buf[8+TokenHashSize:], dmrdPayload)
	return buf
}

// BuildDisconnect creates: CLNT + DISC + 4B token_hash
func BuildDisconnect(tokenHash []byte) []byte {
	buf := make([]byte, 8+TokenHashSize)
	copy(buf[0:4], NativeMagic)
	copy(buf[4:8], CmdDisc)
	copy(buf[8:8+TokenHashSize], tokenHash)
	return buf
}

// --- Cluster-native packet parsers ---

// PongHealth represents cluster health from a PONG response
type PongHealth struct {
	Peers    []PeerInfo `json:"peers"`
	NewToken string     `json:"new_token,omitempty"` // base64-encoded token bytes (refresh)
	Redirect bool       `json:"redirect,omitempty"`  // server is draining, reconnect elsewhere
}

type PeerInfo struct {
	NodeID    string  `json:"node_id"`
	Alive     bool    `json:"alive"`
	LatencyMs float64 `json:"latency_ms"`
}

func ParsePong(data []byte) (*PongHealth, error) {
	if len(data) < NativeHeaderSize {
		return nil, fmt.Errorf("pong too short")
	}
	jsonBytes := data[NativeHeaderSize:]
	if len(jsonBytes) == 0 {
		return &PongHealth{}, nil
	}
	var health PongHealth
	if err := json.Unmarshal(jsonBytes, &health); err != nil {
		return nil, fmt.Errorf("pong JSON: %w", err)
	}
	return &health, nil
}

// ExtractDataPayload extracts DMRD payload from a native DATA packet
// Returns the bytes after CLNT(4) + DATA(4) + token_hash(4) = offset 12
func ExtractDataPayload(data []byte) []byte {
	if len(data) <= 12 {
		return nil
	}
	return data[12:]
}

// --- HomeBrew helpers ---

func HBMakeACK(payload []byte) []byte {
	buf := make([]byte, 6+len(payload))
	copy(buf[0:6], HBRespACK)
	if len(payload) > 0 {
		copy(buf[6:], payload)
	}
	return buf
}

func HBMakeNAK(repeaterID uint32) []byte {
	buf := make([]byte, 10)
	copy(buf[0:6], HBRespNAK)
	binary.BigEndian.PutUint32(buf[6:10], repeaterID)
	return buf
}

func HBMakePong(repeaterID uint32) []byte {
	buf := make([]byte, 11)
	copy(buf[0:7], HBRespPONG)
	binary.BigEndian.PutUint32(buf[7:11], repeaterID)
	return buf
}

func HBExtractRepeaterID(data []byte, offset int) uint32 {
	if len(data) < offset+4 {
		return 0
	}
	return binary.BigEndian.Uint32(data[offset : offset+4])
}

// IsNativePacket returns true if data starts with CLNT magic
func IsNativePacket(data []byte) bool {
	return len(data) >= 4 && data[0] == 'C' && data[1] == 'L' && data[2] == 'N' && data[3] == 'T'
}

// MatchSubCmd checks if bytes 4-8 match a sub-command
func MatchSubCmd(data []byte, cmd []byte) bool {
	return len(data) >= 8 && data[4] == cmd[0] && data[5] == cmd[1] && data[6] == cmd[2] && data[7] == cmd[3]
}
