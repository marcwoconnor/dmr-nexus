package main

import (
	"encoding/json"
	"fmt"
	"os"
)

type Config struct {
	// Local HomeBrew listener (accepts MMDVMHost / DMRGateway)
	Local LocalConfig `json:"local"`

	// Upstream cluster-native connection(s)
	Cluster ClusterConfig `json:"cluster"`

	// Talkgroup subscriptions (default; RPTO from client overrides)
	Subscription SubscriptionConfig `json:"subscription"`

	// Logging
	LogLevel string `json:"log_level"` // debug, info, warn, error
}

type LocalConfig struct {
	Address    string `json:"address"`     // bind address (default 0.0.0.0)
	Port       int    `json:"port"`        // listen port (default 62031)
	Passphrase string `json:"passphrase"`  // auth passphrase for local repeaters
	RepeaterID uint32 `json:"repeater_id"` // our repeater ID for upstream auth
}

type ClusterConfig struct {
	Servers      []ServerConfig `json:"servers"`       // primary + secondary
	Passphrase   string         `json:"passphrase"`    // passphrase for upstream cluster auth
	PingInterval int            `json:"ping_interval"` // seconds (default 5)
	PingTimeout  int            `json:"ping_timeout"`  // missed pings before failover (default 3)
}

type ServerConfig struct {
	Address string `json:"address"`
	Port    int    `json:"port"`
}

type SubscriptionConfig struct {
	Slot1 []uint32 `json:"slot1"` // talkgroup IDs for timeslot 1
	Slot2 []uint32 `json:"slot2"` // talkgroup IDs for timeslot 2
}

func LoadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config: %w", err)
	}
	cfg := &Config{
		Local: LocalConfig{
			Address: "0.0.0.0",
			Port:    62031,
		},
		Cluster: ClusterConfig{
			PingInterval: 5,
			PingTimeout:  3,
		},
		LogLevel: "info",
	}
	if err := json.Unmarshal(data, cfg); err != nil {
		return nil, fmt.Errorf("parse config: %w", err)
	}
	if err := cfg.validate(); err != nil {
		return nil, err
	}
	return cfg, nil
}

func (c *Config) validate() error {
	if c.Local.RepeaterID == 0 {
		return fmt.Errorf("local.repeater_id required")
	}
	if len(c.Cluster.Servers) == 0 {
		return fmt.Errorf("at least one cluster server required")
	}
	if c.Cluster.Passphrase == "" {
		return fmt.Errorf("cluster.passphrase required")
	}
	for i, s := range c.Cluster.Servers {
		if s.Address == "" {
			return fmt.Errorf("cluster.servers[%d].address required", i)
		}
		if s.Port == 0 {
			return fmt.Errorf("cluster.servers[%d].port required", i)
		}
	}
	return nil
}
