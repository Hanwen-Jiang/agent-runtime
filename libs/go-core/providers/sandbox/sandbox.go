// Package sandbox 是 Sandbox Provider 的接口（设计书 4.10）：创建环境（资源上限、网络策略）、执行命令、读写文件、销毁。
//
// Go 侧只有 Sandbox Service（services/go-sandbox-service，SBX-01）用它；Worker 与 task-service 都经 Sandbox Service 用沙箱，
// 不直接持有后端。语义与原来 Python 的 agentruntime_core.providers.sandbox 一致：
//
//   - 环境只属于创建它的作用域；别的作用域访问：not_found。销毁后访问：not_found。Destroy 重复调用不报错。
//   - argv[0] 为 Spec.Runtime 时由实现映射到环境内的解释器，例如 runtime = python 时 ["python", "-c", "..."]。
//   - Exec 超时不报错：TimedOut 为 true，没有退出码。代码自身的非零退出码照常返回。
//   - Exec 的 Env 是这一条命令额外的环境变量（包镜像地址、出网代理与测试用密钥，D-15、D-16），只对这一条有效；
//     名字要是 [A-Za-z_][A-Za-z0-9_]*，不能以 AGENT_ 开头，最多 64 个、共 64 KiB，否则 invalid（CheckExecEnv）。
//   - 文件路径相对于环境的工作目录；绝对路径、含 .. 段或反斜杠：invalid（CheckPath）。读不存在的文件：not_found。
//   - Spec.WorkspaceID 非空时，环境的工作目录就是本租户的这个工作区（挂卷，D-05）；不挂卷的后端靠 Archive 存档与恢复（D-06），
//     这时 Sandbox Service 建环境时 WorkspaceID 置空。
package sandbox

import (
	"context"
	"io"
	"regexp"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Spec 是建环境的参数。
type Spec struct {
	Runtime     string
	CPUMillis   int
	MemoryMB    int
	Network     string // none、allowlist
	AllowHosts  []string
	IdleTimeout time.Duration
	WorkspaceID string
	// SessionID 只用来归属与复用（Sandbox Service 按它在环境表里找），后端不解释它。
	SessionID string
	// Image 是显式的环境镜像（D-17）；后端只接受部署登记的环境镜像仓库下的引用，不支持的后端 invalid（unsupported_image）。
	Image string
}

// Env 是建好的环境。
type Env struct {
	ID   string
	Spec Spec
	// Image 是环境实际用的镜像（后端量得出时按 digest）；量不出时为空。
	Image string
	// HealthConfirmed 只在建环境时做过专门的健康检查才为 true。
	HealthConfirmed bool
}

// ExecOptions 是一次执行的参数。
type ExecOptions struct {
	Timeout time.Duration
	Stdin   []byte
	Env     map[string]string
}

// ExecResult 是一次执行的结果。
type ExecResult struct {
	// ExitCode 超时时为 nil。
	ExitCode  *int
	Stdout    []byte
	Stderr    []byte
	TimedOut  bool
	Truncated bool
	// CPUTime 量不出时为 -1。
	CPUTime time.Duration
}

// Sandbox 是绑定了作用域的客户端。
type Sandbox interface {
	Create(ctx context.Context, spec Spec) (Env, error)
	Exec(ctx context.Context, envID string, argv []string, opts ExecOptions) (ExecResult, error)
	WriteFile(ctx context.Context, envID, path string, data []byte) error
	ReadFile(ctx context.Context, envID, path string) ([]byte, error)
	Destroy(ctx context.Context, envID string) error
}

// Provider 是长期存在的工厂。
type Provider interface {
	Bind(tenant *commonv1.TenantContext) (Sandbox, error)
	BindPlatform(purpose string) (Sandbox, error)
}

// ListedEnvironment 是后端里实际在跑的一个环境（对账用，D-11）。Owner 是所属作用域（租户 ID 或 _platform）。
type ListedEnvironment struct {
	ID        string
	Owner     string
	CreatedAt time.Time // 量不出时为零值
	State     string
}

// Lister 是能列出本部署在跑的环境的后端（平台级，只列本部署建的）。
type Lister interface {
	ListEnvironments(ctx context.Context) ([]ListedEnvironment, error)
}

// Closer 是持有连接或子进程、退出时要收尾的 Provider。
type Closer interface {
	Close() error
}

// FileEntry 是工作目录里的一项（存档的清单，D-06）：Path 相对工作目录、/ 分隔；Type 是 file、dir、symlink；
// Mode 只留权限位（符号链接为 0）；文件有 Size 与 SHA256（十六进制），符号链接有 Link。
type FileEntry struct {
	Path   string `json:"path"`
	Type   string `json:"type"`
	Mode   int    `json:"mode"`
	Size   int64  `json:"size,omitempty"`
	SHA256 string `json:"sha256,omitempty"`
	Link   string `json:"link,omitempty"`
}

// DirEntry 是列目录的一项。
type DirEntry struct {
	Name string `json:"name"`
	Type string `json:"type"`
	Size int64  `json:"size"`
}

// GrepMatch 是一条匹配。
type GrepMatch struct {
	Path string `json:"path"`
	Line int    `json:"line"`
	Text string `json:"text"`
}

// Archive 是能整体导出、导入工作目录的后端（存档模式，D-06）。语义同 sandbox-agent 的 /v1/archive：
// 导出只含普通文件、目录与不出工作目录的符号链接；导入要求工作目录是空的，越界的项一律 invalid；超过 maxBytes：invalid（too_large）。
type Archive interface {
	ExportArchive(ctx context.Context, envID string, out io.Writer, maxBytes int64) (int64, error)
	ImportArchive(ctx context.Context, envID string, src io.Reader, size, maxBytes int64) error
	Manifest(ctx context.Context, envID string) ([]FileEntry, error)
}

// Browse 是能在环境里列目录、按行搜索的后端（D-06）。规则同 sandbox-agent：列目录最多 1000 项、按名字排序；
// 搜索跳过 .git、二进制与超过 1 MiB 的文件，每行最多 500 个字符，最多 maxMatches 条（上限 1000）。
type Browse interface {
	ListDir(ctx context.Context, envID, path string) ([]DirEntry, error)
	Grep(ctx context.Context, envID, pattern, path string, maxMatches int) ([]GrepMatch, bool, error)
}

// Preview 是能给环境的一个端口开预览地址的后端（D-16）：返回出网网关的预览入口转发到的 host:port。
type Preview interface {
	PreviewAddress(ctx context.Context, envID string, port int) (string, error)
}

// Health 是专门的健康探测，只在真正复借时调用。
type Health interface {
	Healthy(ctx context.Context, envID string) bool
}

var (
	envName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	drive   = regexp.MustCompile(`^[A-Za-z]:`)
)

// MaxExecEnv 与 MaxExecEnvBytes 是 Exec 的 Env 的上限。
const (
	MaxExecEnv      = 64
	MaxExecEnvBytes = 64 * 1024
)

// CheckExecEnv 核对 Exec 的 Env（各实现共用，与 sandbox-agent 一致）。
func CheckExecEnv(env map[string]string) error {
	if len(env) == 0 {
		return nil
	}
	total := 0
	for k, v := range env {
		total += len(k) + len(v)
		if !envName.MatchString(k) || strings.HasPrefix(strings.ToUpper(k), "AGENT_") || strings.ContainsRune(v, 0) {
			return providererr.New(providererr.Invalid, "invalid_env", "env 的名字 "+k+" 不合法")
		}
	}
	if len(env) > MaxExecEnv || total > MaxExecEnvBytes {
		return providererr.New(providererr.Invalid, "invalid_env", "env 太多或太大")
	}
	return nil
}

// CheckPath 是与 agent、开发实现相同的路径规则：工作目录内的相对路径，/ 分隔，不含空段、. 与 ..。
func CheckPath(path string) error {
	if path == "" || strings.ContainsAny(path, "\\\x00") || strings.HasPrefix(path, "/") || drive.MatchString(path) {
		return providererr.New(providererr.Invalid, "invalid_path", "路径必须是工作目录内的相对路径")
	}
	for _, part := range strings.Split(path, "/") {
		if part == "" || part == "." || part == ".." {
			return providererr.New(providererr.Invalid, "invalid_path", "路径不得含空段、. 或 ..")
		}
	}
	return nil
}

// CheckDir 同 CheckPath，但空串与 "." 表示工作目录本身。
func CheckDir(path string) error {
	if path == "" || path == "." {
		return nil
	}
	return CheckPath(path)
}

var slug = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

// CheckWorkspaceID 核对工作区 ID（格式同租户 ID）。
func CheckWorkspaceID(id string) error {
	if !slug.MatchString(id) {
		return providererr.New(providererr.Invalid, "invalid_workspace", "workspace_id 的格式不对")
	}
	return nil
}

// IsSlug 是租户、工作区 ID 的格式。
func IsSlug(s string) bool { return slug.MatchString(s) }
