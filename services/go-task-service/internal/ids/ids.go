// Package ids 生成 Session 与 Task 的 ID：前缀加 26 位 ULID（proto IdKind，schemas/defs.schema.json）。
// ULID 的前 48 位是毫秒时间戳，同一毫秒内单调递增，字典序与生成顺序一致。
package ids

import (
	"crypto/rand"
	"io"
	"regexp"
	"sync"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
)

// Crockford base32，不含 I、L、O、U。
const alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

const (
	sessionPrefix = "ses_"
	taskPrefix    = "tsk_"
)

var (
	sessionID = regexp.MustCompile(`^ses_[0-7][0-9A-HJKMNP-TV-Z]{25}$`)
	taskID    = regexp.MustCompile(`^tsk_[0-7][0-9A-HJKMNP-TV-Z]{25}$`)
)

// Generator 生成 ID。
type Generator interface {
	SessionID() string
	TaskID() string
}

type generator struct {
	mu      sync.Mutex
	clock   clock.Clock
	entropy io.Reader
	lastMs  uint64
	random  [10]byte // 80 位随机部分
}

// New 构造生成器。entropy 为 nil 时用 crypto/rand。
func New(c clock.Clock, entropy io.Reader) Generator {
	if c == nil {
		c = clock.System{}
	}
	if entropy == nil {
		entropy = rand.Reader
	}
	return &generator{clock: c, entropy: entropy}
}

func (g *generator) SessionID() string { return sessionPrefix + g.next() }
func (g *generator) TaskID() string    { return taskPrefix + g.next() }

func (g *generator) next() string {
	g.mu.Lock()
	defer g.mu.Unlock()
	ms := uint64(g.clock.Now().UnixMilli()) & (1<<48 - 1)
	if ms <= g.lastMs && g.lastMs != 0 {
		// 同一毫秒内（或时钟回拨）：沿用上一个时间戳，随机部分加 1，保证单调。
		ms = g.lastMs
		if !increment(&g.random) {
			ms++
			g.fill()
		}
	} else {
		g.fill()
	}
	g.lastMs = ms
	return encode(ms, g.random)
}

func (g *generator) fill() {
	if _, err := io.ReadFull(g.entropy, g.random[:]); err != nil {
		panic("ids: 读取随机数失败：" + err.Error())
	}
}

// increment 把 80 位随机部分加 1；溢出时返回 false。
func increment(b *[10]byte) bool {
	for i := len(b) - 1; i >= 0; i-- {
		b[i]++
		if b[i] != 0 {
			return true
		}
	}
	return false
}

// encode 把 48 位时间戳与 80 位随机数编码成 26 位 Crockford base32。
func encode(ms uint64, random [10]byte) string {
	// 128 位整数：hi 是高 64 位，lo 是低 64 位。
	hi := ms<<16 | uint64(random[0])<<8 | uint64(random[1])
	var lo uint64
	for _, b := range random[2:] {
		lo = lo<<8 | uint64(b)
	}
	var out [26]byte
	for i := 25; i >= 0; i-- {
		out[i] = alphabet[lo&31]
		lo = lo>>5 | hi<<59
		hi >>= 5
	}
	return string(out[:])
}

// ValidSessionID 判断 s 是否是合乎格式的 session_id。
func ValidSessionID(s string) bool { return sessionID.MatchString(s) }

// ValidTaskID 判断 s 是否是合乎格式的 task_id。
func ValidTaskID(s string) bool { return taskID.MatchString(s) }
