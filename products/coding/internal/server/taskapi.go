package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// TaskAPI 调 task-service 的 Task API（connect 的 HTTP + JSON：POST /agentruntime.taskapi.v1.TaskService/<方法>）。
// 令牌原样转给 task-service，租户由它按令牌确定。
type TaskAPI struct {
	base   string
	client *http.Client
	stream *http.Client
}

func NewTaskAPI(base string) *TaskAPI {
	return &TaskAPI{base: base, client: &http.Client{Timeout: 30 * time.Second}, stream: &http.Client{}}
}

// APIError 是 Task API 返回的错误：Message 是 task-service 给的说明（原样）；连不上这类我们这边的原因用 Note，按请求的语言写。
type APIError struct {
	Status  int
	Code    string `json:"code"`
	Message string `json:"message"`
	Note    *Msg   `json:"-"`
}

func (e *APIError) Error() string {
	return fmt.Sprintf("Task API %d %s：%s", e.Status, e.Code, e.Message)
}

func (a *TaskAPI) Call(ctx context.Context, token, method string, req, resp any) error {
	body, err := json.Marshal(req)
	if err != nil {
		return err
	}
	r, err := http.NewRequestWithContext(ctx, http.MethodPost, a.base+"/agentruntime.taskapi.v1.TaskService/"+method, bytes.NewReader(body))
	if err != nil {
		return err
	}
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set("Authorization", "Bearer "+token)
	res, err := a.client.Do(r)
	if err != nil {
		note := msg("连不上 task-service")
		return &APIError{Status: http.StatusBadGateway, Code: "unavailable", Message: note.Error(), Note: note}
	}
	defer res.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(res.Body, 8<<20))
	if res.StatusCode != http.StatusOK {
		e := &APIError{Status: res.StatusCode}
		_ = json.Unmarshal(data, e)
		return e
	}
	if resp == nil {
		return nil
	}
	return json.Unmarshal(data, resp)
}

// Events 打开一个 Task 的 SSE 流；调用方负责关闭 Body。
func (a *TaskAPI) Events(ctx context.Context, token, taskID, lastEventID string) (*http.Response, error) {
	r, err := http.NewRequestWithContext(ctx, http.MethodGet, a.base+"/v1/tasks/"+taskID+"/events", nil)
	if err != nil {
		return nil, err
	}
	r.Header.Set("Authorization", "Bearer "+token)
	r.Header.Set("Accept", "text/event-stream")
	if lastEventID != "" {
		r.Header.Set("Last-Event-ID", lastEventID)
	}
	return a.stream.Do(r)
}

type submitResponse struct {
	TaskID       string `json:"taskId"`
	SessionID    string `json:"sessionId"`
	EventsURL    string `json:"eventsUrl"`
	Deduplicated bool   `json:"deduplicated"`
}

type queryResponse struct {
	TaskID      string          `json:"taskId"`
	SessionID   string          `json:"sessionId"`
	Status      string          `json:"status"`
	RoundSeq    int             `json:"roundSeq"`
	LastEventID string          `json:"lastEventId"`
	TaskState   json.RawMessage `json:"taskState,omitempty"`
	Usage       json.RawMessage `json:"usage,omitempty"`
}

// Healthy 看 task-service 的 /healthz 通不通（右栏“环境”用）。
func (a *TaskAPI) Healthy(ctx context.Context) bool {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	r, err := http.NewRequestWithContext(ctx, http.MethodGet, a.base+"/healthz", nil)
	if err != nil {
		return false
	}
	res, err := a.client.Do(r)
	if err != nil {
		return false
	}
	_ = res.Body.Close()
	return res.StatusCode == http.StatusOK
}
