package registry

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
)

// SeedEntry 是开发种子文件（task-service registry-seed）里的一条。
type SeedEntry struct {
	// Kind 取 node_template、recipe、model_profile、tenant_policy、task_families、skill。
	Kind string `json:"kind"`
	// Tenant 只用于租户作用域的 Recipe、Skill 与租户策略。
	Tenant   string          `json:"tenant"`
	Name     string          `json:"name"`
	Document json.RawMessage `json:"document"`
}

// SeedResult 是一条的结果。
type SeedResult struct {
	Entry   SeedEntry
	Version string
	// Skipped：与当前默认版本内容相同，没有发布。
	Skipped bool
}

var seedKinds = map[string]registryv1.RegistryKind{
	"node_template": kindTemplate,
	"recipe":        kindRecipe,
	"model_profile": kindProfile,
	"tenant_policy": kindPolicy,
	"task_families": kindFamilies,
	"skill":         kindSkill,
}

// seedEvidence 是开发种子设为默认时记下的“证据”：说明它没经过评测。
const seedEvidence = `{"source":"registry-seed"}`

// Seed 把一批文档发布并设为默认（开发与端到端测试用，不走发布令牌与评测证据）。先全部做不读业务库的校验，
// 有一条不合格就一条也不写；再按顺序写入（Recipe 引用的模板要排在它前面）。与当前默认版本内容相同的条目跳过，
// 所以重复运行是幂等的。错误信息指出是第几条、哪个名字。
func (r *Registry) Seed(ctx context.Context, entries []SeedEntry) ([]SeedResult, error) {
	candidates := make([]candidate, 0, len(entries))
	for i, e := range entries {
		c, err := r.prepareSeed(e)
		if err != nil {
			return nil, labelled(i, e, err)
		}
		candidates = append(candidates, c)
	}
	results := make([]SeedResult, 0, len(entries))
	for i, c := range candidates {
		current, found, err := c.scope.get(ctx, c.kind, c.name, 0)
		if err != nil {
			return results, labelled(i, entries[i], err)
		}
		if found && current.document == string(c.document) {
			results = append(results, SeedResult{Entry: entries[i], Version: current.label(), Skipped: true})
			continue
		}
		e, err := r.write(ctx, c, "registry-seed")
		if err != nil {
			return results, labelled(i, entries[i], err)
		}
		if !c.spec.immediate {
			if _, err := c.scope.promote(ctx, c.kind, c.name, e.version, seedEvidence, r.clock.Now()); err != nil {
				return results, labelled(i, entries[i], err)
			}
		}
		results = append(results, SeedResult{Entry: entries[i], Version: e.label()})
	}
	return results, nil
}

func (r *Registry) prepareSeed(e SeedEntry) (candidate, error) {
	kind, ok := seedKinds[e.Kind]
	if !ok {
		return candidate{}, invalid("unsupported_kind", fmt.Sprintf("不认识的 kind %q", e.Kind))
	}
	var tenant *commonv1.TenantContext
	if e.Tenant != "" {
		tenant = &commonv1.TenantContext{TenantId: e.Tenant}
	}
	if len(e.Document) == 0 {
		return candidate{}, invalid("missing_document", "缺少 document")
	}
	raw, err := canonical(e.Document)
	if err != nil {
		return candidate{}, err
	}
	return r.prepare(kind, tenant, e.Name, raw)
}

// labelled 在错误说明前加上“第 i 条（kind name）”，类别不变。
func labelled(i int, e SeedEntry, err error) error {
	label := fmt.Sprintf("第 %d 条（%s %s）", i+1, e.Kind, e.Name)
	var pe *providererr.Error
	if errors.As(err, &pe) {
		return providererr.Wrap(pe.Category, pe.Code, label+"："+pe.Message, err)
	}
	return fmt.Errorf("%s：%w", label, err)
}
