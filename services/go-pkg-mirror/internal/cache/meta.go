package cache

import (
	"container/list"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Meta 是元数据（索引页、npm 的包信息、版本列表、sumdb 的应答）：会变，单独的小预算，过期按调用方给的 TTL。
// 过期了照样给出（stale），调用方先给旧的、在后台刷新；上游不可用时继续给旧的。超过预算按 LRU 淘汰。
// 内容放在 <dir>/meta/<sha256(key)>，索引在内存里（启动时从文件重建）。
type Meta struct {
	dir    string
	budget int64
	mu     sync.Mutex
	lru    *list.List
	items  map[string]*metaItem
	used   int64
}

// MetaEntry 是取出的一份元数据。
type MetaEntry struct {
	Body        []byte
	ContentType string
	FetchedAt   time.Time
}

type metaItem struct {
	Key         string    `json:"key"`
	ContentType string    `json:"content_type"`
	FetchedAt   time.Time `json:"fetched_at"`
	Size        int64     `json:"size"`
	elem        *list.Element
}

// OpenMeta 打开元数据缓存。
func OpenMeta(dir string, budget int64) (*Meta, error) {
	if budget <= 0 {
		return nil, errors.New("元数据的预算要大于 0")
	}
	m := &Meta{dir: filepath.Join(dir, "meta"), budget: budget, lru: list.New(), items: map[string]*metaItem{}}
	if err := os.MkdirAll(m.dir, 0o755); err != nil {
		return nil, err
	}
	entries, _ := os.ReadDir(m.dir)
	for _, de := range entries {
		if filepath.Ext(de.Name()) != ".json" {
			continue
		}
		data, err := os.ReadFile(filepath.Join(m.dir, de.Name()))
		if err != nil {
			continue
		}
		var it metaItem
		if json.Unmarshal(data, &it) != nil || it.Key == "" {
			continue
		}
		if _, err := os.Stat(m.bodyPath(it.Key)); err != nil {
			continue
		}
		copied := it
		copied.elem = m.lru.PushBack(&copied)
		m.items[it.Key] = &copied
		m.used += it.Size
	}
	return m, nil
}

func (m *Meta) name(key string) string {
	sum := sha256.Sum256([]byte(key))
	return hex.EncodeToString(sum[:])
}

func (m *Meta) bodyPath(key string) string { return filepath.Join(m.dir, m.name(key)) }
func (m *Meta) infoPath(key string) string { return filepath.Join(m.dir, m.name(key)+".json") }

// Get 取一份元数据；fresh 表示还没过 ttl。
func (m *Meta) Get(key string, ttl time.Duration, now time.Time) (MetaEntry, bool, bool) {
	m.mu.Lock()
	it := m.items[key]
	if it == nil {
		m.mu.Unlock()
		return MetaEntry{}, false, false
	}
	m.lru.MoveToFront(it.elem)
	ct, fetched := it.ContentType, it.FetchedAt
	m.mu.Unlock()
	body, err := os.ReadFile(m.bodyPath(key))
	if err != nil {
		m.Delete(key)
		return MetaEntry{}, false, false
	}
	return MetaEntry{Body: body, ContentType: ct, FetchedAt: fetched}, now.Sub(fetched) < ttl, true
}

// Put 存一份元数据（覆盖旧的），超了预算就淘汰最久没用的。
func (m *Meta) Put(key string, body []byte, contentType string, now time.Time) error {
	if int64(len(body)) > m.budget {
		return nil // 比整个预算还大：不缓存
	}
	if err := os.WriteFile(m.bodyPath(key)+".tmp", body, 0o644); err != nil {
		return err
	}
	if err := os.Rename(m.bodyPath(key)+".tmp", m.bodyPath(key)); err != nil {
		return err
	}
	info := metaItem{Key: key, ContentType: contentType, FetchedAt: now, Size: int64(len(body))}
	data, _ := json.Marshal(info)
	if err := os.WriteFile(m.infoPath(key), data, 0o644); err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if old := m.items[key]; old != nil {
		m.used -= old.Size
		m.lru.Remove(old.elem)
	}
	it := &info
	it.elem = m.lru.PushFront(it)
	m.items[key] = it
	m.used += it.Size
	for m.used > m.budget && m.lru.Len() > 1 {
		oldest := m.lru.Back().Value.(*metaItem)
		m.removeLocked(oldest)
	}
	return nil
}

// Delete 删掉一份元数据（清除、拉黑一个包时）。
func (m *Meta) Delete(key string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if it := m.items[key]; it != nil {
		m.removeLocked(it)
	}
}

func (m *Meta) removeLocked(it *metaItem) {
	m.lru.Remove(it.elem)
	delete(m.items, it.Key)
	m.used -= it.Size
	_ = os.Remove(m.bodyPath(it.Key))
	_ = os.Remove(m.infoPath(it.Key))
}

// Used 是元数据当前占的字节数。
func (m *Meta) Used() int64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.used
}
