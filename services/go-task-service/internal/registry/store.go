package registry

import (
	"context"
	"sort"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
)

// entry 是 registry_entries 的一行：一个版本。
type entry struct {
	kind       registryv1.RegistryKind
	name       string
	version    int64
	state      registryv1.ReleaseState
	taskFamily string
	// document 是规范化的 JSON（键排序），发布后不再修改。
	document string
}

func (e entry) label() string { return versionLabel(e.version) }

// ref 是 name@vN。
func (e entry) ref() string { return e.name + "@" + e.label() }

const entryColumns = `kind, name, version, state, task_family, document`

func asInt(v any) int64 {
	switch n := v.(type) {
	case int64:
		return n
	case float64:
		return int64(n)
	}
	return 0
}

func decodeEntry(row relational.Row) entry {
	str := func(k string) string { s, _ := row[k].(string); return s }
	return entry{
		kind:       registryv1.RegistryKind(asInt(row["kind"])),
		name:       str("name"),
		version:    asInt(row["version"]),
		state:      registryv1.ReleaseState(asInt(row["state"])),
		taskFamily: str("task_family"),
		document:   str("document"),
	}
}

// scope 是绑定了作用域的业务库。
type scope struct {
	st       relational.Store
	platform bool
}

func (r *Registry) platformScope() (scope, error) {
	st, err := r.store.BindPlatform(platformPurpose)
	if err != nil {
		return scope{}, err
	}
	return scope{st: st, platform: true}, nil
}

func (r *Registry) tenantScope(tenant *commonv1.TenantContext) (scope, error) {
	st, err := r.store.Bind(tenant)
	if err != nil {
		return scope{}, err
	}
	return scope{st: st}, nil
}

func (s scope) read(ctx context.Context, fn func(context.Context, relational.Tx) error) error {
	return s.st.Transaction(ctx, relational.TxOptions{ReadOnly: true}, fn)
}

// get 取一个版本；version 为 0 时取默认版本。
func (s scope) get(ctx context.Context, kind registryv1.RegistryKind, name string, version int64) (entry, bool, error) {
	var out entry
	var found bool
	err := s.read(ctx, func(ctx context.Context, tx relational.Tx) error {
		var row relational.Row
		var err error
		if version == 0 {
			row, found, err = tx.QueryOne(ctx, `SELECT `+entryColumns+` FROM registry_entries WHERE kind = $1 AND name = $2 AND state = $3`,
				int64(kind), name, int64(registryv1.ReleaseState_RELEASE_STATE_DEFAULT))
		} else {
			row, found, err = tx.QueryOne(ctx, `SELECT `+entryColumns+` FROM registry_entries WHERE kind = $1 AND name = $2 AND version = $3`,
				int64(kind), name, version)
		}
		if err == nil && found {
			out = decodeEntry(row)
		}
		return err
	})
	return out, found, err
}

// exists 判断名字是否发布过（任何版本、任何状态）。
func (s scope) exists(ctx context.Context, kind registryv1.RegistryKind, name string) (bool, error) {
	var found bool
	err := s.read(ctx, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, `SELECT version FROM registry_entries WHERE kind = $1 AND name = $2 LIMIT 1`, int64(kind), name)
		found = len(rows) > 0
		return err
	})
	return found, err
}

// names 返回发布过的名字（任何版本）。
func (s scope) names(ctx context.Context, kind registryv1.RegistryKind) (map[string]bool, error) {
	out := map[string]bool{}
	err := s.read(ctx, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, `SELECT DISTINCT name FROM registry_entries WHERE kind = $1`, int64(kind))
		for _, row := range rows {
			name, _ := row["name"].(string)
			out[name] = true
		}
		return err
	})
	return out, err
}

// defaults 返回这一种的全部默认版本，按名字排序。
func (s scope) defaults(ctx context.Context, kind registryv1.RegistryKind) ([]entry, error) {
	var out []entry
	err := s.read(ctx, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, `SELECT `+entryColumns+` FROM registry_entries WHERE kind = $1 AND state = $2`,
			int64(kind), int64(registryv1.ReleaseState_RELEASE_STATE_DEFAULT))
		for _, row := range rows {
			out = append(out, decodeEntry(row))
		}
		return err
	})
	sort.Slice(out, func(i, j int) bool { return out[i].name < out[j].name })
	return out, err
}

// maxAttempts 是写入遇到版本号冲突或临时错误时的尝试次数。
const maxAttempts = 20

// retry 在版本号冲突（并发发布）与临时错误（数据库忙、序列化失败）时重试 fn。
func retry(ctx context.Context, fn func() error) error {
	for attempt := 1; ; attempt++ {
		err := fn()
		category := providererr.CategoryOf(err)
		if err == nil || attempt == maxAttempts || (category != providererr.Conflict && category != providererr.Transient) {
			return err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Duration(attempt) * 5 * time.Millisecond):
		}
	}
}

// insert 发布一个新版本：编号为已有最大版本号加一。immediate 时直接成为默认版本，原默认退役（租户策略）。
func (s scope) insert(ctx context.Context, c candidate, note, evaluation string, now time.Time) (entry, error) {
	var out entry
	err := retry(ctx, func() error {
		return s.st.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
			row, _, err := tx.QueryOne(ctx, `SELECT COALESCE(MAX(version), 0) AS latest FROM registry_entries WHERE kind = $1 AND name = $2`,
				int64(c.kind), c.name)
			if err != nil {
				return err
			}
			state := registryv1.ReleaseState_RELEASE_STATE_SHADOW
			if c.spec.immediate {
				state = registryv1.ReleaseState_RELEASE_STATE_DEFAULT
				if _, err := tx.Execute(ctx, `UPDATE registry_entries SET state = $1, updated_at = $2 WHERE kind = $3 AND name = $4 AND state = $5`,
					int64(registryv1.ReleaseState_RELEASE_STATE_RETIRED), now.UnixMilli(), int64(c.kind), c.name, int64(registryv1.ReleaseState_RELEASE_STATE_DEFAULT)); err != nil {
					return err
				}
			}
			out = entry{kind: c.kind, name: c.name, version: asInt(row["latest"]) + 1, state: state, taskFamily: c.taskFamily, document: string(c.document)}
			_, err = tx.Execute(ctx, `INSERT INTO registry_entries (kind, name, version, state, task_family, document, change_note, evaluation, created_at, updated_at)
				VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
				int64(out.kind), out.name, out.version, int64(out.state), out.taskFamily, out.document, note, evaluation, now.UnixMilli(), now.UnixMilli())
			return err
		})
	})
	return out, err
}

// promote 把一个版本设为默认，原默认退役；evaluation 是原样存下的评测证据。返回原默认版本（没有时为空）。
func (s scope) promote(ctx context.Context, kind registryv1.RegistryKind, name string, version int64, evaluation string, now time.Time, expectedDefault ...string) (string, error) {
	var previous string
	err := retry(ctx, func() error {
		previous = ""
		return s.st.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
			_, found, err := tx.QueryOne(ctx, `SELECT version FROM registry_entries WHERE kind = $1 AND name = $2 AND version = $3`, int64(kind), name, version)
			if err != nil {
				return err
			}
			if !found {
				return notFound("version_not_found", "没有这个版本")
			}
			row, found, err := tx.QueryOne(ctx, `SELECT version FROM registry_entries WHERE kind = $1 AND name = $2 AND state = $3`,
				int64(kind), name, int64(registryv1.ReleaseState_RELEASE_STATE_DEFAULT))
			if err != nil {
				return err
			}
			if found {
				previous = versionLabel(asInt(row["version"]))
			}
			if len(expectedDefault) > 0 && previous != expectedDefault[0] {
				return invalid("capability_baseline_changed", "能力分的现行基线版本已变化")
			}
			if found {
				if _, err := tx.Execute(ctx, `UPDATE registry_entries SET state = $1, updated_at = $2 WHERE kind = $3 AND name = $4 AND state = $5`,
					int64(registryv1.ReleaseState_RELEASE_STATE_RETIRED), now.UnixMilli(), int64(kind), name, int64(registryv1.ReleaseState_RELEASE_STATE_DEFAULT)); err != nil {
					return err
				}
			}
			_, err = tx.Execute(ctx, `UPDATE registry_entries SET state = $1, evaluation = $2, updated_at = $3 WHERE kind = $4 AND name = $5 AND version = $6`,
				int64(registryv1.ReleaseState_RELEASE_STATE_DEFAULT), evaluation, now.UnixMilli(), int64(kind), name, version)
			return err
		})
	})
	return previous, err
}
