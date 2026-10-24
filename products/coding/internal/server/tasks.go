package server

import (
	"errors"
	"io"
	"net/http"
	"slices"
	"strings"
	"time"
	"unicode/utf8"
)

const maxMessageChars = 20000

// refresh 向 Runtime 查询还没结束的任务，更新缓存的状态；查不到的保持原样。
func (s *Server) refresh(r *http.Request, id identity, project string) []TaskRecord {
	tasks, err := s.store.Tasks(id.TenantID, project)
	if err != nil {
		return nil
	}
	changed := map[string]string{}
	for _, t := range tasks {
		if terminal(t.Status) {
			continue
		}
		var q queryResponse
		ctx, cancel := ctxTimeout(r, 10*time.Second)
		err := s.api.Call(ctx, id.token, "QueryTask", map[string]string{"taskId": t.ID}, &q)
		cancel()
		if err == nil && q.Status != "" && q.Status != t.Status {
			changed[t.ID] = q.Status
		}
	}
	if len(changed) > 0 {
		_ = s.store.UpdateTasks(id.TenantID, project, func(ts []TaskRecord) []TaskRecord {
			for i := range ts {
				if st, ok := changed[ts[i].ID]; ok {
					ts[i].Status = st
				}
			}
			return ts
		})
		for i := range tasks {
			if st, ok := changed[tasks[i].ID]; ok {
				tasks[i].Status = st
			}
		}
	}
	return tasks
}

// busy 判断项目里是否还有没结束的任务（提交、丢弃、删除之前查）。
func (s *Server) busy(r *http.Request, id identity, project string) bool {
	return slices.ContainsFunc(s.refresh(r, id, project), func(t TaskRecord) bool { return !terminal(t.Status) })
}

func (s *Server) handleListTasks(w http.ResponseWriter, r *http.Request, id identity) {
	p, ok := s.project(w, r, id)
	if !ok {
		return
	}
	tasks := s.refresh(r, id, p.ID)
	if tasks == nil {
		tasks = []TaskRecord{}
	}
	slices.SortFunc(tasks, func(a, b TaskRecord) int { return b.CreatedAt.Compare(a.CreatedAt) })
	writeJSON(w, http.StatusOK, tasks)
}

func (s *Server) handleSubmitTask(w http.ResponseWriter, r *http.Request, id identity) {
	p, ok := s.project(w, r, id)
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
	if msg == "" || utf8.RuneCountInString(msg) > maxMessageChars {
		writeError(w, http.StatusBadRequest, "任务描述不能为空，也不能超过 20000 个字")
		return
	}
	dir := s.workspace(id.TenantID, p.ID)
	base := s.git.Head(r.Context(), dir)
	submit := map[string]any{
		"workspaceId":     p.ID,
		"clientRequestId": randomHex(16),
		"message":         msg,
		"sessionAccess":   "SESSION_ACCESS_READ_WRITE",
	}
	if p.SessionID != "" {
		submit["sessionId"] = p.SessionID
	}
	var resp submitResponse
	ctx, cancel := ctxTimeout(r, 30*time.Second)
	defer cancel()
	if err := s.api.Call(ctx, id.token, "SubmitTask", submit, &resp); err != nil {
		writeAPIError(w, err)
		return
	}
	if p.SessionID == "" && resp.SessionID != "" {
		_ = s.store.UpdateProjects(id.TenantID, func(ps []Project) ([]Project, error) {
			for i := range ps {
				if ps[i].ID == p.ID && ps[i].SessionID == "" {
					ps[i].SessionID = resp.SessionID
				}
			}
			return ps, nil
		})
	}
	record := TaskRecord{ID: resp.TaskID, Message: msg, CreatedAt: time.Now().UTC(), CreatedBy: id.UserID, BaseCommit: base, Status: "TASK_STATUS_QUEUED"}
	if err := s.store.UpdateTasks(id.TenantID, p.ID, func(ts []TaskRecord) []TaskRecord {
		if slices.ContainsFunc(ts, func(t TaskRecord) bool { return t.ID == record.ID }) {
			return ts
		}
		return append(ts, record)
	}); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, record)
}

// task 取出请求路径里的任务，确认它属于这个租户的这个项目。
func (s *Server) task(w http.ResponseWriter, r *http.Request, id identity) (Project, TaskRecord, bool) {
	p, ok := s.project(w, r, id)
	if !ok {
		return Project{}, TaskRecord{}, false
	}
	tasks, err := s.store.Tasks(id.TenantID, p.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return Project{}, TaskRecord{}, false
	}
	i := slices.IndexFunc(tasks, func(t TaskRecord) bool { return t.ID == r.PathValue("task") })
	if i < 0 {
		writeError(w, http.StatusNotFound, "任务不存在")
		return Project{}, TaskRecord{}, false
	}
	return p, tasks[i], true
}

func (s *Server) handleQueryTask(w http.ResponseWriter, r *http.Request, id identity) {
	p, t, ok := s.task(w, r, id)
	if !ok {
		return
	}
	var q map[string]any
	ctx, cancel := ctxTimeout(r, 15*time.Second)
	defer cancel()
	if err := s.api.Call(ctx, id.token, "QueryTask", map[string]string{"taskId": t.ID}, &q); err != nil {
		writeAPIError(w, err)
		return
	}
	if st, _ := q["status"].(string); st != "" && st != t.Status {
		_ = s.store.UpdateTasks(id.TenantID, p.ID, func(ts []TaskRecord) []TaskRecord {
			for i := range ts {
				if ts[i].ID == t.ID {
					ts[i].Status = st
				}
			}
			return ts
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"task": t, "runtime": q})
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
		writeError(w, http.StatusBadGateway, "连不上 task-service")
		return
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(res.Body, 4096))
		writeError(w, res.StatusCode, strings.TrimSpace(string(body)))
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
		writeError(w, http.StatusBadRequest, "要给出 question_id 与 option")
		return
	}
	ctx, cancel := ctxTimeout(r, 15*time.Second)
	defer cancel()
	signal := map[string]any{"taskId": t.ID, "humanReply": map[string]string{"questionId": req.QuestionID, "option": req.Option, "guidance": req.Guidance}}
	if err := s.api.Call(ctx, id.token, "SignalTask", signal, nil); err != nil {
		writeAPIError(w, err)
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
	if err := s.api.Call(ctx, id.token, "CancelTask", map[string]string{"taskId": t.ID, "reason": "用户在网页上取消"}, &resp); err != nil {
		writeAPIError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func writeAPIError(w http.ResponseWriter, err error) {
	var apiErr *APIError
	if errors.As(err, &apiErr) {
		status := apiErr.Status
		if status < 400 || status > 599 {
			status = http.StatusBadGateway
		}
		writeError(w, status, apiErr.Message)
		return
	}
	writeError(w, http.StatusBadGateway, err.Error())
}
