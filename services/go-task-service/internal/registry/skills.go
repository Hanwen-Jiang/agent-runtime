package registry

// Skill（N-14，docs/plan/n14-skill-contract.md；契约 c1836ba）。
//
// 三层都存在 registry_entries 里，表结构不变：
//   - 平台层：平台作用域，名字就是 Skill 名；租户层：租户作用域，名字就是 Skill 名（都经 Publish，要发布令牌）。
//   - 用户层：租户作用域，名字是 ~<user_id>/<Skill 名>（Skill 名小写字母开头，不会与租户层撞）；只经导出的 PutUserSkill、
//     RetireUserSkill（SkillService 调它们），本人才看得到。
//
// 同名时上层优先：下层不能发布上层当前有的名字（Conflict，name_taken）；上层之后才有的同名条目遮住下层的（shadowed：
// 不进目录、不被新 Task 固定），已固定的照旧按版本读。版本发布即成为当前版本（DEFAULT），上一个退役；正文与当前版本相同时
// 不产生新版本。固定值写成 <层>/vN。正文不进日志。
//
// 用户层每人最多 MaxSkillsPerUser 个当前的 Skill：查数与写入不在同一个事务里，并发发布时可能多出一两个（与 SEC-API 的
// 先查后写不同，Skill 没有要回滚的外部状态，多出的不影响隔离）。

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
)

const (
	kindSkill = registryv1.RegistryKind_REGISTRY_KIND_SKILL

	// MaxSkillsPerUser 是每个用户当前（没收回）的 Skill 个数上限。
	MaxSkillsPerUser = 100
	// maxSkillBytes 是一份正文（规范化的 JSON）按 UTF-8 的字节上限。
	maxSkillBytes = 256 << 10

	userNamePrefix = "~"
)

// ErrSkillLimit：用户层的 Skill 到了 MaxSkillsPerUser 个（SkillService 报 ResourceExhausted，code 为 skill_limit）。
var ErrSkillLimit = errors.New("skill_limit")

// skillNamePattern 是 Skill 名：小写字母开头，小写字母、数字、_ 与 -，最多 64 个字符。
var skillNamePattern = regexp.MustCompile(`^[a-z][a-z0-9_-]{0,63}$`)

// layer 是 Skill 登记的层；数字越小越优先。
type layer int

const (
	layerPlatform layer = iota + 1
	layerTenant
	layerUser
)

var layerNames = map[layer]string{layerPlatform: "platform", layerTenant: "tenant", layerUser: "user"}

func (l layer) scope() registryv1.SkillScope {
	switch l {
	case layerPlatform:
		return registryv1.SkillScope_SKILL_SCOPE_PLATFORM
	case layerTenant:
		return registryv1.SkillScope_SKILL_SCOPE_TENANT
	}
	return registryv1.SkillScope_SKILL_SCOPE_USER
}

func skillLabel(l layer, version int64) string { return layerNames[l] + "/" + versionLabel(version) }

// parseSkillVersion 解析 platform/vN、tenant/vN、user/vN。
func parseSkillVersion(label string) (layer, int64, error) {
	name, rest, ok := strings.Cut(label, "/")
	for l, n := range layerNames {
		if ok && n == name {
			v, err := parseVersion(rest)
			if err != nil || v == 0 {
				break
			}
			return l, v, nil
		}
	}
	return 0, 0, invalid("invalid_version", "Skill 的版本要写成 platform/vN、tenant/vN 或 user/vN")
}

func userStoredName(user, name string) string { return userNamePrefix + user + "/" + name }

// skillUser 是用户层的用户；没有或不合规时 denied。
func skillUser(tenant *commonv1.TenantContext) (string, error) {
	user := tenant.GetUserId()
	if user == "" || strings.ContainsAny(user, "/~") {
		return "", providererr.New(providererr.Denied, "no_user", "认证身份里没有用户，不能管用户层的 Skill")
	}
	return user, nil
}

// checkSkill：名字、大小与 JSON Schema 表达不了的路径规则（不分大小写不重复，不能一个是另一个的前缀目录）。
func checkSkill(name string, document []byte) error {
	if !skillNamePattern.MatchString(name) {
		return invalid("invalid_name", fmt.Sprintf("Skill 名 %q 要匹配 %s", name, skillNamePattern))
	}
	if len(document) > maxSkillBytes {
		return invalid("skill_too_large", "Skill 的正文最多 256 KiB")
	}
	var doc struct {
		Files []struct {
			Path string `json:"path"`
		} `json:"files"`
	}
	if err := json.Unmarshal(document, &doc); err != nil {
		return invalid("invalid_document", "Skill 解析不了")
	}
	paths := make([]string, 0, len(doc.Files))
	for _, f := range doc.Files {
		paths = append(paths, strings.ToLower(f.Path))
	}
	sort.Strings(paths)
	for i := 1; i < len(paths); i++ {
		if paths[i] == paths[i-1] || strings.HasPrefix(paths[i], paths[i-1]+"/") {
			return invalid("duplicate_path", fmt.Sprintf("files 的路径 %q 与 %q 重复或嵌套（不分大小写）", paths[i-1], paths[i]))
		}
	}
	// 排序后前缀目录不一定相邻（a、a-b、a/c）：再按目录逐个查。
	seen := map[string]bool{}
	for _, p := range paths {
		seen[p] = true
	}
	for _, p := range paths {
		for dir := p; strings.Contains(dir, "/"); {
			dir = dir[:strings.LastIndex(dir, "/")]
			if seen[dir] {
				return invalid("duplicate_path", fmt.Sprintf("files 的路径 %q 是 %q 的目录", dir, p))
			}
		}
	}
	return nil
}

// skillRow 是 Skill 的一个版本与它所在的层。
type skillRow struct {
	entry
	layer     layer
	owner     string
	createdAt int64
	shadowed  bool
}

// display 是给调用方看的名字（用户层去掉 ~<user>/）。
func (s skillRow) display() string {
	if s.layer == layerUser {
		return s.name[strings.Index(s.name, "/")+1:]
	}
	return s.name
}

func (s skillRow) label() string { return skillLabel(s.layer, s.version) }

const skillColumns = entryColumns + `, created_at`

func decodeSkill(row relational.Row, l layer, owner string) skillRow {
	return skillRow{entry: decodeEntry(row), layer: l, owner: owner, createdAt: asInt(row["created_at"])}
}

// skillScope 是一层所在的作用域。
func (r *Registry) skillScope(l layer, tenant *commonv1.TenantContext) (scope, error) {
	if l == layerPlatform {
		return r.platformScope()
	}
	return r.tenantScope(tenant)
}

// skillAt 取一层里的一个版本；version 为 0 时取当前版本。
func (r *Registry) skillAt(ctx context.Context, tenant *commonv1.TenantContext, l layer, name string, version int64) (skillRow, bool, error) {
	stored, owner := name, ""
	if l == layerUser {
		user, err := skillUser(tenant)
		if err != nil {
			return skillRow{}, false, nil // 没有用户就没有用户层：与不存在一样
		}
		stored, owner = userStoredName(user, name), user
	}
	sc, err := r.skillScope(l, tenant)
	if err != nil {
		return skillRow{}, false, err
	}
	var out skillRow
	var found bool
	err = sc.read(ctx, func(ctx context.Context, tx relational.Tx) error {
		var row relational.Row
		var err error
		if version == 0 {
			row, found, err = tx.QueryOne(ctx, `SELECT `+skillColumns+` FROM registry_entries WHERE kind = $1 AND name = $2 AND state = $3`,
				int64(kindSkill), stored, int64(registryv1.ReleaseState_RELEASE_STATE_DEFAULT))
		} else {
			row, found, err = tx.QueryOne(ctx, `SELECT `+skillColumns+` FROM registry_entries WHERE kind = $1 AND name = $2 AND version = $3`,
				int64(kindSkill), stored, version)
		}
		if err == nil && found {
			out = decodeSkill(row, l, owner)
		}
		return err
	})
	return out, found, err
}

// currentSkills 是一层里全部的当前版本，按名字排序。用户层只有 tenant.user_id 本人的。
func (r *Registry) currentSkills(ctx context.Context, tenant *commonv1.TenantContext, l layer) ([]skillRow, error) {
	sc, err := r.skillScope(l, tenant)
	if err != nil {
		return nil, err
	}
	query := `SELECT ` + skillColumns + ` FROM registry_entries WHERE kind = $1 AND state = $2`
	args := []any{int64(kindSkill), int64(registryv1.ReleaseState_RELEASE_STATE_DEFAULT)}
	owner := ""
	switch l {
	case layerTenant:
		query += ` AND substr(name, 1, 1) <> $3`
		args = append(args, userNamePrefix)
	case layerUser:
		user, err := skillUser(tenant)
		if err != nil {
			return nil, nil
		}
		owner = user
		prefix := userStoredName(user, "")
		query += ` AND substr(name, 1, $3) = $4`
		args = append(args, int64(len(prefix)), prefix)
	}
	var out []skillRow
	err = sc.read(ctx, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, query, args...)
		for _, row := range rows {
			out = append(out, decodeSkill(row, l, owner))
		}
		return err
	})
	sort.Slice(out, func(i, j int) bool { return out[i].name < out[j].name })
	return out, err
}

// upperNames 是比 l 高的各层当前有的名字。
func (r *Registry) upperNames(ctx context.Context, tenant *commonv1.TenantContext, l layer) (map[string]bool, error) {
	out := map[string]bool{}
	for upper := layerPlatform; upper < l; upper++ {
		rows, err := r.currentSkills(ctx, tenant, upper)
		if err != nil {
			return nil, err
		}
		for _, s := range rows {
			out[s.display()] = true
		}
	}
	return out, nil
}

// visibleSkills 是 tenant（含 user_id 本人的用户层）看得到的当前版本，按名字、层排序，标好 shadowed。
func (r *Registry) visibleSkills(ctx context.Context, tenant *commonv1.TenantContext) ([]skillRow, error) {
	if _, err := tenancy.TenantScope(tenant); err != nil {
		return nil, err
	}
	taken := map[string]bool{}
	var out []skillRow
	for l := layerPlatform; l <= layerUser; l++ {
		rows, err := r.currentSkills(ctx, tenant, l)
		if err != nil {
			return nil, err
		}
		for _, s := range rows {
			s.shadowed = taken[s.display()]
			out = append(out, s)
		}
		for _, s := range rows {
			taken[s.display()] = true
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].display() < out[j].display() })
	return out, nil
}

// catalogSkills 是目录与固定用的：看得到、没被遮住的当前版本。
func (r *Registry) catalogSkills(ctx context.Context, tenant *commonv1.TenantContext) ([]skillRow, error) {
	all, err := r.visibleSkills(ctx, tenant)
	if err != nil {
		return nil, err
	}
	out := all[:0]
	for _, s := range all {
		if !s.shadowed {
			out = append(out, s)
		}
	}
	return out, nil
}

// markShadowed 按上层当前的名字标出 s 有没有被遮住。
func (r *Registry) markShadowed(ctx context.Context, tenant *commonv1.TenantContext, s skillRow) (skillRow, error) {
	if s.layer == layerPlatform {
		return s, nil
	}
	upper, err := r.upperNames(ctx, tenant, s.layer)
	s.shadowed = upper[s.display()]
	return s, err
}

func (r *Registry) toSkill(s skillRow, withDocument bool) *registryv1.Skill {
	var doc struct {
		Description string `json:"description"`
	}
	_ = json.Unmarshal([]byte(s.document), &doc)
	sum := sha256.Sum256([]byte(s.document))
	out := &registryv1.Skill{
		Name: s.display(), Version: s.label(), State: s.state, Scope: s.layer.scope(), Description: doc.Description,
		Owner: s.owner, Shadowed: s.shadowed, Digest: hex.EncodeToString(sum[:]),
		PublishedAt: timestamppb.New(time.UnixMilli(s.createdAt)),
	}
	if withDocument {
		out.Document = r.payload(s.entry)
	}
	return out
}

// --- 发布（平台层、租户层经 Publish；用户层经 PutUserSkill）---

// publishedSkill：Publish 发 SKILL 时正文与当前版本相同就不写，返回当前版本。
func (r *Registry) publishedSkill(ctx context.Context, c candidate) (entry, bool, error) {
	current, found, err := c.scope.get(ctx, kindSkill, c.name, 0)
	if err != nil || !found || current.document != string(c.document) {
		return entry{}, false, err
	}
	return current, true, nil
}

// checkSkillName：租户层（不是平台作用域的发布）不能用平台当前有的名字。
func (r *Registry) checkSkillName(ctx context.Context, c candidate) error {
	if c.scope.platform {
		return nil
	}
	platform, err := r.platformScope()
	if err != nil {
		return err
	}
	_, taken, err := platform.get(ctx, kindSkill, c.name, 0)
	if err != nil {
		return err
	}
	if taken {
		return providererr.New(providererr.Conflict, "name_taken", fmt.Sprintf("平台已有名为 %s 的 Skill", c.name))
	}
	return nil
}

// UserSkillResult 是 PutUserSkill 的结果。
type UserSkillResult struct {
	Skill *registryv1.Skill
	// Created：这个名字第一次登记（user/v1）。
	Created bool
	// NewVersion：产生了新版本；正文与当前版本相同时为 false。
	NewVersion bool
}

// PutUserSkill 发布 tenant.user_id 本人的一个用户层 Skill（SkillService.PutSkill）。
func (r *Registry) PutUserSkill(ctx context.Context, tenant *commonv1.TenantContext, name string, document *commonv1.JsonPayload) (UserSkillResult, error) {
	if _, err := tenancy.TenantScope(tenant); err != nil {
		return UserSkillResult{}, err
	}
	user, err := skillUser(tenant)
	if err != nil {
		return UserSkillResult{}, err
	}
	raw, err := payloadJSON(document, "document", r.schemas.ids[kinds[kindSkill].schema])
	if err != nil {
		return UserSkillResult{}, err
	}
	c, err := r.prepare(kindSkill, &commonv1.TenantContext{TenantId: tenant.GetTenantId()}, name, raw)
	if err != nil {
		return UserSkillResult{}, err
	}
	upper, err := r.upperNames(ctx, tenant, layerUser)
	if err != nil {
		return UserSkillResult{}, err
	}
	if upper[name] {
		return UserSkillResult{}, providererr.New(providererr.Conflict, "name_taken", fmt.Sprintf("平台或本租户已有名为 %s 的 Skill", name))
	}
	c.name = userStoredName(user, name)
	current, found, err := r.skillAt(ctx, tenant, layerUser, name, 0)
	if err != nil {
		return UserSkillResult{}, err
	}
	if found && current.document == string(c.document) {
		return UserSkillResult{Skill: r.toSkill(current, false)}, nil
	}
	if !found {
		mine, err := r.currentSkills(ctx, tenant, layerUser)
		if err != nil {
			return UserSkillResult{}, err
		}
		if len(mine) >= MaxSkillsPerUser {
			return UserSkillResult{}, ErrSkillLimit
		}
	}
	existed, err := c.scope.exists(ctx, kindSkill, c.name)
	if err != nil {
		return UserSkillResult{}, err
	}
	if _, err := c.scope.insert(ctx, c, "", "", r.clock.Now()); err != nil {
		return UserSkillResult{}, err
	}
	written, _, err := r.skillAt(ctx, tenant, layerUser, name, 0)
	if err != nil {
		return UserSkillResult{}, err
	}
	r.log.Info("Registry 发布用户层 Skill", "tenant", tenant.GetTenantId(), "user", user, "name", name, "version", written.label())
	return UserSkillResult{Skill: r.toSkill(written, false), Created: !existed, NewVersion: true}, nil
}

// RetireUserSkill 收回 tenant.user_id 本人的一个用户层 Skill：当前版本退役，不再进目录、不再被新 Task 固定。
// 幂等：已经收回时 retired 为 false；这个名字本人从来没登记过时 NotFound（别人的与不存在的一样）。
func (r *Registry) RetireUserSkill(ctx context.Context, tenant *commonv1.TenantContext, name string) (string, bool, error) {
	if _, err := tenancy.TenantScope(tenant); err != nil {
		return "", false, err
	}
	user, err := skillUser(tenant)
	if err != nil {
		return "", false, err
	}
	sc, err := r.tenantScope(tenant)
	if err != nil {
		return "", false, err
	}
	stored := userStoredName(user, name)
	current, found, err := r.skillAt(ctx, tenant, layerUser, name, 0)
	if err != nil {
		return "", false, err
	}
	if !found {
		existed, err := sc.exists(ctx, kindSkill, stored)
		if err != nil || existed {
			return "", false, err
		}
		return "", false, notFound("skill_not_found", "没有这个 Skill")
	}
	err = retry(ctx, func() error {
		return sc.st.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
			_, err := tx.Execute(ctx, `UPDATE registry_entries SET state = $1, updated_at = $2 WHERE kind = $3 AND name = $4 AND state = $5`,
				int64(registryv1.ReleaseState_RELEASE_STATE_RETIRED), r.clock.Now().UnixMilli(), int64(kindSkill), stored,
				int64(registryv1.ReleaseState_RELEASE_STATE_DEFAULT))
			return err
		})
	})
	if err != nil {
		return "", false, err
	}
	r.log.Info("Registry 收回用户层 Skill", "tenant", tenant.GetTenantId(), "user", user, "name", name, "version", current.label())
	return current.label(), true, nil
}

// --- 读 ---

// ReadSkill 取一个 Skill 的正文：version 为空时取看得到的当前版本（平台、租户、本人依次）；否则按 <层>/vN 照原样取
// （含退役的与被遮住的）。别人的用户层与不存在的一样 NotFound（skill_not_found）。
func (r *Registry) ReadSkill(ctx context.Context, tenant *commonv1.TenantContext, name, version string) (*registryv1.Skill, error) {
	if _, err := r.reader(tenant); err != nil {
		return nil, err
	}
	var found skillRow
	var ok bool
	if version == "" {
		for l := layerPlatform; l <= layerUser && !ok; l++ {
			s, hit, err := r.skillAt(ctx, tenant, l, name, 0)
			if err != nil {
				return nil, err
			}
			found, ok = s, hit
		}
	} else {
		l, v, err := parseSkillVersion(version)
		if err != nil {
			return nil, err
		}
		if found, ok, err = r.skillAt(ctx, tenant, l, name, v); err != nil {
			return nil, err
		}
	}
	if !ok {
		return nil, notFound("skill_not_found", "没有这个 Skill")
	}
	found, err := r.markShadowed(ctx, tenant, found)
	if err != nil {
		return nil, err
	}
	return r.toSkill(found, true), nil
}

// SkillPage 是一页 Skill（不带正文）。
type SkillPage struct {
	Skills        []*registryv1.Skill
	NextPageToken string
}

// OwnerSkills 是 SkillService.ListSkills：调用者看得到的当前版本，不带正文，按名字排序；本人被遮住的也列出（shadowed）。
func (r *Registry) OwnerSkills(ctx context.Context, tenant *commonv1.TenantContext, pageSize int32, pageToken string) (SkillPage, error) {
	all, err := r.visibleSkills(ctx, tenant)
	if err != nil {
		return SkillPage{}, err
	}
	kept := all[:0]
	for _, s := range all {
		if !s.shadowed || s.layer == layerUser {
			kept = append(kept, s)
		}
	}
	return r.skillPage(kept, pageSize, pageToken)
}

func (r *Registry) skillPage(rows []skillRow, pageSize int32, pageToken string) (SkillPage, error) {
	page, next, err := paginate(rows, pageSize, pageToken)
	if err != nil {
		return SkillPage{}, err
	}
	out := SkillPage{NextPageToken: next}
	for _, s := range page {
		out.Skills = append(out.Skills, r.toSkill(s, false))
	}
	return out, nil
}

// pinnedSkills 是 resolve 固定的：看得到、没被遮住的当前版本，名字 → <层>/vN。
func (r *Registry) pinnedSkills(ctx context.Context, tenant *commonv1.TenantContext) (map[string]string, error) {
	rows, err := r.catalogSkills(ctx, tenant)
	if err != nil || len(rows) == 0 {
		return nil, err
	}
	out := make(map[string]string, len(rows))
	for _, s := range rows {
		out[s.display()] = s.label()
	}
	return out, nil
}

// GetSkill 实现 RegistryService.GetSkill。
func (r *Registry) GetSkill(ctx context.Context, req *connect.Request[registryv1.GetSkillRequest]) (*connect.Response[registryv1.GetSkillResponse], error) {
	s, err := r.ReadSkill(ctx, req.Msg.GetTenant(), req.Msg.GetName(), req.Msg.GetVersion())
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&registryv1.GetSkillResponse{Skill: s}), nil
}

// ListSkills 实现 RegistryService.ListSkills：带固定版本时只列固定的（缺一个就 NotFound），否则列目录（看得到、没被遮住的
// 当前版本）。不带正文，按名字排序、分页。
func (r *Registry) ListSkills(ctx context.Context, req *connect.Request[registryv1.ListSkillsRequest]) (*connect.Response[registryv1.ListSkillsResponse], error) {
	tenant := req.Msg.GetTenant()
	if _, err := r.reader(tenant); err != nil {
		return nil, fail(err)
	}
	var rows []skillRow
	if pins := req.Msg.GetPinnedVersions().GetSkills(); len(pins) > 0 {
		names := make([]string, 0, len(pins))
		for name := range pins {
			names = append(names, name)
		}
		sort.Strings(names)
		for _, name := range names {
			l, v, err := parseSkillVersion(pins[name])
			if err != nil {
				return nil, fail(err)
			}
			s, found, err := r.skillAt(ctx, tenant, l, name, v)
			if err != nil {
				return nil, fail(err)
			}
			if !found {
				return nil, fail(notFound("pinned_skill_not_found", "固定的 Skill 版本 "+name+"@"+pins[name]+" 不存在"))
			}
			if s, err = r.markShadowed(ctx, tenant, s); err != nil {
				return nil, fail(err)
			}
			rows = append(rows, s)
		}
	} else {
		var err error
		if rows, err = r.catalogSkills(ctx, tenant); err != nil {
			return nil, fail(err)
		}
	}
	page, err := r.skillPage(rows, req.Msg.GetPageSize(), req.Msg.GetPageToken())
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&registryv1.ListSkillsResponse{Skills: page.Skills, NextPageToken: page.NextPageToken}), nil
}
