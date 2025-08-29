package main

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strconv"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
)

const (
	SHM_PATH    = "/dev/shm/eth_price_shm"
	PIPE_PATH   = "/tmp/eth_price_pipe"
	BUFFER_SIZE = 32
	BINANCE_WS  = "wss://stream.binance.com:9443/ws/ethusdt@trade"
	STEP        = 12.5
	MAX_BACKOFF = 60 * time.Second
	PING_PERIOD = 20 * time.Second
)

func main() {
	// Open or create SHM
	f, err := os.OpenFile(SHM_PATH, os.O_CREATE|os.O_RDWR, 0666)
	if err != nil {
		log.Fatal(err)
	}
	defer f.Close()
	if err := f.Truncate(BUFFER_SIZE); err != nil {
		log.Fatal(err)
	}
	mmap, err := syscall.Mmap(int(f.Fd()), 0, BUFFER_SIZE, syscall.PROT_READ|syscall.PROT_WRITE, syscall.MAP_SHARED)
	if err != nil {
		log.Fatal(err)
	}
	defer syscall.Munmap(mmap)

	// Ensure pipe exists
	if _, err := os.Stat(PIPE_PATH); os.IsNotExist(err) {
		if err := syscall.Mkfifo(PIPE_PATH, 0666); err != nil && !os.IsExist(err) {
			log.Fatal(err)
		}
	}
	pipe, err := os.OpenFile(PIPE_PATH, os.O_WRONLY, os.ModeNamedPipe)
	if err != nil {
		log.Fatal(err)
	}
	defer pipe.Close()

	var checkpointPrice float64
	backoff := time.Second

	for {
		err := runClient(mmap, pipe, &checkpointPrice)
		if err != nil {
			fmt.Println("Client error:", err)
		}
		fmt.Printf("Reconnecting in %v...\n", backoff)
		time.Sleep(backoff)
		backoff *= 2
		if backoff > MAX_BACKOFF {
			backoff = MAX_BACKOFF
		}
	}
}

func runClient(mmap []byte, pipe *os.File, checkpointPrice *float64) error {
	c, _, err := websocket.DefaultDialer.Dial(BINANCE_WS, nil)
	if err != nil {
		return fmt.Errorf("dial error: %w", err)
	}
	defer c.Close()

	// Start ping loop
	done := make(chan struct{})
	go func() {
		ticker := time.NewTicker(PING_PERIOD)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				if err := c.WriteMessage(websocket.PingMessage, []byte("keepalive")); err != nil {
					fmt.Println("Ping error:", err)
					c.Close()
					return
				}
			case <-done:
				return
			}
		}
	}()

	// Read loop
	for {
		_, msg, err := c.ReadMessage()
		if err != nil {
			close(done)
			return fmt.Errorf("read error: %w", err)
		}

		var data struct {
			P string `json:"p"`
		}
		if err := json.Unmarshal(msg, &data); err != nil {
			continue
		}
		price, err := strconv.ParseFloat(data.P, 64)
		if err != nil {
			continue
		}

		if *checkpointPrice == 0 {
			*checkpointPrice = roundTo(price, STEP)
			writePrice(mmap, price)
			pipe.Write([]byte{1})
			fmt.Printf("Starting price checkpoint: %.2f\n", price)
			continue
		}

		change := price - *checkpointPrice
		writePrice(mmap, price)
		pipe.Write([]byte{1})

		if change >= STEP {
			fmt.Println("[ALERT] up to", int(price))
			*checkpointPrice = price
		} else if change <= -STEP {
			fmt.Println("[ALERT] down to", int(price))
			*checkpointPrice = price
		} else {
			fmt.Printf("tick %.2f Δ %.2f\n", price, change)
		}
	}
}

// ===================== Utilities =====================
func roundTo(val, step float64) float64 {
	return float64(int(val/step+0.5)) * step
}

func writePrice(mmap []byte, price float64) {
	str := fmt.Sprintf("%.2f", price)
	copy(mmap, []byte(str))
	if len(str) < len(mmap) {
		mmap[len(str)] = 0
	}
}
