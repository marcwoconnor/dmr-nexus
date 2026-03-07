package main

import (
	"reflect"
	"sort"
	"testing"
)

func TestParseOptions(t *testing.T) {
	tests := []struct {
		name  string
		input string
		slot1 []uint32
		slot2 []uint32
	}{
		{
			name:  "both slots",
			input: "TS1=1,2,3;TS2=10,20,30",
			slot1: []uint32{1, 2, 3},
			slot2: []uint32{10, 20, 30},
		},
		{
			name:  "slot1 only",
			input: "TS1=8,9",
			slot1: []uint32{8, 9},
			slot2: nil,
		},
		{
			name:  "slot2 only",
			input: "TS2=3120,3121",
			slot1: nil,
			slot2: []uint32{3120, 3121},
		},
		{
			name:  "with spaces around values",
			input: " TS1=8, 9 ; TS2=3120 ",
			slot1: []uint32{8, 9},
			slot2: []uint32{3120},
		},
		{
			name:  "wildcard returns nil",
			input: "TS1=*;TS2=*",
			slot1: nil,
			slot2: nil,
		},
		{
			name:  "empty slots",
			input: "TS1=;TS2=",
			slot1: nil,
			slot2: nil,
		},
		{
			name:  "empty string",
			input: "",
			slot1: nil,
			slot2: nil,
		},
		{
			name:  "single TG",
			input: "TS1=3120",
			slot1: []uint32{3120},
			slot2: nil,
		},
		{
			name:  "invalid TG ignored",
			input: "TS1=8,abc,9",
			slot1: []uint32{8, 9},
			slot2: nil,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			s1, s2 := parseOptions(tt.input)
			if !reflect.DeepEqual(s1, tt.slot1) {
				t.Errorf("slot1 = %v, want %v", s1, tt.slot1)
			}
			if !reflect.DeepEqual(s2, tt.slot2) {
				t.Errorf("slot2 = %v, want %v", s2, tt.slot2)
			}
		})
	}
}

func TestSliceToSet(t *testing.T) {
	// nil input = wildcard
	if got := sliceToSet(nil); got != nil {
		t.Errorf("nil input should return nil, got %v", got)
	}

	// empty slice = empty map (receive none)
	got := sliceToSet([]uint32{})
	if got == nil || len(got) != 0 {
		t.Errorf("empty slice should return empty map, got %v", got)
	}

	// normal slice
	got = sliceToSet([]uint32{8, 9, 3120})
	if len(got) != 3 {
		t.Errorf("expected 3 entries, got %d", len(got))
	}
	for _, tg := range []uint32{8, 9, 3120} {
		if _, ok := got[tg]; !ok {
			t.Errorf("missing TG %d", tg)
		}
	}
}

func TestSetToSlice(t *testing.T) {
	// empty map = nil
	if got := setToSlice(map[uint32]struct{}{}); got != nil {
		t.Errorf("empty map should return nil, got %v", got)
	}

	// normal map
	m := map[uint32]struct{}{8: {}, 9: {}, 3120: {}}
	got := setToSlice(m)
	sort.Slice(got, func(i, j int) bool { return got[i] < got[j] })
	expected := []uint32{8, 9, 3120}
	if !reflect.DeepEqual(got, expected) {
		t.Errorf("setToSlice = %v, want %v", got, expected)
	}
}

// TestDMRDSlotAndTGExtraction verifies the hot-path bit extraction in BroadcastDMRD
func TestDMRDSlotAndTGExtraction(t *testing.T) {
	tests := []struct {
		name     string
		bits     byte   // byte 11 of DMRD payload
		dstTG    uint32 // 3-byte big-endian at offset 4
		wantSlot int
		wantTG   uint32
	}{
		{"slot1 TG8", 0x00, 8, 1, 8},
		{"slot2 TG3120", 0x80, 3120, 2, 3120},
		{"slot1 TG9 with group call", 0x40, 9, 1, 9},
		{"slot2 TG3121 with flags", 0xE2, 3121, 2, 3121},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			// Build a minimal 12-byte DMRD payload
			payload := make([]byte, 16)
			// dst_id at offset 4-6 (3 bytes big-endian)
			payload[4] = byte(tt.dstTG >> 16)
			payload[5] = byte(tt.dstTG >> 8)
			payload[6] = byte(tt.dstTG)
			// bits at offset 11
			payload[11] = tt.bits

			// Extract the same way BroadcastDMRD does
			tgid := uint32(payload[4])<<16 | uint32(payload[5])<<8 | uint32(payload[6])
			slot := 1 + int(payload[11]>>7)

			if slot != tt.wantSlot {
				t.Errorf("slot = %d, want %d", slot, tt.wantSlot)
			}
			if tgid != tt.wantTG {
				t.Errorf("tgid = %d, want %d", tgid, tt.wantTG)
			}
		})
	}
}

func TestParseTGList(t *testing.T) {
	tests := []struct {
		input string
		want  []uint32
	}{
		{"8,9,3120", []uint32{8, 9, 3120}},
		{"*", nil},
		{"", nil},
		{"  ", nil},
		{"3120", []uint32{3120}},
	}
	for _, tt := range tests {
		got := parseTGList(tt.input)
		if !reflect.DeepEqual(got, tt.want) {
			t.Errorf("parseTGList(%q) = %v, want %v", tt.input, got, tt.want)
		}
	}
}
