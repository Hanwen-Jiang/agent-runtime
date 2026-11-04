package registry

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"sort"
	"strconv"
	"strings"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/connecterr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
)

const (
	defaultPageSize = 100
	maxPageSize     = 500
)

const (
	kindTemplate = registryv1.RegistryKind_REGISTRY_KIND_NODE_TEMPLATE
	kindRecipe   = registryv1.RegistryKind_REGISTRY_KIND_RECIPE
	kindProfile  = registryv1.RegistryKind_REGISTRY_KIND_MODEL_PROFILE
	kindPolicy   = registryv1.RegistryKind_REGISTRY_KIND_TENANT_POLICY
	kindFamilies = registryv1.RegistryKind_REGISTRY_KIND_TASK_FAMILIES
)

// fail 把错误转成带 trailer 的 gRPC 错误；只在 err 不为 nil 时调用。
func fail(err error) error { return connecterr.Error(err) }

// --- 写 ---

// authorize 核对发布令牌（authorization: Bearer …）。缺了、不对、SecretStore 里没有，一律是同一个 denied；
// 令牌本身不进日志与错误。SecretStore 暂时不可用时是 transient。
func (r *Registry) authorize(ctx context.Context, header http.Header, method string) error {
	denied := providererr.New(providererr.Denied, "publish_denied", "要有效的发布令牌")
	if r.secrets == nil {
		r.log.Warn("Registry 拒绝写入：没有配置 SecretStore", "method", method)
		return denied
	}
	store, err := r.secrets.BindPlatform(platformPurpose)
	if err != nil {
		return err
	}
	want, err := store.Read(ctx, PublishTokenSecret)
	switch {
	case providererr.CategoryOf(err) == providererr.Transient:
		return providererr.New(providererr.Transient, "secret_store_unavailable", "暂时读不到发布令牌")
	case err != nil || want.IsZero():
		r.log.Warn("Registry 拒绝写入：SecretStore 里没有发布令牌", "method", method)
		return denied
	}
	given, ok := strings.CutPrefix(header.Get("Authorization"), "Bearer ")
	if !ok || given == "" || !secret.New(given).Equal(want) {
		r.log.Warn("Registry 拒绝写入：发布令牌缺失或不对", "method", method)
		return denied
	}
	return nil
}

// Publish 发布新版本（影子；租户策略发布即生效）。
func (r *Registry) Publish(ctx context.Context, req *connect.Request[registryv1.PublishRequest]) (*connect.Response[registryv1.PublishResponse], error) {
	if err := r.authorize(ctx, req.Header(), "Publish"); err != nil {
		return nil, fail(err)
	}
	msg := req.Msg
	e, err := r.publish(ctx, msg.GetKind(), msg.GetTenant(), msg.GetName(), msg.GetDocument(), msg.GetChangeNote())
	if err != nil {
		return nil, fail(err)
	}
	version := e.label()
	if msg.GetKind() == kindSkill {
		// Skill 的版本带层（N-14）：不带租户是平台层，带租户是租户层。
		l := layerPlatform
		if msg.GetTenant().GetTenantId() != "" {
			l = layerTenant
		}
		version = skillLabel(l, e.version)
	}
	return connect.NewResponse(&registryv1.PublishResponse{Name: e.name, Version: version, State: e.state}), nil
}

func (r *Registry) publish(ctx context.Context, kind registryv1.RegistryKind, tenant *commonv1.TenantContext, name string, doc *commonv1.JsonPayload, note string) (entry, error) {
	spec, err := specOf(kind)
	if err != nil {
		return entry{}, err
	}
	raw, err := payloadJSON(doc, "document", r.schemas.ids[spec.schema])
	if err != nil {
		return entry{}, err
	}
	c, err := r.prepare(kind, tenant, name, raw)
	if err != nil {
		return entry{}, err
	}
	if kind == kindSkill {
		// Skill 的正文与当前版本相同时不产生新版本（N-14）。
		if current, same, err := r.publishedSkill(ctx, c); err != nil || same {
			return current, err
		}
	}
	return r.write(ctx, c, note)
}

// write 做读业务库的检查后写入。
func (r *Registry) write(ctx context.Context, c candidate, note string) (entry, error) {
	if err := r.check(ctx, c); err != nil {
		return entry{}, err
	}
	e, err := c.scope.insert(ctx, c, note, "", r.clock.Now())
	if err != nil {
		return entry{}, err
	}
	r.log.Info("Registry 发布", "kind", c.kind.String(), "name", c.name, "version", e.label(), "state", e.state.String(), "platform", c.scope.platform)
	return e, nil
}

// Promote 带达标的评测证据把一个版本设为默认。
func (r *Registry) Promote(ctx context.Context, req *connect.Request[registryv1.PromoteRequest]) (*connect.Response[registryv1.PromoteResponse], error) {
	if err := r.authorize(ctx, req.Header(), "Promote"); err != nil {
		return nil, fail(err)
	}
	msg := req.Msg
	previous, err := r.promote(ctx, msg)
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&registryv1.PromoteResponse{PreviousDefaultVersion: previous}), nil
}

func (r *Registry) promote(ctx context.Context, msg *registryv1.PromoteRequest) (string, error) {
	spec, err := specOf(msg.GetKind())
	if err != nil {
		return "", err
	}
	if spec.immediate {
		return "", invalid("no_promote", "租户策略发布即生效，不走 Promote")
	}
	sc, err := r.writeScope(spec, msg.GetTenant())
	if err != nil {
		return "", err
	}
	version, err := parseVersion(msg.GetVersion())
	if err != nil {
		return "", err
	}
	if version == 0 {
		return "", invalid("invalid_version", "要指定晋升的版本")
	}
	evidence, err := payloadJSON(msg.GetEvaluation(), "evaluation", r.schemas.ids[evaluationSchema])
	if err != nil {
		return "", err
	}
	if err := r.schemas.validate(evaluationSchema, evidence); err != nil {
		return "", err
	}
	var verdict struct {
		Passed bool `json:"passed"`
	}
	if err := json.Unmarshal(evidence, &verdict); err != nil || !verdict.Passed {
		return "", invalid("evaluation_not_passed", "评测证据的结论不是达标（passed 不是 true）")
	}
	previous, err := sc.promote(ctx, msg.GetKind(), msg.GetName(), version, string(evidence), r.clock.Now())
	if err != nil {
		return "", err
	}
	r.log.Info("Registry 晋升", "kind", msg.GetKind().String(), "name", msg.GetName(), "version", msg.GetVersion(), "previous", previous, "platform", sc.platform)
	return previous, nil
}

// --- 读 ---

// reader 校验读请求的租户（缺租户 denied，格式不对 invalid），返回平台作用域。
func (r *Registry) reader(tenant *commonv1.TenantContext) (scope, error) {
	if _, err := tenancy.TenantScope(tenant); err != nil {
		return scope{}, err
	}
	return r.platformScope()
}

// GetNodeTemplate 按名字取模板；version 为空时取默认版本。
func (r *Registry) GetNodeTemplate(ctx context.Context, req *connect.Request[registryv1.GetNodeTemplateRequest]) (*connect.Response[registryv1.GetNodeTemplateResponse], error) {
	e, err := r.getPlatform(ctx, req.Msg.GetTenant(), kindTemplate, req.Msg.GetName(), req.Msg.GetVersion())
	if err != nil {
		return nil, fail(err)
	}
	t, err := toTemplate(e)
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&registryv1.GetNodeTemplateResponse{NodeTemplate: t}), nil
}

func (r *Registry) getPlatform(ctx context.Context, tenant *commonv1.TenantContext, kind registryv1.RegistryKind, name, version string) (entry, error) {
	platform, err := r.reader(tenant)
	if err != nil {
		return entry{}, err
	}
	v, err := parseVersion(version)
	if err != nil {
		return entry{}, err
	}
	e, found, err := platform.get(ctx, kind, name, v)
	if err != nil {
		return entry{}, err
	}
	if !found {
		return entry{}, notFound("not_found", "Registry 里没有这一项")
	}
	return e, nil
}

// ListNodeTemplates 列模板：带固定版本时只列固定的（含退役的，缺一个就 NotFound），否则列默认版本。按名字排序、分页。
func (r *Registry) ListNodeTemplates(ctx context.Context, req *connect.Request[registryv1.ListNodeTemplatesRequest]) (*connect.Response[registryv1.ListNodeTemplatesResponse], error) {
	platform, err := r.reader(req.Msg.GetTenant())
	if err != nil {
		return nil, fail(err)
	}
	var entries []entry
	if pins := req.Msg.GetPinnedVersions().GetNodeTemplates(); len(pins) > 0 {
		names := make([]string, 0, len(pins))
		for name := range pins {
			names = append(names, name)
		}
		sort.Strings(names)
		for _, name := range names {
			v, err := parseVersion(pins[name])
			if err != nil || v == 0 {
				return nil, fail(invalid("invalid_version", "固定的版本要写成 vN"))
			}
			e, found, err := platform.get(ctx, kindTemplate, name, v)
			if err != nil {
				return nil, fail(err)
			}
			if !found {
				return nil, fail(notFound("pinned_template_not_found", "固定的模板版本 "+name+"@"+pins[name]+" 不存在"))
			}
			entries = append(entries, e)
		}
	} else if entries, err = platform.defaults(ctx, kindTemplate); err != nil {
		return nil, fail(err)
	}
	page, next, err := paginate(entries, req.Msg.GetPageSize(), req.Msg.GetPageToken())
	if err != nil {
		return nil, fail(err)
	}
	out := &registryv1.ListNodeTemplatesResponse{NextPageToken: next}
	for _, e := range page {
		t, err := toTemplate(e)
		if err != nil {
			return nil, fail(err)
		}
		out.NodeTemplates = append(out.NodeTemplates, t)
	}
	return connect.NewResponse(out), nil
}

// GetRecipe 取 Recipe：名字在平台作用域发布过就取平台的，否则取请求租户的。
func (r *Registry) GetRecipe(ctx context.Context, req *connect.Request[registryv1.GetRecipeRequest]) (*connect.Response[registryv1.GetRecipeResponse], error) {
	v, err := parseVersion(req.Msg.GetVersion())
	if err != nil {
		return nil, fail(err)
	}
	e, err := r.recipe(ctx, req.Msg.GetTenant(), req.Msg.GetName(), v)
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&registryv1.GetRecipeResponse{Recipe: r.toRecipe(e)}), nil
}

func (r *Registry) recipe(ctx context.Context, tenant *commonv1.TenantContext, name string, version int64) (entry, error) {
	platform, err := r.reader(tenant)
	if err != nil {
		return entry{}, err
	}
	sc := platform
	if taken, err := platform.exists(ctx, kindRecipe, name); err != nil {
		return entry{}, err
	} else if !taken {
		if sc, err = r.tenantScope(tenant); err != nil {
			return entry{}, err
		}
	}
	e, found, err := sc.get(ctx, kindRecipe, name, version)
	if err != nil {
		return entry{}, err
	}
	if !found {
		return entry{}, notFound("recipe_not_found", "没有这个 Recipe")
	}
	return e, nil
}

// visibleRecipes 是请求租户看得到的默认 Recipe：平台的，加上名字不与平台重的租户的；按名字排序。
func (r *Registry) visibleRecipes(ctx context.Context, tenant *commonv1.TenantContext, family string) ([]entry, error) {
	platform, err := r.reader(tenant)
	if err != nil {
		return nil, err
	}
	own, err := r.tenantScope(tenant)
	if err != nil {
		return nil, err
	}
	shared, err := platform.defaults(ctx, kindRecipe)
	if err != nil {
		return nil, err
	}
	taken, err := platform.names(ctx, kindRecipe)
	if err != nil {
		return nil, err
	}
	mine, err := own.defaults(ctx, kindRecipe)
	if err != nil {
		return nil, err
	}
	var out []entry
	for _, e := range shared {
		if family == "" || e.taskFamily == family {
			out = append(out, e)
		}
	}
	for _, e := range mine {
		if !taken[e.name] && (family == "" || e.taskFamily == family) {
			out = append(out, e)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].name < out[j].name })
	return out, nil
}

// ListRecipes 列请求租户看得到的默认 Recipe，可按任务族过滤；按名字排序、分页。
func (r *Registry) ListRecipes(ctx context.Context, req *connect.Request[registryv1.ListRecipesRequest]) (*connect.Response[registryv1.ListRecipesResponse], error) {
	entries, err := r.visibleRecipes(ctx, req.Msg.GetTenant(), req.Msg.GetTaskFamily())
	if err != nil {
		return nil, fail(err)
	}
	page, next, err := paginate(entries, req.Msg.GetPageSize(), req.Msg.GetPageToken())
	if err != nil {
		return nil, fail(err)
	}
	out := &registryv1.ListRecipesResponse{NextPageToken: next}
	for _, e := range page {
		out.Recipes = append(out.Recipes, r.toRecipe(e))
	}
	return connect.NewResponse(out), nil
}

// GetModelProfile 按 id 取 Model Profile；version 为空时取默认版本。
func (r *Registry) GetModelProfile(ctx context.Context, req *connect.Request[registryv1.GetModelProfileRequest]) (*connect.Response[registryv1.GetModelProfileResponse], error) {
	e, err := r.getPlatform(ctx, req.Msg.GetTenant(), kindProfile, req.Msg.GetId(), req.Msg.GetVersion())
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&registryv1.GetModelProfileResponse{ModelProfile: r.toProfile(e)}), nil
}

// ListModelProfiles 列默认版本的 Model Profile，按 id 排序、分页。
func (r *Registry) ListModelProfiles(ctx context.Context, req *connect.Request[registryv1.ListModelProfilesRequest]) (*connect.Response[registryv1.ListModelProfilesResponse], error) {
	platform, err := r.reader(req.Msg.GetTenant())
	if err != nil {
		return nil, fail(err)
	}
	entries, err := platform.defaults(ctx, kindProfile)
	if err != nil {
		return nil, fail(err)
	}
	page, next, err := paginate(entries, req.Msg.GetPageSize(), req.Msg.GetPageToken())
	if err != nil {
		return nil, fail(err)
	}
	out := &registryv1.ListModelProfilesResponse{NextPageToken: next}
	for _, e := range page {
		out.ModelProfiles = append(out.ModelProfiles, r.toProfile(e))
	}
	return connect.NewResponse(out), nil
}

// GetTenantPolicy 取请求租户的策略；version 为空时取当前版本。租户没有自己的策略时取平台默认的（版本写成 default/vN）。
func (r *Registry) GetTenantPolicy(ctx context.Context, req *connect.Request[registryv1.GetTenantPolicyRequest]) (*connect.Response[registryv1.GetTenantPolicyResponse], error) {
	e, label, found, err := r.tenantPolicy(ctx, req.Msg.GetTenant(), req.Msg.GetVersion())
	if err != nil {
		return nil, fail(err)
	}
	if !found {
		return nil, fail(notFound("tenant_policy_not_found", "没有这个租户策略"))
	}
	return connect.NewResponse(&registryv1.GetTenantPolicyResponse{TenantPolicy: &registryv1.TenantPolicy{
		Version: label, Document: r.payload(e),
	}}), nil
}

// tenantPolicy 取租户策略的一个版本与它的版本号：version 为空时是租户自己的当前版本，没有时是平台默认的当前版本；
// vN 是租户自己的第 N 版；default/vN 是平台默认的第 N 版（固定在 Task 上的版本照原样取回，之后租户有了自己的策略也不变）。
func (r *Registry) tenantPolicy(ctx context.Context, tenant *commonv1.TenantContext, version string) (entry, string, bool, error) {
	own, err := r.tenantScope(tenant)
	if err != nil {
		return entry{}, "", false, err
	}
	if rest, isDefault := strings.CutPrefix(version, defaultPolicyPrefix); isDefault {
		v, err := parseVersion(rest)
		if err != nil || v == 0 {
			return entry{}, "", false, invalid("invalid_version", "版本号要写成 vN 或 "+defaultPolicyPrefix+"vN")
		}
		return r.defaultTenantPolicy(ctx, v)
	}
	v, err := parseVersion(version)
	if err != nil {
		return entry{}, "", false, err
	}
	e, found, err := own.get(ctx, kindPolicy, "", v)
	if err != nil || found || v != 0 {
		return e, e.label(), found, err
	}
	return r.defaultTenantPolicy(ctx, 0)
}

func (r *Registry) defaultTenantPolicy(ctx context.Context, version int64) (entry, string, bool, error) {
	platform, err := r.platformScope()
	if err != nil {
		return entry{}, "", false, err
	}
	e, found, err := platform.get(ctx, kindPolicy, DefaultTenantPolicy, version)
	return e, defaultPolicyPrefix + e.label(), found, err
}

// GetTaskFamilies 取租户的任务族词表的当前版本（TMP 计划第 6 节）；租户没有自己的时取平台默认的（版本写成 default/vN），
// 都没有时 NotFound。
func (r *Registry) GetTaskFamilies(ctx context.Context, req *connect.Request[registryv1.GetTaskFamiliesRequest]) (*connect.Response[registryv1.GetTaskFamiliesResponse], error) {
	own, err := r.tenantScope(req.Msg.GetTenant())
	if err != nil {
		return nil, fail(err)
	}
	e, found, err := own.get(ctx, kindFamilies, "", 0)
	if err != nil {
		return nil, fail(err)
	}
	label, platformDefault := e.label(), false
	if !found {
		platform, err := r.platformScope()
		if err != nil {
			return nil, fail(err)
		}
		if e, found, err = platform.get(ctx, kindFamilies, DefaultTenantPolicy, 0); err != nil {
			return nil, fail(err)
		}
		label, platformDefault = defaultPolicyPrefix+e.label(), true
	}
	if !found {
		return nil, fail(notFound("task_families_not_found", "还没有任务族词表"))
	}
	return connect.NewResponse(&registryv1.GetTaskFamiliesResponse{TaskFamilies: &registryv1.TaskFamilies{
		Version: label, Document: r.payload(e), PlatformDefault: platformDefault,
	}}), nil
}

// CurrentTenantPolicy 是租户策略的当前版本（JSON 文档）；没有策略时 found 为 false。task-service 核对提交时的联网申请用（D-16）。
func (r *Registry) CurrentTenantPolicy(ctx context.Context, tenant *commonv1.TenantContext) (doc []byte, found bool, err error) {
	resp, err := r.GetTenantPolicy(ctx, connect.NewRequest(&registryv1.GetTenantPolicyRequest{Tenant: tenant}))
	if err != nil {
		if connect.CodeOf(err) == connect.CodeNotFound {
			return nil, false, nil
		}
		return nil, false, err
	}
	value := resp.Msg.GetTenantPolicy().GetDocument().GetValue()
	if value == nil {
		return nil, false, nil
	}
	doc, err = protojson.Marshal(value)
	return doc, err == nil, err
}

// ResolvePinnedVersions 给出新 Task 要固定的版本。
func (r *Registry) ResolvePinnedVersions(ctx context.Context, req *connect.Request[registryv1.ResolvePinnedVersionsRequest]) (*connect.Response[registryv1.ResolvePinnedVersionsResponse], error) {
	pins, err := r.resolve(ctx, req.Msg.GetTenant(), req.Msg.GetTaskFamily(), req.Msg.GetRecipe())
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&registryv1.ResolvePinnedVersionsResponse{PinnedVersions: pins}), nil
}

// resolve：全部模板与 Model Profile 的默认版本、租户策略的当前版本、看得到的当前 Skill；Recipe 按请求指定的（必须是看得到的默认版本），
// 或该任务族恰好一个默认 Recipe 时固定它，否则留空（由 Planner 选）。Reasoning Template 第一期没有。
func (r *Registry) resolve(ctx context.Context, tenant *commonv1.TenantContext, family, recipe string) (*workflowv1.PinnedVersions, error) {
	platform, err := r.reader(tenant)
	if err != nil {
		return nil, err
	}
	pins := &workflowv1.PinnedVersions{NodeTemplates: map[string]string{}, ModelProfiles: map[string]string{}}
	for kind, target := range map[registryv1.RegistryKind]map[string]string{kindTemplate: pins.NodeTemplates, kindProfile: pins.ModelProfiles} {
		entries, err := platform.defaults(ctx, kind)
		if err != nil {
			return nil, err
		}
		for _, e := range entries {
			target[e.name] = e.label()
		}
	}
	_, policy, found, err := r.tenantPolicy(ctx, tenant, "")
	if err != nil {
		return nil, err
	}
	if found {
		pins.TenantPolicy = policy
	}
	// N-14：看得到、没被遮住的当前 Skill 全部固定（用户层是 tenant.user_id，即提交者本人的）。
	if pins.Skills, err = r.pinnedSkills(ctx, tenant); err != nil {
		return nil, err
	}
	switch {
	case recipe != "":
		name, version, err := parseRef(recipe)
		if err != nil {
			return nil, err
		}
		e, err := r.recipe(ctx, tenant, name, version)
		if err != nil {
			return nil, err
		}
		if e.state != registryv1.ReleaseState_RELEASE_STATE_DEFAULT {
			return nil, notFound("recipe_not_found", "没有这个 Recipe")
		}
		pins.Recipe = e.ref()
	case family != "":
		candidates, err := r.visibleRecipes(ctx, tenant, family)
		if err != nil {
			return nil, err
		}
		if len(candidates) == 1 {
			pins.Recipe = candidates[0].ref()
		}
	}
	return pins, nil
}

// --- 转换 ---

func (r *Registry) payload(e entry) *commonv1.JsonPayload {
	var v structpb.Value
	_ = protojson.Unmarshal([]byte(e.document), &v)
	return &commonv1.JsonPayload{SchemaId: r.schemas.ids[kinds[e.kind].schema], Value: &v}
}

func (r *Registry) toRecipe(e entry) *registryv1.Recipe {
	return &registryv1.Recipe{Name: e.name, Version: e.label(), State: e.state, TaskFamily: e.taskFamily, Document: r.payload(e)}
}

func (r *Registry) toProfile(e entry) *registryv1.ModelProfile {
	return &registryv1.ModelProfile{Id: e.name, Version: e.label(), State: e.state, Document: r.payload(e)}
}

// toTemplate 从 document 取出骨架字段。
func toTemplate(e entry) (*registryv1.NodeTemplate, error) {
	var doc struct {
		Executor                  string          `json:"executor"`
		Effect                    string          `json:"effect"`
		Role                      string          `json:"role"`
		Output                    string          `json:"output"`
		OutputSchemaID            string          `json:"output_schema_id"`
		ParamsSchema              json.RawMessage `json:"params_schema"`
		Defaults                  json.RawMessage `json:"defaults"`
		RollbackTemplate          string          `json:"rollback_template"`
		SupportsIdempotencyLookup bool            `json:"supports_idempotency_lookup"`
	}
	if err := json.Unmarshal([]byte(e.document), &doc); err != nil {
		return nil, providererr.Wrap(providererr.Invalid, "corrupt_document", "存下的模板解析不了", err)
	}
	enum := func(values map[string]int32, prefix, name string) int32 {
		if name == "" {
			return 0
		}
		return values[prefix+strings.ToUpper(name)]
	}
	t := &registryv1.NodeTemplate{
		Name:                      e.name,
		Version:                   e.label(),
		State:                     e.state,
		Executor:                  commonv1.Executor(enum(commonv1.Executor_value, "EXECUTOR_", doc.Executor)),
		Effect:                    commonv1.Effect(enum(commonv1.Effect_value, "EFFECT_", doc.Effect)),
		Role:                      commonv1.Role(enum(commonv1.Role_value, "ROLE_", doc.Role)),
		Output:                    commonv1.ModelOutput(enum(commonv1.ModelOutput_value, "MODEL_OUTPUT_", doc.Output)),
		OutputSchemaId:            doc.OutputSchemaID,
		RollbackTemplate:          doc.RollbackTemplate,
		SupportsIdempotencyLookup: doc.SupportsIdempotencyLookup,
	}
	if len(doc.ParamsSchema) > 0 {
		var v structpb.Value
		if err := protojson.Unmarshal(doc.ParamsSchema, &v); err != nil {
			return nil, providererr.Wrap(providererr.Invalid, "corrupt_document", "存下的 params_schema 解析不了", err)
		}
		t.ParamsSchema = &commonv1.JsonPayload{Value: &v}
	}
	if len(doc.Defaults) > 0 {
		var s structpb.Struct
		if err := protojson.Unmarshal(doc.Defaults, &s); err != nil {
			return nil, providererr.Wrap(providererr.Invalid, "corrupt_document", "存下的 defaults 解析不了", err)
		}
		t.Defaults = &s
	}
	return t, nil
}

// --- 分页 ---

const pageTokenPrefix = "offset:"

// paginate 按偏移量分页。令牌是不透明的字符串；伪造的令牌是 invalid。
func paginate[T any](items []T, size int32, token string) ([]T, string, error) {
	n := int(size)
	if n <= 0 {
		n = defaultPageSize
	}
	n = min(n, maxPageSize)
	start := 0
	if token != "" {
		raw, err := base64.RawURLEncoding.DecodeString(token)
		rest, ok := strings.CutPrefix(string(raw), pageTokenPrefix)
		offset, convErr := strconv.Atoi(rest)
		if err != nil || !ok || convErr != nil || offset < 0 || offset > len(items) {
			return nil, "", invalid("invalid_page_token", "分页令牌不对")
		}
		start = offset
	}
	end := min(start+n, len(items))
	next := ""
	if end < len(items) {
		next = base64.RawURLEncoding.EncodeToString([]byte(pageTokenPrefix + strconv.Itoa(end)))
	}
	return items[start:end], next, nil
}
