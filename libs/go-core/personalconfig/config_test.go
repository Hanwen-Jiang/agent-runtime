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

func TestCanonicalDigestIsStableAcrossOrderingAndRepeatedSaves(t *testing.T) {
	first := &pb.PersonalConfigDocument{Instructions: "same", Files: []*pb.PersonalConfigFile{{Path: "z", Content: "z"}, {Path: "a", Content: "a"}}}
	second := &pb.PersonalConfigDocument{Instructions: "same", Files: []*pb.PersonalConfigFile{{Path: "a", Content: "a"}, {Path: "z", Content: "z"}}}
	want, err := Canonical(first)
	if err != nil {
		t.Fatal(err)
	}
	for range 100 {
		got, err := Canonical(second)
		if err != nil || string(got) != string(want) {
			t.Fatalf("unstable content: %s %s %v", want, got, err)
		}
	}
	if first.Files[0].Path != "z" {
		t.Fatal("canonicalization mutated input")
	}
}

func TestCommonPlaintextCredentialFormatsAreRejected(t *testing.T) {
	cases := map[string]string{
		"github_fine_grained":  "token=github_pat_" + strings.Repeat("a", 22) + "_" + strings.Repeat("b", 59),
		"gitlab_pat":           "token=glpat-" + strings.Repeat("a", 20),
		"npm_basic":            "//registry.example/:_auth=YWxpY2U6c2VjcmV0",
		"authorization_bearer": "Authorization: Bearer opaque-value",
		"authorization_basic":  "header = 'Authorization: Basic YWxpY2U6c2VjcmV0'",
		"json_authorization":   `{"Authorization":"Bearer opaque-value", "api_key":"${API_KEY}"}`,
		"url_password":         "index-url=https://alice:secret@registry.example/simple",
		"url_username_token":   "registry=https://opaque-token@registry.example",
		"url_encoded_password": "registry=https://alice:%73ecret@registry.example",
		"url_mixed_reference":  "registry=https://${USER}:plain-password@registry.example",
	}
	for name, content := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := Canonical(&pb.PersonalConfigDocument{Files: []*pb.PersonalConfigFile{{Path: ".config/tool/preferences", Content: content}}})
			if err == nil {
				t.Fatal("accepted explicit plaintext credential format")
			}
			if strings.Contains(err.Error(), content) {
				t.Fatal("credential echoed in validation error")
			}
		})
	}
}

func TestCredentialVariableReferencesRemainConfiguration(t *testing.T) {
	cases := map[string]string{
		"npm_token":             "//registry.example/:_authToken=${NPM_TOKEN}",
		"npm_basic":             "//registry.example/:_auth='${NPM_AUTH}'",
		"authorization_bearer":  "Authorization: Bearer ${ACCESS_TOKEN}",
		"authorization_basic":   "Authorization = 'Basic ${BASIC_AUTH}'",
		"json_authorization":    `{"Authorization":"Bearer ${ACCESS_TOKEN}", "api_key":"${API_KEY}"}`,
		"url_password":          "index-url=https://oauth2:${REGISTRY_TOKEN}@registry.example/simple",
		"url_username_token":    "registry=https://${REGISTRY_TOKEN}@registry.example",
		"url_both_references":   "registry=https://${USERNAME}:${PASSWORD}@registry.example",
		"url_encoded_reference": "registry=https://oauth2:%24%7BREGISTRY_TOKEN%7D@registry.example",
		"named_references":      "api_key=${secret:api-key}\nregistry=https://${identity:user}:${secret:registry-token}@registry.example",
		"ordinary_text":         "theme=dark\n# password is configured elsewhere\ndescription=Basic authentication is available\nregistry=https://registry.example/",
	}
	for name, content := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := Canonical(&pb.PersonalConfigDocument{Files: []*pb.PersonalConfigFile{{Path: ".npmrc", Content: content}}}); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestConfigurationPathsStayInsideNonExecutableHomeFiles(t *testing.T) {
	for _, p := range []string{"/etc/passwd", "../escape", "a/../../escape", "a//b", `C:\escape`, ".SSH/config", ".Aws/credentials", ".Git/config", ".netrc", ".bashrc", ".bash_login", ".bash_logout", ".Zshenv", ".zlogin", ".zlogout", "a/boot.PS1", "x\x00y"} {
		t.Run(strings.ReplaceAll(p, "/", "_"), func(t *testing.T) {
			if _, err := Canonical(&pb.PersonalConfigDocument{Files: []*pb.PersonalConfigFile{{Path: p, Content: "non-secret"}}}); err == nil {
				t.Fatal("accepted reserved or escaping path")
			}
		})
	}
}
