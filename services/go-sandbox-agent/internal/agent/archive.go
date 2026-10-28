package agent

// 工作区的存档与恢复、清单、列目录与搜索（D-06a；docs/plan/workspace-layer.md）。工作区存档模式的后端（Cloudflare 等）
// 不挂工作区卷：Worker 建环境后把最近的存档导进来，销毁前把工作目录导出去；repo_* 工具经这里在环境里列目录、搜索。
//
//	GET  /v1/archive?max_bytes=N   工作目录打成 tar（普通文件、目录、符号链接；只留权限位）；文件总大小超过 N 时 413
//	PUT  /v1/archive?max_bytes=N   把 tar 解到工作目录：工作目录要是空的（409）；绝对路径、..、反斜杠、硬链接、设备文件、
//	                               指出工作目录的符号链接、经符号链接写进去的路径一律 400；超过 N 字节 413
//	GET  /v1/manifest              逐项的清单：路径、类型、权限位、大小、sha256、链接目标（按路径的字符串顺序排序）
//	GET  /v1/list/<相对路径>        列一个目录（不写为工作目录本身；不跟随符号链接），最多 1000 项
//	POST /v1/grep                  {"pattern", "path", "max_matches"}：按行搜索（RE2 语法），跳过任何一级 .git、二进制与超过 1 MiB 的文件
//
// 语义与开发实现（libs/py-core 的 agentruntime_core.dev.workdir）相同，两边对着 Sandbox 的一致性测试
// （agentruntime_core.conformance.sandbox 的 SandboxArchiveConformance）。

import (
	"archive/tar"
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// Entry 是工作目录里的一项（清单用）。
type Entry struct {
	Path   string `json:"path"`
	Type   string `json:"type"`
	Mode   int64  `json:"mode"`
	Size   int64  `json:"size,omitempty"`
	SHA256 string `json:"sha256,omitempty"`
	Link   string `json:"link,omitempty"`
}

// marker 是 agent 自己在工作目录里放的文件（适配器建环境时写的空文件），不进存档与清单。
const marker = ".agentruntime"

const (
	maxGrepFile    = 1 << 20
	maxGrepMatches = 1000
	maxListEntries = 1000
)

// inside 判断 rel（相对工作目录、/ 分隔、已规范化）指的链接目标 target 是否还在工作目录里。
func linkInside(rel, target string) bool {
	if target == "" || strings.HasPrefix(target, "/") || strings.ContainsAny(target, "\\\x00") || drive.MatchString(target) {
		return false
	}
	joined := path.Clean(path.Join(path.Dir(rel), target))
	return joined != ".." && !strings.HasPrefix(joined, "../")
}

// walk 按路径的字典序遍历工作目录（不跟随符号链接），跳过 agent 的标记文件与设备、管道、套接字。
func (s *server) walk(fn func(rel string, info fs.FileInfo) error) error {
	root := s.c.Workdir
	return filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if p == root {
			return nil
		}
		rel, err := filepath.Rel(root, p)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		if rel == marker {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		mode := info.Mode()
		if !mode.IsRegular() && !mode.IsDir() && mode&fs.ModeSymlink == 0 {
			return nil
		}
		return fn(rel, info)
	})
}

func queryLimit(r *http.Request) (int64, error) {
	v := r.URL.Query().Get("max_bytes")
	if v == "" {
		return 0, errors.New("要给 max_bytes")
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil || n <= 0 {
		return 0, errors.New("max_bytes 要是正整数")
	}
	return n, nil
}

func (s *server) exportArchive(w http.ResponseWriter, r *http.Request) {
	limit, err := queryLimit(r)
	if err != nil {
		fail(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	var total int64
	if err := s.walk(func(_ string, info fs.FileInfo) error {
		if info.Mode().IsRegular() {
			total += info.Size()
		}
		return nil
	}); err != nil {
		fail(w, http.StatusInternalServerError, "read_failed", "读不了工作目录")
		return
	}
	if total > limit {
		fail(w, http.StatusRequestEntityTooLarge, "too_large", fmt.Sprintf("工作目录有 %d 字节，超过上限 %d", total, limit))
		return
	}
	w.Header().Set("Content-Type", "application/x-tar")
	tw := tar.NewWriter(w)
	err = s.walk(func(rel string, info fs.FileInfo) error {
		hdr := &tar.Header{Name: rel, Mode: int64(info.Mode().Perm()), ModTime: info.ModTime(), Format: tar.FormatPAX}
		switch {
		case info.IsDir():
			hdr.Typeflag, hdr.Name = tar.TypeDir, rel+"/"
		case info.Mode()&fs.ModeSymlink != 0:
			target, err := os.Readlink(filepath.Join(s.c.Workdir, filepath.FromSlash(rel)))
			if err != nil || !linkInside(rel, filepath.ToSlash(target)) {
				return nil // 指出工作目录的链接不进存档
			}
			hdr.Typeflag, hdr.Linkname = tar.TypeSymlink, filepath.ToSlash(target)
		default:
			hdr.Typeflag, hdr.Size = tar.TypeReg, info.Size()
		}
		if err := tw.WriteHeader(hdr); err != nil {
			return err
		}
		if hdr.Typeflag != tar.TypeReg {
			return nil
		}
		f, err := os.Open(filepath.Join(s.c.Workdir, filepath.FromSlash(rel)))
		if err != nil {
			return err
		}
		defer f.Close()
		// 导出中途文件变短：tar 写不满就是错，连接断开，Worker 当作暂时性的失败重试。
		_, err = io.CopyN(tw, f, hdr.Size)
		return err
	})
	if err == nil {
		err = tw.Close()
	}
	if err != nil {
		// 已经开始写响应体，只能断开连接让对方看到不完整的 tar。
		panic(http.ErrAbortHandler)
	}
}

func (s *server) importArchive(w http.ResponseWriter, r *http.Request) {
	limit, err := queryLimit(r)
	if err != nil {
		fail(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	entries, err := os.ReadDir(s.c.Workdir)
	if err != nil {
		fail(w, http.StatusInternalServerError, "read_failed", "读不了工作目录")
		return
	}
	for _, e := range entries {
		if e.Name() != marker {
			fail(w, http.StatusConflict, "not_empty", "工作目录不是空的：只能把存档导进新建的环境")
			return
		}
	}
	body := http.MaxBytesReader(w, r.Body, limit+limit/8+(1<<20))
	if err := s.extract(tar.NewReader(body), limit); err != nil {
		var tooBig *http.MaxBytesError
		var bad badEntry
		switch {
		case errors.As(err, &tooBig), errors.Is(err, errTooLarge):
			fail(w, http.StatusRequestEntityTooLarge, "too_large", "存档超过上限")
		case errors.As(err, &bad):
			fail(w, http.StatusBadRequest, bad.code, bad.message)
		default:
			fail(w, http.StatusBadRequest, "invalid_archive", "不是合法的 tar")
		}
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

type badEntry struct{ code, message string }

func (b badEntry) Error() string { return b.message }

var errTooLarge = errors.New("存档超过上限")

// clean 把 tar 里的名字规范化成相对工作目录的路径；不合法时报错。
func clean(name string) (string, error) {
	if name == "" || strings.ContainsAny(name, "\\\x00") || strings.HasPrefix(name, "/") || drive.MatchString(name) {
		return "", badEntry{"invalid_path", "存档里有绝对路径或不合法的名字"}
	}
	rel := path.Clean(strings.TrimSuffix(name, "/"))
	if rel == ".." || strings.HasPrefix(rel, "../") {
		return "", badEntry{"invalid_path", "存档里有出工作目录的路径"}
	}
	return rel, nil
}

// parentsAreDirs 核对 rel 的每一级上级目录要么不存在、要么是真目录（不是符号链接）：不能经链接写到工作目录外面。
func (s *server) parentsAreDirs(rel string) error {
	dir := path.Dir(rel)
	if dir == "." {
		return nil
	}
	cur := s.c.Workdir
	for _, part := range strings.Split(dir, "/") {
		cur = filepath.Join(cur, part)
		info, err := os.Lstat(cur)
		if errors.Is(err, fs.ErrNotExist) {
			return os.MkdirAll(filepath.Join(s.c.Workdir, filepath.FromSlash(dir)), 0o755)
		}
		if err != nil {
			return err
		}
		if !info.IsDir() {
			return badEntry{"invalid_path", "存档里的路径经过了符号链接或文件"}
		}
	}
	return nil
}

func (s *server) extract(tr *tar.Reader, limit int64) error {
	var total int64
	for {
		hdr, err := tr.Next()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		rel, err := clean(hdr.Name)
		if err != nil {
			return err
		}
		if rel == "." || rel == marker {
			continue
		}
		if err := s.parentsAreDirs(rel); err != nil {
			return err
		}
		target := filepath.Join(s.c.Workdir, filepath.FromSlash(rel))
		perm := fs.FileMode(hdr.Mode) & fs.ModePerm
		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := os.Mkdir(target, perm|0o700); err != nil && !errors.Is(err, fs.ErrExist) {
				return err
			}
			if info, err := os.Lstat(target); err != nil || !info.IsDir() {
				return badEntry{"invalid_path", "存档里的目录与已有的项重名"}
			}
			_ = os.Chmod(target, perm|0o700)
		case tar.TypeReg:
			total += hdr.Size
			if total > limit {
				return errTooLarge
			}
			f, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, perm|0o600)
			if err != nil {
				return badEntry{"invalid_path", "存档里有重名的文件"}
			}
			_, err = io.CopyN(f, tr, hdr.Size)
			if cerr := f.Close(); err == nil {
				err = cerr
			}
			if err != nil {
				return err
			}
			_ = os.Chmod(target, perm)
		case tar.TypeSymlink:
			if !linkInside(rel, hdr.Linkname) {
				return badEntry{"invalid_link", "存档里的符号链接指出了工作目录"}
			}
			if err := os.Symlink(filepath.FromSlash(hdr.Linkname), target); err != nil {
				return badEntry{"symlink_unsupported", "这里建不了符号链接"}
			}
		default:
			return badEntry{"unsupported_entry", "存档里只能有普通文件、目录与符号链接"}
		}
	}
}

func (s *server) manifest(w http.ResponseWriter, _ *http.Request) {
	var out []Entry
	err := s.walk(func(rel string, info fs.FileInfo) error {
		e := Entry{Path: rel, Mode: int64(info.Mode().Perm())}
		switch {
		case info.IsDir():
			e.Type = "dir"
		case info.Mode()&fs.ModeSymlink != 0:
			target, err := os.Readlink(filepath.Join(s.c.Workdir, filepath.FromSlash(rel)))
			if err != nil {
				return err
			}
			e.Type, e.Link, e.Mode = "symlink", filepath.ToSlash(target), 0
		default:
			f, err := os.Open(filepath.Join(s.c.Workdir, filepath.FromSlash(rel)))
			if err != nil {
				return err
			}
			h := sha256.New()
			n, err := io.Copy(h, f)
			f.Close()
			if err != nil {
				return err
			}
			e.Type, e.Size, e.SHA256 = "file", n, hex.EncodeToString(h.Sum(nil))
		}
		out = append(out, e)
		return nil
	})
	if err != nil {
		fail(w, http.StatusInternalServerError, "read_failed", "读不了工作目录")
		return
	}
	if out == nil {
		out = []Entry{}
	}
	// 遍历是深度优先的（父目录在前）；清单按路径的字符串顺序排，与开发实现一致。
	sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	writeJSON(w, http.StatusOK, map[string]any{"entries": out})
}

type listEntry struct {
	Name string `json:"name"`
	Type string `json:"type"`
	Size int64  `json:"size,omitempty"`
}

func (s *server) listDir(w http.ResponseWriter, r *http.Request) {
	target := s.c.Workdir
	if p := r.PathValue("path"); p != "" {
		resolved, err := s.resolve(p)
		if err != nil {
			fail(w, http.StatusBadRequest, "invalid_path", err.Error())
			return
		}
		target = resolved
	}
	// 不跟随符号链接（与开发实现一致）：链接到别处的目录不当目录列。
	if info, err := os.Lstat(target); err != nil || !info.IsDir() {
		fail(w, http.StatusNotFound, "dir_not_found", "没有这个目录")
		return
	}
	items, err := os.ReadDir(target)
	if err != nil {
		fail(w, http.StatusNotFound, "dir_not_found", "没有这个目录")
		return
	}
	out := []listEntry{}
	for _, it := range items {
		if target == s.c.Workdir && it.Name() == marker {
			continue
		}
		info, err := it.Info()
		if err != nil {
			continue
		}
		switch {
		case info.IsDir():
			out = append(out, listEntry{Name: it.Name(), Type: "dir"})
		case info.Mode().IsRegular():
			out = append(out, listEntry{Name: it.Name(), Type: "file", Size: info.Size()})
		case info.Mode()&fs.ModeSymlink != 0:
			out = append(out, listEntry{Name: it.Name(), Type: "symlink"})
		}
		if len(out) >= maxListEntries {
			break
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	writeJSON(w, http.StatusOK, map[string]any{"entries": out})
}

type grepRequest struct {
	Pattern    string `json:"pattern"`
	Path       string `json:"path"`
	MaxMatches int    `json:"max_matches"`
}

type grepMatch struct {
	Path string `json:"path"`
	Line int    `json:"line"`
	Text string `json:"text"`
}

func (s *server) grep(w http.ResponseWriter, r *http.Request) {
	var req grepRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<16)).Decode(&req); err != nil || req.Pattern == "" {
		fail(w, http.StatusBadRequest, "invalid_request", "要 {pattern, path, max_matches}")
		return
	}
	re, err := regexp.Compile(req.Pattern)
	if err != nil {
		fail(w, http.StatusBadRequest, "invalid_pattern", "正则表达式不合法（RE2 语法）")
		return
	}
	limit := req.MaxMatches
	if limit <= 0 {
		limit = 100
	}
	if limit > maxGrepMatches {
		limit = maxGrepMatches
	}
	prefix := ""
	if req.Path != "" && req.Path != "." {
		if _, err := s.resolve(req.Path); err != nil {
			fail(w, http.StatusBadRequest, "invalid_path", err.Error())
			return
		}
		prefix = path.Clean(req.Path)
	}
	matches := []grepMatch{}
	truncated := false
	stop := errors.New("stop")
	err = s.walk(func(rel string, info fs.FileInfo) error {
		if info.IsDir() && (info.Name() == ".git" || rel == ".git") {
			return filepath.SkipDir
		}
		if prefix != "" && rel != prefix && !strings.HasPrefix(rel, prefix+"/") {
			return nil
		}
		if !info.Mode().IsRegular() || info.Size() > maxGrepFile || strings.HasPrefix(rel, ".git/") {
			return nil
		}
		data, err := os.ReadFile(filepath.Join(s.c.Workdir, filepath.FromSlash(rel)))
		if err != nil {
			return nil
		}
		head := data
		if len(head) > 1024 {
			head = head[:1024]
		}
		if bytes.IndexByte(head, 0) >= 0 {
			return nil // 二进制文件
		}
		sc := bufio.NewScanner(bytes.NewReader(data))
		sc.Buffer(make([]byte, 64<<10), maxGrepFile+1)
		for n := 1; sc.Scan(); n++ {
			line := sc.Text()
			if re.MatchString(line) {
				if r := []rune(line); len(r) > 500 {
					line = string(r[:500])
				}
				matches = append(matches, grepMatch{Path: rel, Line: n, Text: line})
				if len(matches) >= limit {
					truncated = true
					return stop
				}
			}
		}
		return nil
	})
	if err != nil && !errors.Is(err, stop) {
		fail(w, http.StatusInternalServerError, "read_failed", "读不了工作目录")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"matches": matches, "truncated": truncated})
}
