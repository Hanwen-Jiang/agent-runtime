// Package broker 是 Sandbox Service 的借还（D-11、D-12、D-13；docs/plan/sandbox-platform.md）。SBX-01 从 Worker 的
// agentruntime_worker.sandbox.broker 搬来，行为不变：
//
//   - 选后端：登记里停用的后端不接新环境（首选停用时 denied sandbox_backend_disabled，回退里停用的跳过）；首选暂时不可用（transient）
//     时按回退列表依次试，回退的后端不能比 min_isolation（没写时取首选的）更弱；denied、invalid 原样报出，不回退。绑定了工作区时
//     只在同一组后端之间回退（挂同一个卷的一组，存档模式的一组）。落点与回退原因记进环境表。
//   - 存档模式（D-06）：后端不挂工作区卷，建环境后从最近的快照恢复（不一致时换一个环境再试一次），可写的环境销毁之前先存档；
//     存档暂时失败时留着环境、由下一轮对账再试，永久性的失败照样销毁。每个工作区同一时间只有一个可写的环境，不分会话。
//   - 配额与预算：建环境前在环境表的事务里核对（W10）。
//   - 会话级复用（W5）：reuse 时先按（租户，工作区，会话）在环境表里找没到期的 ready 环境，找不到才建；任何服务副本都一样。
//   - 一个工作区同一时间只有一个可写的环境：别的会话占着、但那边没有命令在跑时，回收它再建；正在跑就 denied（workspace_busy）。
//   - 冷热分层（DISK-B）：挂卷模式下工作区是 cold 的，先清空本地目录、建好环境后从最近的快照恢复。
//   - 环境的短期凭据（D-15、D-16）与测试用密钥：只在服务里，Exec 时补进这条命令的 env（平台的优先）。
//   - 环境配置（ENV-02）：要执行业务命令的借用（Prepare）读工作区选用的环境配置：新建的环境先跑 setup、再跑 start；setup 失败或
//     超时这个环境不启动（记进环境表、销毁，报 environment_setup_failed / environment_setup_timeout），start 失败只记录。
//   - 联网授权（D-16）：托管后端没有网关，Task 有授权时环境建成按域名放行（allowlist，主机是各授权的并集），授权变了就换环境；
//     授权带用量上限时只能落在经网关的后端。
//
// Lease 的 id 就是环境表的 env_ref：服务重启之后按环境表恢复（Get），调用方照常用。
package broker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/archive"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
)

// BuildBackend 是构建项目环境时在环境表里记的“后端”（不在后端登记里；D-17）。
const BuildBackend = "env-builder"

// Prepared 是这次借用时准备环境的结果（ENV-02）。
type Prepared struct {
	EnvironmentID, Setup, Start string
}

// Lease 是一次借用。Env 里有凭据，只在服务里用。
type Lease struct {
	Backend, Kind, Isolation     string
	Box                          sandbox.Sandbox
	EnvID                        string
	Record                       *ledger.Record
	Reused                       bool
	FallbackFrom, FallbackReason string
	Env                          map[string]string
	Network                      string
	TestEnvNames                 []string
	Owner, WorkspaceID           string
	Archive, Writable            bool
	Restored                     string
	Prepared                     *Prepared
	// EnvironmentJSON 是项目环境的解析结果（D-17），给 shell_run 的结果。
	EnvironmentJSON string
}

// ID 是 Lease 的 id（环境表的 env_ref）。
func (l *Lease) ID() string {
	if l.Record == nil {
		return ""
	}
	return l.Record.EnvRef
}

// SetupCache 是 setup 结果的镜像缓存（D-17 的环境构建；没配时为 nil）。
type SetupCache interface {
	// Cached 是命中的（缓存键，镜像）；没有、还在构建时 ok 为 false。
	Cached(ctx context.Context, owner, backend, script string) (key, image string, ok bool)
	// BaseDigest 是 setup 缓存键里的基础镜像 digest；后端用不了缓存时为空。
	BaseDigest(backend string) string
	// Start 在 setup 成功之后按缓存键起一次构建（只缓存成功的结果）；起不了不影响这次。
	Start(ctx context.Context, owner, workspaceID, environmentID, script, key string)
}

// ProjectEnvironments 是按项目定制的运行环境（D-17）：shell_run 第一次用到工作区时解析定义，镜像就绪就用它。
type ProjectEnvironments interface {
	// Resolve 读工作区里的定义，返回要用的镜像（没有或没就绪时为空）与给 shell_run 的结果（JSON）。
	Resolve(ctx context.Context, tenant *commonv1.TenantContext, backend string, read func(path string) ([]byte, error)) (image, resultJSON string, err error)
}

// Options 是构造参数。
type Options struct {
	Registry     *registry.Registry
	Policies     *registry.Policies
	Ledger       *ledger.Ledger
	Archiver     *archive.Archiver
	Store        relational.Provider
	Credentials  *Credentials
	SetupCache   SetupCache
	Projects     ProjectEnvironments
	HoldMax      time.Duration
	SetupTimeout time.Duration
	StartTimeout time.Duration
	// EgressCA 读出网网关 CA 的证书（有网络密钥时交给沙箱信任；SecretStore 平台作用域的 sandbox/egress-ca-cert）。
	EgressCA func(ctx context.Context) []byte
	Logger   *slog.Logger
}

// Broker 是借还。
type Broker struct {
	reg      *registry.Registry
	policies *registry.Policies
	ledger   *ledger.Ledger
	archiver *archive.Archiver
	store    relational.Provider
	creds    *Credentials
	opts     Options
	log      *slog.Logger

	mu       sync.Mutex
	leases   map[string]*Lease
	cold     ColdOptions
	lastCool time.Time
	// 同一个借用上的命令一条一条执行（execLock）；被取消、沙箱又没确认停下的命令记在 notStopped 里，直到还掉这个借用：
	// 之后的命令（Worker 还原检查点）报 conflict（exec_not_stopped），不在可能还在跑的命令旁边改文件。
	execLocks  map[string]*execLock
	notStopped map[string]bool
}

type execLock struct {
	mu   sync.Mutex
	refs int
}

// New 构造。
func New(o Options) *Broker {
	if o.SetupTimeout == 0 {
		o.SetupTimeout = 10 * time.Minute
	}
	if o.StartTimeout == 0 {
		o.StartTimeout = 60 * time.Second
	}
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	return &Broker{reg: o.Registry, policies: o.Policies, ledger: o.Ledger, archiver: o.Archiver, store: o.Store, creds: o.Credentials, opts: o,
		log: o.Logger, leases: map[string]*Lease{}, execLocks: map[string]*execLock{}, notStopped: map[string]bool{}}
}

// Registry、Ledger、Archiver、Policies 给服务的其余部分用。
func (b *Broker) Registry() *registry.Registry { return b.reg }
func (b *Broker) Ledger() *ledger.Ledger       { return b.ledger }
func (b *Broker) Archiver() *archive.Archiver  { return b.archiver }
func (b *Broker) Policies() *registry.Policies { return b.policies }

// ArchiveModeFor 判断这个租户的工作区是不是存档模式（按首选后端；回退只在同一组里）。
func (b *Broker) ArchiveModeFor(ctx context.Context, tenantID string) (bool, error) {
	p, err := b.policies.For(ctx, tenantID)
	if err != nil {
		return false, err
	}
	return b.reg.Archive(p.Backend), nil
}

// HasGateway 判断后端经不经出网网关。
func (b *Broker) HasGateway(name string) bool {
	spec, err := b.reg.Spec(name)
	return err == nil && b.creds.EgressFor(spec) != ""
}

// AcquireArgs 是 Acquire 的参数。
type AcquireArgs struct {
	Spec     sandbox.Spec
	TaskID   string
	Writable bool
	Reuse    bool
	BusyFor  *time.Duration
	Prepare  bool
	Cooling  bool
	// Project：按项目定制运行环境（D-17）。
	Project bool
	// Flush：存档模式下先把这个工作区别人闲着的可写环境各存一份快照（不销毁），有命令在跑时 workspace_busy。
	Flush bool
}

func defaults(spec sandbox.Spec) sandbox.Spec {
	if spec.Runtime == "" {
		spec.Runtime = "python"
	}
	if spec.CPUMillis <= 0 {
		spec.CPUMillis = 1000
	}
	if spec.MemoryMB <= 0 {
		spec.MemoryMB = 512
	}
	if spec.IdleTimeout <= 0 {
		spec.IdleTimeout = 10 * time.Minute
	}
	if spec.Network == "" {
		spec.Network = "none"
	}
	return spec
}

// SameImage 判断两个镜像引用是不是同一个（都按 digest 写时比 digest）。
func SameImage(a, b string) bool {
	if i, j := strings.LastIndex(a, "@"), strings.LastIndex(b, "@"); i >= 0 && j >= 0 {
		return a[i+1:] == b[j+1:]
	}
	return a == b
}

// Acquire 借一个环境。
func (b *Broker) Acquire(ctx context.Context, tenant *commonv1.TenantContext, a AcquireArgs) (*Lease, error) {
	owner := tenant.GetTenantId()
	a.Spec = defaults(a.Spec)
	policy, err := b.policies.For(ctx, owner)
	if err != nil {
		return nil, err
	}
	grants, err := b.ledger.GrantsForTask(ctx, owner, a.TaskID)
	if err != nil {
		return nil, err
	}
	var config *EnvironmentConfig
	if a.Prepare {
		if config, err = b.Environment(ctx, owner, a.Spec.WorkspaceID); err != nil {
			return nil, err
		}
	}
	envJSON := ""
	if a.Project && b.opts.Projects != nil && a.Spec.Image == "" && a.Spec.WorkspaceID != "" {
		image, result, err := b.opts.Projects.Resolve(ctx, tenant, policy.Backend, b.workspaceReader(ctx, tenant, a.Spec.WorkspaceID))
		if err != nil {
			return nil, err
		}
		a.Spec.Image, envJSON = image, result
	}
	if a.Flush && a.Spec.WorkspaceID != "" && b.reg.Archive(policy.Backend) {
		if err := b.flushHolders(ctx, owner, a.Spec.WorkspaceID); err != nil {
			return nil, err
		}
	}
	session := tenant.GetSessionId()
	if a.Reuse && session != "" {
		lease, err := b.reuse(ctx, tenant, a, policy, grants, config)
		if err != nil || lease != nil {
			if lease != nil {
				lease.EnvironmentJSON = envJSON
			}
			return lease, err
		}
	}
	cachedKey := ""
	if config != nil && config.SetupScript != "" && a.Spec.Image == "" && b.opts.SetupCache != nil {
		if key, image, ok := b.opts.SetupCache.Cached(ctx, owner, policy.Backend, config.SetupScript); ok {
			cachedKey, a.Spec.Image = key, image
		}
	}
	pendingEnv, pendingSetup := "", "none"
	if config != nil {
		pendingEnv = config.EnvironmentID
		if config.SetupScript != "" {
			pendingSetup = "running"
		}
	}
	projectImage := envJSON != "" && a.Spec.Image != "" && cachedKey == ""
	lease, err := b.create(ctx, tenant, owner, a, policy, grants, pendingEnv, pendingSetup)
	if err != nil {
		var pe *providererr.Error
		if (cachedKey == "" && !projectImage) || !errors.As(err, &pe) || pe.Code != "unsupported_image" {
			return nil, err
		}
		// 回退到的后端用不了缓存的镜像：这一次照常跑 setup。项目环境的镜像落不到这个后端（例如回退到了托管后端）：退回 universal，
		// 并在结果里说明（D-17）。
		if projectImage {
			envJSON = fallbackEnvironment(envJSON, pe.Message+"，这次用 universal")
		}
		cachedKey, a.Spec.Image = "", ""
		if lease, err = b.create(ctx, tenant, owner, a, policy, grants, pendingEnv, pendingSetup); err != nil {
			return nil, err
		}
	}
	if err := b.withEnv(ctx, lease, policy, config); err != nil {
		return nil, err
	}
	if config != nil {
		if err := b.prepare(ctx, lease, config, cachedKey); err != nil {
			return nil, err
		}
	}
	lease.EnvironmentJSON = envJSON
	b.remember(lease)
	return lease, nil
}

func (b *Broker) reuse(ctx context.Context, tenant *commonv1.TenantContext, a AcquireArgs, policy registry.Policy, grants []ledger.Grant, config *EnvironmentConfig) (*Lease, error) {
	owner, ws := tenant.GetTenantId(), a.Spec.WorkspaceID
	preferWritable := !a.Writable && ws != "" && b.reg.Archive(policy.Backend)
	wants := []bool{a.Writable && ws != ""}
	if preferWritable {
		// 存档模式下读的也先找本会话可写的环境：只读的环境是在快照上建的，看不到本会话刚写的内容。
		wants = []bool{true, false}
	}
	var found *ledger.Record
	for _, want := range wants {
		var err error
		if found, err = b.ledger.FindReusable(ctx, owner, ws, tenant.GetSessionId(), policy.Names(), want); err != nil {
			return nil, err
		}
		if found != nil {
			break
		}
	}
	if found == nil {
		return nil, nil
	}
	if !b.fitsGrants(found.Backend, *found, grants) || (a.Spec.Image != "" && !SameImage(a.Spec.Image, found.Image)) {
		// 托管后端的域名白名单是建环境时定的：授权变了，这个环境不能再用，闲着就换掉；项目环境的镜像构建好了，也换掉。
		if b.ledger.IdleNow(*found) {
			if _, err := b.DestroyRecord(ctx, *found, "network grants or environment image changed", true, false); err != nil {
				return nil, err
			}
		}
		return nil, nil
	}
	provider, err := b.reg.Provider(ctx, found.Backend)
	if err != nil {
		return nil, err
	}
	box, err := provider.Bind(&commonv1.TenantContext{TenantId: owner, WorkspaceId: found.WorkspaceID})
	if err != nil {
		return nil, err
	}
	var healthy *bool
	if probe, ok := box.(sandbox.Health); ok {
		h := probe.Healthy(ctx, found.BackendEnvID)
		healthy = &h
	} else {
		f := false
		healthy = &f
	}
	rec, err := b.ledger.Touch(ctx, *found, ledger.TouchArgs{TaskID: a.TaskID, BusyFor: a.BusyFor, HealthConfirmed: healthy, Reused: true})
	if err != nil {
		return nil, err
	}
	lease := &Lease{Backend: rec.Backend, Kind: b.reg.Kind(rec.Backend), Isolation: b.reg.Isolation(rec.Backend), Box: box, EnvID: rec.BackendEnvID,
		Record: &rec, Reused: true, Owner: owner, WorkspaceID: rec.WorkspaceID, Archive: rec.WorkspaceID != "" && b.reg.Archive(rec.Backend), Writable: rec.Writable}
	if err := b.withEnv(ctx, lease, policy, config); err != nil {
		return nil, err
	}
	if config != nil && !prepared(lease.Record, config) {
		if err := b.prepare(ctx, lease, config, ""); err != nil {
			return nil, err
		}
	}
	b.remember(lease)
	return lease, nil
}

func prepared(rec *ledger.Record, config *EnvironmentConfig) bool {
	return rec != nil && rec.EnvironmentID == config.EnvironmentID && rec.SetupFinishedAt != "" &&
		(rec.SetupState == "none" || rec.SetupState == "succeeded" || rec.SetupState == "cached")
}

func (b *Broker) allowHosts(name string, grants []ledger.Grant) string {
	if len(grants) == 0 || b.HasGateway(name) {
		return ""
	}
	raw, _ := json.Marshal(ledger.UnionHosts(grants))
	return string(raw)
}

func (b *Broker) fitsGrants(name string, rec ledger.Record, grants []ledger.Grant) bool {
	for _, g := range grants {
		if g.Audited() && !b.HasGateway(name) {
			return false
		}
	}
	return rec.AllowHosts == b.allowHosts(name, grants)
}

func (b *Broker) withEnv(ctx context.Context, lease *Lease, policy registry.Policy, config *EnvironmentConfig) error {
	lease.Network = "none"
	if b.HasGateway(lease.Backend) {
		lease.Network = "gateway"
	} else if lease.Record != nil && lease.Record.AllowHosts != "" {
		lease.Network = "allowlist"
	}
	lease.Env = map[string]string{}
	if lease.Record != nil {
		spec, _ := b.reg.Spec(lease.Backend)
		env, err := b.creds.ExecEnv(ctx, *lease.Record, spec, policy.Quota)
		if err != nil {
			return err
		}
		secrets, err := b.creds.TestEnv(ctx, lease.Record.Owner)
		if err != nil {
			return err
		}
		lease.TestEnvNames = nil
		for k, v := range secrets {
			env[k] = v
			lease.TestEnvNames = append(lease.TestEnvNames, k)
		}
		sort.Strings(lease.TestEnvNames)
		lease.Env = env
	}
	if config != nil {
		merged := map[string]string{}
		for k, v := range config.Variables {
			merged[k] = v
		}
		for k, v := range lease.Env {
			merged[k] = v
		}
		if config.SecretHeaders && b.opts.EgressCA != nil && b.HasGateway(lease.Backend) {
			for _, k := range CAEnv {
				merged[k] = CABundle
			}
		}
		lease.Env = merged
	}
	return nil
}

func (b *Broker) prepare(ctx context.Context, lease *Lease, config *EnvironmentConfig, cachedKey string) error {
	if config.SecretHeaders && b.opts.EgressCA != nil && b.HasGateway(lease.Backend) {
		b.installCA(ctx, lease)
	}
	rec := lease.Record
	key, setup := cachedKey, "none"
	if cachedKey != "" {
		setup = "cached"
	} else if config.SetupScript != "" {
		base := ""
		if b.opts.SetupCache != nil {
			base = b.opts.SetupCache.BaseDigest(lease.Backend)
		}
		if rec != nil && rec.Image != "" && base != "" && !SameImage(rec.Image, base) {
			base = rec.Image
		}
		key = SetupKey(config.SetupScript, base)
		res, err := b.script(ctx, lease, config.SetupScript, b.opts.SetupTimeout)
		if err != nil {
			return err
		}
		if res.TimedOut || res.ExitCode == nil || *res.ExitCode != 0 {
			code := "environment_setup_failed"
			why := "失败"
			if res.TimedOut {
				code, why = "environment_setup_timeout", fmt.Sprintf("超时（%d 秒）", int(b.opts.SetupTimeout.Seconds()))
			} else if res.ExitCode != nil {
				why = fmt.Sprintf("失败（退出码 %d）", *res.ExitCode)
			}
			if rec != nil {
				r, _ := b.ledger.MarkSetup(ctx, *rec, map[string]string{"environment_id": config.EnvironmentID, "setup_state": "failed",
					"setup_key": key, "setup_error": code, "setup_finished_at": ledger.ISO(b.ledger.Now())})
				lease.Record = &r
			}
			_ = b.destroy(ctx, lease, false, code)
			tail := append(append([]byte{}, res.Stderr...), res.Stdout...)
			if len(tail) > 2000 {
				tail = tail[len(tail)-2000:]
			}
			return providererr.New(providererr.Invalid, code, fmt.Sprintf("环境 %s 的安装脚本%s，这个环境没有启动、命令没有执行：%s",
				config.EnvironmentID, why, redact(lease, string(tail))))
		}
		setup = "succeeded"
		if b.opts.SetupCache != nil && base != "" && base == b.opts.SetupCache.BaseDigest(lease.Backend) {
			b.opts.SetupCache.Start(ctx, lease.Owner, lease.WorkspaceID, config.EnvironmentID, config.SetupScript, key)
		}
	}
	start := "none"
	if config.StartCommand != "" {
		res, err := b.script(ctx, lease, config.StartCommand, b.opts.StartTimeout)
		start = "failed"
		if err == nil && !res.TimedOut && res.ExitCode != nil && *res.ExitCode == 0 {
			start = "succeeded"
		}
	}
	if rec != nil {
		r, err := b.ledger.MarkSetup(ctx, *lease.Record, map[string]string{"environment_id": config.EnvironmentID, "setup_state": setup,
			"setup_key": key, "setup_error": "", "setup_finished_at": ledger.ISO(b.ledger.Now()), "start_state": start})
		if err != nil {
			return err
		}
		lease.Record = &r
	}
	lease.Prepared = &Prepared{EnvironmentID: config.EnvironmentID, Setup: setup, Start: start}
	return nil
}

func (b *Broker) script(ctx context.Context, lease *Lease, script string, timeout time.Duration) (sandbox.ExecResult, error) {
	return lease.Box.Exec(ctx, lease.EnvID, []string{"bash", "-s"}, sandbox.ExecOptions{Timeout: timeout, Stdin: []byte(script), Env: lease.Env})
}

func (b *Broker) installCA(ctx context.Context, lease *Lease) {
	cert := b.opts.EgressCA(ctx)
	if len(cert) == 0 {
		return
	}
	script := fmt.Sprintf("cat /etc/ssl/certs/ca-certificates.crt 2>/dev/null > %[1]s.tmp || true\ncat >> %[1]s.tmp\nmv %[1]s.tmp %[1]s\n", CABundle)
	_, _ = lease.Box.Exec(ctx, lease.EnvID, []string{"sh", "-c", script}, sandbox.ExecOptions{Timeout: 30 * time.Second, Stdin: cert})
}

// redact 遮掉平台注入的凭据（代理与包镜像的地址里带着短期凭据），setup 的输出进错误信息之前用。
func redact(lease *Lease, text string) string {
	for name, value := range lease.Env {
		if Reserved[strings.ToUpper(name)] && len(value) >= 8 {
			text = strings.ReplaceAll(text, value, "[已遮蔽]")
		}
	}
	return strings.TrimSpace(text)
}

func (b *Broker) create(ctx context.Context, tenant *commonv1.TenantContext, owner string, a AcquireArgs, policy registry.Policy, grants []ledger.Grant,
	pendingEnv, pendingSetup string) (*Lease, error) {
	restoring := false
	if a.Spec.WorkspaceID != "" && !a.Cooling && !b.reg.Archive(policy.Backend) {
		var err error
		if restoring, err = b.claimCold(ctx, owner, a.Spec.WorkspaceID); err != nil {
			return nil, err
		}
	}
	if !restoring {
		return b.createAny(ctx, tenant, owner, a, policy, grants, pendingEnv, pendingSetup)
	}
	// 冷的工作区（DISK-B）：本地目录清空，建好挂卷的环境后从最近的快照恢复；失败时退回 cold，下次再恢复。
	back := func() {
		_, _ = b.archiver.Transition(context.WithoutCancel(ctx), owner, a.Spec.WorkspaceID, "restoring", "cold")
	}
	b.clearLocal(owner, a.Spec.WorkspaceID)
	lease, err := b.createAny(ctx, tenant, owner, a, policy, grants, pendingEnv, pendingSetup)
	if err != nil {
		back()
		return nil, err
	}
	snap, err := b.archiver.Restore(ctx, lease.Box, lease.EnvID, owner, a.Spec.WorkspaceID, policy.Workspace.MaxBytes, b.transferOptions(lease))
	if err != nil {
		_ = b.destroy(ctx, lease, false, "restore of a cold workspace failed")
		back()
		return nil, err
	}
	if _, err := b.archiver.Transition(ctx, owner, a.Spec.WorkspaceID, "restoring", "active"); err != nil {
		return nil, err
	}
	if snap != nil {
		lease.Restored = snap.SnapshotID
	}
	b.log.Info("工作区从存档恢复到本地盘（冷热分层）", "workspace", a.Spec.WorkspaceID)
	return lease, nil
}

func (b *Broker) claimCold(ctx context.Context, owner, workspaceID string) (bool, error) {
	if owner == ledger.PlatformID {
		return false, nil
	}
	w, err := b.archiver.Get(ctx, owner, workspaceID)
	if err != nil || w == nil || w.State == "active" || w.State == "deleted" {
		return false, err
	}
	if w.State == "cooling" {
		return false, providererr.New(providererr.Transient, "workspace_cooling", "工作区正在转冷（存档到对象存储），稍后再试")
	}
	if w.State == "cold" {
		ok, err := b.archiver.Transition(ctx, owner, workspaceID, "cold", "restoring")
		if err != nil || ok {
			return ok, err
		}
	}
	return false, providererr.New(providererr.Transient, "workspace_restoring", "工作区正在从存档恢复到本地盘，稍后再试")
}

// clearLocal 清空挂卷的工作区目录：冷的工作区以快照为准，残留的文件会让恢复后的核对不一致。
func (b *Broker) clearLocal(owner, workspaceID string) {
	root := b.reg.WorkspaceRoot()
	if root == "" || !sandbox.IsSlug(owner) || !sandbox.IsSlug(workspaceID) {
		return
	}
	_ = os.RemoveAll(filepath.Join(root, owner, workspaceID))
}

func (b *Broker) candidates(policy registry.Policy, spec sandbox.Spec, gatewayOnly bool) ([]string, []string) {
	names := policy.Names()
	floor := policy.MinIsolation
	if floor == "" {
		floor = b.reg.Isolation(names[0])
	}
	group := ""
	if spec.WorkspaceID != "" {
		group = b.reg.WorkspaceGroup(names[0])
	}
	var out, skipped []string
	for _, n := range names {
		switch {
		case !b.reg.Enabled(n):
			skipped = append(skipped, n+"（登记里停用了）")
		case registry.IsolationRank[b.reg.Isolation(n)] < registry.IsolationRank[floor]:
			skipped = append(skipped, fmt.Sprintf("%s（隔离强度 %s 低于 %s）", n, b.reg.Isolation(n), floor))
		case spec.WorkspaceID != "" && b.reg.WorkspaceGroup(n) != group:
			skipped = append(skipped, n+"（与首选不共用工作区）")
		case gatewayOnly && !b.HasGateway(n):
			skipped = append(skipped, n+"（不经出网网关，做不到联网授权的用量上限与审计）")
		default:
			out = append(out, n)
		}
	}
	return out, skipped
}

func (b *Broker) createAny(ctx context.Context, tenant *commonv1.TenantContext, owner string, a AcquireArgs, policy registry.Policy, grants []ledger.Grant,
	pendingEnv, pendingSetup string) (*Lease, error) {
	if !b.reg.Enabled(policy.Backend) {
		return nil, providererr.New(providererr.Denied, "sandbox_backend_disabled",
			"沙箱后端 "+policy.Backend+" 在登记里停用了（enabled: false）：不接新环境，已有的环境照常用到回收")
	}
	audited := false
	for _, g := range grants {
		audited = audited || g.Audited()
	}
	names, skipped := b.candidates(policy, a.Spec, audited)
	if len(names) == 0 {
		return nil, providererr.New(providererr.Invalid, "no_sandbox_backend", "租户策略里没有满足隔离强度、工作区与出网审计要求的沙箱后端："+strings.Join(skipped, "；"))
	}
	idle := policy.IdleTimeout
	if !a.Reuse {
		idle = min(a.Spec.IdleTimeout, policy.IdleTimeout)
	}
	var errs []string
	for _, name := range names {
		fallbackFrom := ""
		if name != policy.Backend {
			fallbackFrom = policy.Backend
		}
		reason := ""
		if len(errs) > 0 {
			reason = errs[len(errs)-1]
		}
		allow := b.allowHosts(name, grants)
		archived := a.Spec.WorkspaceID != "" && b.reg.Archive(name)
		if archived {
			if err := b.archiver.Ensure(ctx, owner, a.Spec.WorkspaceID, policy.Workspace); err != nil {
				return nil, err
			}
		}
		attempts := 1
		if archived {
			attempts = 2
		}
		for attempt := 0; attempt < attempts; attempt++ {
			lease, err := b.createOne(ctx, tenant, owner, name, a, policy, idle, fallbackFrom, allow, archived, &errs, pendingEnv, pendingSetup)
			if err != nil {
				return nil, err
			}
			if lease == nil {
				break // 这个后端暂时不可用：试下一个
			}
			lease.FallbackReason = reason
			if !archived {
				return lease, nil
			}
			snap, err := b.archiver.Restore(ctx, lease.Box, lease.EnvID, owner, a.Spec.WorkspaceID, policy.Workspace.MaxBytes, b.transferOptions(lease))
			if err != nil {
				var pe *providererr.Error
				_ = b.destroy(ctx, lease, false, "restore failed")
				if errors.As(err, &pe) && pe.Code == "workspace_restore_mismatch" {
					if attempt == 1 {
						return nil, providererr.New(providererr.Invalid, "workspace_restore_mismatch", "工作区的快照恢复了两次都与清单不一致，不在半个工作区上执行")
					}
					b.log.Warn("工作区恢复后与清单不一致，换一个环境再试", "workspace", a.Spec.WorkspaceID)
					continue
				}
				return nil, err
			}
			if snap != nil {
				lease.Restored = snap.SnapshotID
			}
			return lease, nil
		}
	}
	hint := ""
	var weaker, disabled []string
	for _, s := range skipped {
		if strings.Contains(s, "隔离强度") {
			weaker = append(weaker, s)
		}
		if strings.Contains(s, "停用") {
			disabled = append(disabled, s)
		}
	}
	if len(weaker) > 0 {
		hint += "；回退列表里更弱的后端不用：" + strings.Join(weaker, "、")
	}
	if len(disabled) > 0 {
		hint += "；回退列表里停用的后端不接新环境：" + strings.Join(disabled, "、")
	}
	return nil, providererr.New(providererr.Transient, "sandbox_unavailable", "沙箱后端都暂时不可用（"+strings.Join(errs, "、")+"）"+hint)
}

func (b *Broker) createOne(ctx context.Context, tenant *commonv1.TenantContext, owner, name string, a AcquireArgs, policy registry.Policy,
	idle time.Duration, fallbackFrom, allow string, archived bool, errs *[]string, pendingEnv, pendingSetup string) (*Lease, error) {
	rec, err := b.begin(ctx, tenant, owner, name, a, policy, idle, fallbackFrom, allow, archived, pendingEnv, pendingSetup)
	if err != nil {
		return nil, err
	}
	provider, err := b.reg.Provider(ctx, name)
	if err != nil {
		_, _ = b.ledger.MarkFailed(ctx, rec, errText(err))
		return nil, err
	}
	box, err := provider.Bind(&commonv1.TenantContext{TenantId: owner, UserId: tenant.GetUserId(), WorkspaceId: tenant.GetWorkspaceId(),
		SessionId: tenant.GetSessionId(), Trace: tenant.GetTrace()})
	if err != nil {
		_, _ = b.ledger.MarkFailed(ctx, rec, errText(err))
		return nil, err
	}
	spec := a.Spec
	spec.IdleTimeout = max(spec.IdleTimeout, idle)
	if a.Reuse {
		spec.IdleTimeout += b.opts.HoldMax
	}
	spec.SessionID = tenant.GetSessionId()
	if !archived && spec.WorkspaceID != "" && b.reg.WorkspaceRoot() != "" {
		// 挂卷：先以服务的身份建好 <工作区根>/<租户>/<workspace>（K8s 按 subPath 挂，目录不在时 kubelet 以 root 建，沙箱就写不进去）。
		_ = os.MkdirAll(filepath.Join(b.reg.WorkspaceRoot(), owner, spec.WorkspaceID), 0o755)
	}
	if archived {
		spec.WorkspaceID = "" // 不挂卷：建好后从快照恢复
	}
	if allow != "" {
		spec.Network = "allowlist"
		_ = json.Unmarshal([]byte(allow), &spec.AllowHosts)
	}
	env, err := box.Create(ctx, spec)
	if err != nil {
		var pe *providererr.Error
		if errors.As(err, &pe) && pe.Category == providererr.Transient {
			*errs = append(*errs, name+"："+pe.Code)
			_, _ = b.ledger.MarkFailed(ctx, rec, pe.Code+": "+pe.Message)
			b.log.Warn("沙箱后端暂时不可用，试下一个", "backend", name, "code", pe.Code)
			return nil, nil
		}
		_, _ = b.ledger.MarkFailed(ctx, rec, errText(err))
		return nil, err
	}
	if rec, err = b.ledger.MarkReady(ctx, rec, env.ID, env.Image, env.HealthConfirmed); err != nil {
		return nil, err
	}
	if a.BusyFor != nil {
		if rec, err = b.ledger.Touch(ctx, rec, ledger.TouchArgs{TaskID: a.TaskID, BusyFor: a.BusyFor}); err != nil {
			return nil, err
		}
	}
	return &Lease{Backend: name, Kind: b.reg.Kind(name), Isolation: b.reg.Isolation(name), Box: box, EnvID: env.ID, Record: &rec,
		FallbackFrom: fallbackFrom, Owner: owner, WorkspaceID: a.Spec.WorkspaceID, Archive: archived, Writable: a.Writable && a.Spec.WorkspaceID != ""}, nil
}

func errText(err error) string {
	var pe *providererr.Error
	if errors.As(err, &pe) {
		return pe.Code + ": " + pe.Message
	}
	return err.Error()
}

func (b *Broker) begin(ctx context.Context, tenant *commonv1.TenantContext, owner, name string, a AcquireArgs, policy registry.Policy,
	idle time.Duration, fallbackFrom, allow string, exclusive bool, pendingEnv, pendingSetup string) (ledger.Record, error) {
	session := ""
	if a.Reuse {
		session = tenant.GetSessionId()
	}
	image := a.Spec.Image
	if image == "" {
		image = b.reg.ImageFor(name, a.Spec.Runtime)
	}
	quota := policy.Quota
	args := ledger.CreateArgs{Backend: name, Kind: b.reg.Kind(name), Isolation: b.reg.Isolation(name), WorkspaceID: a.Spec.WorkspaceID,
		SessionID: session, TaskID: a.TaskID, Writable: a.Writable && a.Spec.WorkspaceID != "", Runtime: a.Spec.Runtime, Image: image, Idle: idle,
		MaxLifetime: policy.MaxLifetime, Quota: &quota, FallbackFrom: fallbackFrom, AllowHosts: allow, Exclusive: exclusive && a.Writable,
		EnvironmentID: pendingEnv, SetupState: pendingSetup}
	rec, err := b.ledger.BeginCreate(ctx, owner, args)
	var pe *providererr.Error
	if err == nil || !errors.As(err, &pe) || pe.Code != "workspace_busy" {
		return rec, err
	}
	// 别的会话（存档模式下不分会话）占着这个工作区的可写环境：那边闲着就回收（存档模式先存档），再建一次；正在跑命令就报给调用方。
	var only *string
	if !args.Exclusive {
		only = &args.SessionID
	}
	holders, err := b.ledger.WritableHolders(ctx, owner, a.Spec.WorkspaceID, only)
	if err != nil {
		return ledger.Record{}, err
	}
	for _, h := range holders {
		if !b.ledger.IdleNow(h) {
			return ledger.Record{}, providererr.New(providererr.Denied, "workspace_busy", "这个工作区正被另一个可写的环境用着（命令还在跑），稍后再试")
		}
	}
	why := "preempted: 另一个会话要写同一个工作区"
	if args.Exclusive {
		why = "preempted: 另一个环境要写同一个工作区（存档模式）"
	}
	for _, h := range holders {
		if _, err := b.DestroyRecord(ctx, h, why, true, false); err != nil {
			return ledger.Record{}, err
		}
	}
	return b.ledger.BeginCreate(ctx, owner, args)
}

// --- 按 lease_id 取回 ---

func (b *Broker) remember(lease *Lease) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.leases[lease.ID()] = lease
}

func (b *Broker) forget(id string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.leases, id)
	delete(b.notStopped, id)
}

// Get 按 lease_id（env_ref）取回借用：先看进程内的，没有时按环境表恢复（服务重启之后）。别的租户、已结束的：not_found。
func (b *Broker) Get(ctx context.Context, tenant *commonv1.TenantContext, id string) (*Lease, error) {
	owner := tenant.GetTenantId()
	b.mu.Lock()
	lease, ok := b.leases[id]
	b.mu.Unlock()
	if ok && lease.Owner == owner {
		return lease, nil
	}
	rec, err := b.ledger.Get(ctx, owner, id)
	if err != nil {
		return nil, err
	}
	if rec == nil || rec.State != "ready" || rec.BackendEnvID == "" || !b.reg.Has(rec.Backend) {
		return nil, providererr.New(providererr.NotFound, "unknown_lease", "借用不存在或已经结束")
	}
	provider, err := b.reg.Provider(ctx, rec.Backend)
	if err != nil {
		return nil, err
	}
	box, err := provider.Bind(&commonv1.TenantContext{TenantId: owner, WorkspaceId: rec.WorkspaceID})
	if err != nil {
		return nil, err
	}
	policy, err := b.policies.For(ctx, owner)
	if err != nil {
		return nil, err
	}
	lease = &Lease{Backend: rec.Backend, Kind: b.reg.Kind(rec.Backend), Isolation: b.reg.Isolation(rec.Backend), Box: box, EnvID: rec.BackendEnvID,
		Record: rec, Owner: owner, WorkspaceID: rec.WorkspaceID, Archive: rec.WorkspaceID != "" && b.reg.Archive(rec.Backend), Writable: rec.Writable,
		FallbackFrom: rec.FallbackFrom}
	var config *EnvironmentConfig
	if rec.EnvironmentID != "" {
		if config, err = b.Environment(ctx, owner, rec.WorkspaceID); err != nil {
			return nil, err
		}
	}
	if err := b.withEnv(ctx, lease, policy, config); err != nil {
		return nil, err
	}
	b.remember(lease)
	return lease, nil
}

// Exec 在借到的环境里执行一条命令；extra 是调用方的变量，与平台注入的同名时报 invalid。
func (b *Broker) Exec(ctx context.Context, lease *Lease, argv []string, timeout time.Duration, stdin []byte, extra map[string]string, offloadInline ...int64) (sandbox.ExecResult, error) {
	env := map[string]string{}
	for k, v := range extra {
		upper := strings.ToUpper(k)
		if Reserved[upper] || strings.HasPrefix(upper, "AGENT_") {
			return sandbox.ExecResult{}, providererr.New(providererr.Invalid, "env_reserved_name", "变量 "+k+" 是平台保留的")
		}
		env[k] = v
	}
	for k, v := range lease.Env {
		env[k] = v
	}
	unlock := b.lockExec(lease.ID())
	defer unlock()
	if b.stillRunning(lease.ID()) {
		return sandbox.ExecResult{}, providererr.New(providererr.Conflict, "exec_not_stopped",
			"上一条被取消的命令沙箱没确认停下，可能还在跑：这个环境先不执行别的命令，还掉之后再借")
	}
	var inline int64
	if len(offloadInline) > 0 {
		inline = offloadInline[0]
	}
	res, err := lease.Box.Exec(ctx, lease.EnvID, argv, sandbox.ExecOptions{Timeout: timeout, Stdin: stdin, Env: env, OffloadInlineBytes: inline})
	if errors.Is(err, sandbox.ErrExecNotStopped) {
		b.mu.Lock()
		b.notStopped[lease.ID()] = true
		b.mu.Unlock()
		b.log.Warn("取消命令之后沙箱没确认它停了", "env_ref", lease.ID(), "backend", lease.Backend)
	}
	return res, err
}

// lockExec 拿到这个借用的执行锁；返回的函数放掉它。
func (b *Broker) lockExec(id string) func() {
	b.mu.Lock()
	l := b.execLocks[id]
	if l == nil {
		l = &execLock{}
		b.execLocks[id] = l
	}
	l.refs++
	b.mu.Unlock()
	l.mu.Lock()
	return func() {
		l.mu.Unlock()
		b.mu.Lock()
		l.refs--
		if l.refs == 0 {
			delete(b.execLocks, id)
		}
		b.mu.Unlock()
	}
}

func (b *Broker) stillRunning(id string) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.notStopped[id]
}

// --- 还 ---

// flushHolders 把这个工作区别人闲着的可写环境各存一份快照（不销毁它们）；有命令在跑时 workspace_busy（原来 Worker 的 _current）。
func (b *Broker) flushHolders(ctx context.Context, owner, workspaceID string) error {
	holders, err := b.ledger.WritableHolders(ctx, owner, workspaceID, nil)
	if err != nil {
		return err
	}
	for _, h := range holders {
		if !b.ledger.IdleNow(h) {
			return providererr.New(providererr.Denied, "workspace_busy", "这个工作区正被一个可写的环境用着（命令还在跑），稍后再试")
		}
	}
	busy := 10 * time.Minute
	for _, h := range holders {
		if h.State != "ready" || h.BackendEnvID == "" {
			continue
		}
		// 存的时候占着它，别的命令不在半途改它。
		rec, err := b.ledger.Touch(ctx, h, ledger.TouchArgs{BusyFor: &busy})
		if err != nil {
			return err
		}
		lease, err := b.Get(ctx, &commonv1.TenantContext{TenantId: owner, WorkspaceId: workspaceID}, rec.EnvRef)
		if err == nil {
			_, err = b.Snapshot(ctx, lease)
		}
		if providererr.CategoryOf(err) == providererr.NotFound {
			b.forget(rec.EnvRef)
			if _, err := b.ledger.MarkLost(ctx, rec, "存档时后端里已经没有这个环境"); err != nil {
				return err
			}
			continue
		}
		if _, terr := b.ledger.Touch(context.WithoutCancel(ctx), rec, ledger.TouchArgs{}); terr != nil && err == nil {
			err = terr
		}
		if err != nil {
			return err
		}
	}
	return nil
}

// Release 用完：会话级的环境留着（顺延空闲、记 CPU 时间），一次性的销毁（存档模式的可写环境先存档，skipSnapshot 时不存）。存档暂时
// 失败时环境留着、报错，由下一轮对账或下一个要写的环境再存；永久性的失败照样销毁、报错。
func (b *Broker) Release(ctx context.Context, lease *Lease, keep bool, cpuMS int64, skipSnapshot ...bool) error {
	b.mu.Lock()
	delete(b.notStopped, lease.ID())
	b.mu.Unlock()
	if lease.Record != nil {
		rec, err := b.ledger.Touch(ctx, *lease.Record, ledger.TouchArgs{CPUMS: cpuMS})
		if err != nil {
			return err
		}
		lease.Record = &rec
	}
	if keep {
		return nil
	}
	if lease.Archive && lease.Writable && !(len(skipSnapshot) > 0 && skipSnapshot[0]) {
		if _, err := b.Snapshot(ctx, lease); err != nil {
			if providererr.CategoryOf(err) == providererr.Invalid {
				var pe *providererr.Error
				errors.As(err, &pe)
				_ = b.destroy(ctx, lease, false, "存档失败："+pe.Code)
			}
			return err
		}
	}
	return b.destroy(ctx, lease, false, "")
}

// Snapshot 把借到的环境里的工作区存一份快照（存档模式；挂卷的后端一样能导出）。
func (b *Broker) Snapshot(ctx context.Context, lease *Lease) (archive.Snapshot, error) {
	if lease.WorkspaceID == "" {
		return archive.Snapshot{}, providererr.New(providererr.Invalid, "workspace_archive_unavailable", "环境没有绑定工作区")
	}
	policy, err := b.policies.For(ctx, lease.Owner)
	if err != nil {
		return archive.Snapshot{}, err
	}
	if err := b.archiver.Ensure(ctx, lease.Owner, lease.WorkspaceID, policy.Workspace); err != nil {
		return archive.Snapshot{}, err
	}
	return b.archiver.Snapshot(ctx, lease.Box, lease.EnvID, lease.Owner, lease.WorkspaceID, policy.Workspace.MaxBytes, lease.ID(), b.transferOptions(lease))
}

// Discard：后端里已经没有这个环境（被回收、节点重启）：记为丢失，尽量删掉残留。
func (b *Broker) Discard(ctx context.Context, lease *Lease, reason string) error {
	if lease.Record != nil {
		rec, err := b.ledger.MarkLost(ctx, *lease.Record, reason)
		if err != nil {
			return err
		}
		lease.Record = &rec
	}
	_ = lease.Box.Destroy(ctx, lease.EnvID)
	b.forget(lease.ID())
	return nil
}

func (b *Broker) destroy(ctx context.Context, lease *Lease, snapshot bool, reason string) error {
	if snapshot && lease.Archive && lease.Writable {
		if _, err := b.Snapshot(ctx, lease); err != nil {
			return err
		}
	}
	b.forget(lease.ID())
	if lease.Record == nil {
		_ = lease.Box.Destroy(ctx, lease.EnvID)
		return nil
	}
	rec, err := b.ledger.BeginDestroy(ctx, *lease.Record, reason)
	if err != nil {
		return err
	}
	lease.Record = &rec
	if err := lease.Box.Destroy(ctx, lease.EnvID); err != nil && providererr.CategoryOf(err) != providererr.NotFound {
		// 留在 destroying，由对账再删。
		b.log.Warn("销毁沙箱环境失败，交给对账", "env_ref", rec.EnvRef, "error", errText(err))
		return nil
	}
	perHour, perCPU := b.reg.Prices(rec.Backend)
	rec, err = b.ledger.MarkDestroyed(ctx, rec, perHour, perCPU)
	lease.Record = &rec
	return err
}

func (b *Broker) archived(rec ledger.Record) bool {
	return rec.WorkspaceID != "" && rec.Writable && rec.State == "ready" && rec.BackendEnvID != "" && b.reg.Has(rec.Backend) && b.reg.Archive(rec.Backend)
}

// DestroyRecord 按环境表的一行销毁（对账、回收、抢占用）；后端里已经没了也算销毁完成。存档模式的可写环境先存档：存档暂时失败时
// 报错、环境留着（force 时照样销毁，例如活满）；永久性的失败照样销毁，原因记进环境表。
func (b *Broker) DestroyRecord(ctx context.Context, rec ledger.Record, reason string, snapshot, force bool) (ledger.Record, error) {
	b.forget(rec.EnvRef)
	if snapshot && b.archived(rec) {
		provider, err := b.reg.Provider(ctx, rec.Backend)
		if err != nil {
			return rec, err
		}
		box, err := provider.Bind(&commonv1.TenantContext{TenantId: rec.Owner, WorkspaceId: rec.WorkspaceID})
		if err != nil {
			return rec, err
		}
		lease := &Lease{Backend: rec.Backend, Box: box, EnvID: rec.BackendEnvID, Record: &rec, Owner: rec.Owner, WorkspaceID: rec.WorkspaceID, Archive: true, Writable: true}
		if _, err := b.Snapshot(ctx, lease); err != nil {
			if providererr.CategoryOf(err) == providererr.NotFound {
				return b.ledger.MarkLost(ctx, rec, "存档时后端里已经没有这个环境")
			}
			if !force && providererr.CategoryOf(err) != providererr.Invalid {
				return rec, err
			}
			var pe *providererr.Error
			code := "internal"
			if errors.As(err, &pe) {
				code = pe.Code
			}
			b.log.Warn("工作区销毁前存档失败，照样销毁，这个环境里的改动丢了", "workspace", rec.WorkspaceID, "code", code)
			reason += "; 存档失败：" + code
		}
	}
	if rec.Backend == BuildBackend {
		// 构建记的环境：Job 由构建器自己删，这里只收尾记录与授权。
		if rec.TaskID != "" {
			if err := b.ledger.RevokeTask(ctx, rec.Owner, rec.TaskID); err != nil {
				return rec, err
			}
		}
		rec, err := b.ledger.BeginDestroy(ctx, rec, reason)
		if err != nil {
			return rec, err
		}
		return b.ledger.MarkDestroyed(ctx, rec, 0, 0)
	}
	provider, err := b.reg.Provider(ctx, rec.Backend)
	if err != nil {
		return rec, err
	}
	var box sandbox.Sandbox
	if rec.Owner == ledger.PlatformID {
		box, err = provider.BindPlatform("sandbox-reconcile")
	} else {
		box, err = provider.Bind(&commonv1.TenantContext{TenantId: rec.Owner, WorkspaceId: rec.WorkspaceID})
	}
	if err != nil {
		return rec, err
	}
	if rec, err = b.ledger.BeginDestroy(ctx, rec, reason); err != nil {
		return rec, err
	}
	if rec.BackendEnvID != "" {
		if err := box.Destroy(ctx, rec.BackendEnvID); err != nil && providererr.CategoryOf(err) != providererr.NotFound {
			return rec, err
		}
	}
	perHour, perCPU := b.reg.Prices(rec.Backend)
	return b.ledger.MarkDestroyed(ctx, rec, perHour, perCPU)
}

// --- 批量 ---

// DestroySession 是 Session 关闭时销毁它的环境（W5）；返回销毁了几个。
func (b *Broker) DestroySession(ctx context.Context, tenantID, workspaceID, sessionID string) (int, error) {
	recs, err := b.ledger.Active(ctx, tenantID)
	if err != nil {
		return 0, err
	}
	n := 0
	for _, r := range recs {
		if r.SessionID == sessionID && r.WorkspaceID == workspaceID && r.State == "ready" {
			if _, err := b.DestroyRecord(ctx, r, "session closed", true, false); err != nil {
				return n, err
			}
			n++
		}
	}
	return n, nil
}

// DestroyWorkspace 是删除工作区时销毁它的全部环境（不存档，正在跑命令的也销毁）。
func (b *Broker) DestroyWorkspace(ctx context.Context, tenantID, workspaceID string) (int, error) {
	recs, err := b.ledger.Active(ctx, tenantID)
	if err != nil {
		return 0, err
	}
	n := 0
	for _, r := range recs {
		if r.WorkspaceID != workspaceID {
			continue
		}
		if r.State == "creating" {
			if _, err := b.ledger.MarkFailed(ctx, r, "workspace deleted"); err != nil {
				return n, err
			}
			continue
		}
		if _, err := b.DestroyRecord(ctx, r, "workspace deleted", false, false); err != nil {
			return n, err
		}
		n++
	}
	// 挂卷时一并删掉卷上的目录（Worker 不一定挂着工作区卷）。
	if policy, err := b.policies.For(ctx, tenantID); err == nil && !b.reg.Archive(policy.Backend) {
		b.clearLocal(tenantID, workspaceID)
	}
	return n, nil
}

// DestroyTenant 是租户删除时销毁它的全部环境、收回授权，工作区与它的存档一并删除（W9）。
func (b *Broker) DestroyTenant(ctx context.Context, tenantID string) (int, error) {
	if err := b.ledger.RevokeAll(ctx, tenantID); err != nil {
		return 0, err
	}
	recs, err := b.ledger.Active(ctx, tenantID)
	if err != nil {
		return 0, err
	}
	n := 0
	for _, r := range recs {
		if r.State == "creating" {
			if _, err := b.ledger.MarkFailed(ctx, r, "tenant deleted"); err != nil {
				return n, err
			}
			continue
		}
		if _, err := b.DestroyRecord(ctx, r, "tenant deleted", false, false); err != nil {
			return n, err
		}
		n++
	}
	workspaces, err := b.archiver.List(ctx, tenantID)
	if err != nil {
		return n, err
	}
	for _, w := range workspaces {
		if err := b.archiver.Delete(ctx, tenantID, w.WorkspaceID); err != nil {
			return n, err
		}
	}
	return n, nil
}

// workspaceReader 读工作区里的一个文件（D-17 的定义检测用）：挂卷时直接读卷，存档模式读最近的快照。
func (b *Broker) workspaceReader(ctx context.Context, tenant *commonv1.TenantContext, workspaceID string) func(path string) ([]byte, error) {
	return func(path string) ([]byte, error) {
		data, _, err := b.ReadWorkspaceFile(ctx, tenant, workspaceID, path, 1<<20)
		if providererr.CategoryOf(err) == providererr.NotFound {
			return nil, nil
		}
		return data, err
	}
}

// fallbackEnvironment 把项目环境的解析结果改成 fallback（镜像落不到这次的后端，用了 universal），并写上原因。
func fallbackEnvironment(envJSON, note string) string {
	var doc map[string]any
	if json.Unmarshal([]byte(envJSON), &doc) != nil || doc == nil {
		doc = map[string]any{}
	}
	doc["state"], doc["note"] = "fallback", note
	data, _ := json.Marshal(doc)
	return string(data)
}
