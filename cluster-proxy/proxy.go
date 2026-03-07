package main

import (
	"log"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Proxy bridges HomeBrew (local repeaters) and cluster-native (upstream servers)
type Proxy struct {
	hb      *HomebrewServer
	cluster *ClusterClient
	cfg     *Config
	stopCh  chan struct{}

	// Per-repeater subscriptions for computing cluster-level union
	rptSubs map[uint32]*rptSubscription
	subMu   sync.RWMutex
}

type rptSubscription struct {
	slot1 []uint32 // nil = all
	slot2 []uint32
}

func NewProxy(cfg *Config) (*Proxy, error) {
	hb, err := NewHomebrewServer(cfg.Local.Address, cfg.Local.Port, cfg.Local.Passphrase, cfg.LogLevel == "debug")
	if err != nil {
		return nil, err
	}

	cc, err := NewClusterClient(cfg)
	if err != nil {
		hb.Close()
		return nil, err
	}

	p := &Proxy{
		hb:      hb,
		cluster: cc,
		cfg:     cfg,
		stopCh:  make(chan struct{}),
		rptSubs: make(map[uint32]*rptSubscription),
	}

	// Wire up callbacks:
	// HomeBrew DMRD → strip prefix, forward to cluster
	hb.SetCallbacks(p.onLocalDMRD, p.onLocalOptions)

	// Cluster DMRD → add "DMRD" prefix, forward to matching repeaters
	cc.SetDMRDCallback(p.onClusterDMRD)

	return p, nil
}

// Run starts all components (blocking)
func (p *Proxy) Run() {
	log.Printf("[PROXY] starting — local %s:%d → cluster (%d servers)",
		p.cfg.Local.Address, p.cfg.Local.Port, len(p.cfg.Cluster.Servers))

	// Start HomeBrew listener in background
	go p.hb.Run()

	// Start stale repeater pruning
	go p.pruneLoop()

	// Start cluster client (blocking — runs keepalive loop)
	p.cluster.Run()
}

func (p *Proxy) Close() {
	close(p.stopCh)
	p.cluster.Close()
	p.hb.Close()
}

// onLocalDMRD handles DMRD from a local repeater → forward to cluster
func (p *Proxy) onLocalDMRD(repeaterID uint32, dmrdPayload []byte) {
	p.cluster.SendDMRD(dmrdPayload)
}

// onClusterDMRD handles DMRD from cluster → forward to matching local repeaters
func (p *Proxy) onClusterDMRD(dmrdPayload []byte) {
	p.hb.BroadcastDMRD(dmrdPayload)
}

// onLocalOptions handles RPTO talkgroup options from a local repeater
// Format: "TS1=tg1,tg2,tg3;TS2=tg4,tg5,tg6"
func (p *Proxy) onLocalOptions(repeaterID uint32, options string) {
	slot1, slot2 := parseOptions(options)

	// Store per-repeater subscription and apply filter
	p.subMu.Lock()
	p.rptSubs[repeaterID] = &rptSubscription{slot1: slot1, slot2: slot2}
	p.subMu.Unlock()

	// Set per-repeater filter on the homebrew server
	p.hb.SetSubscription(repeaterID, slot1, slot2)

	// Recompute cluster-level subscription as union of all repeaters
	p.recomputeClusterSubscription()
}

// recomputeClusterSubscription merges all per-repeater TG lists into a single
// cluster subscription. If any repeater has nil (wildcard) for a slot, the
// cluster subscription for that slot uses the config default.
func (p *Proxy) recomputeClusterSubscription() {
	p.subMu.RLock()
	defer p.subMu.RUnlock()

	if len(p.rptSubs) == 0 {
		// No RPTO received yet — use config defaults
		p.cluster.UpdateSubscription(p.cfg.Subscription.Slot1, p.cfg.Subscription.Slot2)
		return
	}

	var s1wildcard, s2wildcard bool
	s1set := make(map[uint32]struct{})
	s2set := make(map[uint32]struct{})

	for _, sub := range p.rptSubs {
		if sub.slot1 == nil {
			s1wildcard = true
		} else {
			for _, tg := range sub.slot1 {
				s1set[tg] = struct{}{}
			}
		}
		if sub.slot2 == nil {
			s2wildcard = true
		} else {
			for _, tg := range sub.slot2 {
				s2set[tg] = struct{}{}
			}
		}
	}

	// If any repeater wants wildcard, use config default for that slot
	var slot1, slot2 []uint32
	if s1wildcard {
		slot1 = p.cfg.Subscription.Slot1
	} else {
		slot1 = setToSlice(s1set)
	}
	if s2wildcard {
		slot2 = p.cfg.Subscription.Slot2
	} else {
		slot2 = setToSlice(s2set)
	}

	log.Printf("[PROXY] cluster subscription updated: TS1=%d TGs, TS2=%d TGs", len(slot1), len(slot2))
	p.cluster.UpdateSubscription(slot1, slot2)
}

func setToSlice(m map[uint32]struct{}) []uint32 {
	if len(m) == 0 {
		return nil
	}
	s := make([]uint32, 0, len(m))
	for tg := range m {
		s = append(s, tg)
	}
	return s
}

func (p *Proxy) pruneLoop() {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-p.stopCh:
			return
		case <-ticker.C:
			pruned := p.hb.PruneStale(60 * time.Second)
			if len(pruned) > 0 {
				// Remove pruned repeaters from subscription tracking
				p.subMu.Lock()
				for _, id := range pruned {
					delete(p.rptSubs, id)
				}
				p.subMu.Unlock()
				p.recomputeClusterSubscription()
			}
		}
	}
}

// parseOptions parses "TS1=1,2,3;TS2=10,20,30" into talkgroup slices
func parseOptions(options string) (slot1, slot2 []uint32) {
	parts := strings.Split(options, ";")
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if strings.HasPrefix(part, "TS1=") {
			slot1 = parseTGList(part[4:])
		} else if strings.HasPrefix(part, "TS2=") {
			slot2 = parseTGList(part[4:])
		}
	}
	return
}

func parseTGList(s string) []uint32 {
	s = strings.TrimSpace(s)
	if s == "" || s == "*" {
		return nil // nil means "all" or "not specified"
	}
	parts := strings.Split(s, ",")
	tgs := make([]uint32, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if v, err := strconv.ParseUint(p, 10, 32); err == nil {
			tgs = append(tgs, uint32(v))
		}
	}
	return tgs
}
