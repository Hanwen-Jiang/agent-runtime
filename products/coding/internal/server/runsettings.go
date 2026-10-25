package server

import (
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"slices"
	"strings"
	"sync"
	"time"
)

// 沙箱等级就是 Task 的 effect 上限（docs/plan/coding-agent-mvp.md 第 7.3 节）：
// read 只读代码、回答问题（shell_run 是 write_reversible，跑不了）；write_reversible 能改工作区、在沙箱里跑命令。
// Task API 没有逐个任务的上限，上限来自租户策略：提交前把开发租户策略文件里这个租户的上限改成所选的等级，
// task-service 在提交那一刻把它固定进 Task。完全访问（联网、不可逆的外部写）不开放。
const defaultSandbox = "write_reversible"

var sandboxLevels = []string{"read", "write_reversible"}

// 模型倾向放进 inputs.constraints.model_preference，进 Task State 的约束，Planner 选 Tier 时参考。
const defaultModel = "balanced"

var modelPreferences = []string{"economy", "balanced", "best"}

func pickOption(value string, allowed []string, fallback string) (string, bool) {
	value = strings.TrimSpace(value)
	if value == "" {
		return fallback, true
	}
	return value, slices.Contains(allowed, value)
}

// lockTenant 取这个租户的锁，返回解锁函数。
func (s *Server) lockTenant(tenant string) func() {
	mu, _ := s.tenantLocks.LoadOrStore(tenant, &sync.Mutex{})
	m := mu.(*sync.Mutex)
	m.Lock()
	return m.Unlock
}

// withQuery 用 QueryTask 的结果更新任务记录：状态、各 Round 花费之和、轮数与 usage；结束了就标为 Settled。
func withQuery(t TaskRecord, q queryResponse) TaskRecord {
	t.Status = q.Status
	var st struct {
		Value struct {
			Rounds []struct {
				CostUSD float64 `json:"cost_usd"`
			} `json:"rounds"`
		} `json:"value"`
	}
	if len(q.TaskState) > 0 && json.Unmarshal(q.TaskState, &st) == nil && len(st.Value.Rounds) > 0 {
		cost := 0.0
		for _, r := range st.Value.Rounds {
			cost += r.CostUSD
		}
		t.CostUSD = math.Round(cost*1e6) / 1e6
		t.Rounds = len(st.Value.Rounds)
	}
	if usage := strings.TrimSpace(string(q.Usage)); usage != "" && usage != "null" && usage != "{}" {
		t.Usage = q.Usage
	}
	t.Settled = terminal(q.Status)
	return t
}

func sameRecord(a, b TaskRecord) bool {
	return a.Status == b.Status && a.CostUSD == b.CostUSD && a.Rounds == b.Rounds && a.Settled == b.Settled && string(a.Usage) == string(b.Usage)
}

// handleEnv 是右栏“环境”：Runtime 是否连得上、沙箱、工作区的分支与 HEAD、项目来源与 Session。
func (s *Server) handleEnv(w http.ResponseWriter, r *http.Request, id identity) {
	p, ok := s.project(w, r, id)
	if !ok {
		return
	}
	dir := s.workspace(id.TenantID, p.ID)
	ctx, cancel := ctxTimeout(r, 10*time.Second)
	defer cancel()
	subject, _ := s.git.Run(ctx, dir, "log", "-1", "--format=%s")
	status, _ := s.git.Run(ctx, dir, "status", "--porcelain")
	dirty := 0
	for _, line := range strings.Split(status, "\n") {
		if strings.TrimSpace(line) != "" {
			dirty++
		}
	}
	sandbox := map[string]any{"mode": s.cfg.Sandbox}
	if s.cfg.Sandbox == "docker" {
		sandbox["image"] = s.cfg.SandboxImage
		sandbox["network"] = "none"
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"runtime": map[string]any{"ok": s.api.Healthy(ctx)},
		"sandbox": sandbox,
		"git": map[string]any{
			"branch": s.git.Branch(ctx, dir), "head": s.git.Head(ctx, dir), "subject": strings.TrimSpace(subject),
			"dirty": dirty, "base_branch": p.BaseBranch,
		},
		"project": map[string]any{"source": p.Source, "github_repo": p.GitHubRepo, "session_id": p.SessionID, "created_at": p.CreatedAt},
		// 租户策略里现在的上限；每个任务实际的上限在它的 Task State 里。
		"effect_ceiling": s.tenantCeiling(id.TenantID),
	})
}

type activeTask struct {
	ProjectID   string     `json:"project_id"`
	ProjectName string     `json:"project_name"`
	Task        TaskRecord `json:"task"`
}

// handleActiveTasks 是右栏“后台任务”：这个租户所有项目里还没结束的任务，按提交时间排。
func (s *Server) handleActiveTasks(w http.ResponseWriter, r *http.Request, id identity) {
	projects, err := s.store.Projects(id.TenantID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := []activeTask{}
	for _, p := range projects {
		for _, t := range s.refresh(r, id, p.ID) {
			if !terminal(t.Status) {
				out = append(out, activeTask{ProjectID: p.ID, ProjectName: p.Name, Task: t})
			}
		}
	}
	slices.SortFunc(out, func(a, b activeTask) int { return a.Task.CreatedAt.Compare(b.Task.CreatedAt) })
	writeJSON(w, http.StatusOK, out)
}

// notFound 判断 Task API 的错误是不是“没有这个任务”（例如 Runtime 的数据重建过）：这样的任务不再去查。
func notFound(err error) bool {
	var apiErr *APIError
	return errors.As(err, &apiErr) && apiErr.Status == http.StatusNotFound
}
