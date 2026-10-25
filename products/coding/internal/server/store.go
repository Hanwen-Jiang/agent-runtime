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

// Project 是一个项目：租户下的一个 Git 工作区，对应 Runtime 的 workspace；同一项目的任务共用一个 Session。
type Project struct {
	ID        string    `json:"id"`
	Name      string    `json:"name"`
	Source    string    `json:"source"`
	CreatedAt time.Time `json:"created_at"`
	CreatedBy string    `json:"created_by"`
	SessionID string    `json:"session_id,omitempty"`
	// GitHub 上的仓库（owner/repo）；只有这样的项目能推送、开 PR。
	GitHubRepo string `json:"github_repo,omitempty"`
	// 克隆时的分支（PR 的目标分支）；Agent 的改动推到 Branch（agent/…），PR 记在 PRURL、PRNumber。
	BaseBranch string `json:"base_branch,omitempty"`
	Branch     string `json:"branch,omitempty"`
	PRURL      string `json:"pr_url,omitempty"`
	PRNumber   int    `json:"pr_number,omitempty"`
	// 上次推送到 Branch 的提交；没有新提交也没有未提交的改动时不再推。
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

// Store 把元数据存成 JSON 文件：<dir>/<租户>/projects.json 与 <dir>/<租户>/tasks-<项目>.json。MVP 用，单进程。
type Store struct {
	dir string
	mu  sync.Mutex
}

func NewStore(dir string) *Store { return &Store{dir: dir} }

func (s *Store) projectsFile(tenant string) string {
	return filepath.Join(s.dir, tenant, "projects.json")
}

func (s *Store) tasksFile(tenant, project string) string {
	return filepath.Join(s.dir, tenant, "tasks-"+project+".json")
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

func (s *Store) Tasks(tenant, project string) ([]TaskRecord, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return readJSONFile[[]TaskRecord](s.tasksFile(tenant, project))
}

// UpdateTasks 在锁里读出、修改、写回一个项目的任务列表。
func (s *Store) UpdateTasks(tenant, project string, fn func([]TaskRecord) []TaskRecord) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	tasks, err := readJSONFile[[]TaskRecord](s.tasksFile(tenant, project))
	if err != nil {
		return err
	}
	return writeFileAtomic(s.tasksFile(tenant, project), fn(tasks))
}

// DeleteProject 删掉项目的元数据（工作区由调用方删）。
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
	if err := os.Remove(s.tasksFile(tenant, id)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}
