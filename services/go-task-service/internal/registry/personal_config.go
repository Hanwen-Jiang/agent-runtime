package registry

import (
	"context"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/personalconfig"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	pb "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/personalconfig/v1"
	rg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"
	"strings"
)

const personalKind = rg.RegistryKind_REGISTRY_KIND_PERSONAL_CONFIG

func personalEntry(e entry) (*pb.PersonalConfig, error) {
	doc := &pb.PersonalConfigDocument{}
	if err := protojson.Unmarshal([]byte(e.document), doc); err != nil {
		return nil, invalid("invalid_document", "个人配置正文损坏")
	}
	return &pb.PersonalConfig{Version: "user/" + e.label(), Digest: personalconfig.Digest([]byte(e.document)), Document: doc}, nil
}
func (r *Registry) GetPersonalConfig(ctx context.Context, t *commonv1.TenantContext, version string) (*pb.PersonalConfig, error) {
	user, err := skillUser(t)
	if err != nil {
		return nil, err
	}
	v := int64(0)
	if version != "" {
		if !strings.HasPrefix(version, "user/") {
			return nil, invalid("invalid_version", "版本必须是 user/vN")
		}
		v, err = parseVersion(strings.TrimPrefix(version, "user/"))
		if err != nil || v == 0 {
			return nil, invalid("invalid_version", "版本必须是 user/vN")
		}
	}
	scope, err := r.tenantScope(t)
	if err != nil {
		return nil, err
	}
	e, ok, err := scope.get(ctx, personalKind, personalconfig.StoredName(user), v)
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, notFound("personal_config_not_found", "没有这个个人配置")
	}
	return personalEntry(e)
}
func (r *Registry) PutPersonalConfig(ctx context.Context, t *commonv1.TenantContext, doc *pb.PersonalConfigDocument, expected string) (*pb.PersonalConfig, bool, error) {
	user, err := skillUser(t)
	if err != nil {
		return nil, false, err
	}
	body, err := personalconfig.Canonical(doc)
	if err != nil {
		return nil, false, err
	}
	scope, err := r.tenantScope(t)
	if err != nil {
		return nil, false, err
	}
	name := personalconfig.StoredName(user)
	var output entry
	created := false
	err = retry(ctx, func() error {
		return scope.st.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
			// Read the document and version counter from one database snapshot.
			// A separate MAX(version) read can observe a concurrent insert that
			// the earlier default read missed and publish identical data again.
			row, ok, e := tx.QueryOne(ctx, `SELECT `+entryColumns+` FROM registry_entries WHERE kind = $1 AND name = $2 ORDER BY version DESC LIMIT 1`, int64(personalKind), name)
			if e != nil {
				return e
			}
			current := ""
			latest := int64(0)
			active := false
			if ok {
				output = decodeEntry(row)
				latest = output.version
				active = output.state == rg.ReleaseState_RELEASE_STATE_DEFAULT
				if active {
					current = "user/" + output.label()
				}
			}
			if expected != "" && expected != current {
				return providererr.New(providererr.Conflict, "personal_config_version_conflict", "个人配置已更新，请重新读取")
			}
			if active && output.document == string(body) {
				created = false
				return nil
			}
			now := r.clock.Now().UnixMilli()
			if active {
				affected, e := tx.Execute(ctx, `UPDATE registry_entries SET state = $1, updated_at = $2 WHERE kind = $3 AND name = $4 AND state = $5 AND version = $6`, int64(rg.ReleaseState_RELEASE_STATE_RETIRED), now, int64(personalKind), name, int64(rg.ReleaseState_RELEASE_STATE_DEFAULT), latest)
				if e != nil {
					return e
				}
				if affected != 1 {
					return providererr.New(providererr.Conflict, "personal_config_version_conflict", "个人配置在写入期间变化，请重试")
				}
			}
			output = entry{kind: personalKind, name: name, version: latest + 1, state: rg.ReleaseState_RELEASE_STATE_DEFAULT, document: string(body)}
			_, e = tx.Execute(ctx, `INSERT INTO registry_entries (kind,name,version,state,task_family,document,change_note,evaluation,created_at,updated_at) VALUES ($1,$2,$3,$4,'',$5,'personal config','',$6,$6)`, int64(personalKind), name, output.version, int64(output.state), output.document, now)
			created = e == nil
			return e
		})
	})
	if err != nil {
		return nil, false, err
	}
	out, err := personalEntry(output)
	return out, created, err
}
func (r *Registry) RetirePersonalConfig(ctx context.Context, t *commonv1.TenantContext) (bool, error) {
	user, err := skillUser(t)
	if err != nil {
		return false, err
	}
	scope, err := r.tenantScope(t)
	if err != nil {
		return false, err
	}
	var changed int64
	err = scope.st.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		var e error
		changed, e = tx.Execute(ctx, `UPDATE registry_entries SET state = $1, updated_at = $2 WHERE kind = $3 AND name = $4 AND state = $5`, int64(rg.ReleaseState_RELEASE_STATE_RETIRED), r.clock.Now().UnixMilli(), int64(personalKind), personalconfig.StoredName(user), int64(rg.ReleaseState_RELEASE_STATE_DEFAULT))
		return e
	})
	return changed > 0, err
}

// PinPersonalConfig freezes presence as well as absence once, including submit retries.
func (r *Registry) PinPersonalConfig(ctx context.Context, t *commonv1.TenantContext, taskID, user string, inputs ...*structpb.Struct) error {
	scope, err := r.tenantScope(t)
	if err != nil {
		return err
	}
	return scope.st.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		task, ok, e := tx.QueryOne(ctx, `SELECT personal_config_pin, submitted_by FROM tasks WHERE task_id = $1`, taskID)
		if e != nil {
			return e
		}
		if !ok || task["submitted_by"] != user {
			return providererr.New(providererr.NotFound, "task_not_found", "没有这个 Task")
		}
		if task["personal_config_pin"] != "" {
			return nil
		}
		value := personalconfig.NoConfig
		var defaults *pb.PersonalConfigDefaults
		if user != "" {
			row, found, e := tx.QueryOne(ctx, `SELECT `+entryColumns+` FROM registry_entries WHERE kind = $1 AND name = $2 AND state = $3`, int64(personalKind), personalconfig.StoredName(user), int64(rg.ReleaseState_RELEASE_STATE_DEFAULT))
			if e != nil {
				return e
			}
			if found {
				entry := decodeEntry(row)
				config, e := personalEntry(entry)
				if e != nil {
					return e
				}
				defaults = config.Document.Defaults
				raw, _ := json.Marshal(personalconfig.Pin{Version: "user/" + entry.label(), Digest: personalconfig.Digest([]byte(entry.document))})
				value = string(raw)
			}
		}
		var input *structpb.Struct
		if len(inputs) > 0 {
			input = inputs[0]
		}
		selection, e := personalconfig.Select(defaults, input)
		if e != nil {
			return e
		}
		selectionJSON, _ := json.Marshal(selection)
		_, e = tx.Execute(ctx, `UPDATE tasks SET personal_config_pin = $1, personal_config_selection = $2 WHERE task_id = $3 AND personal_config_pin = ''`, value, string(selectionJSON), taskID)
		return e
	})
}
