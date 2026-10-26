package server

import (
	"errors"
	"net/http"
	"regexp"
	"strings"
	"time"
)

// agentBranch 是允许推送的分支：只推 agent/*，引用由服务端拼出，不强推。
var agentBranch = regexp.MustCompile(`^agent/[a-z0-9][a-z0-9._-]{0,60}$`)

func branchName(title string) string {
	base := strings.Trim(nonSlug.ReplaceAllString(strings.ToLower(title), "-"), "-")
	if len(base) > 30 {
		base = strings.Trim(base[:30], "-")
	}
	if base == "" {
		base = "change"
	}
	return "agent/" + base + "-" + randomHex(2)
}

// updateProject 在锁里改一个项目的记录，返回改后的值。
func (s *Server) updateProject(tenant, id string, fn func(*Project)) (Project, error) {
	var out Project
	err := s.store.UpdateProjects(tenant, func(ps []Project) ([]Project, error) {
		for i := range ps {
			if ps[i].ID == id {
				fn(&ps[i])
				out = ps[i]
				return ps, nil
			}
		}
		return nil, msg("项目不存在")
	})
	return out, err
}

// handlePublish 把会话工作副本里的改动推到这个会话的 agent/* 分支并开草稿 PR（已有 PR 时只推送，PR 自动更新）。
// 一个会话一个分支、一个 PR。由人点按钮触发。
func (s *Server) handlePublish(w http.ResponseWriter, r *http.Request, id identity) {
	p, sess, ok := s.session(w, r, id)
	if !ok {
		return
	}
	prov := s.providers[p.Provider]
	if p.Repo == "" || prov == nil {
		writeMsg(w, r, http.StatusBadRequest, "只有从已配置的 Git 平台克隆的工作区能推送并开 PR")
		return
	}
	conn, token, err := s.connToken(r, id, prov)
	if errors.Is(err, errNotConnected) {
		writeMsg(w, r, http.StatusForbidden, "先在“账号与平台”里连接 %s，才能推送到 %[1]s", prov.Name)
		return
	} else if err != nil {
		writeGitError(w, r, err)
		return
	}
	var req struct {
		Title string `json:"title"`
		Body  string `json:"body"`
	}
	if !readJSON(w, r, &req) {
		return
	}
	title := strings.TrimSpace(req.Title)
	if title == "" {
		title = "Agent changes"
	}
	if len(title) > 200 {
		title = title[:200]
	}
	if s.busy(r, id, sess.WorkspaceID) {
		writeMsg(w, r, http.StatusConflict, "还有任务在跑，等它结束再推送")
		return
	}
	dir := s.sessionDir(id.TenantID, sess)
	ctx, cancel := ctxTimeout(r, 3*time.Minute)
	defer cancel()
	host := prov.Web + "/"
	gitUser, gitPass := prov.gitAuth(conn, token)
	name, email := prov.commitAuthor(conn)
	base := p.BaseBranch
	if base == "" {
		base = "main"
	}

	status, err := s.git.Run(ctx, dir, "status", "--porcelain")
	if err != nil {
		writeErr(w, r, http.StatusInternalServerError, err)
		return
	}
	dirty := strings.TrimSpace(status) != ""
	branch := sess.Branch
	if branch == "" {
		// 第一次推：工作区干净、也不比基线多提交时没有东西可推，不建分支。
		if ahead, err := s.git.Run(ctx, dir, "rev-list", "--count", "origin/"+base+"..HEAD"); !dirty && err == nil && strings.TrimSpace(ahead) == "0" {
			writeMsg(w, r, http.StatusBadRequest, "没有可推送的改动")
			return
		}
		branch = branchName(title)
		if _, err := s.git.Run(ctx, dir, "checkout", "-q", "-b", branch); err != nil {
			writeErr(w, r, http.StatusInternalServerError, err)
			return
		}
	} else {
		if s.git.Branch(ctx, dir) != branch {
			args := []string{"checkout", "-q", branch}
			if _, err := s.git.Run(ctx, dir, "rev-parse", "--verify", "-q", "refs/heads/"+branch); err != nil {
				args = []string{"checkout", "-q", "-b", branch} // 本地没有这个分支（例如工作区重建过）：从当前 HEAD 建
			}
			if _, err := s.git.Run(ctx, dir, args...); err != nil {
				writeErr(w, r, http.StatusInternalServerError, err)
				return
			}
		}
		if !dirty && s.git.Head(ctx, dir) == sess.PushedHead {
			writeMsg(w, r, http.StatusBadRequest, "没有可推送的改动")
			return
		}
	}
	if !agentBranch.MatchString(branch) {
		writeMsg(w, r, http.StatusInternalServerError, "分支名不合法：%s", branch)
		return
	}
	if dirty {
		if _, err := s.git.Run(ctx, dir, "add", "-A"); err != nil {
			writeErr(w, r, http.StatusInternalServerError, err)
			return
		}
		if _, err := s.git.RunAs(ctx, dir, name, email, "commit", "-q", "-m", title); err != nil {
			writeErr(w, r, http.StatusInternalServerError, err)
			return
		}
	}
	if _, err := s.git.RunWithAuth(ctx, dir, host, gitUser, gitPass, "push", "-q", "origin", "HEAD:refs/heads/"+branch); err != nil {
		_, _ = s.updateSession(id.TenantID, sess, func(x *Session) { x.Branch = branch })
		writeMsg(w, r, http.StatusBadGateway, "推送失败（是否有这个仓库的写权限？）：%v", err)
		return
	}
	pushed := s.git.Head(ctx, dir)

	created := false
	prURL, prNumber := sess.PRURL, sess.PRNumber
	if prNumber == 0 {
		body := strings.TrimSpace(req.Body)
		if body == "" {
			body = msg("由 Coding Agent 生成的改动，请审阅后再合并。").In(requestLang(r))
		}
		prCtx, prCancel := ctxTimeout(r, 30*time.Second)
		pr, err := prov.openPR(prCtx, token, p.Repo, title, branch, base, body)
		prCancel()
		if err != nil {
			// 分支已推上去；PR 没开成时把分支记下来，下次推送再试。
			_, _ = s.updateSession(id.TenantID, sess, func(x *Session) { x.Branch, x.PushedHead = branch, pushed })
			writeGitError(w, r, err)
			return
		}
		prURL, prNumber, created = pr.HTMLURL, pr.Number, true
	}
	updated, err := s.updateSession(id.TenantID, sess, func(x *Session) {
		x.Branch, x.PRURL, x.PRNumber, x.PushedHead = branch, prURL, prNumber, pushed
	})
	if err != nil {
		writeErr(w, r, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"session": updated, "branch": branch, "pr_url": prURL, "pr_number": prNumber, "created": created, "head": pushed})
}

// handleResetBranch 让会话的工作副本回到基线分支并拉取最新：PR 合并之后开始下一轮。要求没有未提交的改动。
func (s *Server) handleResetBranch(w http.ResponseWriter, r *http.Request, id identity) {
	p, sess, ok := s.session(w, r, id)
	if !ok {
		return
	}
	if s.busy(r, id, sess.WorkspaceID) {
		writeMsg(w, r, http.StatusConflict, "还有任务在跑，等它结束再切换")
		return
	}
	dir := s.sessionDir(id.TenantID, sess)
	ctx, cancel := ctxTimeout(r, 3*time.Minute)
	defer cancel()
	if status, _ := s.git.Run(ctx, dir, "status", "--porcelain"); strings.TrimSpace(status) != "" {
		writeMsg(w, r, http.StatusConflict, "还有未提交的改动：先推送、提交或丢弃")
		return
	}
	base := p.BaseBranch
	if base == "" {
		writeMsg(w, r, http.StatusBadRequest, "不知道基线分支")
		return
	}
	if p.Repo != "" && s.providers[p.Provider] != nil {
		host, user, pass := s.gitCredentials(r, id, p)
		if _, err := s.git.RunWithAuth(ctx, dir, host, user, pass, "fetch", "-q", "--depth", "50", "origin", base); err != nil {
			writeMsg(w, r, http.StatusBadGateway, "拉取失败：%v", err)
			return
		}
	}
	target := base
	if out, err := s.git.Run(ctx, dir, "rev-parse", "--verify", "-q", "origin/"+base); err == nil && strings.TrimSpace(out) != "" {
		target = "origin/" + base
	}
	if _, err := s.git.Run(ctx, dir, "checkout", "-q", "-B", base, target); err != nil {
		writeErr(w, r, http.StatusInternalServerError, err)
		return
	}
	updated, err := s.updateSession(id.TenantID, sess, func(x *Session) { x.Branch, x.PRURL, x.PRNumber, x.PushedHead = "", "", 0, "" })
	if err != nil {
		writeErr(w, r, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"session": updated, "head": s.git.Head(ctx, dir)})
}
