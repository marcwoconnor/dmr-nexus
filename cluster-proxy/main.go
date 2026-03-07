package main

import (
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	cfgPath := "config.json"
	if len(os.Args) > 1 {
		cfgPath = os.Args[1]
	}

	cfg, err := LoadConfig(cfgPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "config error: %v\n", err)
		os.Exit(1)
	}

	// Configure logging
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)

	log.Printf("cluster-proxy starting — repeater_id=%d servers=%d",
		cfg.Local.RepeaterID, len(cfg.Cluster.Servers))

	proxy, err := NewProxy(cfg)
	if err != nil {
		log.Fatalf("failed to create proxy: %v", err)
	}

	// Graceful shutdown
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		sig := <-sigCh
		log.Printf("received %v — shutting down", sig)
		proxy.Close()
		os.Exit(0)
	}()

	proxy.Run()
}
