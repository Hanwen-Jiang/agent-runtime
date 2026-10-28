package registry

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/santhosh-tekuri/jsonschema/v6"
	"google.golang.org/protobuf/encoding/protojson"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
)

// evaluationSchema 是 Promote 的评测证据的 schema。
const evaluationSchema = "registry_evaluation"

// schemaSet 是编译好的 /schemas：Registry 用到的几个 schema 与它们的 $id。
type schemaSet struct {
	compiled map[string]*jsonschema.Schema
	ids      map[string]string
}

// loadSchemas 把目录里的 *.schema.json 都登记进编译器（按各自的 $id，互相 $ref 才解析得了），再编译 Registry 用到的几个。
// 格式（date、date-time）按断言处理，与契约测试的 Python 校验一致。
func loadSchemas(dir string) (*schemaSet, error) {
	files, err := filepath.Glob(filepath.Join(dir, "*.schema.json"))
	if err != nil || len(files) == 0 {
		return nil, fmt.Errorf("registry：在 %s 找不到 schema（*.schema.json）", dir)
	}
	c := jsonschema.NewCompiler()
	c.AssertFormat()
	set := &schemaSet{compiled: map[string]*jsonschema.Schema{}, ids: map[string]string{}}
	for _, f := range files {
		data, err := os.ReadFile(f)
		if err != nil {
			return nil, fmt.Errorf("registry：读不了 %s：%w", f, err)
		}
		doc, err := jsonschema.UnmarshalJSON(bytes.NewReader(data))
		if err != nil {
			return nil, fmt.Errorf("registry：%s 不是合法的 JSON：%w", f, err)
		}
		obj, _ := doc.(map[string]any)
		id, _ := obj["$id"].(string)
		if id == "" {
			return nil, fmt.Errorf("registry：%s 没有 $id", f)
		}
		if err := c.AddResource(id, doc); err != nil {
			return nil, fmt.Errorf("registry：登记 %s 失败：%w", f, err)
		}
		set.ids[strings.TrimSuffix(filepath.Base(f), ".schema.json")] = id
	}
	names := []string{evaluationSchema}
	for _, spec := range kinds {
		names = append(names, spec.schema)
	}
	for _, name := range names {
		id, ok := set.ids[name]
		if !ok {
			return nil, fmt.Errorf("registry：%s 里缺 %s.schema.json", dir, name)
		}
		sch, err := c.Compile(id)
		if err != nil {
			return nil, fmt.Errorf("registry：编译 %s 失败：%w", name, err)
		}
		set.compiled[name] = sch
	}
	return set, nil
}

// validate 按 schema 校验一份规范化的 JSON；不合格时返回 Invalid，说明里带校验器指出的位置。
func (s *schemaSet) validate(name string, raw []byte) error {
	inst, err := jsonschema.UnmarshalJSON(bytes.NewReader(raw))
	if err != nil {
		return invalid("invalid_document", "document 不是合法的 JSON")
	}
	if err := s.compiled[name].Validate(inst); err != nil {
		msg := err.Error()
		if len(msg) > 800 {
			msg = msg[:800] + "…"
		}
		return invalid("invalid_document", fmt.Sprintf("document 不合 %s.schema.json：%s", name, msg))
	}
	return nil
}

// canonical 把 JSON 规范化：对象的键排序，数字保留原样。
func canonical(raw []byte) ([]byte, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, invalid("invalid_document", "document 不是合法的 JSON")
	}
	if dec.More() {
		return nil, invalid("invalid_document", "document 后面还有多余的内容")
	}
	return json.Marshal(v)
}

// payloadJSON 取出 JsonPayload 的值，规范化成 JSON。schema_id 不为空时必须是 want。
func payloadJSON(p *commonv1.JsonPayload, what, want string) ([]byte, error) {
	if p.GetValue() == nil {
		return nil, invalid("missing_"+what, "缺少 "+what)
	}
	if id := p.GetSchemaId(); id != "" && id != want {
		return nil, invalid("schema_mismatch", fmt.Sprintf("%s 的 schema_id 应为 %s", what, want))
	}
	raw, err := protojson.Marshal(p.GetValue())
	if err != nil {
		return nil, invalid("invalid_"+what, what+" 不是合法的 JSON")
	}
	return canonical(raw)
}

// --- 发布前的检查 ---

var (
	// namePattern 是 Node Template 与 Recipe 的名字，与 defs.schema.json 里 versioned_ref 的名字部分一致。
	namePattern = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)
	refPattern  = regexp.MustCompile(`^([a-z][a-z0-9_]*)@v([0-9]+)$`)
)

var effectRank = map[string]int{"none": 0, "read": 1, "write_reversible": 2, "write_irreversible": 3}

func isWrite(effect string) bool { return effectRank[effect] >= effectRank["write_reversible"] }

// parseRef 解析 name@vN。
func parseRef(ref string) (string, int64, error) {
	m := refPattern.FindStringSubmatch(ref)
	if m == nil {
		return "", 0, invalid("invalid_ref", fmt.Sprintf("引用 %q 要写成 name@vN", ref))
	}
	version, err := parseVersion("v" + m[2])
	if err != nil {
		return "", 0, err
	}
	return m[1], version, nil
}

// candidate 是校验过格式、等待写入的一个版本。
type candidate struct {
	kind       registryv1.RegistryKind
	spec       kindSpec
	scope      scope
	name       string
	document   []byte
	taskFamily string
}

// writeScope 按种类定发布的作用域：平台专属的不能带租户，租户策略必须带租户，Recipe 不带租户是平台的。
func (r *Registry) writeScope(spec kindSpec, tenant *commonv1.TenantContext) (scope, error) {
	present := tenant.GetTenantId() != ""
	switch {
	case spec.scope == platformOnly && present:
		return scope{}, invalid("platform_only", "这一种只在平台作用域，发布时不带租户")
	case spec.scope == tenantOnly && !present:
		return scope{}, invalid("tenant_required", "租户策略要带租户")
	case present:
		return r.tenantScope(tenant)
	default:
		return r.platformScope()
	}
}

// prepare 做不读业务库的检查：种类、作用域、schema、名字；document 是规范化的 JSON。
func (r *Registry) prepare(kind registryv1.RegistryKind, tenant *commonv1.TenantContext, name string, document []byte) (candidate, error) {
	spec, err := specOf(kind)
	if err != nil {
		return candidate{}, err
	}
	var sc scope
	if kind == registryv1.RegistryKind_REGISTRY_KIND_TENANT_POLICY && tenant.GetTenantId() == "" {
		// 不带租户的租户策略只有一份：平台默认（DefaultTenantPolicy），给没有自己策略的租户用。
		if name != DefaultTenantPolicy {
			return candidate{}, invalid("tenant_required", "租户策略要带租户；不带租户的只能是平台默认的，名字为 "+DefaultTenantPolicy)
		}
		sc, err = r.platformScope()
	} else {
		sc, err = r.writeScope(spec, tenant)
	}
	if err != nil {
		return candidate{}, err
	}
	if err := r.schemas.validate(spec.schema, document); err != nil {
		return candidate{}, err
	}
	c := candidate{kind: kind, spec: spec, scope: sc, name: name, document: document}
	switch kind {
	case registryv1.RegistryKind_REGISTRY_KIND_NODE_TEMPLATE, registryv1.RegistryKind_REGISTRY_KIND_RECIPE:
		if !namePattern.MatchString(name) {
			return candidate{}, invalid("invalid_name", fmt.Sprintf("名字 %q 要匹配 %s", name, namePattern))
		}
	case registryv1.RegistryKind_REGISTRY_KIND_MODEL_PROFILE:
		var doc struct {
			ID    string `json:"id"`
			Tiers []struct {
				Name string `json:"name"`
			} `json:"tiers"`
		}
		if err := json.Unmarshal(document, &doc); err != nil {
			return candidate{}, invalid("invalid_document", "Model Profile 解析不了")
		}
		if name != doc.ID {
			return candidate{}, invalid("invalid_name", "Model Profile 的发布名要等于 document.id")
		}
		seen := map[string]bool{}
		for _, t := range doc.Tiers {
			if seen[t.Name] {
				return candidate{}, invalid("duplicate_tier", fmt.Sprintf("Tier 名 %q 重复", t.Name))
			}
			seen[t.Name] = true
		}
	case registryv1.RegistryKind_REGISTRY_KIND_TENANT_POLICY:
		if !sc.platform && name != "" {
			return candidate{}, invalid("invalid_name", "租户策略不起名字（一个租户一份）")
		}
	}
	if kind == registryv1.RegistryKind_REGISTRY_KIND_RECIPE {
		var doc struct {
			TaskFamily string `json:"task_family"`
		}
		_ = json.Unmarshal(document, &doc)
		c.taskFamily = doc.TaskFamily
	}
	return c, nil
}

// check 做要读业务库的检查：Recipe 不与平台同名、rollback_template 已发布、Recipe 引用的模板与 effect 规则（附录 D.4）。
func (r *Registry) check(ctx context.Context, c candidate) error {
	switch c.kind {
	case registryv1.RegistryKind_REGISTRY_KIND_NODE_TEMPLATE:
		var doc struct {
			RollbackTemplate string `json:"rollback_template"`
		}
		_ = json.Unmarshal(c.document, &doc)
		if doc.RollbackTemplate == "" {
			return nil
		}
		ok, err := c.scope.exists(ctx, registryv1.RegistryKind_REGISTRY_KIND_NODE_TEMPLATE, doc.RollbackTemplate)
		if err != nil {
			return err
		}
		if !ok {
			return invalid("rollback_template_not_found", fmt.Sprintf("回滚模板 %s 还没有发布", doc.RollbackTemplate))
		}
	case registryv1.RegistryKind_REGISTRY_KIND_RECIPE:
		platform, err := r.platformScope()
		if err != nil {
			return err
		}
		if !c.scope.platform {
			taken, err := platform.exists(ctx, registryv1.RegistryKind_REGISTRY_KIND_RECIPE, c.name)
			if err != nil {
				return err
			}
			if taken {
				return providererr.New(providererr.Conflict, "name_taken", fmt.Sprintf("平台已有名为 %s 的 Recipe", c.name))
			}
		}
		return r.checkRecipe(ctx, platform, c.document)
	}
	return nil
}

type recipeNode struct {
	ID       string   `json:"id"`
	Kind     string   `json:"kind"`
	Template string   `json:"template"`
	After    []string `json:"after"`
	Cache    *struct {
		TTLS *json.Number `json:"ttl_s"`
	} `json:"cache"`
	Retry *struct {
		Max int `json:"max"`
	} `json:"retry"`
	EffectCeiling string `json:"effect_ceiling"`
}

// checkRecipe：Node id 在整个 Recipe 里唯一；after 只引用同一 Stage 里的节点且无环；atomic 节点引用的模板版本已发布，
// 其 effect 不高于 risk，写操作不缓存、write_irreversible 不重试、read 的缓存要 TTL；Goal Node 的 effect 上限不高于 risk。
func (r *Registry) checkRecipe(ctx context.Context, platform scope, document []byte) error {
	var doc struct {
		Risk   string `json:"risk"`
		Stages []struct {
			Nodes []recipeNode `json:"nodes"`
		} `json:"stages"`
	}
	if err := json.Unmarshal(document, &doc); err != nil {
		return invalid("invalid_document", "Recipe 解析不了")
	}
	risk := effectRank[doc.Risk]
	seen := map[string]bool{}
	for si, stage := range doc.Stages {
		inStage := map[string]recipeNode{}
		for _, n := range stage.Nodes {
			if seen[n.ID] {
				return invalid("duplicate_node", fmt.Sprintf("Node id %s 重复", n.ID))
			}
			seen[n.ID] = true
			inStage[n.ID] = n
		}
		for _, n := range stage.Nodes {
			for _, up := range n.After {
				if _, ok := inStage[up]; !ok {
					return invalid("unknown_after", fmt.Sprintf("第 %d 个 Stage 的 %s 引用了本 Stage 里没有的 %s", si+1, n.ID, up))
				}
			}
			if err := r.checkRecipeNode(ctx, platform, n, risk, doc.Risk); err != nil {
				return err
			}
		}
		if cyclic(stage.Nodes) {
			return invalid("cyclic_after", fmt.Sprintf("第 %d 个 Stage 的 after 成环", si+1))
		}
	}
	return nil
}

func (r *Registry) checkRecipeNode(ctx context.Context, platform scope, n recipeNode, risk int, riskName string) error {
	if n.Kind == "goal" {
		if n.EffectCeiling != "" && effectRank[n.EffectCeiling] > risk {
			return invalid("risk_too_low", fmt.Sprintf("%s 的 effect 上限 %s 高于 Recipe 的 risk %s", n.ID, n.EffectCeiling, riskName))
		}
		return nil
	}
	name, version, err := parseRef(n.Template)
	if err != nil {
		return err
	}
	tpl, found, err := platform.get(ctx, registryv1.RegistryKind_REGISTRY_KIND_NODE_TEMPLATE, name, version)
	if err != nil {
		return err
	}
	if !found {
		return invalid("template_not_found", fmt.Sprintf("%s 引用的模板 %s 还没有发布", n.ID, n.Template))
	}
	var t struct {
		Effect string `json:"effect"`
	}
	_ = json.Unmarshal([]byte(tpl.document), &t)
	switch {
	case effectRank[t.Effect] > risk:
		return invalid("risk_too_low", fmt.Sprintf("%s 的模板 %s 是 %s，高于 Recipe 的 risk %s", n.ID, n.Template, t.Effect, riskName))
	case isWrite(t.Effect) && n.Cache != nil:
		return invalid("write_cached", fmt.Sprintf("%s 是写操作，不能缓存（附录 D.4）", n.ID))
	case t.Effect == "write_irreversible" && n.Retry != nil && n.Retry.Max > 0:
		return invalid("irreversible_retried", fmt.Sprintf("%s 是 write_irreversible，不自动重试（附录 D.4）", n.ID))
	case t.Effect == "read" && n.Cache != nil && n.Cache.TTLS == nil:
		return invalid("cache_without_ttl", fmt.Sprintf("%s 是 read，缓存要设 TTL（附录 D.4）", n.ID))
	}
	return nil
}

// cyclic 判断一个 Stage 里 after 构成的图有没有环。
func cyclic(nodes []recipeNode) bool {
	after := map[string][]string{}
	for _, n := range nodes {
		after[n.ID] = n.After
	}
	const (
		visiting = 1
		done     = 2
	)
	mark := map[string]int{}
	var visit func(id string) bool
	visit = func(id string) bool {
		switch mark[id] {
		case visiting:
			return true
		case done:
			return false
		}
		mark[id] = visiting
		for _, up := range after[id] {
			if visit(up) {
				return true
			}
		}
		mark[id] = done
		return false
	}
	for _, n := range nodes {
		if visit(n.ID) {
			return true
		}
	}
	return false
}
