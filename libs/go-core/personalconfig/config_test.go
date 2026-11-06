package personalconfig

import (
	pb "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/personalconfig/v1"
	"google.golang.org/protobuf/types/known/structpb"
	"strings"
	"testing"
)

func TestRejectPackageBeforeWriting(t *testing.T) {
	cases := []*pb.PersonalConfigDocument{
		{Instructions: strings.Repeat("x", 4097)},
		{Files: []*pb.PersonalConfigFile{{Path: "ok.md", Content: "fine"}, {Path: "../escape", Content: "no"}}},
		{Files: []*pb.PersonalConfigFile{{Path: ".ssh/config", Content: "no"}}},
		{Files: []*pb.PersonalConfigFile{{Path: "a", Content: "x"}, {Path: "a/b", Content: "x"}}},
		{Files: []*pb.PersonalConfigFile{{Path: "config", Content: "api_key=sk-abcdefghijklmnopqrstuvwxyz"}}},
		{Files: []*pb.PersonalConfigFile{{Path: "start.sh", Content: "echo x"}}},
		{Files: []*pb.PersonalConfigFile{{Path: "config", Content: strings.Repeat("x", 256<<10+1)}}},
	}
	for i, doc := range cases {
		if _, err := Canonical(doc); err == nil {
			t.Errorf("accepted case %d", i)
		}
	}
	doc := &pb.PersonalConfigDocument{Files: []*pb.PersonalConfigFile{{Path: ".npmrc", Content: "//registry.example/:_authToken=${NPM_TOKEN}"}, {Path: ".config/tool/config.toml", Content: "theme='dark'"}}}
	if _, err := Canonical(doc); err != nil {
		t.Fatal(err)
	}
}
func TestDefaultSelectionAndExplicitOverride(t *testing.T) {
	defaults := &pb.PersonalConfigDefaults{SkillNames: []string{"one"}, McpConnectionIds: []string{"mc1"}}
	selected, err := Select(defaults, nil)
	if err != nil {
		t.Fatal(err)
	}
	pins, err := FilterSkills(map[string]string{"one": "user/v1", "two": "platform/v2"}, selected.SkillNames)
	if err != nil || len(pins) != 1 || pins["one"] != "user/v1" {
		t.Fatalf("filter: %v %v", pins, err)
	}
	if _, err = FilterSkills(pins, []string{"foreign"}); err == nil {
		t.Fatal("expanded authority")
	}
	override, _ := structpb.NewStruct(map[string]any{"personal_config": map[string]any{"skill_names": []any{}, "mcp_connection_ids": []any{"mc2"}}})
	selected, err = Select(defaults, override)
	if err != nil || selected.SkillNames == nil || len(selected.SkillNames) != 0 || selected.MCPConnectionIDs[0] != "mc2" {
		t.Fatalf("override: %v %v", selected, err)
	}
	legacy, err := Select(nil, nil)
	if err != nil || legacy.SkillNames != nil || legacy.MCPConnectionIDs != nil {
		t.Fatal("legacy changed")
	}
}
