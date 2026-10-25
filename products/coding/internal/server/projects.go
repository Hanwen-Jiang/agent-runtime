package server

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/products/coding/internal/samples"
)

const (
	maxDiffBytes = 2 << 20
	maxFileBytes = 512 << 10
)

var nonSlug = regexp.MustCompile(`[^a-z0-9]+`)

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// newProjectID 由名字派生项目 id（也是 Runtime 的 workspace id），后面加 4 位随机数避免重名。
func newProjectID(name string) string {
	base := strings.Trim(nonSlug.ReplaceAllString(strings.ToLower(name), "-"), "-")
	if len(base) > 40 {
		base = strings.Trim(base[:40], "-")
	}
	if base == "" {
		base = "project"
	}
	return base + "-" + randomHex(2)
}

func (s *Server) handleSamples(w http.ResponseWriter, _ *http.Request, _ identity) {
	writeJSON(w, http.StatusOK, samples.All)
}

func (s *Server) handleListProjects(w http.ResponseWriter, _ *http.Request, id identity) {
	projects, err := s.store.Projects(id.TenantID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if projects == nil {
		projects = []Project{}
	}
	slices.SortFunc(projects, func(a, b Project) int { return b.CreatedAt.Compare(a.CreatedAt) })
	writeJSON(w, http.StatusOK, projects)
}

func (s *Server) handleCreateProject(w http.ResponseWriter, r *http.Request, id identity) {
	var req struct {
		Name   string `json:"name"`
		GitURL string `json:"git_url"`
		Branch string `json:"branch"`
		Sample string `json:"sample"`
	}
	if !readJSON(w, r, &req) {
		return
	}
	req.Name, req.GitURL, req.Branch = strings.TrimSpace(req.Name), strings.TrimSpace(req.GitURL), strings.TrimSpace(req.Branch)
	if req.Name == "" || len(req.Name) > 80 {
		writeError(w, http.StatusBadRequest, "项目名要有，且不超过 80 个字符")
		return
	}
	source := "empty"
	switch {
	case req.GitURL != "":
		u, err := url.Parse(req.GitURL)
		if err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil {
			writeError(w, http.StatusBadRequest, "只支持 https 的 Git 地址，且地址里不能带用户名或密码")
			return
		}
		if req.Branch != "" && (strings.HasPrefix(req.Branch, "-") || strings.ContainsAny(req.Branch, " \t\n~^:?*[\\")) {
			writeError(w, http.StatusBadRequest, "分支名不合法")
			return
		}
		source = req.GitURL
	case req.Sample != "":
		if _, ok := samples.Find(req.Sample); !ok {
			writeError(w, http.StatusBadRequest, "没有这个示例")
			return
		}
		source = "sample:" + req.Sample
	}
	p := Project{ID: newProjectID(req.Name), Name: req.Name, Source: source, CreatedAt: time.Now().UTC(), CreatedBy: id.UserID}
	dir := s.workspace(id.TenantID, p.ID)
	if err := os.MkdirAll(filepath.Dir(dir), 0o755); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	ctx, cancel := ctxTimeout(r, 5*time.Minute)
	defer cancel()
	name, email := id.UserID, id.UserID+"@"+id.TenantID+".coding.local"
	err := func() error {
		if req.GitURL != "" {
			args := []string{"clone", "--depth", "50", "--no-tags"}
			if req.Branch != "" {
				args = append(args, "--branch", req.Branch)
			}
			args = append(args, "--", req.GitURL, dir)
			// 已配置的 Git 平台上的仓库：记下平台与仓库路径；连接了这一家就带令牌克隆（私有仓库要），令牌只在这次 git 进程的环境变量里。
			if prov, repo := s.providerOfURL(req.GitURL); prov != nil {
				p.Provider, p.Repo = prov.id, repo
				host, user, pass := s.gitCredentials(r, id, p)
				_, err := s.git.RunWithAuth(ctx, filepath.Dir(dir), host, user, pass, args...)
				return err
			}
			_, err := s.git.Run(ctx, filepath.Dir(dir), args...)
			return err
		}
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return err
		}
		if req.Sample != "" {
			if err := samples.Write(req.Sample, dir); err != nil {
				return err
			}
		} else if err := os.WriteFile(filepath.Join(dir, "README.md"), []byte("# "+req.Name+"\n"), 0o644); err != nil {
			return err
		}
		for _, args := range [][]string{{"init", "-q", "-b", "main"}, {"add", "-A"}, {"commit", "-q", "-m", "Initial commit"}} {
			if _, err := s.git.RunAs(ctx, dir, name, email, args...); err != nil {
				return err
			}
		}
		return nil
	}()
	if err != nil {
		_ = os.RemoveAll(dir)
		writeError(w, http.StatusBadRequest, "建项目失败："+err.Error())
		return
	}
	p.BaseBranch = s.git.Branch(ctx, dir)
	if err := s.store.UpdateProjects(id.TenantID, func(ps []Project) ([]Project, error) { return append(ps, p), nil }); err != nil {
		_ = os.RemoveAll(dir)
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, p)
}

// handleDeleteProject 删掉工作区：基线仓库、所有会话的工作副本与记录。
func (s *Server) handleDeleteProject(w http.ResponseWriter, r *http.Request, id identity) {
	p, ok := s.project(w, r, id)
	if !ok {
		return
	}
	sessions, err := s.projectSessions(id.TenantID, p.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	for _, x := range sessions {
		if s.busy(r, id, x.WorkspaceID) {
			writeError(w, http.StatusConflict, "还有任务在跑，等它结束或先取消")
			return
		}
	}
	for _, dir := range append(s.sessionDirs(id.TenantID, p, sessions), s.workspace(id.TenantID, p.ID)) {
		if err := os.RemoveAll(dir); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
	}
	if err := s.store.DeleteProject(id.TenantID, p.ID); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

// safePath 把请求里的相对路径解析到工作区里；.git、绝对路径、..、反斜杠、经符号链接出界的一律拒绝。
func safePath(root, rel string) (string, error) {
	rel = strings.TrimSpace(rel)
	if rel == "" || rel == "." {
		return root, nil
	}
	if strings.ContainsAny(rel, "\\\x00") {
		return "", errors.New("路径不合法")
	}
	clean := strings.TrimPrefix(path.Clean("/"+rel), "/")
	if clean == "" {
		return root, nil
	}
	if slices.Contains(strings.Split(clean, "/"), ".git") {
		return "", errors.New("不能访问 .git")
	}
	target := filepath.Join(root, filepath.FromSlash(clean))
	realRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		return "", err
	}
	realTarget, err := filepath.EvalSymlinks(target)
	if err != nil {
		return "", err
	}
	if within, _ := filepath.Rel(realRoot, realTarget); within == ".." || strings.HasPrefix(within, ".."+string(filepath.Separator)) {
		return "", errors.New("路径不在工作区里")
	}
	return target, nil
}

func (s *Server) handleTree(w http.ResponseWriter, r *http.Request, id identity) {
	_, sess, ok := s.session(w, r, id)
	if !ok {
		return
	}
	dir, err := safePath(s.sessionDir(id.TenantID, sess), r.URL.Query().Get("path"))
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		writeError(w, http.StatusNotFound, "目录不存在")
		return
	}
	type entry struct {
		Name string `json:"name"`
		Dir  bool   `json:"dir"`
		Size int64  `json:"size"`
	}
	out := []entry{}
	for _, e := range entries {
		if e.Name() == ".git" {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		out = append(out, entry{Name: e.Name(), Dir: e.IsDir(), Size: info.Size()})
	}
	slices.SortFunc(out, func(a, b entry) int {
		if a.Dir != b.Dir {
			if a.Dir {
				return -1
			}
			return 1
		}
		return strings.Compare(a.Name, b.Name)
	})
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) handleFile(w http.ResponseWriter, r *http.Request, id identity) {
	_, sess, ok := s.session(w, r, id)
	if !ok {
		return
	}
	file, err := safePath(s.sessionDir(id.TenantID, sess), r.URL.Query().Get("path"))
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	info, err := os.Stat(file)
	if err != nil || info.IsDir() {
		writeError(w, http.StatusNotFound, "文件不存在")
		return
	}
	if info.Size() > maxFileBytes {
		writeJSON(w, http.StatusOK, map[string]any{"too_large": true, "size": info.Size()})
		return
	}
	data, err := os.ReadFile(file)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if bytes.IndexByte(data[:min(len(data), 8192)], 0) >= 0 {
		writeJSON(w, http.StatusOK, map[string]any{"binary": true, "size": info.Size()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"content": string(data), "size": info.Size()})
}

func (s *Server) handleLog(w http.ResponseWriter, r *http.Request, id identity) {
	_, sess, ok := s.session(w, r, id)
	if !ok {
		return
	}
	ctx, cancel := ctxTimeout(r, 30*time.Second)
	defer cancel()
	out, err := s.git.Run(ctx, s.sessionDir(id.TenantID, sess), "log", "-n", "30", "--date=iso-strict", "--pretty=format:%H%x1f%an%x1f%ad%x1f%s")
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	commits := []map[string]string{}
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		if f := strings.Split(line, "\x1f"); len(f) == 4 {
			commits = append(commits, map[string]string{"hash": f[0], "author": f[1], "date": f[2], "subject": f[3]})
		}
	}
	writeJSON(w, http.StatusOK, commits)
}

// diff 返回工作区相对 HEAD 的改动（含新文件）。
func (s *Server) diff(r *http.Request, dir string) (files []map[string]any, diff string, truncated bool, err error) {
	ctx, cancel := ctxTimeout(r, time.Minute)
	defer cancel()
	if _, err = s.git.Run(ctx, dir, "add", "--intent-to-add", "--", "."); err != nil {
		return nil, "", false, err
	}
	numstat, err := s.git.Run(ctx, dir, "diff", "HEAD", "--numstat", "--no-color", "--no-ext-diff")
	if err != nil {
		return nil, "", false, err
	}
	files = []map[string]any{}
	for _, line := range strings.Split(strings.TrimSpace(numstat), "\n") {
		f := strings.SplitN(line, "\t", 3)
		if len(f) != 3 {
			continue
		}
		added, _ := strconv.Atoi(f[0])
		deleted, _ := strconv.Atoi(f[1])
		files = append(files, map[string]any{"path": f[2], "added": added, "deleted": deleted, "binary": f[0] == "-"})
	}
	diff, err = s.git.Run(ctx, dir, "diff", "HEAD", "--no-color", "--no-ext-diff", "--find-renames")
	if err != nil {
		return nil, "", false, err
	}
	if len(diff) > maxDiffBytes {
		diff, truncated = diff[:maxDiffBytes], true
	}
	return files, diff, truncated, nil
}

func (s *Server) handleChanges(w http.ResponseWriter, r *http.Request, id identity) {
	_, sess, ok := s.session(w, r, id)
	if !ok {
		return
	}
	dir := s.sessionDir(id.TenantID, sess)
	files, diff, truncated, err := s.diff(r, dir)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"head": s.git.Head(r.Context(), dir), "branch": s.git.Branch(r.Context(), dir), "files": files, "diff": diff, "truncated": truncated})
}

func (s *Server) handlePatch(w http.ResponseWriter, r *http.Request, id identity) {
	_, sess, ok := s.session(w, r, id)
	if !ok {
		return
	}
	_, diff, _, err := s.diff(r, s.sessionDir(id.TenantID, sess))
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	w.Header().Set("Content-Type", "text/x-patch; charset=utf-8")
	w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=%q", sess.WorkspaceID+".patch"))
	_, _ = w.Write([]byte(diff))
}

func (s *Server) handleCommit(w http.ResponseWriter, r *http.Request, id identity) {
	_, sess, ok := s.session(w, r, id)
	if !ok {
		return
	}
	var req struct {
		Message string `json:"message"`
	}
	if !readJSON(w, r, &req) {
		return
	}
	msg := strings.TrimSpace(req.Message)
	if msg == "" {
		msg = "Apply agent changes"
	}
	if s.busy(r, id, sess.WorkspaceID) {
		writeError(w, http.StatusConflict, "还有任务在跑，等它结束再提交")
		return
	}
	dir := s.sessionDir(id.TenantID, sess)
	ctx, cancel := ctxTimeout(r, time.Minute)
	defer cancel()
	if _, err := s.git.Run(ctx, dir, "add", "-A"); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	status, err := s.git.Run(ctx, dir, "status", "--porcelain")
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if strings.TrimSpace(status) == "" {
		writeError(w, http.StatusBadRequest, "没有可提交的改动")
		return
	}
	if _, err := s.git.RunAs(ctx, dir, id.UserID, id.UserID+"@"+id.TenantID+".coding.local", "commit", "-q", "-m", msg); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"head": s.git.Head(ctx, dir)})
}

func (s *Server) handleDiscard(w http.ResponseWriter, r *http.Request, id identity) {
	_, sess, ok := s.session(w, r, id)
	if !ok {
		return
	}
	if s.busy(r, id, sess.WorkspaceID) {
		writeError(w, http.StatusConflict, "还有任务在跑，等它结束或先取消")
		return
	}
	dir := s.sessionDir(id.TenantID, sess)
	ctx, cancel := ctxTimeout(r, time.Minute)
	defer cancel()
	for _, args := range [][]string{{"reset", "-q", "--hard", "HEAD"}, {"clean", "-q", "-fd"}} {
		if _, err := s.git.Run(ctx, dir, args...); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}
