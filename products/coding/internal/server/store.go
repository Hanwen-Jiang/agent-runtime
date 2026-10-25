package server

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"time"
)

// Project 是一个工作区：租户下的一个基线 Git 仓库（<工作区根>/<租户>/<ID>）。它下面可以开很多会话（Session），
// 每个会话从基线仓库复制一份自己的工作副本，互不干扰，可以同时跑。
type Project struct {
	ID        string    `json:"id"`
	Name      string    `json:"name"`
	Source    string    `json:"source"`
	CreatedAt time.Time `json:"created_at"`
	CreatedBy string    `json:"created_by"`
	// GitHub 上的仓库（owner/repo）；只有这样的工作区能推送、开 PR。
	GitHubRepo string `json:"github_repo,omitempty"`
	// 克隆时的分支（PR 的目标分支）。
	BaseBranch string `json:"base_branch,omitempty"`

	// 以下是会话出现之前的字段（一个项目一段对话、直接在基线仓库里改）。读到时迁移成这个工作区的 main 会话，之后不再写。
	SessionID  string `json:"session_id,omitempty"`
	Branch     string `json:"branch,omitempty"`
	PRURL      string `json:"pr_url,omitempty"`
	PRNumber   int    `json:"pr_number,omitempty"`
	PushedHead string `json:"pushed_head,omitempty"`
}

// Session 是工作区里的一个会话：一段对话（Runtime 的一个 Session）加一份独立的工作副本。
type Session struct {
	ID        string    `json:"id"`
	ProjectID string    `json:"project_id"`
	Title     string    `json:"title"`
	CreatedAt time.Time `json:"created_at"`
	CreatedBy string    `json:"created_by"`
	// UpdatedAt 是最近一次提交任务的时间（侧栏按它排序）。
	UpdatedAt time.Time `json:"updated_at"`
	// WorkspaceID 是 Runtime 的 workspace id，也是工作副本的目录名（<工作区根>/<租户>/<WorkspaceID>）；
	// 新会话是 <项目>--<会话>，迁移来的 main 会话就是项目自己的目录。
	WorkspaceID      string `json:"workspace_id"`
	RuntimeSessionID string `json:"runtime_session_id,omitempty"`
	// 复制时基线仓库的提交。
	BaseCommit string `json:"base_commit,omitempty"`
	// Agent 的改动推到 Branch（agent/…），PR 记在 PRURL、PRNumber；PushedHead 是上次推送的提交。
	Branch     string `json:"branch,omitempty"`
	PRURL      string `json:"pr_url,omitempty"`
	PRNumber   int    `json:"pr_number,omitempty"`
	PushedHead string `json:"pushed_head,omitempty"`
}

// TaskRecord 是在项目里下过的一个任务（状态以 Runtime 为准，这里只缓存最后一次看到的）。
type TaskRecord struct {
	ID         string    `json:"id"`
	Message    string    `json:"message"`
	CreatedAt  time.Time `json:"created_at"`
	CreatedBy  string    `json:"created_by"`
	BaseCommit string    `json:"base_commit,omitempty"`
	Status     string    `json:"status"`
	// 提交时选的沙箱等级（effect 上限：read 或 write_reversible）与模型倾向（economy、balanced、best）。
	Sandbox string `json:"sandbox,omitempty"`
	Model   string `json:"model,omitempty"`
	// 从 Task State 读到的花费（各 Round 的 cost_usd 之和）与轮数；Usage 是 QueryTask 的 usage（Runtime 填了才有）。
	CostUSD float64         `json:"cost_usd,omitempty"`
	Rounds  int             `json:"rounds,omitempty"`
	Usage   json.RawMessage `json:"usage,omitempty"`
	// Settled 表示任务已结束、上面的计量已经取过，之后不再查 Runtime。
	Settled bool `json:"settled,omitempty"`
}

func terminal(status string) bool {
	switch status {
	case "TASK_STATUS_DONE", "TASK_STATUS_ABORTED", "TASK_STATUS_CANCELLED", "TASK_STATUS_FAILED":
		return true
	}
	return false
}

// Store 把元数据存成 JSON 文件：<dir>/<租户>/projects.json、sessions.json 与 tasks-<会话的 WorkspaceID>.json。MVP 用，单进程。
type Store struct {
	dir string
	mu  sync.Mutex
}

func NewStore(dir string) *Store { return &Store{dir: dir} }

func (s *Store) projectsFile(tenant string) string {
	return filepath.Join(s.dir, tenant, "projects.json")
}

func (s *Store) sessionsFile(tenant string) string {
	return filepath.Join(s.dir, tenant, "sessions.json")
}

func (s *Store) tasksFile(tenant, workspaceID string) string {
	return filepath.Join(s.dir, tenant, "tasks-"+workspaceID+".json")
}

func (s *Store) Sessions(tenant string) ([]Session, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return readJSONFile[[]Session](s.sessionsFile(tenant))
}

// UpdateSessions 在锁里读出、修改、写回会话列表。
func (s *Store) UpdateSessions(tenant string, fn func([]Session) ([]Session, error)) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	sessions, err := readJSONFile[[]Session](s.sessionsFile(tenant))
	if err != nil {
		return err
	}
	sessions, err = fn(sessions)
	if err != nil {
		return err
	}
	return writeFileAtomic(s.sessionsFile(tenant), sessions)
}

// HasTasks 判断某个工作副本有没有任务记录（迁移旧项目时用）。
func (s *Store) HasTasks(tenant, workspaceID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := os.Stat(s.tasksFile(tenant, workspaceID))
	return err == nil
}

// DeleteSession 删掉一个会话的元数据与任务记录（工作副本由调用方删）。
func (s *Store) DeleteSession(tenant string, sess Session) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	sessions, err := readJSONFile[[]Session](s.sessionsFile(tenant))
	if err != nil {
		return err
	}
	sessions = slices.DeleteFunc(sessions, func(x Session) bool { return x.ProjectID == sess.ProjectID && x.ID == sess.ID })
	if err := writeFileAtomic(s.sessionsFile(tenant), sessions); err != nil {
		return err
	}
	if err := os.Remove(s.tasksFile(tenant, sess.WorkspaceID)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

func readJSONFile[T any](path string) (T, error) {
	var v T
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return v, nil
	}
	if err != nil {
		return v, err
	}
	return v, json.Unmarshal(data, &v)
}

func writeFileAtomic(path string, v any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".tmp-*")
	if err != nil {
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		_ = os.Remove(tmp.Name())
		return err
	}
	if err := tmp.Close(); err != nil {
		_ = os.Remove(tmp.Name())
		return err
	}
	// Windows 上别的进程正读着目标文件时 rename 会失败，重试几次。
	for attempt := 0; ; attempt++ {
		err = os.Rename(tmp.Name(), path)
		if err == nil || attempt >= 20 {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if err != nil {
		_ = os.Remove(tmp.Name())
	}
	return err
}

func (s *Store) Projects(tenant string) ([]Project, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return readJSONFile[[]Project](s.projectsFile(tenant))
}

func (s *Store) Project(tenant, id string) (Project, bool, error) {
	projects, err := s.Projects(tenant)
	if err != nil {
		return Project{}, false, err
	}
	for _, p := range projects {
		if p.ID == id {
			return p, true, nil
		}
	}
	return Project{}, false, nil
}

// UpdateProjects 在锁里读出、修改、写回项目列表。
func (s *Store) UpdateProjects(tenant string, fn func([]Project) ([]Project, error)) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	projects, err := readJSONFile[[]Project](s.projectsFile(tenant))
	if err != nil {
		return err
	}
	projects, err = fn(projects)
	if err != nil {
		return err
	}
	return writeFileAtomic(s.projectsFile(tenant), projects)
}

// Tasks 是一个会话（按它的 WorkspaceID）的任务列表。
func (s *Store) Tasks(tenant, workspaceID string) ([]TaskRecord, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return readJSONFile[[]TaskRecord](s.tasksFile(tenant, workspaceID))
}

// UpdateTasks 在锁里读出、修改、写回一个会话的任务列表。
func (s *Store) UpdateTasks(tenant, workspaceID string, fn func([]TaskRecord) []TaskRecord) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	tasks, err := readJSONFile[[]TaskRecord](s.tasksFile(tenant, workspaceID))
	if err != nil {
		return err
	}
	return writeFileAtomic(s.tasksFile(tenant, workspaceID), fn(tasks))
}

// DeleteProject 删掉工作区与它所有会话的元数据、任务记录（目录由调用方删）。
func (s *Store) DeleteProject(tenant, id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	projects, err := readJSONFile[[]Project](s.projectsFile(tenant))
	if err != nil {
		return err
	}
	projects = slices.DeleteFunc(projects, func(p Project) bool { return p.ID == id })
	if err := writeFileAtomic(s.projectsFile(tenant), projects); err != nil {
		return err
	}
	sessions, err := readJSONFile[[]Session](s.sessionsFile(tenant))
	if err != nil {
		return err
	}
	files := []string{s.tasksFile(tenant, id)}
	for _, x := range sessions {
		if x.ProjectID == id {
			files = append(files, s.tasksFile(tenant, x.WorkspaceID))
		}
	}
	if err := writeFileAtomic(s.sessionsFile(tenant), slices.DeleteFunc(sessions, func(x Session) bool { return x.ProjectID == id })); err != nil {
		return err
	}
	for _, f := range files {
		if err := os.Remove(f); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	return nil
}
