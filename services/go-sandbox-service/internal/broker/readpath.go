package broker

import (
	"bytes"
	"context"
	"errors"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
)

// 工作区的只读操作（SBX-01 一期的快路径，WS-READ-01）：不进 Temporal、不改环境的 busy、不触发存档。
//
//   - 存档模式、有活着的可写环境（同一个工作区同一时间只有一个）：直接在那个环境里读，看得到没存档的改动。
//   - 存档模式、没有：读最近的快照（清单与块），不建环境。
//   - 挂卷：直接读卷（os.Root：不跟随指出工作区的符号链接，.. 出不去）。
//   - GetDiff、GetLog 在沙箱里跑 git（不在服务进程里对用户的仓库跑 git：.git/config 能配会执行命令的项）：会话里有活着的
//     可写环境时用它，否则建一个只读的短命环境，用完即毁。
//
// 规则同原来 Worker 里 Workspace 操作的读：路径相对工作区根、/ 分隔，任何一级都不能是 .git；列目录最多 1000 项（不列 .git）；
// 超过 512 KiB 的文件只给大小，前 8 KiB 有 NUL 的是二进制、不给内容。

// 读的上限。
const (
	MaxReadBytes = 512 << 10
	BinaryProbe  = 8192
	MaxList      = 1000
	MaxDiffBytes = 1 << 20
	DefaultLog   = 30
	MaxLog       = 200
)

// FileInfo 是列目录的一项。
type FileInfo struct {
	Name string
	Type string // file、dir、symlink
	Size int64
}

// ReadResult 是读文件的结果。
type ReadResult struct {
	Size     int64
	TooLarge bool
	Binary   bool
	Content  string
}

var drivePattern = regexp.MustCompile(`^[A-Za-z]:`)

// CheckWorkspacePath 核对应用给的路径；返回规范的写法（根是空串）。
func CheckWorkspacePath(p string, allowRoot bool) (string, error) {
	if strings.ContainsAny(p, "\\\x00") || strings.HasPrefix(p, "/") || drivePattern.MatchString(p) {
		return "", providererr.New(providererr.Invalid, "invalid_path", "路径要相对工作区根，用 / 分隔")
	}
	if p == "" || p == "." {
		if allowRoot {
			return "", nil
		}
		return "", providererr.New(providererr.Invalid, "invalid_path", "要给工作区里的一个文件")
	}
	for _, part := range strings.Split(p, "/") {
		if part == "" || part == "." || part == ".." {
			return "", providererr.New(providererr.Invalid, "invalid_path", "路径不得含空段、. 或 ..")
		}
		if part == ".git" {
			return "", providererr.New(providererr.Invalid, "invalid_path", "不能读 .git 里的内容")
		}
	}
	return p, nil
}

type source struct {
	root    *os.Root            // 挂卷
	lease   *Lease              // 活着的可写环境
	entries []sandbox.FileEntry // 快照
	foreign map[string]string
	ws      string
}

func (s *source) close() {
	if s.root != nil {
		_ = s.root.Close()
	}
}

func (b *Broker) readSource(ctx context.Context, tenant *commonv1.TenantContext, workspaceID string) (*source, error) {
	owner := tenant.GetTenantId()
	if err := sandbox.CheckWorkspaceID(workspaceID); err != nil {
		return nil, err
	}
	archived, err := b.ArchiveModeFor(ctx, owner)
	if err != nil {
		return nil, err
	}
	if !archived {
		root := b.reg.WorkspaceRoot()
		if root == "" {
			return nil, providererr.New(providererr.Invalid, "workspace_api_unavailable", "这个部署没有挂工作区卷")
		}
		dir := filepath.Join(root, owner, workspaceID)
		r, err := os.OpenRoot(dir)
		if err != nil {
			return nil, providererr.New(providererr.NotFound, "workspace_not_found", "没有这个工作区")
		}
		return &source{root: r, ws: workspaceID}, nil
	}
	if lease, err := b.liveWritable(ctx, tenant, workspaceID); err != nil || lease != nil {
		return &source{lease: lease, ws: workspaceID}, err
	}
	snap, err := b.archiver.Latest(ctx, owner, workspaceID)
	if err != nil {
		return nil, err
	}
	if snap == nil {
		w, err := b.archiver.Get(ctx, owner, workspaceID)
		if err != nil {
			return nil, err
		}
		if w == nil || w.State == "deleted" {
			return nil, providererr.New(providererr.NotFound, "workspace_not_found", "没有这个工作区")
		}
		return &source{ws: workspaceID}, nil // 还没有快照：空的工作区
	}
	entries, foreign, err := b.archiver.Manifest(ctx, owner, *snap)
	if err != nil {
		return nil, err
	}
	return &source{entries: entries, foreign: foreign, ws: workspaceID}, nil
}

// liveWritable 是这个工作区活着的可写环境（同一会话的优先；存档模式下同一时间只有一个）；没有时 nil。不 touch、不改 busy。
func (b *Broker) liveWritable(ctx context.Context, tenant *commonv1.TenantContext, workspaceID string) (*Lease, error) {
	owner := tenant.GetTenantId()
	holders, err := b.ledger.WritableHolders(ctx, owner, workspaceID, nil)
	if err != nil {
		return nil, err
	}
	sort.SliceStable(holders, func(i, j int) bool {
		return holders[i].SessionID == tenant.GetSessionId() && holders[j].SessionID != tenant.GetSessionId()
	})
	now := ledger.ISO(b.ledger.Now())
	for _, h := range holders {
		if h.State != "ready" || h.BackendEnvID == "" || h.ExpiresAt <= now {
			continue
		}
		lease, err := b.Get(ctx, &commonv1.TenantContext{TenantId: owner, SessionId: h.SessionID, WorkspaceId: workspaceID}, h.EnvRef)
		if err != nil {
			continue
		}
		if _, ok := lease.Box.(sandbox.Browse); ok {
			return lease, nil
		}
	}
	return nil, nil
}

// ListWorkspace 列工作区里的一个目录。
func (b *Broker) ListWorkspace(ctx context.Context, tenant *commonv1.TenantContext, workspaceID, dir string) ([]FileInfo, bool, error) {
	rel, err := CheckWorkspacePath(dir, true)
	if err != nil {
		return nil, false, err
	}
	src, err := b.readSource(ctx, tenant, workspaceID)
	if err != nil {
		return nil, false, err
	}
	defer src.close()
	var items []FileInfo
	switch {
	case src.root != nil:
		items, err = listRoot(src.root, rel)
	case src.lease != nil:
		var listed []sandbox.DirEntry
		listed, err = src.lease.Box.(sandbox.Browse).ListDir(ctx, src.lease.EnvID, rel)
		for _, e := range listed {
			items = append(items, FileInfo{Name: e.Name, Type: e.Type, Size: e.Size})
		}
		if providererr.CategoryOf(err) == providererr.NotFound {
			err = providererr.New(providererr.NotFound, "dir_not_found", "工作区里没有这个目录")
		}
	default:
		items, err = listManifest(src.entries, rel)
	}
	if err != nil {
		return nil, false, err
	}
	out := make([]FileInfo, 0, len(items))
	for _, it := range items {
		if it.Name != ".git" {
			out = append(out, it)
		}
	}
	return out, len(items) >= MaxList, nil
}

func listRoot(root *os.Root, rel string) ([]FileInfo, error) {
	dir := rel
	if dir == "" {
		dir = "."
	}
	f, err := root.Open(dir)
	if err != nil {
		return nil, providererr.New(providererr.NotFound, "dir_not_found", "工作区里没有这个目录")
	}
	defer f.Close()
	if st, err := f.Stat(); err != nil || !st.IsDir() {
		return nil, providererr.New(providererr.NotFound, "dir_not_found", "工作区里没有这个目录")
	}
	names, err := f.Readdirnames(-1)
	if err != nil {
		return nil, providererr.Wrap(providererr.Transient, "workspace_read_failed", "读不了目录", err)
	}
	sort.Strings(names)
	var out []FileInfo
	for _, n := range names {
		if len(out) >= MaxList {
			break
		}
		st, err := root.Lstat(path.Join(dir, n))
		if err != nil {
			continue
		}
		switch {
		case st.Mode().IsRegular():
			out = append(out, FileInfo{Name: n, Type: "file", Size: st.Size()})
		case st.IsDir():
			out = append(out, FileInfo{Name: n, Type: "dir"})
		case st.Mode()&fs.ModeSymlink != 0:
			out = append(out, FileInfo{Name: n, Type: "symlink"})
		}
	}
	return out, nil
}

func listManifest(entries []sandbox.FileEntry, rel string) ([]FileInfo, error) {
	if rel != "" {
		found := false
		for _, e := range entries {
			if e.Path == rel && e.Type == "dir" {
				found = true
			}
		}
		if !found {
			return nil, providererr.New(providererr.NotFound, "dir_not_found", "工作区里没有这个目录")
		}
	}
	var out []FileInfo
	for _, e := range entries {
		if path.Dir(e.Path) != orDot(rel) {
			continue
		}
		out = append(out, FileInfo{Name: path.Base(e.Path), Type: e.Type, Size: e.Size})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	if len(out) > MaxList {
		out = out[:MaxList]
	}
	return out, nil
}

func orDot(s string) string {
	if s == "" {
		return "."
	}
	return s
}

// ReadWorkspaceFile 读工作区里的一个文件，最多 limit 字节；超过时只给大小（第二个返回值）。
func (b *Broker) ReadWorkspaceFile(ctx context.Context, tenant *commonv1.TenantContext, workspaceID, p string, limit int64) ([]byte, int64, error) {
	rel, err := CheckWorkspacePath(p, false)
	if err != nil {
		return nil, 0, err
	}
	src, err := b.readSource(ctx, tenant, workspaceID)
	if err != nil {
		return nil, 0, err
	}
	defer src.close()
	notFound := providererr.New(providererr.NotFound, "file_not_found", "工作区里没有文件 "+rel)
	switch {
	case src.root != nil:
		st, err := src.root.Stat(rel)
		if err != nil || !st.Mode().IsRegular() {
			return nil, 0, notFound
		}
		if st.Size() > limit {
			return nil, st.Size(), nil
		}
		f, err := src.root.Open(rel)
		if err != nil {
			return nil, 0, notFound
		}
		defer f.Close()
		data, err := io.ReadAll(io.LimitReader(f, limit+1))
		if err != nil {
			return nil, 0, providererr.Wrap(providererr.Transient, "workspace_read_failed", "读不了文件", err)
		}
		if int64(len(data)) > limit {
			return nil, int64(len(data)), nil
		}
		return data, int64(len(data)), nil
	case src.lease != nil:
		browse := src.lease.Box.(sandbox.Browse)
		parent, name := path.Split(rel)
		listing, _ := browse.ListDir(ctx, src.lease.EnvID, strings.TrimSuffix(parent, "/"))
		var entry *sandbox.DirEntry
		for i := range listing {
			if listing[i].Name == name {
				entry = &listing[i]
			}
		}
		if (entry == nil && len(listing) < MaxList) || (entry != nil && entry.Type != "file") {
			return nil, 0, notFound
		}
		if entry != nil && entry.Size > limit {
			return nil, entry.Size, nil
		}
		data, err := src.lease.Box.ReadFile(ctx, src.lease.EnvID, rel)
		if err != nil {
			if providererr.CategoryOf(err) == providererr.NotFound {
				return nil, 0, notFound
			}
			return nil, 0, err
		}
		if int64(len(data)) > limit {
			return nil, int64(len(data)), nil
		}
		return data, int64(len(data)), nil
	default:
		for _, e := range src.entries {
			if e.Path != rel {
				continue
			}
			if e.Type != "file" {
				return nil, 0, notFound
			}
			if e.Size > limit {
				return nil, e.Size, nil
			}
			data, err := b.archiver.Blob(ctx, tenant.GetTenantId(), src.ws, e, src.foreign)
			return data, e.Size, err
		}
		return nil, 0, notFound
	}
}

// ReadFile 是 Workspace API 的读文件：大文件只给大小，二进制不给内容。
func (b *Broker) ReadFile(ctx context.Context, tenant *commonv1.TenantContext, workspaceID, p string) (ReadResult, error) {
	data, size, err := b.ReadWorkspaceFile(ctx, tenant, workspaceID, p, MaxReadBytes)
	if err != nil {
		return ReadResult{}, err
	}
	if data == nil && size > MaxReadBytes {
		return ReadResult{Size: size, TooLarge: true}, nil
	}
	probe := data
	if len(probe) > BinaryProbe {
		probe = probe[:BinaryProbe]
	}
	if bytes.IndexByte(probe, 0) >= 0 {
		return ReadResult{Size: size, Binary: true}, nil
	}
	return ReadResult{Size: size, Content: strings.ToValidUTF8(string(data), string(utf8.RuneError))}, nil
}

// GrepWorkspace 按行搜索（RE2）：挂卷与快照在服务里搜，活着的环境里交给 agent；规则同 sandbox-agent。
func (b *Broker) GrepWorkspace(ctx context.Context, tenant *commonv1.TenantContext, workspaceID, pattern, dir string, maxMatches int) ([]sandbox.GrepMatch, bool, error) {
	rel, err := CheckWorkspacePath(dir, true)
	if err != nil {
		return nil, false, err
	}
	if maxMatches <= 0 {
		maxMatches = 100
	}
	maxMatches = min(maxMatches, 1000)
	re, err := regexp.Compile(pattern)
	if err != nil {
		return nil, false, providererr.New(providererr.Invalid, "invalid_pattern", "正则不合法")
	}
	src, err := b.readSource(ctx, tenant, workspaceID)
	if err != nil {
		return nil, false, err
	}
	defer src.close()
	if src.lease != nil {
		return src.lease.Box.(sandbox.Browse).Grep(ctx, src.lease.EnvID, pattern, rel, maxMatches)
	}
	var matches []sandbox.GrepMatch
	scan := func(p string, data []byte) bool {
		if len(data) > 1<<20 || bytes.IndexByte(data[:min(len(data), 1024)], 0) >= 0 {
			return true
		}
		for i, line := range strings.Split(string(data), "\n") {
			if re.MatchString(line) {
				if len(matches) >= maxMatches {
					return false
				}
				text := line
				if r := []rune(text); len(r) > 500 {
					text = string(r[:500])
				}
				matches = append(matches, sandbox.GrepMatch{Path: p, Line: i + 1, Text: text})
			}
		}
		return true
	}
	if src.root != nil {
		truncated := false
		start := orDot(rel)
		err := fs.WalkDir(src.root.FS(), start, func(p string, d fs.DirEntry, err error) error {
			if err != nil {
				return nil
			}
			if d.IsDir() && d.Name() == ".git" {
				return fs.SkipDir
			}
			if !d.Type().IsRegular() {
				return nil
			}
			info, err := d.Info()
			if err != nil || info.Size() > 1<<20 {
				return nil
			}
			data, err := src.root.ReadFile(p)
			if err != nil {
				return nil
			}
			if !scan(p, data) {
				truncated = true
				return fs.SkipAll
			}
			return nil
		})
		if err != nil && !errors.Is(err, fs.SkipAll) {
			return nil, false, providererr.New(providererr.NotFound, "dir_not_found", "工作区里没有这个目录")
		}
		return matches, truncated, nil
	}
	for _, e := range src.entries {
		if e.Type != "file" || e.Size > 1<<20 || (rel != "" && !strings.HasPrefix(e.Path, rel+"/")) || strings.HasPrefix(e.Path, ".git/") || strings.Contains(e.Path, "/.git/") {
			continue
		}
		data, err := b.archiver.Blob(ctx, tenant.GetTenantId(), src.ws, e, src.foreign)
		if err != nil {
			return nil, false, err
		}
		if !scan(e.Path, data) {
			return matches, true, nil
		}
	}
	return matches, false, nil
}

// --- git（在沙箱里跑）---

var (
	refPattern = regexp.MustCompile(`^[A-Za-z0-9._/@^~+-]{1,200}$`)
	shaPattern = regexp.MustCompile(`^[0-9a-f]{40}([0-9a-f]{24})?$`)
)

// CheckRev 核对 base。
func CheckRev(rev string) error {
	if !refPattern.MatchString(rev) || strings.HasPrefix(rev, "-") || strings.Contains(rev, "..") {
		return providererr.New(providererr.Invalid, "invalid_base", "base 要是 commit、分支或标签")
	}
	return nil
}

// GitTimeout 是一条 git 命令的时限。
var GitTimeout = 2 * time.Minute

// gitLease 是跑 git 用的环境：会话里有活着的可写环境时用它（keep），否则借一个只读的短命环境（用完即毁）。
func (b *Broker) gitLease(ctx context.Context, tenant *commonv1.TenantContext, workspaceID, runtime string) (*Lease, bool, error) {
	if err := sandbox.CheckWorkspaceID(workspaceID); err != nil {
		return nil, false, err
	}
	archived, err := b.ArchiveModeFor(ctx, tenant.GetTenantId())
	if err != nil {
		return nil, false, err
	}
	if archived {
		if lease, err := b.liveWritable(ctx, tenant, workspaceID); err != nil || lease != nil {
			return lease, true, err
		}
		// 没有这个工作区（或已删除）时不借环境：同原来 Worker 的 _record_of。
		w, err := b.archiver.Get(ctx, tenant.GetTenantId(), workspaceID)
		if err != nil {
			return nil, false, err
		}
		if w == nil || w.State == "deleted" {
			return nil, false, providererr.New(providererr.NotFound, "workspace_not_found", "没有这个工作区")
		}
	} else if root := b.reg.WorkspaceRoot(); root != "" {
		if st, err := os.Stat(filepath.Join(root, tenant.GetTenantId(), workspaceID)); err != nil || !st.IsDir() {
			return nil, false, providererr.New(providererr.NotFound, "workspace_not_found", "没有这个工作区")
		}
	}
	readTenant := &commonv1.TenantContext{TenantId: tenant.GetTenantId(), UserId: tenant.GetUserId(), WorkspaceId: workspaceID, Trace: tenant.GetTrace()}
	lease, err := b.Acquire(ctx, readTenant, AcquireArgs{Spec: sandbox.Spec{Runtime: runtime, WorkspaceID: workspaceID, IdleTimeout: 5 * time.Minute}, Writable: false})
	return lease, false, err
}

func (b *Broker) git(ctx context.Context, lease *Lease, argv []string, extra map[string]string) (sandbox.ExecResult, error) {
	config := [][2]string{{"safe.directory", "*"}, {"core.hooksPath", "/dev/null"}, {"core.autocrlf", "false"}}
	env := map[string]string{"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": strconv.Itoa(len(config))}
	for i, kv := range config {
		env["GIT_CONFIG_KEY_"+strconv.Itoa(i)], env["GIT_CONFIG_VALUE_"+strconv.Itoa(i)] = kv[0], kv[1]
	}
	for k, v := range extra {
		env[k] = v
	}
	res, err := b.Exec(ctx, lease, append([]string{"git"}, argv...), GitTimeout, nil, env)
	if err != nil {
		return res, err
	}
	if res.TimedOut {
		return res, providererr.New(providererr.Transient, "git_timeout", "git 超时")
	}
	return res, nil
}

func ok(res sandbox.ExecResult) bool { return res.ExitCode != nil && *res.ExitCode == 0 }

func (b *Broker) head(ctx context.Context, lease *Lease) (string, string, error) {
	res, err := b.git(ctx, lease, []string{"rev-parse", "--verify", "--quiet", "HEAD"}, nil)
	if err != nil {
		return "", "", err
	}
	sha := strings.TrimSpace(string(res.Stdout))
	if !ok(res) || !shaPattern.MatchString(sha) {
		sha = ""
	}
	res, err = b.git(ctx, lease, []string{"symbolic-ref", "--quiet", "--short", "HEAD"}, nil)
	if err != nil {
		return "", "", err
	}
	branch := ""
	if ok(res) {
		branch = strings.TrimSpace(string(res.Stdout))
	}
	return sha, branch, nil
}

// DiffStat 是一个改动文件的行数。
type DiffStat struct {
	Path           string
	Added, Deleted int32
	Binary         bool
}

// DiffResult 是 GetDiff 的结果。
type DiffResult struct {
	Diff      string
	Files     []string
	Stats     []DiffStat
	Truncated bool
	Head      string
	Branch    string
}

const (
	diffFile  = ".git/agentruntime-diff.patch"
	diffIndex = ".git/agentruntime-diff.index"
)

func (b *Broker) withGit(ctx context.Context, tenant *commonv1.TenantContext, workspaceID, runtime string, fn func(lease *Lease) error) error {
	lease, kept, err := b.gitLease(ctx, tenant, workspaceID, runtime)
	if err != nil {
		return err
	}
	defer func() {
		if !kept {
			_ = b.Release(context.WithoutCancel(ctx), lease, false, 0)
		}
	}()
	return fn(lease)
}

// Diff 是工作区相对 base（空时 HEAD）的改动，含未跟踪的新文件；不动工作区自己的 index。
func (b *Broker) Diff(ctx context.Context, tenant *commonv1.TenantContext, workspaceID, base string, renames bool, runtime string) (DiffResult, error) {
	if base != "" {
		if err := CheckRev(base); err != nil {
			return DiffResult{}, err
		}
	}
	rename := "--no-renames"
	if renames {
		rename = "--find-renames"
	}
	var out DiffResult
	err := b.withGit(ctx, tenant, workspaceID, runtime, func(lease *Lease) error {
		head, branch, err := b.head(ctx, lease)
		if err != nil {
			return err
		}
		out.Head, out.Branch = head, branch
		index := map[string]string{"GIT_INDEX_FILE": diffIndex}
		readTree := []string{"read-tree", "--empty"}
		if head != "" {
			readTree = []string{"read-tree", "HEAD"}
		}
		for _, argv := range [][]string{readTree, {"add", "-A"}} {
			res, err := b.git(ctx, lease, argv, index)
			if err != nil {
				return err
			}
			if !ok(res) {
				return gitError("准备 diff", res)
			}
		}
		var target []string
		switch {
		case base != "":
			target = []string{base, "--"}
		case head != "":
			target = []string{"HEAD", "--"}
		}
		common := []string{"diff", "--cached", "--no-color", "--no-ext-diff", rename}
		res, err := b.git(ctx, lease, append(append(append([]string{}, common...), "--binary", "--output="+diffFile), target...), index)
		if err != nil {
			return err
		}
		if !ok(res) {
			return gitError("diff", res)
		}
		num, err := b.git(ctx, lease, append(append(append([]string{}, common...), "--numstat", "-z"), target...), index)
		if err != nil {
			return err
		}
		if !ok(num) {
			return gitError("diff", num)
		}
		data, err := lease.Box.ReadFile(ctx, lease.EnvID, diffFile)
		if err != nil {
			return err
		}
		out.Truncated = len(data) > MaxDiffBytes
		if out.Truncated {
			data = data[:MaxDiffBytes]
		}
		out.Diff = strings.ToValidUTF8(string(data), string(utf8.RuneError))
		out.Stats = ParseNumstat(string(num.Stdout))
		for _, s := range out.Stats {
			out.Files = append(out.Files, s.Path)
		}
		return nil
	})
	return out, err
}

// ParseNumstat 解析 git diff --numstat -z（改名时取新路径；二进制文件的行数是 -）。
func ParseNumstat(text string) []DiffStat {
	tokens := strings.Split(text, "\x00")
	var out []DiffStat
	for i := 0; i < len(tokens); {
		fields := strings.Split(tokens[i], "\t")
		if len(fields) != 3 {
			i++
			continue
		}
		added, deleted, p := fields[0], fields[1], fields[2]
		if p == "" && i+2 < len(tokens) {
			p = tokens[i+2]
			i += 3
		} else {
			i++
		}
		binary := added == "-" || deleted == "-"
		s := DiffStat{Path: p, Binary: binary}
		if !binary {
			s.Added, s.Deleted = atoi32(added), atoi32(deleted)
		}
		out = append(out, s)
	}
	return out
}

func atoi32(s string) int32 {
	var n int32
	for _, c := range s {
		if c < '0' || c > '9' {
			return n
		}
		n = n*10 + int32(c-'0')
	}
	return n
}

// Commit 是一个提交。
type Commit struct {
	SHA, AuthorName, AuthorEmail, AuthoredAt, Subject string
}

// Log 是 HEAD 上的提交（给了 base 时只给 HEAD 有、它没有的）。
func (b *Broker) Log(ctx context.Context, tenant *commonv1.TenantContext, workspaceID string, limit int, base, runtime string) ([]Commit, error) {
	if limit == 0 {
		limit = DefaultLog
	}
	if limit < 1 || limit > MaxLog {
		return nil, providererr.New(providererr.Invalid, "invalid_limit", "limit 最多 200")
	}
	if base != "" {
		if err := CheckRev(base); err != nil {
			return nil, err
		}
	}
	var out []Commit
	err := b.withGit(ctx, tenant, workspaceID, runtime, func(lease *Lease) error {
		head, _, err := b.head(ctx, lease)
		if err != nil || head == "" {
			return err
		}
		rev := "HEAD"
		if base != "" {
			rev = base + "..HEAD"
		}
		res, err := b.git(ctx, lease, []string{"log", "-n" + strconv.Itoa(limit), "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%s", "--end-of-options", rev}, nil)
		if err != nil {
			return err
		}
		if !ok(res) {
			return gitError("读提交记录", res)
		}
		for _, line := range strings.Split(strings.ToValidUTF8(string(res.Stdout), string(utf8.RuneError)), "\n") {
			f := strings.Split(line, "\x1f")
			if len(f) == 5 && shaPattern.MatchString(f[0]) {
				out = append(out, Commit{SHA: f[0], AuthorName: f[1], AuthorEmail: f[2], AuthoredAt: f[3], Subject: f[4]})
			}
		}
		return nil
	})
	return out, err
}

// gitError 按 git 的输出分类（同原来 Worker 的 workspace.operations._git_error）：远端拒绝是 denied，连不上是 transient，
// 推送被拒是 conflict，分支、提交、对象不在是 not_found，其余 invalid。
func gitError(what string, res sandbox.ExecResult) error {
	msg := strings.TrimSpace(string(res.Stderr))
	if len(msg) > 300 {
		msg = msg[:300]
	}
	lower := strings.ToLower(msg)
	has := func(words ...string) bool {
		for _, w := range words {
			if strings.Contains(lower, w) {
				return true
			}
		}
		return false
	}
	switch {
	case has("authentication failed", "permission denied", "403", "401", "could not read username"):
		return providererr.New(providererr.Denied, "git_denied", what+"被远端拒绝（凭据不对或没有权限）："+msg)
	case has("could not resolve host", "connection timed out", "connection refused", "failed to connect", "early eof", "rpc failed"):
		return providererr.New(providererr.Transient, "git_unavailable", what+"时连不上远端："+msg)
	case has("[rejected]", "non-fast-forward", "fetch first", "stale info"):
		return providererr.New(providererr.Conflict, "push_rejected", what+"被拒绝（远端分支有别的提交）："+msg)
	case has("not found", "does not exist", "couldn't find remote ref", "unknown revision", "bad revision", "invalid upstream",
		"not our ref", "unadvertised object", "bad object"):
		return providererr.New(providererr.NotFound, "git_not_found", what+"失败："+msg)
	}
	return providererr.New(providererr.Invalid, "git_failed", what+"失败："+msg)
}
