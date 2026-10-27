package mirror_test

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/agentruntime/agent-runtime/services/go-pkg-mirror/internal/mirror"
)

// upstream 是测试用的官方源：PyPI 的 simple 与文件站、npm 的 registry、Go 的 proxy 与 sumdb，都在一个 httptest 服务里。
// 产物是真的（wheel、npm 的 tgz、Go 的模块 zip），真实的 pip、npm、go 也能装（realclients_test.go）。
type upstream struct {
	srv      *httptest.Server
	mu       sync.Mutex
	hits     map[string]int
	down     bool
	wheel    []byte
	sdist    []byte
	tgz      []byte
	modZip   []byte
	badIndex bool // 索引里写错的 sha256
}

func wheelBytes(t *testing.T) []byte {
	t.Helper()
	files := map[string]string{
		"demo_pkg/__init__.py":              "VALUE = 42\n",
		"demo_pkg-1.0.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.0.0\n",
		"demo_pkg-1.0.0.dist-info/WHEEL":    "Wheel-Version: 1.0\nGenerator: agentruntime-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
	}
	var record strings.Builder
	names := []string{"demo_pkg/__init__.py", "demo_pkg-1.0.0.dist-info/METADATA", "demo_pkg-1.0.0.dist-info/WHEEL"}
	for _, n := range names {
		h := sha256.Sum256([]byte(files[n]))
		fmt.Fprintf(&record, "%s,sha256=%s,%d\n", n, base64.RawURLEncoding.EncodeToString(h[:]), len(files[n]))
	}
	record.WriteString("demo_pkg-1.0.0.dist-info/RECORD,,\n")
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	for _, n := range names {
		w, _ := zw.Create(n)
		_, _ = w.Write([]byte(files[n]))
	}
	w, _ := zw.Create("demo_pkg-1.0.0.dist-info/RECORD")
	_, _ = w.Write([]byte(record.String()))
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func tgzBytes(t *testing.T) []byte {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	for name, body := range map[string]string{
		"package/package.json": `{"name":"demo-pkg","version":"1.0.0","main":"index.js"}`,
		"package/index.js":     "module.exports = 42;\n",
	} {
		_ = tw.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: int64(len(body))})
		_, _ = tw.Write([]byte(body))
	}
	_ = tw.Close()
	_ = gz.Close()
	return buf.Bytes()
}

const goMod = "module example.com/demo\n\ngo 1.21\n"

func modZipBytes(t *testing.T) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	for name, body := range map[string]string{
		"example.com/demo@v1.0.0/go.mod":  goMod,
		"example.com/demo@v1.0.0/demo.go": "package demo\n\n// Value 是测试用的值。\nconst Value = 42\n",
	} {
		w, _ := zw.Create(name)
		_, _ = w.Write([]byte(body))
	}
	_ = zw.Close()
	return buf.Bytes()
}

func newUpstream(t *testing.T) *upstream {
	u := &upstream{hits: map[string]int{}, wheel: wheelBytes(t), sdist: []byte("sdist-bytes-0.9"), tgz: tgzBytes(t), modZip: modZipBytes(t)}
	u.srv = httptest.NewServer(http.HandlerFunc(u.handle))
	t.Cleanup(u.srv.Close)
	return u
}

func (u *upstream) hit(path string) int {
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.hits[path]
}

func (u *upstream) total(prefix string) int {
	u.mu.Lock()
	defer u.mu.Unlock()
	n := 0
	for p, c := range u.hits {
		if strings.HasPrefix(p, prefix) {
			n += c
		}
	}
	return n
}

func (u *upstream) upstreams() mirror.Upstreams {
	b := u.srv.URL
	return mirror.Upstreams{PyPISimple: b + "/simple", PyPIFiles: b + "/files", NPM: b + "/npm", GoProxy: b + "/go", SumDB: b + "/sumdb"}
}

func hexSum(data []byte) string {
	h := sha256.Sum256(data)
	return hex.EncodeToString(h[:])
}

func (u *upstream) handle(w http.ResponseWriter, r *http.Request) {
	u.mu.Lock()
	u.hits[r.URL.Path]++
	down, badIndex := u.down, u.badIndex
	u.mu.Unlock()
	if down {
		http.Error(w, "down", http.StatusServiceUnavailable)
		return
	}
	base := u.srv.URL
	switch r.URL.Path {
	case "/simple/demo-pkg/":
		sum := hexSum(u.wheel)
		if badIndex {
			sum = strings.Repeat("0", 64)
		}
		w.Header().Set("Content-Type", "application/vnd.pypi.simple.v1+json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"meta": map[string]any{"api-version": "1.1"}, "name": "demo-pkg",
			"files": []map[string]any{
				{"filename": "demo_pkg-1.0.0-py3-none-any.whl", "url": base + "/files/packages/aa/bb/demo_pkg-1.0.0-py3-none-any.whl",
					"hashes": map[string]string{"sha256": sum}, "requires-python": ">=3.8", "core-metadata": map[string]string{"sha256": "x"}},
				{"filename": "demo-pkg-0.9.tar.gz", "url": base + "/files/packages/cc/dd/demo-pkg-0.9.tar.gz",
					"hashes": map[string]string{"sha256": hexSum(u.sdist)}},
				{"filename": "evil-1.0.whl", "url": "https://elsewhere.example/evil-1.0.whl", "hashes": map[string]string{}},
			},
			"versions": []string{"0.9", "1.0.0"},
		})
	case "/files/packages/aa/bb/demo_pkg-1.0.0-py3-none-any.whl":
		_, _ = w.Write(u.wheel)
	case "/files/packages/cc/dd/demo-pkg-0.9.tar.gz":
		_, _ = w.Write(u.sdist)
	case "/npm/demo-pkg":
		h := sha512.Sum512(u.tgz)
		w.Header().Set("Content-Type", "application/vnd.npm.install-v1+json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"name": "demo-pkg", "dist-tags": map[string]string{"latest": "1.0.0"},
			"versions": map[string]any{
				"1.0.0": map[string]any{"name": "demo-pkg", "version": "1.0.0",
					"dist": map[string]string{"tarball": base + "/npm/demo-pkg/-/demo-pkg-1.0.0.tgz", "integrity": "sha512-" + base64.StdEncoding.EncodeToString(h[:])}},
				"0.9.0": map[string]any{"name": "demo-pkg", "version": "0.9.0",
					"dist": map[string]string{"tarball": base + "/npm/demo-pkg/-/demo-pkg-0.9.0.tgz", "integrity": "sha512-" + base64.StdEncoding.EncodeToString(h[:])}},
			},
		})
	case "/npm/demo-pkg/-/demo-pkg-1.0.0.tgz", "/npm/demo-pkg/-/demo-pkg-0.9.0.tgz":
		_, _ = w.Write(u.tgz)
	case "/go/example.com/demo/@v/list":
		_, _ = w.Write([]byte("v1.0.0\nv0.9.0\n"))
	case "/go/example.com/demo/@latest", "/go/example.com/demo/@v/v1.0.0.info":
		_, _ = w.Write([]byte(`{"Version":"v1.0.0","Time":"2026-01-01T00:00:00Z"}`))
	case "/go/example.com/demo/@v/v1.0.0.mod":
		_, _ = w.Write([]byte(goMod))
	case "/go/example.com/demo/@v/v1.0.0.zip":
		_, _ = w.Write(u.modZip)
	case "/sumdb/lookup/example.com/demo@v1.0.0":
		_, _ = w.Write([]byte("sumdb-answer\n"))
	default:
		http.NotFound(w, r)
	}
}
