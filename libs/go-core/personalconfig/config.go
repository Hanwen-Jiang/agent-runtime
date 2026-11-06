// Package personalconfig validates and reads immutable private preference packages.
package personalconfig

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	pb "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/personalconfig/v1"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"net/url"
	"path"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

const Kind int64 = 8
const Name = "personal-config"
const NoConfig = "none"

type Pin struct {
	Version string `json:"version"`
	Digest  string `json:"digest"`
}

func StoredName(user string) string { return "~" + user + "/" + Name }
func Digest(raw []byte) string      { sum := sha256.Sum256(raw); return hex.EncodeToString(sum[:]) }
func invalid(code string) error {
	return providererr.New(providererr.Invalid, code, "个人配置格式不合法")
}

var segment = regexp.MustCompile(`^[A-Za-z0-9_.-]+$`)
var reference = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_./:@-]{0,127}$`)
var token = regexp.MustCompile(`(?i)(sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|glpat-[a-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)`)
var secretSetting = regexp.MustCompile(`(?i)(?:^|[^A-Za-z0-9])(_authtoken|_auth|password|api[_-]?key|secret|authorization)["']?\s*[=:]\s*`)

const variablePattern = `\$\{[A-Za-z_][A-Za-z0-9_.:-]*\}`

var variable = regexp.MustCompile("^" + variablePattern + "$")
var variableInText = regexp.MustCompile(variablePattern)
var urlUserinfo = regexp.MustCompile(`(?i)\b[a-z][a-z0-9+.-]*://([^/?#\s"'<>]*@)`)

func maskConfigVariables(value string) string {
	return variableInText.ReplaceAllStringFunc(value, func(v string) string { return strings.Repeat(" ", len(v)) })
}

// Recognize explicit credential formats and assignments, not arbitrary secrets.
// Variable references are configuration, not resolved credential values.
func knownSecret(content string) bool {
	if token.MatchString(maskConfigVariables(content)) {
		return true
	}
	for _, match := range urlUserinfo.FindAllStringSubmatch(content, -1) {
		userinfo, err := url.PathUnescape(strings.TrimSuffix(match[1], "@"))
		if err != nil {
			return true
		}
		separator := strings.IndexByte(maskConfigVariables(userinfo), ':')
		if separator >= 0 {
			// A static username (e.g. oauth2) plus a referenced password is useful.
			if !variable.MatchString(userinfo[separator+1:]) {
				return true
			}
		} else if !variable.MatchString(userinfo) {
			return true
		}
	}
	for _, line := range strings.Split(content, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "#") || strings.HasPrefix(trimmed, ";") {
			continue
		}
		for _, match := range secretSetting.FindAllStringSubmatchIndex(maskConfigVariables(line), -1) {
			key := line[match[2]:match[3]]
			value := strings.TrimSpace(line[match[1]:])
			// JSON quoted values can be followed by a comma/closing object. Read
			// just that value so a quoted ${VAR} remains a reference in JSON too.
			if strings.HasPrefix(value, "\"") {
				var decoded string
				if json.NewDecoder(strings.NewReader(value)).Decode(&decoded) == nil {
					value = decoded
				}
			}
			value = strings.Trim(strings.TrimSpace(value), "\"'")
			if strings.EqualFold(key, "authorization") {
				parts := strings.Fields(value)
				if len(parts) == 2 && (strings.EqualFold(parts[0], "Bearer") || strings.EqualFold(parts[0], "Basic")) {
					value = parts[1]
				}
			}
			if value != "" && !variable.MatchString(value) {
				return true
			}
		}
	}
	return false
}

// Canonical validates the entire package before any persistent write.
func Canonical(doc *pb.PersonalConfigDocument) ([]byte, error) {
	if doc == nil {
		return nil, invalid("invalid_document")
	}
	if len(doc.Instructions) > 4096 || !utf8.ValidString(doc.Instructions) || strings.ContainsRune(doc.Instructions, 0) {
		return nil, invalid("instructions_too_large")
	}
	if len(doc.Files) > 10 {
		return nil, invalid("too_many_files")
	}
	copy := proto.Clone(doc).(*pb.PersonalConfigDocument)
	total := 0
	paths := []string{}
	for _, file := range copy.Files {
		p := file.GetPath()
		lower := strings.ToLower(p)
		if p == "" || len(p) > 240 || path.IsAbs(p) || strings.ContainsAny(p, "\\\x00") || path.Clean(p) != p {
			return nil, invalid("invalid_config_path")
		}
		for _, part := range strings.Split(p, "/") {
			if part == "." || part == ".." || !segment.MatchString(part) {
				return nil, invalid("invalid_config_path")
			}
		}
		first := strings.Split(lower, "/")[0]
		switch first {
		case ".ssh", ".aws", ".git", ".netrc", ".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".profile", ".zshrc", ".zprofile", ".zshenv", ".zlogin", ".zlogout":
			return nil, invalid("executable_or_secret_config")
		}
		switch strings.ToLower(path.Ext(p)) {
		case ".sh", ".bash", ".py", ".js", ".exe", ".bat", ".cmd", ".ps1":
			return nil, invalid("executable_config")
		}
		if !utf8.ValidString(file.Content) || strings.ContainsRune(file.Content, 0) {
			return nil, invalid("config_not_text")
		}
		total += len(file.Content)
		if total > 256<<10 {
			return nil, invalid("config_too_large")
		}
		if knownSecret(file.Content) {
			return nil, invalid("secret_in_config")
		}
		paths = append(paths, lower)
	}
	sort.Strings(paths)
	for i := 1; i < len(paths); i++ {
		if paths[i] == paths[i-1] || strings.HasPrefix(paths[i], paths[i-1]+"/") {
			return nil, invalid("duplicate_config_path")
		}
	}
	sort.Slice(copy.Files, func(i, j int) bool { return copy.Files[i].Path < copy.Files[j].Path })
	if d := copy.Defaults; d != nil {
		if len(d.SkillNames) > 100 || len(d.McpConnectionIds) > 100 {
			return nil, invalid("too_many_defaults")
		}
		refs := append(append([]string{}, d.SkillNames...), d.McpConnectionIds...)
		if d.EnvironmentId != "" {
			refs = append(refs, d.EnvironmentId)
		}
		for _, r := range refs {
			if !reference.MatchString(r) {
				return nil, invalid("invalid_default_reference")
			}
		}
	}
	raw, err := (protojson.MarshalOptions{UseProtoNames: true}).Marshal(copy)
	if err != nil {
		return nil, err
	}
	// protojson deliberately does not promise byte stability. Normalize JSON
	// keys/whitespace before hashing and comparing immutable package versions.
	var normalized any
	if err = json.Unmarshal(raw, &normalized); err != nil {
		return nil, err
	}
	return json.Marshal(normalized)
}

func Migrations() []relational.Migration {
	return []relational.Migration{{Version: 215, Name: "task_personal_config_pin", Statements: []string{`ALTER TABLE tasks ADD COLUMN personal_config_pin TEXT NOT NULL DEFAULT ''`, `ALTER TABLE tasks ADD COLUMN personal_config_selection TEXT NOT NULL DEFAULT ''`}}}
}

// LoadTask always uses the persisted submitter, never a caller-supplied user id.
func LoadTask(ctx context.Context, store relational.Provider, tenant *commonv1.TenantContext, taskID string) (*pb.PersonalConfig, error) {
	if taskID == "" || store == nil {
		return nil, nil
	}
	db, err := store.Bind(tenant)
	if err != nil {
		return nil, err
	}
	var config *pb.PersonalConfig
	err = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		row, ok, e := tx.QueryOne(ctx, `SELECT * FROM tasks WHERE task_id = $1`, taskID)
		if e != nil {
			// Standalone Sandbox deployments may have no Task Service schema yet.
			for cause := e; cause != nil; cause = errors.Unwrap(cause) {
				message := cause.Error()
				if strings.Contains(message, "no such table: tasks") || strings.Contains(message, `relation "tasks" does not exist`) {
					return nil
				}
			}
			return e
		}
		if !ok {
			return nil
		}
		raw, _ := row["personal_config_pin"].(string)
		if raw == "" || raw == NoConfig {
			return nil
		}
		var pin Pin
		if json.Unmarshal([]byte(raw), &pin) != nil {
			return invalid("invalid_personal_config_pin")
		}
		v, e := strconv.ParseInt(strings.TrimPrefix(pin.Version, "user/v"), 10, 64)
		if e != nil || v <= 0 || !strings.HasPrefix(pin.Version, "user/v") {
			return invalid("invalid_personal_config_pin")
		}
		user, _ := row["submitted_by"].(string)
		entry, found, e := tx.QueryOne(ctx, `SELECT document FROM registry_entries WHERE kind = $1 AND name = $2 AND version = $3`, Kind, StoredName(user), v)
		if e != nil {
			return e
		}
		if !found {
			return providererr.New(providererr.NotFound, "personal_config_version_missing", "固定的个人配置版本不存在")
		}
		body, _ := entry["document"].(string)
		if Digest([]byte(body)) != pin.Digest {
			return invalid("personal_config_digest_mismatch")
		}
		doc := &pb.PersonalConfigDocument{}
		if e = protojson.Unmarshal([]byte(body), doc); e != nil {
			return invalid("invalid_document")
		}
		if _, e = Canonical(doc); e != nil {
			return e
		}
		config = &pb.PersonalConfig{Version: pin.Version, Digest: pin.Digest, Document: doc}
		return nil
	})
	return config, err
}
