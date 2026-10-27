// Package cache 是包镜像的两块存储（设计书改动稿 W7；docs/plan/cloud-deploy.md D-15）：包文件（Blobs）与元数据（Meta）。
//
// 包文件发布后不变：按 sha256 内容寻址，同一个文件只存一份；写入时先落临时文件、边写边算哈希，与上游给的哈希（PyPI 的
// sha256、npm 的 sha512 integrity）对得上才改名进库，对不上不缓存、不返回。读出时再算一遍，被改过（磁盘坏、有人动过）
// 就删掉、记告警，当作没命中。
//
// 容量：总预算 budget；用量到高水位（默认 90%）开始淘汰，降到低水位（默认 75%）停。淘汰用两段 LRU：第一次拉到的进试用段，
// 再次被用才升到保护段（保护段最多占预算的 protectedShare，超了把保护段最旧的降回试用段）；先淘汰试用段，再淘汰保护段。
// 一次性拉一大棵依赖树挤不掉常用的包。常驻（pinned）的不淘汰。正在被读的文件不当场删：标记为待删，最后一个读者关掉时再删。
// 超过 maxFile 的文件不进库（只转发，由调用方处理）。
//
// 状态（每个文件的大小、段、来源）定期写进 state.json；启动时读回，库里有、状态里没有的文件（孤儿）删掉，临时目录清空。
package cache

import (
	"container/list"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

// Segment 是一个文件所在的段。
type Segment int

const (
	Probation Segment = iota
	Protected
	Pinned
)

func (s Segment) String() string {
	return [...]string{"probation", "protected", "pinned"}[s]
}

// Origin 是一个文件的来源：哪个生态、哪个包、哪个版本（清除与拉黑用），以及上游地址。
type Origin struct {
	Ecosystem string `json:"ecosystem"`
	Name      string `json:"name"`
	Version   string `json:"version"`
	URL       string `json:"url"`
}

// Expect 是上游给的校验值；都为空时只按内容寻址，不另核对。
type Expect struct {
	SHA256 string // 十六进制
	// Integrity 是 npm 的 sha512-<base64>（或 sha1-…，不支持时忽略）。
	Integrity string
}

// ErrMismatch 表示下载的内容与上游给的哈希对不上。
var ErrMismatch = errors.New("包文件的哈希与上游给的对不上")

// ErrTooLarge 表示文件超过了大小上限，不进库。
var ErrTooLarge = errors.New("包文件超过大小上限")

// ErrCorrupt 表示库里的文件被改过。
var ErrCorrupt = errors.New("库里的包文件被改过")

var hexKey = regexp.MustCompile(`^[0-9a-f]{64}$`)

type entry struct {
	Key    string  `json:"key"`
	Size   int64   `json:"size"`
	Seg    Segment `json:"seg"`
	Origin Origin  `json:"origin"`
	refs   int
	doomed bool
	elem   *list.Element
	// 指向它的上游地址（同一个内容可能有几个地址）。
	urls map[string]bool
}

// Options 是 Blobs 的配置。
type Options struct {
	Dir            string
	Budget         int64
	HighWatermark  float64
	LowWatermark   float64
	ProtectedShare float64
	MaxFile        int64
	// Pinned 判断一个来源是不是常驻的（预热的常用包）。
	Pinned func(Origin) bool
	// Alert 收告警（文件被改过、上游哈希对不上）；不记凭据。
	Alert func(kind, detail string)
}

// Stats 是给指标用的计数。
type Stats struct {
	Used      int64
	BySegment map[string]int64
	Files     int
	Evicted   int64
	Corrupt   int64
	Mismatch  int64
}

// Blobs 是内容寻址的包文件库。
type Blobs struct {
	opt       Options
	mu        sync.Mutex
	entries   map[string]*entry
	byURL     map[string]string
	probation *list.List
	protected *list.List
	pinned    *list.List
	used      int64
	segUsed   [3]int64
	evicted   int64
	corrupt   int64
	mismatch  int64
	dirty     bool
}

// Open 打开（或新建）库，读回状态、清掉孤儿与临时文件。
func Open(opt Options) (*Blobs, error) {
	if opt.Budget <= 0 {
		return nil, errors.New("包文件的预算要大于 0")
	}
	if opt.HighWatermark <= 0 {
		opt.HighWatermark = 0.9
	}
	if opt.LowWatermark <= 0 {
		opt.LowWatermark = 0.75
	}
	if opt.ProtectedShare <= 0 {
		opt.ProtectedShare = 0.8
	}
	if opt.LowWatermark >= opt.HighWatermark || opt.HighWatermark > 1 {
		return nil, errors.New("水位要满足 0 < 低水位 < 高水位 <= 1")
	}
	if opt.MaxFile <= 0 {
		opt.MaxFile = 500 << 20
	}
	if opt.Pinned == nil {
		opt.Pinned = func(Origin) bool { return false }
	}
	if opt.Alert == nil {
		opt.Alert = func(string, string) {}
	}
	b := &Blobs{opt: opt, entries: map[string]*entry{}, byURL: map[string]string{}, probation: list.New(), protected: list.New(), pinned: list.New()}
	for _, d := range []string{b.blobDir(), b.tmpDir()} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, err
		}
	}
	if err := b.load(); err != nil {
		return nil, err
	}
	b.CleanTemp(0)
	return b, nil
}

func (b *Blobs) blobDir() string { return filepath.Join(b.opt.Dir, "blobs") }
func (b *Blobs) tmpDir() string  { return filepath.Join(b.opt.Dir, "tmp") }
func (b *Blobs) statePath() string {
	return filepath.Join(b.opt.Dir, "state.json")
}

func (b *Blobs) path(key string) string {
	return filepath.Join(b.blobDir(), key[:2], key)
}

// MaxFile 是能进库的最大文件。
func (b *Blobs) MaxFile() int64 { return b.opt.MaxFile }

// Lookup 按上游地址找库里的文件。
func (b *Blobs) Lookup(url string) (string, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	key, ok := b.byURL[url]
	if ok {
		if e := b.entries[key]; e == nil || e.doomed {
			return "", false
		}
	}
	return key, ok
}

// Reader 是从库里读一个文件；读完要 Close。
type Reader struct {
	*os.File
	Size   int64
	Origin Origin
	b      *Blobs
	key    string
}

// Close 关掉文件；待删的文件在最后一个读者关掉时删除。
func (r *Reader) Close() error {
	err := r.File.Close()
	r.b.release(r.key)
	return err
}

// Get 打开库里的文件：先核对内容的哈希，对不上就删掉、记告警，返回 ErrCorrupt（调用方当作没命中）。命中会让它在 LRU 里前移。
func (b *Blobs) Get(key string) (*Reader, error) {
	b.mu.Lock()
	e := b.entries[key]
	if e == nil || e.doomed {
		b.mu.Unlock()
		return nil, fs.ErrNotExist
	}
	e.refs++
	b.touchLocked(e)
	b.mu.Unlock()
	f, err := os.Open(b.path(key))
	if err != nil {
		b.release(key)
		b.mu.Lock()
		b.removeLocked(e)
		b.mu.Unlock()
		return nil, fs.ErrNotExist
	}
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil || hex.EncodeToString(h.Sum(nil)) != key {
		f.Close()
		b.release(key)
		b.mu.Lock()
		b.corrupt++
		b.removeLocked(e)
		b.mu.Unlock()
		b.opt.Alert("blob_corrupt", fmt.Sprintf("%s %s@%s：库里的文件被改过，已删除", e.Origin.Ecosystem, e.Origin.Name, e.Origin.Version))
		return nil, ErrCorrupt
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		f.Close()
		b.release(key)
		return nil, err
	}
	return &Reader{File: f, Size: e.Size, Origin: e.Origin, b: b, key: key}, nil
}

func (b *Blobs) release(key string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	e := b.entries[key]
	if e == nil {
		return
	}
	e.refs--
	if e.refs <= 0 && e.doomed {
		b.deleteLocked(e)
	}
}

// Staged 是下载到一半的文件：边写边算哈希，Commit 核对后进库，Abort 丢掉。
type Staged struct {
	b      *Blobs
	f      *os.File
	sha256 hash.Hash
	sha512 hash.Hash
	size   int64
	origin Origin
	expect Expect
	done   bool
}

// Stage 开始一个下载。
func (b *Blobs) Stage(origin Origin, expect Expect) (*Staged, error) {
	f, err := os.CreateTemp(b.tmpDir(), "dl-*")
	if err != nil {
		return nil, err
	}
	s := &Staged{b: b, f: f, sha256: sha256.New(), origin: origin, expect: expect}
	if strings.HasPrefix(expect.Integrity, "sha512-") {
		s.sha512 = sha512.New()
	}
	return s, nil
}

// Write 写一段；超过大小上限时返回 ErrTooLarge（已写的仍在临时文件里，Abort 丢掉）。
func (s *Staged) Write(p []byte) (int, error) {
	if s.size+int64(len(p)) > s.b.opt.MaxFile {
		return 0, ErrTooLarge
	}
	n, err := s.f.Write(p)
	s.size += int64(n)
	s.sha256.Write(p[:n])
	if s.sha512 != nil {
		s.sha512.Write(p[:n])
	}
	return n, err
}

// Abort 丢掉下载到一半的文件。
func (s *Staged) Abort() {
	if s.done {
		return
	}
	s.done = true
	name := s.f.Name()
	s.f.Close()
	_ = os.Remove(name)
}

// Commit 核对上游给的哈希，对得上就进库（store 为 false 时只核对、不进库：调用方转发后丢掉），返回内容的 sha256。
// 对不上返回 ErrMismatch，文件丢掉。
func (s *Staged) Commit(store bool) (string, error) {
	if s.done {
		return "", errors.New("已经结束的下载")
	}
	key := hex.EncodeToString(s.sha256.Sum(nil))
	if s.expect.SHA256 != "" && !strings.EqualFold(s.expect.SHA256, key) {
		s.Abort()
		s.b.mu.Lock()
		s.b.mismatch++
		s.b.mu.Unlock()
		s.b.opt.Alert("upstream_mismatch", fmt.Sprintf("%s %s@%s：sha256 与上游的索引不符", s.origin.Ecosystem, s.origin.Name, s.origin.Version))
		return "", ErrMismatch
	}
	if s.sha512 != nil {
		got := "sha512-" + base64.StdEncoding.EncodeToString(s.sha512.Sum(nil))
		if got != s.expect.Integrity {
			s.Abort()
			s.b.mu.Lock()
			s.b.mismatch++
			s.b.mu.Unlock()
			s.b.opt.Alert("upstream_mismatch", fmt.Sprintf("%s %s@%s：integrity 与上游的包信息不符", s.origin.Ecosystem, s.origin.Name, s.origin.Version))
			return "", ErrMismatch
		}
	}
	if err := s.f.Sync(); err != nil {
		s.Abort()
		return "", err
	}
	name := s.f.Name()
	if err := s.f.Close(); err != nil {
		s.Abort()
		return "", err
	}
	s.done = true
	if !store {
		return key, nil
	}
	return key, s.b.insert(name, key, s.size, s.origin)
}

// TempPath 是下载到一半的文件的位置（只转发不进库时，调用方从这里读，读完 Discard）。
func (s *Staged) TempPath() string { return s.f.Name() }

// Discard 删掉只转发、不进库的临时文件。
func (s *Staged) Discard() { _ = os.Remove(s.f.Name()) }

func (b *Blobs) insert(tmp, key string, size int64, origin Origin) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if e := b.entries[key]; e != nil && !e.doomed {
		_ = os.Remove(tmp) // 同一个内容已经有了
		if origin.URL != "" {
			b.byURL[origin.URL] = key
			e.urls[origin.URL] = true
		}
		b.touchLocked(e)
		return nil
	}
	dest := b.path(key)
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, dest); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	seg := Probation
	if b.opt.Pinned(origin) {
		seg = Pinned
	}
	e := &entry{Key: key, Size: size, Seg: seg, Origin: origin, urls: map[string]bool{}}
	e.elem = b.list(seg).PushFront(e)
	b.entries[key] = e
	if origin.URL != "" {
		b.byURL[origin.URL] = key
		e.urls[origin.URL] = true
	}
	b.used += size
	b.segUsed[seg] += size
	b.dirty = true
	b.evictLocked()
	return nil
}

func (b *Blobs) list(seg Segment) *list.List {
	switch seg {
	case Protected:
		return b.protected
	case Pinned:
		return b.pinned
	default:
		return b.probation
	}
}

// touchLocked：命中。试用段里的升到保护段；保护段里的移到最前；保护段超了份额，最旧的降回试用段。
func (b *Blobs) touchLocked(e *entry) {
	switch e.Seg {
	case Pinned:
		b.pinned.MoveToFront(e.elem)
		return
	case Probation:
		b.probation.Remove(e.elem)
		b.segUsed[Probation] -= e.Size
		e.Seg = Protected
		e.elem = b.protected.PushFront(e)
		b.segUsed[Protected] += e.Size
	default:
		b.protected.MoveToFront(e.elem)
	}
	limit := int64(float64(b.opt.Budget) * b.opt.ProtectedShare)
	for b.segUsed[Protected] > limit && b.protected.Len() > 1 {
		old := b.protected.Back().Value.(*entry)
		b.protected.Remove(old.elem)
		b.segUsed[Protected] -= old.Size
		old.Seg = Probation
		old.elem = b.probation.PushFront(old)
		b.segUsed[Probation] += old.Size
	}
	b.dirty = true
}

// evictLocked：到了高水位就从试用段最旧的开始淘汰，降到低水位为止；不够再淘汰保护段。常驻的不淘汰。
func (b *Blobs) evictLocked() {
	high := int64(float64(b.opt.Budget) * b.opt.HighWatermark)
	low := int64(float64(b.opt.Budget) * b.opt.LowWatermark)
	if b.used <= high {
		return
	}
	for _, l := range []*list.List{b.probation, b.protected} {
		for el := l.Back(); el != nil && b.used > low; {
			prev := el.Prev()
			e := el.Value.(*entry)
			if !e.doomed {
				b.evicted++
				b.removeLocked(e)
			}
			el = prev
		}
		if b.used <= low {
			return
		}
	}
}

// removeLocked 把文件从库里拿掉：没人在读就当场删，有人在读就标记待删（用量在真删时才减）。
func (b *Blobs) removeLocked(e *entry) {
	if e.elem != nil {
		b.list(e.Seg).Remove(e.elem)
		e.elem = nil
	}
	for url := range e.urls {
		if b.byURL[url] == e.Key {
			delete(b.byURL, url)
		}
	}
	e.doomed = true
	b.dirty = true
	if e.refs <= 0 {
		b.deleteLocked(e)
	}
}

func (b *Blobs) deleteLocked(e *entry) {
	if _, ok := b.entries[e.Key]; !ok {
		return
	}
	delete(b.entries, e.Key)
	_ = os.Remove(b.path(e.Key))
	b.used -= e.Size
	b.segUsed[e.Seg] -= e.Size
	b.dirty = true
}

// Purge 删掉来源匹配（生态、包名、版本；版本为空时是这个包的全部版本）的文件，返回删了几个。正在被读的读完再删。
func (b *Blobs) Purge(ecosystem, name, version string) int {
	b.mu.Lock()
	defer b.mu.Unlock()
	n := 0
	for _, e := range b.entries {
		if e.doomed || e.Origin.Ecosystem != ecosystem || !strings.EqualFold(e.Origin.Name, name) || (version != "" && e.Origin.Version != version) {
			continue
		}
		b.removeLocked(e)
		n++
	}
	return n
}

// CleanTemp 删掉临时目录里早于 olderThan 的文件（下载到一半中断留下的）；olderThan 为 0 时全删（启动时）。
func (b *Blobs) CleanTemp(olderThan time.Duration) int {
	entries, err := os.ReadDir(b.tmpDir())
	if err != nil {
		return 0
	}
	n := 0
	for _, de := range entries {
		info, err := de.Info()
		if err != nil {
			continue
		}
		if olderThan == 0 || time.Since(info.ModTime()) > olderThan {
			if os.Remove(filepath.Join(b.tmpDir(), de.Name())) == nil {
				n++
			}
		}
	}
	return n
}

// Stats 是当前的计数。
func (b *Blobs) Stats() Stats {
	b.mu.Lock()
	defer b.mu.Unlock()
	return Stats{
		Used: b.used, Files: len(b.entries), Evicted: b.evicted, Corrupt: b.corrupt, Mismatch: b.mismatch,
		BySegment: map[string]int64{"probation": b.segUsed[Probation], "protected": b.segUsed[Protected], "pinned": b.segUsed[Pinned]},
	}
}

// Segment 是一个文件当前所在的段（测试与指标用）。
func (b *Blobs) Segment(key string) (Segment, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	e := b.entries[key]
	if e == nil || e.doomed {
		return 0, false
	}
	return e.Seg, true
}

// --- 状态 ---

type state struct {
	Version int      `json:"version"`
	Entries []*entry `json:"entries"` // 按段、从新到旧
}

// Save 把状态写进 state.json（有改动时）。
func (b *Blobs) Save() error {
	b.mu.Lock()
	if !b.dirty {
		b.mu.Unlock()
		return nil
	}
	st := state{Version: 1}
	for _, l := range []*list.List{b.pinned, b.protected, b.probation} {
		for el := l.Front(); el != nil; el = el.Next() {
			e := el.Value.(*entry)
			st.Entries = append(st.Entries, &entry{Key: e.Key, Size: e.Size, Seg: e.Seg, Origin: e.Origin})
		}
	}
	b.dirty = false
	b.mu.Unlock()
	data, err := json.Marshal(st)
	if err != nil {
		return err
	}
	tmp := b.statePath() + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, b.statePath())
}

func (b *Blobs) load() error {
	known := map[string]bool{}
	if data, err := os.ReadFile(b.statePath()); err == nil {
		var st state
		if err := json.Unmarshal(data, &st); err == nil {
			for _, e := range st.Entries {
				if !hexKey.MatchString(e.Key) || b.entries[e.Key] != nil {
					continue
				}
				info, err := os.Stat(b.path(e.Key))
				if err != nil || info.Size() != e.Size {
					continue // 状态里有、文件没了（或大小不对）：丢掉
				}
				e.elem = b.list(e.Seg).PushBack(e)
				e.urls = map[string]bool{}
				b.entries[e.Key] = e
				if e.Origin.URL != "" {
					b.byURL[e.Origin.URL] = e.Key
					e.urls[e.Origin.URL] = true
				}
				b.used += e.Size
				b.segUsed[e.Seg] += e.Size
				known[e.Key] = true
			}
		}
	}
	// 孤儿：库里有、状态里没有的文件。
	var orphans []string
	_ = filepath.WalkDir(b.blobDir(), func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		if !known[d.Name()] {
			orphans = append(orphans, path)
		}
		return nil
	})
	sort.Strings(orphans)
	for _, p := range orphans {
		_ = os.Remove(p)
	}
	b.dirty = len(orphans) > 0
	return nil
}
