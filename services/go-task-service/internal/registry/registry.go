// Package registry 是 Registry（设计书 3.1.2）：版本化存放 Node Template、Recipe、Model Profile 与租户策略，
// 实现 agentruntime.registry.v1.RegistryService，挂在 task-service 的内部端口上。细化见 docs/plan/registry.md。
//
// 规则（细化第 2、4、8 节）：
//   - 存储：业务库一张表 registry_entries，按作用域隔离由 RelationalStore 负责，SQL 不写租户条件。Node Template、
//     Model Profile 只在平台作用域（BindPlatform("registry")）；租户策略在租户作用域，另有平台作用域里一份名为 default
//     的平台默认策略，给没有自己策略的租户用；Recipe 两种都有，同名时平台的优先，租户发布与平台同名的 Recipe 是 Conflict。
//   - 版本：同一作用域、种类、名字下按发布顺序编号 v1、v2……；document 一经发布不再修改。新版本是影子（SHADOW）；
//     Promote 带达标的评测证据把一个版本设为默认（DEFAULT），原默认退役（RETIRED），退役的仍可按版本号读。
//     租户策略没有影子：发布即成为当前版本，不走 Promote。
//   - 校验：document 按种类对应的 /schemas 校验，另加 JSON Schema 表达不了的检查（validate.go）。
//   - 固定：ResolvePinnedVersions 只固定默认版本与当前的租户策略；按固定的版本列模板不受之后的发布与晋升影响。
//   - Skill（N-14）：平台、租户、用户三层，同名上层优先，固定值写成 <层>/vN；规则见 skills.go。
//   - 写权限：Publish、Promote 要带发布令牌（authorization: Bearer …，SecretStore 平台作用域的 PublishTokenSecret）；
//     令牌缺了、不对或没配置一律 denied，说明相同；令牌不进日志与错误。读方法不认证（内部端口，服务间认证留给 mTLS）。
//   - 读方法一律要租户：缺租户 denied，格式不对 invalid（tenancy.TenantScope）。
//   - 错误经 connecterr 转成 gRPC 错误，trailer ar-error-bin 带 common.v1.Error。
package registry

import (
	"context"
	"errors"
	"log/slog"
	"strconv"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1/registryv1connect"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
)

// PublishTokenSecret 是 SecretStore 平台作用域里发布令牌的名字：Publish、Promote 要带它（authorization: Bearer …）。
const PublishTokenSecret = "registry/publish-token"

// platformPurpose 是 Registry 在平台作用域的用途（RelationalStore 与 SecretStore 都用它）。
const platformPurpose = "registry"

// DefaultTenantPolicy 是平台默认的租户策略的名字：发布时不带租户、名字为它；没有自己策略的租户读到它（例如应用开放注册后
// 新来的租户），固定在 Task 上的版本写成 default/vN。租户自己的策略总是优先。
const DefaultTenantPolicy = "default"

const defaultPolicyPrefix = DefaultTenantPolicy + "/"

// Options 是 Registry 的依赖。
type Options struct {
	// Store 是业务库；表按作用域隔离由它负责。
	Store relational.Provider
	// Secrets 存发布令牌（平台作用域）；为 nil 时所有发布都被拒。
	Secrets secretstore.Provider
	// SchemaDir 是 /schemas 目录：按它校验 document 与评测证据。
	SchemaDir string
	// Logger 为 nil 时不记日志。
	Logger *slog.Logger
	// Clock 为 nil 时用系统时钟。
	Clock clock.Clock
}

// Registry 实现 registryv1connect.RegistryServiceHandler。
type Registry struct {
	store   relational.Provider
	secrets secretstore.Provider
	schemas *schemaSet
	log     *slog.Logger
	clock   clock.Clock
}

var _ registryv1connect.RegistryServiceHandler = (*Registry)(nil)

// New 构造 Registry：编译 SchemaDir 里的 schema，编译不了时报错。
func New(opts Options) (*Registry, error) {
	if opts.Store == nil {
		return nil, errors.New("registry：Store 不能为空")
	}
	schemas, err := loadSchemas(opts.SchemaDir)
	if err != nil {
		return nil, err
	}
	r := &Registry{store: opts.Store, secrets: opts.Secrets, schemas: schemas, log: opts.Logger, clock: opts.Clock}
	if r.log == nil {
		r.log = slog.New(slog.DiscardHandler)
	}
	if r.clock == nil {
		r.clock = clock.System{}
	}
	return r, nil
}

// Migrations 返回 Registry 的表结构。版本号从 101 起，与同一业务库里 taskstore 的迁移分开。
func Migrations() []relational.Migration {
	return []relational.Migration{{
		Version: 101,
		Name:    "registry_entries",
		Statements: []string{
			`CREATE TABLE registry_entries (
				kind BIGINT NOT NULL,
				name TEXT NOT NULL,
				version BIGINT NOT NULL,
				state BIGINT NOT NULL,
				task_family TEXT NOT NULL,
				document TEXT NOT NULL,
				change_note TEXT NOT NULL,
				evaluation TEXT NOT NULL,
				created_at BIGINT NOT NULL,
				updated_at BIGINT NOT NULL,
				PRIMARY KEY (kind, name, version)
			)`,
		},
	}}
}

// scopeRule 是种类允许的作用域。
type scopeRule int

const (
	platformOnly scopeRule = iota + 1
	tenantOnly
	eitherScope
)

// kindSpec 是一种对象的规则。
type kindSpec struct {
	// schema 是 /schemas 里的文件名（不带 .schema.json）。
	schema string
	scope  scopeRule
	// immediate：发布即成为当前版本（租户策略），不走 Promote。
	immediate bool
}

var kinds = map[registryv1.RegistryKind]kindSpec{
	registryv1.RegistryKind_REGISTRY_KIND_NODE_TEMPLATE: {schema: "node_template", scope: platformOnly},
	registryv1.RegistryKind_REGISTRY_KIND_RECIPE:        {schema: "recipe", scope: eitherScope},
	registryv1.RegistryKind_REGISTRY_KIND_MODEL_PROFILE: {schema: "model_profile", scope: platformOnly},
	registryv1.RegistryKind_REGISTRY_KIND_TENANT_POLICY: {schema: "tenant_policy", scope: tenantOnly, immediate: true},
	// TMP 计划第 6 节：任务族词表，规则同租户策略（平台默认一份、租户自己的优先、发布即生效）。
	registryv1.RegistryKind_REGISTRY_KIND_TASK_FAMILIES: {schema: "task_families", scope: tenantOnly, immediate: true},
	// N-14：Skill 发布即生效；不带租户是平台层，带租户是租户层，用户层另走 PutUserSkill（skills.go）。
	registryv1.RegistryKind_REGISTRY_KIND_SKILL: {schema: "skill", scope: eitherScope, immediate: true},
}

// specOf 返回种类的规则。Reasoning Template 第一期不收（还没有 schema）。
func specOf(kind registryv1.RegistryKind) (kindSpec, error) {
	spec, ok := kinds[kind]
	if !ok {
		return kindSpec{}, invalid("unsupported_kind", "Registry 不收这一种对象："+kind.String())
	}
	return spec, nil
}

// Pins 实现 taskapi.PinResolver：新 Task 按提交那一刻的默认版本固定。错误是 *providererr.Error（Registry 不可用时 transient）。
type Pins struct{ Registry *Registry }

// Resolve 实现 taskapi.PinResolver。
func (p Pins) Resolve(ctx context.Context, tenant *commonv1.TenantContext) (*workflowv1.PinnedVersions, error) {
	return p.Registry.resolve(ctx, tenant, "", "")
}

func invalid(code, message string) error { return providererr.New(providererr.Invalid, code, message) }

func notFound(code, message string) error {
	return providererr.New(providererr.NotFound, code, message)
}

// versionLabel 把版本号写成 vN。
func versionLabel(n int64) string { return "v" + strconv.FormatInt(n, 10) }

// parseVersion 解析 vN；空串表示默认版本（0）。
func parseVersion(s string) (int64, error) {
	if s == "" {
		return 0, nil
	}
	if len(s) < 2 || s[0] != 'v' {
		return 0, invalid("invalid_version", "版本号要写成 vN")
	}
	n, err := strconv.ParseInt(s[1:], 10, 64)
	if err != nil || n < 1 {
		return 0, invalid("invalid_version", "版本号要写成 vN")
	}
	return n, nil
}
