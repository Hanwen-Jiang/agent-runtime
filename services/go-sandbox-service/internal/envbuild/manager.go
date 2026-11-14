package envbuild

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/broker"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
)

// Limits 是构建的上限。
type Limits struct {
	// MaxImages 是一个租户同时有的镜像（building 与 ready）；0 不限。
	MaxImages     int
	MaxImageBytes int64
	BuildTimeout  time.Duration
	// FailedRetryAfter：构建失败之后多久可以再试（失败可能只是一次网络问题或放行配置错误，不能一直记着）。
	FailedRetryAfter time.Duration
	IdleDays         int
	// MaxConcurrent 是这个服务进程里同时跑的构建数；排队的照样记心跳。
	MaxConcurrent int
}

// DefaultLimits 是默认的上限。
func DefaultLimits() Limits {
	return Limits{MaxImages: 5, MaxImageBytes: 8 << 30, BuildTimeout: 30 * time.Minute, FailedRetryAfter: time.Hour, IdleDays: 14, MaxConcurrent: 4}
}

// BuildJob 是交给构建器的一次构建。Env 是构建里联网用的变量（出网代理与包镜像，带短期凭据），不进日志。
type BuildJob struct {
	Owner, Key, Dockerfile string
	// Context 是相对工作区根的目录（""：不用仓库里的文件）；WorkspaceID 是哪个工作区。
	Context, WorkspaceID string
	Timeout              time.Duration
	MaxBytes             int64
	Env                  map[string]string
}

// Outcome 是构建的结果。Failure 是 timed_out、too_large、failed：退回 universal 时告诉用户的分类。
type Outcome struct {
	OK              bool
	Reason, Failure string
}

// Builder 构建；成功时把 OCI 打包写进 out（不超过 job.MaxBytes，超了 too_large）。返回错误表示构建器本身出错。
type Builder interface {
	Build(ctx context.Context, job BuildJob, out io.Writer) (Outcome, error)
}

// Pusher 推、删镜像（RegistryClient 实现）。
type Pusher interface {
	Reference(repo, digest string) string
	Push(ctx context.Context, repo, tag string, l *Layout) (string, error)
	Delete(ctx context.Context, repo, digest string) (bool, error)
}

// Network 是构建联网用的环境记录与授权（broker.BuildNetwork 实现）：Open 给出构建要的代理与镜像源变量，Close 收回。
type Network interface {
	Open(ctx context.Context, owner, key string, timeout time.Duration) (map[string]string, error)
	Close(ctx context.Context, owner, key string) error
}

// Resolution 是 shell_run 这次用哪个镜像：Image 为空时用登记的运行环境（universal）。State 是 none（仓库没有定义）、ready、building、
// failed、unsupported、quota；Note 写给用户看（退回 universal 的原因）。
type Resolution struct {
	State, Image, Kind, Path, Key, Note string
}

// Output 是给 shell_run 结果的那一节。
func (r Resolution) Output() map[string]any {
	out := map[string]any{"state": r.State, "definition": r.Path, "kind": r.Kind}
	if r.Note != "" {
		out["note"] = r.Note
	}
	return out
}

// Config 是构造参数。
type Config struct {
	BaseImage, BaseDigest string
	Registry              RegistryPath
	Builder               Builder
	Pusher                Pusher
	Limits                Limits
	// BuildHosts 是构建联网时放行的主机（镜像仓库与系统包的源）。
	BuildHosts []string
	TmpDir     string
	Logger     *slog.Logger
	// Heartbeat 是构建中的心跳间隔（默认一分钟）；心跳停了三倍这么久的 building 由别人接手。
	Heartbeat time.Duration
	// Start 替换“在进程里起构建”（测试里记下来，再手动 RunBuild）。
	Start func(owner, key string)
}

// Manager 是项目环境的解析、构建与 setup 缓存。实现 broker.ProjectEnvironments 与 broker.SetupCache。
type Manager struct {
	cfg     Config
	ledger  *ledger.Ledger
	Store   *Store
	network Network
	usable  func(backend string) bool
	strict  func(backend string) bool

	ctx    context.Context
	cancel context.CancelFunc
	sem    chan struct{}
	wg     sync.WaitGroup

	mu      sync.Mutex
	running map[string]bool
	lastGC  map[string]time.Time
}

// New 构造；要 Attach 之后才能用。
func New(cfg Config) *Manager {
	d := DefaultLimits()
	if cfg.Limits.MaxImageBytes <= 0 {
		cfg.Limits.MaxImageBytes = d.MaxImageBytes
	}
	if cfg.Limits.BuildTimeout <= 0 {
		cfg.Limits.BuildTimeout = d.BuildTimeout
	}
	if cfg.Limits.FailedRetryAfter <= 0 {
		cfg.Limits.FailedRetryAfter = d.FailedRetryAfter
	}
	if cfg.Limits.IdleDays <= 0 {
		cfg.Limits.IdleDays = d.IdleDays
	}
	if cfg.Limits.MaxConcurrent <= 0 {
		cfg.Limits.MaxConcurrent = d.MaxConcurrent
	}
	if cfg.BaseDigest == "" {
		cfg.BaseDigest = cfg.BaseImage
	}
	if cfg.Heartbeat <= 0 {
		cfg.Heartbeat = time.Minute
	}
	if cfg.Logger == nil {
		cfg.Logger = slog.Default()
	}
	ctx, cancel := context.WithCancel(context.Background())
	return &Manager{cfg: cfg, ctx: ctx, cancel: cancel, sem: make(chan struct{}, cfg.Limits.MaxConcurrent), running: map[string]bool{}, lastGC: map[string]time.Time{}}
}

// Attach 接上服务的借还：环境表、构建联网（BuildNetwork）与“哪些后端能用环境镜像”（K8s，或登记里写了 custom_images: true）。
func (m *Manager) Attach(b *broker.Broker) {
	m.ledger = b.Ledger()
	m.Store = NewStore(m.ledger)
	m.network = broker.NewBuildNetwork(b, m.cfg.BuildHosts)
	reg := b.Registry()
	m.usable = func(name string) bool {
		spec, err := reg.Spec(name)
		if err != nil {
			return false
		}
		custom, _ := spec["custom_images"].(bool)
		return spec.Kind() == "k8s" || custom
	}
	m.strict = func(name string) bool {
		spec, err := reg.Spec(name)
		if err != nil {
			return false
		}
		strict, _ := spec["strict_project_images"].(bool)
		prepare, _ := spec["image_prepare_url"].(string)
		return strict || (spec.Kind() == "firecracker" && prepare != "")
	}
}

// Limits 是生效的上限。
func (m *Manager) Limits() Limits { return m.cfg.Limits }

// Close 停下进程里的构建（留着 building：心跳停了之后由别的副本或重启后的服务接手）。
func (m *Manager) Close() {
	m.cancel()
	done := make(chan struct{})
	go func() { m.wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(30 * time.Second):
	}
}

// From 是生成的 Dockerfile 的 FROM：基础镜像按 digest 固定（去掉标签，带上 digest）。
func (m *Manager) From() string {
	img := m.cfg.BaseImage
	if !strings.HasPrefix(m.cfg.BaseDigest, "sha256:") || strings.Contains(img, "@") {
		return img
	}
	if strings.LastIndex(img, ":") > strings.LastIndex(img, "/") {
		img = img[:strings.LastIndex(img, ":")]
	}
	return img + "@" + m.cfg.BaseDigest
}

const fallbackNote = "，这次先用 universal"

// ResolveDefinition 读工作区里的定义，决定这次用哪个镜像；没就绪时起构建（或接手心跳停了的构建）。
func (m *Manager) ResolveDefinition(ctx context.Context, tenant *commonv1.TenantContext, read Reader) (Resolution, error) {
	owner := tenant.GetTenantId()
	def, err := Detect(read, m.From())
	if err != nil {
		return Resolution{}, err
	}
	if def == nil || owner == "" || owner == ledger.PlatformID {
		return Resolution{State: "none"}, nil
	}
	m.maybeGC(ctx, owner)
	res := Resolution{Kind: def.Kind, Path: def.Path}
	if def.Problem != "" {
		res.State, res.Note = "unsupported", def.Problem+fallbackNote
		return res, nil
	}
	if problems := CheckDockerfile(def.Dockerfile, owner, m.cfg.Registry); len(problems) > 0 {
		res.State, res.Note = "unsupported", "环境定义没通过检查："+strings.Join(problems, "；")+fallbackNote
		return res, nil
	}
	key := def.Key(m.cfg.BaseDigest)
	res.Key = key
	rec, err := m.Store.Get(ctx, owner, key)
	if err != nil {
		return Resolution{}, err
	}
	if rec != nil && rec.State == "ready" && rec.Image != "" {
		if err := m.Store.Touch(ctx, owner, key); err != nil {
			return Resolution{}, err
		}
		res.State, res.Image = "ready", rec.Image
		return res, nil
	}
	if rec != nil && rec.State == "building" {
		m.adopt(ctx, owner, key)
		res.State, res.Note = "building", "项目环境还在构建"+fallbackNote
		return res, nil
	}
	retry := rec != nil && rec.State == "failed" && m.retryDue(*rec)
	if rec != nil && rec.State == "failed" && !retry {
		res.State, res.Note = "failed", "项目环境构建失败："+rec.Reason+fallbackNote+"；过一段时间会重试，改了环境定义会马上重新构建"
		return res, nil
	}
	if m.cfg.Builder == nil || m.cfg.Pusher == nil {
		res.State, res.Note = "unsupported", "这个部署没有配置环境构建器"+fallbackNote
		return res, nil
	}
	_, created, err := m.Store.BeginBuild(ctx, owner, *def, key, tenant.GetWorkspaceId(), m.From(), m.cfg.Limits.MaxImages, retry)
	if err != nil {
		var pe *providererr.Error
		if errors.As(err, &pe) && pe.Code == "environment_quota_exceeded" {
			res.State, res.Note = "quota", pe.Message+fallbackNote
			return res, nil
		}
		return Resolution{}, err
	}
	if created {
		m.start(owner, key)
	}
	res.State, res.Note = "building", "第一次用到这个环境定义，正在构建"+fallbackNote
	return res, nil
}

// Resolve 实现 broker.ProjectEnvironments：只对首选后端能用环境镜像的租户查；仓库没有定义时两个返回值都为空。
func (m *Manager) Resolve(ctx context.Context, tenant *commonv1.TenantContext, backend string, read func(path string) ([]byte, error)) (string, string, error) {
	if m.usable == nil || !m.usable(backend) {
		return "", "", nil
	}
	res, err := m.ResolveDefinition(ctx, tenant, read)
	if err != nil || res.State == "none" {
		return "", "", err
	}
	data, _ := json.Marshal(res.Output())
	if m.strict != nil && m.strict(backend) && res.State != "ready" {
		if res.State == "building" {
			return "", string(data), providererr.New(providererr.Transient, "environment_build_pending", "项目环境正在构建；不会使用通用环境替代")
		}
		return "", string(data), providererr.New(providererr.Invalid, "environment_not_ready", "项目环境尚未就绪；不会使用通用环境替代")
	}
	return res.Image, string(data), nil
}

func (m *Manager) retryDue(rec ImageRecord) bool {
	stamp := rec.LastUsedAt
	if stamp == "" {
		stamp = rec.CreatedAt
	}
	at := ledger.ParseISO(stamp)
	return at.IsZero() || m.ledger.Now().Sub(at) >= m.cfg.Limits.FailedRetryAfter
}

func runKey(owner, key string) string { return owner + "\x00" + key }

func (m *Manager) isRunning(owner, key string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.running[runKey(owner, key)]
}

// adopt 接手心跳停了的构建（服务重启、换了副本）：只有一个调用方改得成那一行。
func (m *Manager) adopt(ctx context.Context, owner, key string) {
	if m.cfg.Start == nil && m.isRunning(owner, key) {
		return
	}
	ok, err := m.Store.Claim(ctx, owner, key, m.ledger.Now().Add(-3*m.cfg.Heartbeat))
	if err != nil {
		m.cfg.Logger.Warn("接手不了卡住的项目环境构建", "key", key[:12], "code", code(err))
		return
	}
	if ok {
		m.cfg.Logger.Info("接手心跳停了的项目环境构建", "key", key[:12])
		m.start(owner, key)
	}
}

func code(err error) string {
	var pe *providererr.Error
	if errors.As(err, &pe) {
		return pe.Code
	}
	return "internal"
}

// start 在服务进程里起一次构建（同一个键只起一个）：先记心跳，再等并发的名额。
func (m *Manager) start(owner, key string) {
	if m.cfg.Start != nil {
		m.cfg.Start(owner, key)
		return
	}
	id := runKey(owner, key)
	m.mu.Lock()
	if m.running[id] {
		m.mu.Unlock()
		return
	}
	m.running[id] = true
	m.mu.Unlock()
	m.wg.Add(1)
	go func() {
		defer m.wg.Done()
		defer func() {
			m.mu.Lock()
			delete(m.running, id)
			m.mu.Unlock()
		}()
		ctx, stop := context.WithCancel(m.ctx)
		defer stop()
		go m.heartbeat(ctx, owner, key)
		select {
		case m.sem <- struct{}{}:
		case <-ctx.Done():
			return
		}
		defer func() { <-m.sem }()
		rec, err := m.RunBuild(ctx, owner, key)
		if err != nil {
			m.cfg.Logger.Warn("项目环境构建没跑完", "tenant", owner, "key", key[:12], "code", code(err))
			return
		}
		m.cfg.Logger.Info("项目环境构建结束", "tenant", owner, "key", key[:12], "state", rec.State, "reason", rec.Reason)
	}()
}

func (m *Manager) heartbeat(ctx context.Context, owner, key string) {
	t := time.NewTicker(m.cfg.Heartbeat)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if ok, err := m.Store.Heartbeat(ctx, owner, key); err == nil && !ok {
				return
			}
		}
	}
}

// RunBuild 构建一个镜像并推进仓库；失败记原因。返回最终的记录。ctx 取消（服务在退出）时不记失败：留着 building 由别人接手。
func (m *Manager) RunBuild(ctx context.Context, owner, key string) (ImageRecord, error) {
	rec, err := m.Store.Get(ctx, owner, key)
	if err != nil {
		return ImageRecord{}, err
	}
	if rec == nil {
		return ImageRecord{}, providererr.New(providererr.NotFound, "unknown_environment", "没有这个环境定义的构建记录")
	}
	if rec.State != "building" {
		return *rec, nil
	}
	if m.cfg.Builder == nil || m.cfg.Pusher == nil {
		if err := m.Store.MarkFailed(ctx, owner, key, "这个部署没有配置环境构建器", 0); err != nil {
			return ImageRecord{}, err
		}
		return m.reload(ctx, owner, key)
	}
	started := time.Now()
	elapsed := func() int64 { return time.Since(started).Milliseconds() }
	// 产物先落到临时文件（几个 GB，不进内存），推完就删。
	tmp, err := os.CreateTemp(m.cfg.TmpDir, "envbuild-*.tar")
	if err != nil {
		return ImageRecord{}, providererr.Wrap(providererr.Transient, "tmp_unavailable", "建不了临时文件", err)
	}
	defer func() { _ = tmp.Close(); _ = os.Remove(tmp.Name()) }()
	outcome := m.build(ctx, *rec, tmp)
	if ctx.Err() != nil {
		return *rec, ctx.Err()
	}
	if !outcome.OK {
		reason := outcome.Reason
		if reason == "" {
			reason = "构建失败"
		}
		if err := m.Store.MarkFailed(ctx, owner, key, reason, elapsed()); err != nil {
			return ImageRecord{}, err
		}
		return m.reload(ctx, owner, key)
	}
	// 推到本租户的路径（由这里按租户拼，打包里写的名字一概不看）。
	layout, err := ReadLayout(tmp, m.cfg.Limits.MaxImageBytes)
	var reason string
	var digest string
	repo := m.cfg.Registry.Repo(owner)
	if err != nil {
		var pe *providererr.Error
		if errors.As(err, &pe) && pe.Code == "image_too_large" {
			reason = "镜像超过大小上限"
		} else if errors.As(err, &pe) {
			reason = "构建产物不对（" + pe.Message + "）"
		} else {
			reason = "构建产物不对"
		}
	} else if digest, err = m.cfg.Pusher.Push(ctx, repo, key[:32], layout); err != nil {
		if ctx.Err() != nil {
			return *rec, ctx.Err()
		}
		var pe *providererr.Error
		if errors.As(err, &pe) && pe.Category == providererr.Invalid && pe.Code == "invalid_image" {
			reason = "构建产物不对（" + pe.Message + "）"
		} else {
			reason = "推不进镜像仓库（" + code(err) + "）"
		}
	}
	if reason != "" {
		if err := m.Store.MarkFailed(ctx, owner, key, reason, elapsed()); err != nil {
			return ImageRecord{}, err
		}
		return m.reload(ctx, owner, key)
	}
	if err := m.Store.MarkReady(ctx, owner, key, m.cfg.Pusher.Reference(repo, digest), digest, layout.Size(), elapsed()); err != nil {
		return ImageRecord{}, err
	}
	return m.reload(ctx, owner, key)
}

func (m *Manager) build(ctx context.Context, rec ImageRecord, out io.Writer) Outcome {
	env := map[string]string{}
	if m.network != nil {
		var err error
		env, err = m.network.Open(ctx, rec.Owner, rec.ImageKey, m.cfg.Limits.BuildTimeout)
		defer func() {
			cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
			defer cancel()
			if err := m.network.Close(cctx, rec.Owner, rec.ImageKey); err != nil {
				m.cfg.Logger.Warn("构建联网收不回", "key", rec.ImageKey[:12], "code", code(err))
			}
		}()
		if err != nil {
			return Outcome{Reason: fmt.Sprintf("构建器出错（%s）", code(err)), Failure: "failed"}
		}
	}
	job := BuildJob{Owner: rec.Owner, Key: rec.ImageKey, Dockerfile: rec.Dockerfile, Context: rec.Context, WorkspaceID: rec.WorkspaceID,
		Timeout: m.cfg.Limits.BuildTimeout, MaxBytes: m.cfg.Limits.MaxImageBytes, Env: env}
	outcome, err := m.cfg.Builder.Build(ctx, job, out)
	if err != nil {
		return Outcome{Reason: fmt.Sprintf("构建器出错（%s）", code(err)), Failure: "failed"}
	}
	return outcome
}

func (m *Manager) reload(ctx context.Context, owner, key string) (ImageRecord, error) {
	rec, err := m.Store.Get(ctx, owner, key)
	if err != nil {
		return ImageRecord{}, err
	}
	if rec == nil {
		return ImageRecord{}, providererr.New(providererr.NotFound, "unknown_environment", "没有这个环境定义的构建记录")
	}
	return *rec, nil
}

// GC 清掉长期不用的镜像（删仓库里的清单、记为 deleted）；返回清了几个。
func (m *Manager) GC(ctx context.Context, owner string) (int, error) {
	if m.cfg.Pusher == nil {
		return 0, nil
	}
	cutoff := ledger.ISO(m.ledger.Now().Add(-time.Duration(m.cfg.Limits.IdleDays) * 24 * time.Hour))
	records, err := m.Store.All(ctx, owner)
	if err != nil {
		return 0, err
	}
	n := 0
	for _, rec := range records {
		if rec.State != "ready" || rec.LastUsedAt == "" || rec.LastUsedAt > cutoff {
			continue
		}
		if _, err := m.cfg.Pusher.Delete(ctx, m.cfg.Registry.Repo(owner), rec.Digest); err != nil && providererr.CategoryOf(err) != providererr.NotFound {
			m.cfg.Logger.Warn("清不掉项目环境的镜像", "key", rec.ImageKey[:12], "code", code(err))
			continue
		}
		if err := m.Store.MarkDeleted(ctx, owner, rec.ImageKey); err != nil {
			return n, err
		}
		n++
	}
	return n, nil
}

// maybeGC：每个租户每小时最多清一次（随用随清，不另起定时任务）。
func (m *Manager) maybeGC(ctx context.Context, owner string) {
	now := m.ledger.Now()
	m.mu.Lock()
	last, ok := m.lastGC[owner]
	if ok && now.Sub(last) < time.Hour {
		m.mu.Unlock()
		return
	}
	m.lastGC[owner] = now
	m.mu.Unlock()
	if _, err := m.GC(ctx, owner); err != nil {
		m.cfg.Logger.Warn("清理项目环境的镜像失败", "tenant", owner, "code", code(err))
	}
}

// --- setup 的缓存（ENV-02；broker.SetupCache）---

func (m *Manager) cacheUsable(backend string) bool {
	return m.cfg.Builder != nil && m.cfg.Pusher != nil && m.usable != nil && m.usable(backend)
}

// Cached 是 setup 缓存的命中：（缓存键，镜像）。还在构建时顺便接手心跳停了的构建。
func (m *Manager) Cached(ctx context.Context, owner, backend, script string) (string, string, bool) {
	if !m.cacheUsable(backend) || owner == ledger.PlatformID {
		return "", "", false
	}
	key := broker.SetupKey(script, m.cfg.BaseDigest)
	rec, err := m.Store.Get(ctx, owner, key)
	if err != nil {
		m.cfg.Logger.Warn("查不了 setup 的缓存，这次照常跑 setup", "code", code(err))
		return "", "", false
	}
	if rec == nil {
		return "", "", false
	}
	if rec.State == "building" {
		m.adopt(ctx, owner, key)
		return "", "", false
	}
	if rec.State != "ready" || rec.Image == "" {
		return "", "", false
	}
	if err := m.Store.Touch(ctx, owner, key); err != nil {
		m.cfg.Logger.Warn("记不了 setup 缓存的使用时间", "code", code(err))
	}
	return key, rec.Image, true
}

// BaseDigest 是 setup 缓存键里的基础镜像 digest；后端用不了缓存时为空。
func (m *Manager) BaseDigest(backend string) string {
	if !m.cacheUsable(backend) {
		return ""
	}
	return m.cfg.BaseDigest
}

// Start 在 setup 成功之后按缓存键起一次构建（只缓存成功的结果）；已有就绪或正在构建的不重起。缓存只是省时间：起不了也不影响这次。
func (m *Manager) Start(ctx context.Context, owner, workspaceID, environmentID, script, key string) {
	rec, err := m.Store.Get(ctx, owner, key)
	if err != nil {
		m.cfg.Logger.Warn("setup 的缓存构建起不了，之后的新环境照常跑 setup", "code", code(err))
		return
	}
	if rec != nil && (rec.State == "ready" || rec.State == "building") {
		return
	}
	base := m.From()
	def := Definition{Kind: "setup", Path: "environment:" + environmentID, Content: []byte(script), Dockerfile: SetupDockerfile(base, script)}
	_, created, err := m.Store.BeginBuild(ctx, owner, def, key, workspaceID, base, m.cfg.Limits.MaxImages, true)
	if err != nil {
		m.cfg.Logger.Warn("setup 的缓存构建起不了，之后的新环境照常跑 setup", "code", code(err))
		return
	}
	if created {
		m.start(owner, key)
	}
}

var (
	_ broker.ProjectEnvironments = (*Manager)(nil)
	_ broker.SetupCache          = (*Manager)(nil)
)
