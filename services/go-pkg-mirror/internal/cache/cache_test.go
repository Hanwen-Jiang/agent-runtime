package cache

import (
	"bytes"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func put(t *testing.T, b *Blobs, url string, data []byte, expect Expect) (string, error) {
	t.Helper()
	s, err := b.Stage(Origin{Ecosystem: "pypi", Name: "demo", Version: "1.0", URL: url}, expect)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.Write(data); err != nil {
		s.Abort()
		return "", err
	}
	return s.Commit(true)
}

func sum(data []byte) string {
	h := sha256.Sum256(data)
	return hex.EncodeToString(h[:])
}

func open(t *testing.T, opt Options) *Blobs {
	t.Helper()
	if opt.Dir == "" {
		opt.Dir = t.TempDir()
	}
	b, err := Open(opt)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestContentAddressedAndVerifiedAgainstUpstreamHashes(t *testing.T) {
	b := open(t, Options{Budget: 1 << 20})
	data := []byte("wheel bytes")
	key, err := put(t, b, "https://files/demo-1.0.whl", data, Expect{SHA256: sum(data)})
	if err != nil || key != sum(data) {
		t.Fatalf("%s %v", key, err)
	}
	if got, ok := b.Lookup("https://files/demo-1.0.whl"); !ok || got != key {
		t.Fatal("按上游地址找得到")
	}
	r, err := b.Get(key)
	if err != nil {
		t.Fatal(err)
	}
	read, _ := io.ReadAll(r)
	r.Close()
	if !bytes.Equal(read, data) {
		t.Fatal("内容不对")
	}
	// 同一个内容第二次进库只存一份。
	if k2, _ := put(t, b, "https://mirror2/demo-1.0.whl", data, Expect{}); k2 != key || b.Stats().Files != 1 {
		t.Fatal("同一个内容只存一份")
	}
	if _, err := put(t, b, "https://files/bad.whl", []byte("tampered"), Expect{SHA256: sum(data)}); !errors.Is(err, ErrMismatch) {
		t.Fatalf("哈希对不上要拒：%v", err)
	}
	if _, ok := b.Lookup("https://files/bad.whl"); ok || b.Stats().Mismatch != 1 {
		t.Fatal("对不上的不进库")
	}
	tgz := []byte("npm tarball")
	h := sha512.Sum512(tgz)
	if _, err := put(t, b, "https://npm/x.tgz", tgz, Expect{Integrity: "sha512-" + base64.StdEncoding.EncodeToString(h[:])}); err != nil {
		t.Fatalf("npm 的 integrity 对得上：%v", err)
	}
	if _, err := put(t, b, "https://npm/y.tgz", []byte("other"), Expect{Integrity: "sha512-" + base64.StdEncoding.EncodeToString(h[:])}); !errors.Is(err, ErrMismatch) {
		t.Fatal("npm 的 integrity 对不上要拒")
	}
}

func TestTamperedFileIsNotServedAndIsReported(t *testing.T) {
	var alerts []string
	b := open(t, Options{Budget: 1 << 20, Alert: func(kind, detail string) { alerts = append(alerts, kind) }})
	data := []byte("good bytes")
	key, _ := put(t, b, "u", data, Expect{SHA256: sum(data)})
	if err := os.WriteFile(b.path(key), []byte("evil bytes"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := b.Get(key); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("被改过的文件不给：%v", err)
	}
	if _, ok := b.Lookup("u"); ok || len(alerts) != 1 || alerts[0] != "blob_corrupt" {
		t.Fatalf("删掉并告警：%v", alerts)
	}
}

func TestUsageStaysUnderTheHighWatermarkAndHotFilesSurviveAFlood(t *testing.T) {
	const budget = 1 << 20 // 1 MiB：验收是 1 GB 灌 3 GB，这里按比例缩小
	b := open(t, Options{Budget: budget})
	hot := bytes.Repeat([]byte("h"), 20<<10)
	hotKey, _ := put(t, b, "hot", hot, Expect{})
	budgetBytes := int64(budget)
	high := int64(float64(budgetBytes) * 0.9)
	for i := 0; i < 150; i++ { // 150 × 20 KiB ≈ 3 MiB 的不同文件
		data := bytes.Repeat([]byte{byte(i)}, 20<<10)
		data = append(data, []byte(fmt.Sprint(i))...)
		if _, err := put(t, b, fmt.Sprintf("flood-%d", i), data, Expect{}); err != nil {
			t.Fatal(err)
		}
		if used := b.Stats().Used; used > high {
			t.Fatalf("第 %d 个之后用量 %d 超过了高水位 %d", i, used, high)
		}
		if i%10 == 0 { // 常用的包被反复用
			r, err := b.Get(hotKey)
			if err != nil {
				t.Fatalf("常用的包被挤掉了（第 %d 个之后）", i)
			}
			r.Close()
		}
	}
	if seg, ok := b.Segment(hotKey); !ok || seg != Protected {
		t.Fatalf("常用的包在保护段：%v %v", seg, ok)
	}
	if b.Stats().Evicted == 0 {
		t.Fatal("应该淘汰过")
	}
}

func TestOversizedFilesAreRefusedAndPinnedOnesAreNeverEvicted(t *testing.T) {
	b := open(t, Options{Budget: 100 << 10, MaxFile: 50 << 10, Pinned: func(o Origin) bool { return o.URL == "pinned" }})
	if _, err := put(t, b, "big", bytes.Repeat([]byte("x"), 60<<10), Expect{}); !errors.Is(err, ErrTooLarge) {
		t.Fatalf("超过大小上限的不进库：%v", err)
	}
	pinned, _ := put(t, b, "pinned", bytes.Repeat([]byte("p"), 30<<10), Expect{})
	for i := 0; i < 20; i++ {
		_, _ = put(t, b, fmt.Sprintf("f%d", i), append(bytes.Repeat([]byte("f"), 30<<10), byte(i)), Expect{})
	}
	if seg, ok := b.Segment(pinned); !ok || seg != Pinned {
		t.Fatal("常驻的不淘汰")
	}
}

func TestFilesBeingReadAreDeletedOnlyAfterTheReaderCloses(t *testing.T) {
	b := open(t, Options{Budget: 100 << 10})
	data := bytes.Repeat([]byte("r"), 40<<10)
	key, _ := put(t, b, "reading", data, Expect{})
	r, err := b.Get(key)
	if err != nil {
		t.Fatal(err)
	}
	if n := b.Purge("pypi", "demo", "1.0"); n != 1 {
		t.Fatalf("清除：%d", n)
	}
	if _, err := os.Stat(b.path(key)); err != nil {
		t.Fatal("正在读的文件不能当场删")
	}
	read, _ := io.ReadAll(r)
	r.Close()
	if !bytes.Equal(read, data) {
		t.Fatal("读到一半被删")
	}
	if _, err := os.Stat(b.path(key)); !os.IsNotExist(err) {
		t.Fatal("读完之后删掉")
	}
	if b.Stats().Used != 0 {
		t.Fatalf("用量：%d", b.Stats().Used)
	}
}

func TestStateSurvivesARestartAndOrphansAndTempFilesAreCleaned(t *testing.T) {
	dir := t.TempDir()
	b := open(t, Options{Dir: dir, Budget: 1 << 20})
	data := []byte("persisted")
	key, _ := put(t, b, "kept", data, Expect{})
	if err := b.Save(); err != nil {
		t.Fatal(err)
	}
	orphan := filepath.Join(dir, "blobs", "ab", "ab"+fmt.Sprintf("%062d", 0))
	_ = os.MkdirAll(filepath.Dir(orphan), 0o755)
	_ = os.WriteFile(orphan, []byte("x"), 0o644)
	partial := filepath.Join(dir, "tmp", "dl-123")
	_ = os.WriteFile(partial, []byte("half"), 0o644)
	again := open(t, Options{Dir: dir, Budget: 1 << 20})
	if got, ok := again.Lookup("kept"); !ok || got != key {
		t.Fatal("重启后还认得")
	}
	if _, err := os.Stat(orphan); !os.IsNotExist(err) {
		t.Fatal("孤儿文件要删")
	}
	if _, err := os.Stat(partial); !os.IsNotExist(err) {
		t.Fatal("下载到一半的临时文件要清")
	}
}

func TestMetadataExpiresButStaysAvailableAndHasItsOwnBudget(t *testing.T) {
	m, err := OpenMeta(t.TempDir(), 100)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	_ = m.Put("pypi:demo", []byte("index v1"), "application/json", now)
	if e, fresh, ok := m.Get("pypi:demo", 5*time.Minute, now.Add(time.Minute)); !ok || !fresh || string(e.Body) != "index v1" {
		t.Fatal("没过期")
	}
	if _, fresh, ok := m.Get("pypi:demo", 5*time.Minute, now.Add(10*time.Minute)); !ok || fresh {
		t.Fatal("过期了照样给（旧的），标为不新鲜")
	}
	for i := 0; i < 20; i++ {
		_ = m.Put(fmt.Sprintf("k%d", i), bytes.Repeat([]byte("m"), 10), "", now)
	}
	if m.Used() > 100 {
		t.Fatalf("元数据超了预算：%d", m.Used())
	}
	if _, _, ok := m.Get("pypi:demo", time.Hour, now); ok {
		t.Fatal("最久没用的被淘汰")
	}
}
