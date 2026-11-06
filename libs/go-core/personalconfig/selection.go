package personalconfig

import (
	"context"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	pb "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/personalconfig/v1"
	"google.golang.org/protobuf/types/known/structpb"
)

// nil means legacy all-visible; an explicit empty list means select none.
type Selection struct {
	SkillNames       []string `json:"skill_names"`
	MCPConnectionIDs []string `json:"mcp_connection_ids"`
	EnvironmentID    string   `json:"environment_id,omitempty"`
}

func Select(defaults *pb.PersonalConfigDefaults, inputs *structpb.Struct) (Selection, error) {
	out := Selection{}
	if defaults != nil {
		if len(defaults.SkillNames) > 0 {
			out.SkillNames = append([]string{}, defaults.SkillNames...)
		}
		if len(defaults.McpConnectionIds) > 0 {
			out.MCPConnectionIDs = append([]string{}, defaults.McpConnectionIds...)
		}
		out.EnvironmentID = defaults.EnvironmentId
	}
	value, exists := inputs.GetFields()["personal_config"]
	if !exists {
		return out, nil
	}
	object := value.GetStructValue()
	if object == nil {
		return out, invalid("invalid_personal_selection")
	}
	for key, value := range object.Fields {
		switch key {
		case "skill_names", "mcp_connection_ids":
			list := value.GetListValue()
			if list == nil || len(list.Values) > 100 {
				return out, invalid("invalid_personal_selection")
			}
			names := []string{}
			seen := map[string]bool{}
			for _, v := range list.Values {
				name := v.GetStringValue()
				if !reference.MatchString(name) || seen[name] {
					return out, invalid("invalid_personal_selection")
				}
				seen[name] = true
				names = append(names, name)
			}
			if key == "skill_names" {
				out.SkillNames = names
			} else {
				out.MCPConnectionIDs = names
			}
		case "environment_id":
			if _, ok := value.Kind.(*structpb.Value_StringValue); !ok {
				return out, invalid("invalid_personal_selection")
			}
			out.EnvironmentID = value.GetStringValue()
			if out.EnvironmentID != "" && !reference.MatchString(out.EnvironmentID) {
				return out, invalid("invalid_personal_selection")
			}
		default:
			return out, invalid("invalid_personal_selection")
		}
	}
	return out, nil
}
func LoadSelection(ctx context.Context, store relational.Provider, tenant *commonv1.TenantContext, taskID string) (Selection, error) {
	db, err := store.Bind(tenant)
	if err != nil {
		return Selection{}, err
	}
	out := Selection{}
	err = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		row, ok, e := tx.QueryOne(ctx, `SELECT personal_config_selection FROM tasks WHERE task_id=$1`, taskID)
		if e != nil || !ok {
			return e
		}
		text, _ := row["personal_config_selection"].(string)
		if text != "" {
			if json.Unmarshal([]byte(text), &out) != nil {
				return invalid("invalid_personal_selection")
			}
		}
		return nil
	})
	return out, err
}
func FilterSkills(visible map[string]string, selected []string) (map[string]string, error) {
	if selected == nil {
		return visible, nil
	}
	out := map[string]string{}
	for _, name := range selected {
		version, ok := visible[name]
		if !ok {
			return nil, providererr.New(providererr.Invalid, "personal_skill_unavailable", "个人配置引用的 Skill 不在当前可用目录："+name)
		}
		out[name] = version
	}
	return out, nil
}
