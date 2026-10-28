package agent

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// D-06a：工作区的存档与恢复、清单、列目录与搜索（docs/plan/workspace-layer.md）。

func writeTree(t *testing.T, dir string, files map[string]string) {
	t.Helper()
	for rel, body := range files {
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func (h *harness) manifestOf() []Entry {
	h.t.Helper()
	res := h.request(http.MethodGet, "/v1/manifest", nil, testToken)
	defer res.Body.Close()
	var out struct{ Entries []Entry }
	if res.StatusCode != http.StatusOK || json.NewDecoder(res.Body).Decode(&out) != nil {
		h.t.Fatalf("清单：HTTP %d", res.StatusCode)
	}
	return out.Entries
}

func (h *harness) export(limit string) (int, []byte) {
	h.t.Helper()
	res := h.request(http.MethodGet, "/v1/archive?max_bytes="+limit, nil, testToken)
	defer res.Body.Close()
	data, _ := io.ReadAll(res.Body)
	return res.StatusCode, data
}

func (h *harness) importTar(data []byte, limit string) (int, apiError) {
	h.t.Helper()
	res := h.request(http.MethodPut, "/v1/archive?max_bytes="+limit, bytes.NewReader(data), testToken)
	defer res.Body.Close()
	var bad apiError
	body, _ := io.ReadAll(res.Body)
	_ = json.Unmarshal(body, &bad)
	return res.StatusCode, bad
}

type tarItem struct {
	name, body, link string
	typ              byte
	mode             int64
}

func makeTar(t *testing.T, items ...tarItem) []byte {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	for _, it := range items {
		hdr := &tar.Header{Name: it.name, Typeflag: it.typ, Mode: it.mode, Linkname: it.link, Size: int64(len(it.body))}
		if hdr.Mode == 0 {
			hdr.Mode = 0o644
		}
		if it.typ != tar.TypeReg {
			hdr.Size = 0
		}
		if err := tw.WriteHeader(hdr); err != nil {
			t.Fatal(err)
		}
		if it.typ == tar.TypeReg {
			_, _ = tw.Write([]byte(it.body))
		}
	}
	_ = tw.Close()
	return buf.Bytes()
}

func comparable(entries []Entry) []Entry {
	out := make([]Entry, 0, len(entries))
	for _, e := range entries {
		if runtime.GOOS == "windows" {
			e.Mode = 0 // Windows 上没有权限位
		}
		out = append(out, e)
	}
	return out
}

func TestArchiveRoundTripsIntoAFreshEnvironmentAndTheManifestMatches(t *testing.T) {
	src := newHarness(t, nil)
	writeTree(t, src.dir, map[string]string{"README.md": "# 退款\n", "src/app/main.py": "print('hi')\n", "empty.txt": "", ".git/HEAD": "ref: refs/heads/main\n"})
	_ = os.WriteFile(filepath.Join(src.dir, marker), nil, 0o644) // agent 的标记文件不进存档
	if runtime.GOOS != "windows" {
		_ = os.Chmod(filepath.Join(src.dir, "src/app/main.py"), 0o755)
	}
	code, data := src.export("1048576")
	if code != http.StatusOK {
		t.Fatalf("导出：HTTP %d %s", code, data)
	}
	dst := newHarness(t, nil)
	_ = os.WriteFile(filepath.Join(dst.dir, marker), nil, 0o644) // 新环境只有标记文件，算空的
	if code, bad := dst.importTar(data, "1048576"); code != http.StatusNoContent {
		t.Fatalf("导入：HTTP %d %v", code, bad)
	}
	want, got := comparable(src.manifestOf()), comparable(dst.manifestOf())
	wj, _ := json.Marshal(want)
	gj, _ := json.Marshal(got)
	if string(wj) != string(gj) {
		t.Fatalf("恢复后的清单不同：\n%s\n%s", wj, gj)
	}
	for _, e := range got {
		if e.Path == marker {
			t.Fatal("标记文件不进清单")
		}
		if e.Path == "README.md" {
			sum := sha256.Sum256([]byte("# 退款\n"))
			if e.SHA256 != hex.EncodeToString(sum[:]) || e.Size != int64(len("# 退款\n")) || e.Type != "file" {
				t.Fatalf("清单里的文件：%+v", e)
			}
		}
		if runtime.GOOS != "windows" && e.Path == "src/app/main.py" && e.Mode != 0o755 {
			t.Fatalf("权限位：%o", e.Mode)
		}
	}
}

func TestImportOnlyIntoAnEmptyWorkdirAndWithinTheLimit(t *testing.T) {
	h := newHarness(t, nil)
	writeTree(t, h.dir, map[string]string{"already.txt": "x"})
	tarball := makeTar(t, tarItem{name: "a.txt", body: "a", typ: tar.TypeReg})
	if code, bad := h.importTar(tarball, "1024"); code != http.StatusConflict || bad.Code != "not_empty" {
		t.Fatalf("工作目录不空时 409：%d %v", code, bad)
	}
	empty := newHarness(t, nil)
	big := makeTar(t, tarItem{name: "big.bin", body: strings.Repeat("x", 2048), typ: tar.TypeReg})
	if code, bad := empty.importTar(big, "1024"); code != http.StatusRequestEntityTooLarge {
		t.Fatalf("超过上限 413：%d %v", code, bad)
	}
	writeTree(t, h.dir, map[string]string{"big.bin": strings.Repeat("y", 4096)})
	if code, _ := h.export("1024"); code != http.StatusRequestEntityTooLarge {
		t.Fatalf("导出超过上限 413：%d", code)
	}
	if code, _ := h.export(""); code != http.StatusBadRequest {
		t.Fatal("要给 max_bytes")
	}
}

func TestHostileArchivesAreRefusedAndWriteNothingOutside(t *testing.T) {
	cases := map[string][]tarItem{
		"absolute":       {{name: "/etc/passwd", body: "x", typ: tar.TypeReg}},
		"dotdot":         {{name: "../escape.txt", body: "x", typ: tar.TypeReg}},
		"nested-dotdot":  {{name: "a/../../escape.txt", body: "x", typ: tar.TypeReg}},
		"backslash":      {{name: "a\\b.txt", body: "x", typ: tar.TypeReg}},
		"hardlink":       {{name: "a.txt", body: "x", typ: tar.TypeReg}, {name: "b.txt", link: "a.txt", typ: tar.TypeLink}},
		"device":         {{name: "dev", typ: tar.TypeChar}},
		"escaping-link":  {{name: "out", link: "../../etc", typ: tar.TypeSymlink}},
		"absolute-link":  {{name: "out", link: "/etc", typ: tar.TypeSymlink}},
		"duplicate-file": {{name: "a.txt", body: "x", typ: tar.TypeReg}, {name: "a.txt", body: "y", typ: tar.TypeReg}},
	}
	if runtime.GOOS != "windows" {
		// 先放一个指向工作目录里的链接，再经它写文件：上级目录不能是链接。
		cases["through-link"] = []tarItem{{name: "sub", typ: tar.TypeDir, mode: 0o755}, {name: "ln", link: "sub", typ: tar.TypeSymlink}, {name: "ln/x.txt", body: "x", typ: tar.TypeReg}}
	}
	for name, items := range cases {
		t.Run(name, func(t *testing.T) {
			parent := t.TempDir()
			work := filepath.Join(parent, "work")
			h := newHarness(t, func(c *Config) { c.Workdir = work })
			code, bad := h.importTar(makeTar(t, items...), "1048576")
			if code != http.StatusBadRequest {
				t.Fatalf("应当 400：%d %v", code, bad)
			}
			if _, err := os.Stat(filepath.Join(parent, "escape.txt")); err == nil {
				t.Fatal("写到了工作目录外面")
			}
		})
	}
}

func TestSymlinksInsideTheWorkdirRoundTrip(t *testing.T) {
	h := newHarness(t, nil)
	tarball := makeTar(t, tarItem{name: "real", typ: tar.TypeDir, mode: 0o755}, tarItem{name: "real/f.txt", body: "f", typ: tar.TypeReg},
		tarItem{name: "alias", link: "real", typ: tar.TypeSymlink})
	code, bad := h.importTar(tarball, "1024")
	if code == http.StatusBadRequest && bad.Code == "symlink_unsupported" {
		t.Skip("这台机器建不了符号链接")
	}
	if code != http.StatusNoContent {
		t.Fatalf("导入：%d %v", code, bad)
	}
	var link Entry
	for _, e := range h.manifestOf() {
		if e.Path == "alias" {
			link = e
		}
	}
	if link.Type != "symlink" || link.Link != "real" {
		t.Fatalf("链接：%+v", link)
	}
}

func TestListAndGrepRunInsideTheWorkdir(t *testing.T) {
	h := newHarness(t, nil)
	writeTree(t, h.dir, map[string]string{
		"a.py": "def refund():\n    return 1\n", "pkg/b.py": "x = 1\n# refund policy\n", "pkg/c.txt": "nothing\n", ".git/config": "refund\n",
	})
	_ = os.WriteFile(filepath.Join(h.dir, "bin.dat"), []byte("refund\x00\x01"), 0o644)
	_ = os.WriteFile(filepath.Join(h.dir, marker), nil, 0o644)
	res := h.request(http.MethodGet, "/v1/list", nil, testToken)
	var listed struct{ Entries []listEntry }
	_ = json.NewDecoder(res.Body).Decode(&listed)
	res.Body.Close()
	names := []string{}
	for _, e := range listed.Entries {
		names = append(names, e.Name+":"+e.Type)
	}
	if strings.Join(names, ",") != ".git:dir,a.py:file,bin.dat:file,pkg:dir" {
		t.Fatalf("列根目录：%v", names)
	}
	res = h.request(http.MethodGet, "/v1/list/pkg", nil, testToken)
	_ = json.NewDecoder(res.Body).Decode(&listed)
	res.Body.Close()
	if len(listed.Entries) != 2 || listed.Entries[0].Name != "b.py" || listed.Entries[0].Size != int64(len("x = 1\n# refund policy\n")) {
		t.Fatalf("列子目录：%+v", listed.Entries)
	}
	if res := h.request(http.MethodGet, "/v1/list/missing", nil, testToken); res.StatusCode != http.StatusNotFound {
		t.Fatalf("不存在的目录 404：%d", res.StatusCode)
	}
	if res := h.request(http.MethodGet, "/v1/list/..%2Fescape", nil, testToken); res.StatusCode != http.StatusBadRequest {
		t.Fatalf("出界的路径 400：%d", res.StatusCode)
	}
	grep := func(body string) (int, map[string]any) {
		res := h.request(http.MethodPost, "/v1/grep", strings.NewReader(body), testToken)
		defer res.Body.Close()
		var out map[string]any
		_ = json.NewDecoder(res.Body).Decode(&out)
		return res.StatusCode, out
	}
	code, out := grep(`{"pattern": "refund"}`)
	matches, _ := out["matches"].([]any)
	if code != http.StatusOK || len(matches) != 2 || out["truncated"] != false {
		t.Fatalf("搜索：%d %v", code, out)
	}
	first := matches[0].(map[string]any)
	if first["path"] != "a.py" || first["line"] != float64(1) || first["text"] != "def refund():" {
		t.Fatalf("第一条：%v", first)
	}
	if _, out := grep(`{"pattern": "refund", "path": "pkg"}`); len(out["matches"].([]any)) != 1 {
		t.Fatalf("只在 pkg 里：%v", out)
	}
	if _, out := grep(`{"pattern": "refund", "max_matches": 1}`); out["truncated"] != true {
		t.Fatalf("截断：%v", out)
	}
	if code, out := grep(`{"pattern": "("}`); code != http.StatusBadRequest || out["code"] != "invalid_pattern" {
		t.Fatalf("坏正则 400：%d %v", code, out)
	}
	for _, path := range []string{"/v1/manifest", "/v1/archive?max_bytes=1", "/v1/list"} {
		if res := h.request(http.MethodGet, path, nil, "wrong-token-0123456789abcdef0123456789"); res.StatusCode != http.StatusUnauthorized {
			t.Fatalf("%s 要令牌：%d", path, res.StatusCode)
		}
	}
}
