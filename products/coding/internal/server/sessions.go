package server

import (
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"
	"unicode/utf8"
)

// 会话（docs/plan/coding-agent-mvp.md 第 8 节）：一个工作区可以开很多会话，每个会话一段对话（Runtime 的一个 Session）
// 加一份自己的工作副本。工作副本从工作区的基线仓库本地克隆（对象用硬链接），目录与 Runtime 的 workspace id 都是
// <项目>--<会话>：Worker 的工具与 Docker 沙箱看到的是一个完整的仓库，会话之间可以同时跑、互不干扰。

// legacySession 是会话出现之前的项目迁移成的会话：直接用项目自己的目录。
const legacySession = "main"

const maxTitleRunes = 40

func newSessionID() string {
	return "s" + randomHex(3)
}

// sessionTitle 从任务描述里取会话标题：第一行，最多 40 个字。
func sessionTitle(message string) string {
	line, _, _ := strings.Cut(strings.TrimSpace(message), "\n")
	line = strings.TrimSpace(line)
	if utf8.RuneCountInString(line) > maxTitleRunes {
		line = string([]rune(line)[:maxTitleRunes]) + "…"
	}
	return line
}

// ensureSessions 返回租户的全部会话；还没有会话、但有旧数据（对话、任务或推送记录）的项目先迁移成 main 会话。
func (s *Server) ensureSessions(tenant string) ([]Session, error) {
	projects, err := s.store.Projects(tenant)
	if err != nil {
		return nil, err
	}
	sessions, err := s.store.Sessions(tenant)
	if err != nil {
		return nil, err
	}
	var add []Session
	for _, p := range projects {
		if slices.ContainsFunc(sessions, func(x Session) bool { return x.ProjectID == p.ID }) {
			continue
		}
		if p.SessionID == "" && p.Branch == "" && !s.store.HasTasks(tenant, p.ID) {
			continue
		}
		main := Session{
			ID: legacySession, ProjectID: p.ID, Title: p.Name, CreatedAt: p.CreatedAt, CreatedBy: p.CreatedBy, UpdatedAt: p.CreatedAt,
			WorkspaceID: p.ID, RuntimeSessionID: p.SessionID, Branch: p.Branch, PRURL: p.PRURL, PRNumber: p.PRNumber, PushedHead: p.PushedHead,
		}
		if tasks, _ := s.store.Tasks(tenant, p.ID); len(tasks) > 0 {
			main.Title = sessionTitle(tasks[0].Message)
			main.UpdatedAt = tasks[len(tasks)-1].CreatedAt
		}
		add = append(add, main)
	}
	if len(add) == 0 {
		return sessions, nil
	}
	err = s.store.UpdateSessions(tenant, func(cur []Session) ([]Session, error) {
		for _, m := range add {
			if !slices.ContainsFunc(cur, func(x Session) bool { return x.ProjectID == m.ProjectID }) {
				cur = append(cur, m)
			}
		}
		sessions = cur
		return cur, nil
	})
	return sessions, err
}

// session 取出请求路径里的工作区与会话，确认它们属于这个租户。
func (s *Server) session(w http.ResponseWriter, r *http.Request, id identity) (Project, Session, bool) {
	p, ok := s.project(w, r, id)
	if !ok {
		return Project{}, Session{}, false
	}
	sid := r.PathValue("session")
	sessions, err := s.ensureSessions(id.TenantID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return Project{}, Session{}, false
	}
	i := slices.IndexFunc(sessions, func(x Session) bool { return x.ProjectID == p.ID && x.ID == sid })
	if !slug.MatchString(sid) || i < 0 {
		writeError(w, http.StatusNotFound, "会话不存在")
		return Project{}, Session{}, false
	}
	return p, sessions[i], true
}

func (s *Server) sessionDir(tenant string, sess Session) string {
	return s.workspace(tenant, sess.WorkspaceID)
}

// updateSession 在锁里改一个会话的记录，返回改后的值。
func (s *Server) updateSession(tenant string, sess Session, fn func(*Session)) (Session, error) {
	var out Session
	err := s.store.UpdateSessions(tenant, func(all []Session) ([]Session, error) {
		for i := range all {
			if all[i].ProjectID == sess.ProjectID && all[i].ID == sess.ID {
				fn(&all[i])
				out = all[i]
				return all, nil
			}
		}
		return nil, errors.New("会话不存在")
	})
	return out, err
}

func (s *Server) handleListSessions(w http.ResponseWriter, _ *http.Request, id identity) {
	sessions, err := s.ensureSessions(id.TenantID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if sessions == nil {
		sessions = []Session{}
	}
	slices.SortFunc(sessions, func(a, b Session) int { return b.UpdatedAt.Compare(a.UpdatedAt) })
	writeJSON(w, http.StatusOK, sessions)
}

func (s *Server) handleCreateSession(w http.ResponseWriter, r *http.Request, id identity) {
	p, ok := s.project(w, r, id)
	if !ok {
		return
	}
	var req struct {
		Title string `json:"title"`
	}
	if !readJSON(w, r, &req) {
		return
	}
	if _, err := s.ensureSessions(id.TenantID); err != nil { // 旧项目先迁移出 main 会话，免得被新会话挤掉
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	sess, err := s.createSession(r, id, p, sessionTitle(req.Title))
	if err != nil {
		writeError(w, http.StatusInternalServerError, "开会话失败："+err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, sess)
}

// createSession 从工作区的基线仓库复制出会话的工作副本（检出基线分支），Git 平台的工作区再把远端换回平台、尽量拉到基线分支的最新提交。
func (s *Server) createSession(r *http.Request, id identity, p Project, title string) (Session, error) {
	now := time.Now().UTC()
	sess := Session{ID: newSessionID(), ProjectID: p.ID, Title: title, CreatedAt: now, CreatedBy: id.UserID, UpdatedAt: now}
	sess.WorkspaceID = p.ID + "--" + sess.ID
	base := s.workspace(id.TenantID, p.ID)
	dir := s.sessionDir(id.TenantID, sess)
	ctx, cancel := ctxTimeout(r, 3*time.Minute)
	defer cancel()
	if _, err := s.git.CloneLocal(ctx, base, dir, p.BaseBranch); err != nil {
		_ = os.RemoveAll(dir)
		return Session{}, err
	}
	if p.Repo != "" && strings.HasPrefix(p.Source, "https://") && s.providers[p.Provider] != nil {
		if _, err := s.git.Run(ctx, dir, "remote", "set-url", "origin", p.Source); err != nil {
			_ = os.RemoveAll(dir)
			return Session{}, err
		}
		if host, user, pass := s.gitCredentials(r, id, p); p.BaseBranch != "" {
			if _, err := s.git.RunWithAuth(ctx, dir, host, user, pass, "fetch", "-q", "--depth", "50", "origin", p.BaseBranch); err == nil {
				_, _ = s.git.Run(ctx, dir, "reset", "-q", "--hard", "FETCH_HEAD")
			}
		}
	}
	sess.BaseCommit = s.git.Head(ctx, dir)
	if err := s.store.UpdateSessions(id.TenantID, func(all []Session) ([]Session, error) { return append(all, sess), nil }); err != nil {
		_ = os.RemoveAll(dir)
		return Session{}, err
	}
	return sess, nil
}

func (s *Server) handleRenameSession(w http.ResponseWriter, r *http.Request, id identity) {
	_, sess, ok := s.session(w, r, id)
	if !ok {
		return
	}
	var req struct {
		Title string `json:"title"`
	}
	if !readJSON(w, r, &req) {
		return
	}
	title := sessionTitle(req.Title)
	if title == "" {
		writeError(w, http.StatusBadRequest, "标题不能为空")
		return
	}
	updated, err := s.updateSession(id.TenantID, sess, func(x *Session) { x.Title = title })
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, updated)
}

// handleDeleteSession 删掉会话与它的工作副本；迁移来的 main 会话用的是工作区自己的目录，只删记录，不删目录。
func (s *Server) handleDeleteSession(w http.ResponseWriter, r *http.Request, id identity) {
	p, sess, ok := s.session(w, r, id)
	if !ok {
		return
	}
	if s.busy(r, id, sess.WorkspaceID) {
		writeError(w, http.StatusConflict, "还有任务在跑，等它结束或先取消")
		return
	}
	if sess.WorkspaceID != p.ID {
		if err := os.RemoveAll(s.sessionDir(id.TenantID, sess)); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
	}
	if err := s.store.DeleteSession(id.TenantID, sess); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

// projectSessions 是工作区下的全部会话。
func (s *Server) projectSessions(tenant, project string) ([]Session, error) {
	sessions, err := s.ensureSessions(tenant)
	if err != nil {
		return nil, err
	}
	return slices.DeleteFunc(sessions, func(x Session) bool { return x.ProjectID != project }), nil
}

// sessionDirs 是工作区下所有会话自己的工作副本目录（不含基线仓库）。
func (s *Server) sessionDirs(tenant string, p Project, sessions []Session) []string {
	var dirs []string
	for _, x := range sessions {
		if x.WorkspaceID != p.ID {
			dirs = append(dirs, filepath.Clean(s.sessionDir(tenant, x)))
		}
	}
	return dirs
}
