package server

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"slices"
	"strings"
	"time"
	"unicode/utf8"
)

const maxMessageChars = 20000

// maxBackfill 是一次刷新最多补查几个“已结束、还没取过计量”的任务（之前版本留下的记录）。
const maxBackfill = 8

// refresh 向 Runtime 查询一个会话（按 WorkspaceID）里还没结束的任务（以及少量还没取过计量的已结束任务），
// 更新缓存的状态与计量；查不到的保持原样。
func (s *Server) refresh(r *http.Request, id identity, workspaceID string) []TaskRecord {
	tasks, err := s.store.Tasks(id.TenantID, workspaceID)
	if err != nil {
		return nil
	}
	changed := map[string]TaskRecord{}
	backfill := 0
	for _, t := range tasks {
		if t.Settled {
			continue
		}
		if terminal(t.Status) {
			if backfill >= maxBackfill {
				continue
			}
			backfill++
		}
		var q queryResponse
		ctx, cancel := ctxTimeout(r, 10*time.Second)
		err := s.api.Call(ctx, id.token, "QueryTask", map[string]string{"taskId": t.ID}, &q)
		cancel()
		switch {
		case err == nil && q.Status != "":
			if next := withQuery(t, q); !sameRecord(next, t) {
				changed[t.ID] = next
			}
		case notFound(err) && terminal(t.Status):
			t.Settled = true
			changed[t.ID] = t
		}
	}
	if len(changed) > 0 {
		_ = s.store.UpdateTasks(id.TenantID, workspaceID, func(ts []TaskRecord) []TaskRecord {
			for i := range ts {
				if next, ok := changed[ts[i].ID]; ok {
					ts[i] = next
				}
			}
			return ts
		})
		for i := range tasks {
			if next, ok := changed[tasks[i].ID]; ok {
				tasks[i] = next
			}
		}
	}
	return tasks
}

// busy 判断一个会话里是否还有没结束的任务（提交、丢弃、删除之前查）。
func (s *Server) busy(r *http.Request, id identity, workspaceID string) bool {
	return slices.ContainsFunc(s.refresh(r, id, workspaceID), func(t TaskRecord) bool { return !terminal(t.Status) })
}

func (s *Server) handleListTasks(w http.ResponseWriter, r *http.Request, id identity) {
	_, sess, ok := s.session(w, r, id)
	if !ok {
		return
	}
	tasks := s.refresh(r, id, sess.WorkspaceID)
	if tasks == nil {
		tasks = []TaskRecord{}
	}
	slices.SortFunc(tasks, func(a, b TaskRecord) int { return b.CreatedAt.Compare(a.CreatedAt) })
	writeJSON(w, http.StatusOK, tasks)
}

func (s *Server) handleSubmitTask(w http.ResponseWriter, r *http.Request, id identity) {
	_, sess, ok := s.session(w, r, id)
	if !ok {
		return
	}
	var req struct {
		Message string `json:"message"`
		Sandbox string `json:"sandbox"`
		Model   string `json:"model"`
	}
	if !readJSON(w, r, &req) {
		return
	}
	msg := strings.TrimSpace(req.Message)
	if msg == "" || utf8.RuneCountInString(msg) > maxMessageChars {
		writeMsg(w, r, http.StatusBadRequest, "任务描述不能为空，也不能超过 20000 个字")
		return
	}
	sandbox, ok := pickOption(req.Sandbox, sandboxLevels, defaultSandbox)
	if !ok {
		writeMsg(w, r, http.StatusBadRequest, "沙箱等级只能是 read 或 write_reversible")
		return
	}
	model, ok := pickOption(req.Model, modelPreferences, defaultModel)
	if !ok {
		writeMsg(w, r, http.StatusBadRequest, "模型倾向只能是 economy、balanced 或 best")
		return
	}
	base := s.git.Head(r.Context(), s.sessionDir(id.TenantID, sess))
	submit := map[string]any{
		"workspaceId":     sess.WorkspaceID,
		"clientRequestId": randomHex(16),
		"message":         msg,
		"sessionAccess":   "SESSION_ACCESS_READ_WRITE",
		"inputs":          map[string]any{"constraints": map[string]any{"model_preference": model}},
	}
	if sess.RuntimeSessionID != "" {
		submit["sessionId"] = sess.RuntimeSessionID
	}
	// 改写租户策略与提交在同一把租户锁里：task-service 提交时读策略文件，把上限固定进这个 Task。
	unlock := s.lockTenant(id.TenantID)
	defer unlock()
	if s.cfg.PoliciesFile == "" {
		sandbox = "" // 这个部署定不了沙箱等级，以 task-service 的租户策略为准
	} else if err := s.writeTenantCeiling(id.TenantID, sandbox, false); err != nil {
		writeMsg(w, r, http.StatusInternalServerError, "写租户策略失败：%v", err)
		return
	}
	var resp submitResponse
	ctx, cancel := ctxTimeout(r, 30*time.Second)
	defer cancel()
	if err := s.api.Call(ctx, id.token, "SubmitTask", submit, &resp); err != nil {
		writeAPIError(w, r, err)
		return
	}
	now := time.Now().UTC()
	_, _ = s.updateSession(id.TenantID, sess, func(x *Session) {
		if x.RuntimeSessionID == "" {
			x.RuntimeSessionID = resp.SessionID
		}
		if x.Title == "" {
			x.Title = sessionTitle(msg)
		}
		x.UpdatedAt = now
	})
	record := TaskRecord{ID: resp.TaskID, Message: msg, CreatedAt: now, CreatedBy: id.UserID, BaseCommit: base, Status: "TASK_STATUS_QUEUED", Sandbox: sandbox, Model: model}
	if err := s.store.UpdateTasks(id.TenantID, sess.WorkspaceID, func(ts []TaskRecord) []TaskRecord {
		if slices.ContainsFunc(ts, func(t TaskRecord) bool { return t.ID == record.ID }) {
			return ts
		}
		return append(ts, record)
	}); err != nil {
		writeErr(w, r, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusCreated, record)
}

// task 取出请求路径里的任务，确认它属于这个租户的这个会话。
func (s *Server) task(w http.ResponseWriter, r *http.Request, id identity) (Session, TaskRecord, bool) {
	_, sess, ok := s.session(w, r, id)
	if !ok {
		return Session{}, TaskRecord{}, false
	}
	tasks, err := s.store.Tasks(id.TenantID, sess.WorkspaceID)
	if err != nil {
		writeErr(w, r, http.StatusInternalServerError, err)
		return Session{}, TaskRecord{}, false
	}
	i := slices.IndexFunc(tasks, func(t TaskRecord) bool { return t.ID == r.PathValue("task") })
	if i < 0 {
		writeMsg(w, r, http.StatusNotFound, "任务不存在")
		return Session{}, TaskRecord{}, false
	}
	return sess, tasks[i], true
}

func (s *Server) handleQueryTask(w http.ResponseWriter, r *http.Request, id identity) {
	sess, t, ok := s.task(w, r, id)
	if !ok {
		return
	}
	var raw json.RawMessage
	ctx, cancel := ctxTimeout(r, 15*time.Second)
	defer cancel()
	if err := s.api.Call(ctx, id.token, "QueryTask", map[string]string{"taskId": t.ID}, &raw); err != nil {
		writeAPIError(w, r, err)
		return
	}
	var q queryResponse
	if err := json.Unmarshal(raw, &q); err == nil && q.Status != "" {
		if next := withQuery(t, q); !sameRecord(next, t) {
			_ = s.store.UpdateTasks(id.TenantID, sess.WorkspaceID, func(ts []TaskRecord) []TaskRecord {
				for i := range ts {
					if ts[i].ID == t.ID {
						ts[i] = next
					}
				}
				return ts
			})
			t = next
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"task": t, "runtime": raw})
}

// handleEvents 代理 Task 的 SSE 流：浏览器的 EventSource 带不了 Authorization 头，由这里补上令牌。
// 断线重连时浏览器会带 Last-Event-ID，原样转给 task-service 续传。
func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request, id identity) {
	_, t, ok := s.task(w, r, id)
	if !ok {
		return
	}
	last := r.Header.Get("Last-Event-ID")
	if last == "" {
		last = r.URL.Query().Get("last_event_id")
	}
	res, err := s.api.Events(r.Context(), id.token, t.ID, last)
	if err != nil {
		writeMsg(w, r, http.StatusBadGateway, "连不上 task-service")
		return
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(res.Body, 4096))
		writeErr(w, r, res.StatusCode, errors.New(strings.TrimSpace(string(body)))) // task-service 的说明原样转出
		return
	}
	flusher, _ := w.(http.Flusher)
	h := w.Header()
	h.Set("Content-Type", "text/event-stream")
	h.Set("Cache-Control", "no-cache")
	h.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	buf := make([]byte, 32<<10)
	for {
		n, err := res.Body.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				return
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if err != nil {
			return
		}
	}
}

func (s *Server) handleReply(w http.ResponseWriter, r *http.Request, id identity) {
	_, t, ok := s.task(w, r, id)
	if !ok {
		return
	}
	var req struct {
		QuestionID string `json:"question_id"`
		Option     string `json:"option"`
		Guidance   string `json:"guidance"`
	}
	if !readJSON(w, r, &req) {
		return
	}
	if req.QuestionID == "" || req.Option == "" {
		writeMsg(w, r, http.StatusBadRequest, "要给出 question_id 与 option")
		return
	}
	ctx, cancel := ctxTimeout(r, 15*time.Second)
	defer cancel()
	signal := map[string]any{"taskId": t.ID, "humanReply": map[string]string{"questionId": req.QuestionID, "option": req.Option, "guidance": req.Guidance}}
	if err := s.api.Call(ctx, id.token, "SignalTask", signal, nil); err != nil {
		writeAPIError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func (s *Server) handleCancel(w http.ResponseWriter, r *http.Request, id identity) {
	_, t, ok := s.task(w, r, id)
	if !ok {
		return
	}
	ctx, cancel := ctxTimeout(r, 15*time.Second)
	defer cancel()
	var resp map[string]any
	if err := s.api.Call(ctx, id.token, "CancelTask", map[string]string{"taskId": t.ID, "reason": msg("用户在网页上取消").In(requestLang(r))}, &resp); err != nil {
		writeAPIError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func writeAPIError(w http.ResponseWriter, r *http.Request, err error) {
	var apiErr *APIError
	if errors.As(err, &apiErr) {
		status := apiErr.Status
		if status < 400 || status > 599 {
			status = http.StatusBadGateway
		}
		if apiErr.Note != nil {
			writeErr(w, r, status, apiErr.Note)
		} else {
			writeErr(w, r, status, errors.New(apiErr.Message)) // task-service 的说明原样转出
		}
		return
	}
	writeErr(w, r, http.StatusBadGateway, err)
}
