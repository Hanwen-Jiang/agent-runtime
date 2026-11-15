package taskapi

import (
	"context"
	"encoding/json"
	"fmt"
	"net/netip"
	"regexp"
	"slices"
	"sort"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/sandboxgrants"
)

// 联网授权（设计书改动稿 W8；docs/plan/cloud-deploy.md D-16）。规则与 Worker 一侧（agentruntime_worker.sandbox.grants）
// 相同，两边对着同一组用例测（network_test.go 与 py-worker 的 test_sandbox_grants.py）：
//   - 网络授权与工作区读写上限独立。租户策略不允许、主机不在范围、时长超过上限：denied；写法不对：invalid。
//   - 主机只能是域名：IP 地址、localhost、集群内的名字不收。时长 15、30、60 分钟；显式 unlimited 可选 0。端口不写为 80 与 443。
//   - approver 是 submitter 时提交本身就是提交者的批准；是 admin 时只接受带 ScopeApprove 的身份提交。
//   - 批准的申请写进业务库的 sandbox_grants（表由 Worker 迁移，D-11），出网网关按它放行；Task 结束时由 FinishTask 收回。

var (
	grantHost        = regexp.MustCompile(`^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*\.[a-z]([a-z0-9-]{0,61}[a-z0-9])?$`)
	internalSuffixes = []string{".local", ".localhost", ".internal", ".svc", ".cluster.local", ".home.arpa", ".lan", ".intranet", ".corp"}
	grantMinutes     = []int32{0, 15, 30, 60}
	defaultPorts     = []int32{80, 443}
)

const maxGrantHosts = 20

// NetworkPolicy 是租户策略的 sandbox.network；用量上限 0 表示不限，时长不限由 AllowUnlimited 显式控制。
type NetworkPolicy struct {
	Allowed         bool     `json:"allowed"`
	AllowUnlimited  bool     `json:"allow_unlimited"`
	Modes           []string `json:"modes"`
	Hosts           []string `json:"hosts"`
	MaxGrantMinutes int32    `json:"max_grant_minutes"`
	Approver        string   `json:"approver"`
	MaxUpBytes      int64    `json:"max_up_bytes"`
	MaxDownBytes    int64    `json:"max_down_bytes"`
	MaxConns        int64    `json:"max_conns"`
	MaxRPS          int64    `json:"max_rps"`
	Preview         bool     `json:"preview"`
}

// NetworkPolicySource 给出租户的 sandbox.network。没有策略时返回零值（不允许联网）。
type NetworkPolicySource interface {
	NetworkPolicy(ctx context.Context, tenant *commonv1.TenantContext) (NetworkPolicy, error)
}

// NetworkPolicyFunc 让普通函数实现 NetworkPolicySource（Registry 的租户策略由 main 接上）。
type NetworkPolicyFunc func(ctx context.Context, tenant *commonv1.TenantContext) (NetworkPolicy, error)

func (f NetworkPolicyFunc) NetworkPolicy(ctx context.Context, tenant *commonv1.TenantContext) (NetworkPolicy, error) {
	return f(ctx, tenant)
}

// NetworkPolicyFromDocument 从租户策略文档（tenant_policy.schema.json）里取 sandbox.network。
func NetworkPolicyFromDocument(doc []byte) (NetworkPolicy, error) {
	var parsed struct {
		Sandbox struct {
			Network NetworkPolicy `json:"network"`
		} `json:"sandbox"`
	}
	if len(doc) == 0 {
		return NetworkPolicy{}, nil
	}
	if err := json.Unmarshal(doc, &parsed); err != nil {
		return NetworkPolicy{}, providererr.Wrap(providererr.Invalid, "tenant_policy_malformed", "租户策略的 sandbox.network 格式不对", err)
	}
	return parsed.Sandbox.Network, nil
}

func (p NetworkPolicy) maxMinutes() int32 {
	if p.MaxGrantMinutes > 0 {
		return p.MaxGrantMinutes
	}
	return 60
}

func (p NetworkPolicy) approver() string {
	if p.Approver == "" {
		return "submitter"
	}
	return p.Approver
}

func denied(code, msg string) error { return providererr.New(providererr.Denied, code, msg) }

func invalidNetwork(msg string) error {
	return providererr.New(providererr.Invalid, "invalid_network_request", msg)
}

// checkGrantHost 规范化并校验申请里的一个主机。
func checkGrantHost(host string) (string, error) {
	h := strings.TrimSuffix(strings.ToLower(strings.TrimSpace(host)), ".")
	bare := strings.TrimPrefix(h, "*.")
	if _, err := netip.ParseAddr(strings.Trim(bare, "[]")); err == nil || len(h) > 253 || !grantHost.MatchString(h) {
		return "", providererr.New(providererr.Invalid, "invalid_host", fmt.Sprintf("%q 不是合法的主机名（不能是 IP 地址，不带端口与路径）", host))
	}
	if bare == "localhost" {
		return "", denied("host_not_allowed", h+" 是本机的名字，不放行")
	}
	for _, s := range internalSuffixes {
		if strings.HasSuffix(bare, s) || bare == s[1:] {
			return "", denied("host_not_allowed", h+" 是内网的名字，不放行")
		}
	}
	return h, nil
}

// coversHost：pattern（策略或授权里的一项）是否覆盖 host（具体主机，或申请里的 *. 通配）。
func coversHost(pattern, host string) bool {
	if pattern == host {
		return true
	}
	if !strings.HasPrefix(pattern, "*.") {
		return false
	}
	suffix := pattern[1:]
	target := host
	if strings.HasPrefix(host, "*.") {
		target = host[1:]
	}
	return strings.HasSuffix(target, suffix) && target != suffix[1:]
}

// checkedNetwork 是核对过的申请。
type checkedNetwork struct {
	Scope   string
	Hosts   []string
	Ports   []int32
	Minutes int32
	Reason  string
}

// CheckNetworkRequest 按租户策略核对用户明示的联网申请；scopes 是提交者身份的权限。
// 保留 effect 参数兼容调用方，但网络授权不提升或限制工作区读写上限，后者仍由普通提交与工具检查执行。
func CheckNetworkRequest(policy NetworkPolicy, req *taskapiv1.NetworkRequest, _ commonv1.Effect, scopes []string) (checkedNetwork, error) {
	if !policy.Allowed {
		return checkedNetwork{}, denied("network_not_allowed", "租户策略不允许沙箱联网（sandbox.network.allowed）")
	}
	modes := policy.Modes
	if modes == nil {
		modes = []string{"deny", "allowlist"}
	}
	scope := "hosts"
	switch req.GetScope() {
	case taskapiv1.NetworkScope_NETWORK_SCOPE_UNSPECIFIED, taskapiv1.NetworkScope_NETWORK_SCOPE_HOSTS:
		if !slices.Contains(modes, "allowlist") {
			return checkedNetwork{}, denied("network_allowlist_not_allowed", "租户策略不允许按主机申请联网")
		}
	case taskapiv1.NetworkScope_NETWORK_SCOPE_PUBLIC:
		scope = "public"
		if !slices.Contains(modes, "open") {
			return checkedNetwork{}, denied("network_open_not_allowed", "租户策略不允许所有公网域名联网")
		}
		if len(req.GetHosts()) != 0 {
			return checkedNetwork{}, invalidNetwork("public 授权不能同时指定 hosts")
		}
	default:
		return checkedNetwork{}, invalidNetwork("未知的联网授权 scope")
	}
	switch {
	case scope == "hosts" && len(req.GetHosts()) == 0:
		return checkedNetwork{}, invalidNetwork("要写明访问哪些主机")
	case len(req.GetHosts()) > maxGrantHosts:
		return checkedNetwork{}, invalidNetwork(fmt.Sprintf("一次最多申请 %d 个主机", maxGrantHosts))
	case len(req.GetReason()) > 500 || !printable(req.GetReason()):
		return checkedNetwork{}, invalidNetwork("理由最多 500 个字符，不能有控制字符")
	}
	out := checkedNetwork{Scope: scope, Reason: strings.TrimSpace(req.GetReason())}
	for _, raw := range req.GetHosts() {
		h, err := checkGrantHost(raw)
		if err != nil {
			return checkedNetwork{}, err
		}
		if !slices.Contains(modes, "open") && !slices.ContainsFunc(policy.Hosts, func(p string) bool { return coversHost(strings.ToLower(p), h) }) {
			return checkedNetwork{}, denied("host_not_allowed", h+" 不在租户策略能放行的主机范围里")
		}
		if !slices.Contains(out.Hosts, h) {
			out.Hosts = append(out.Hosts, h)
		}
	}
	ports := req.GetPorts()
	if len(ports) == 0 {
		ports = defaultPorts
	}
	for _, p := range ports {
		if p == 25 {
			return checkedNetwork{}, denied("port_25", "不允许访问 25 端口")
		}
		if p < 1 || p > 65535 {
			return checkedNetwork{}, invalidNetwork(fmt.Sprintf("端口 %d 不在 1 到 65535 之间", p))
		}
		if !slices.Contains(out.Ports, p) {
			out.Ports = append(out.Ports, p)
		}
	}
	if req.GetMinutes() == 0 {
		if !req.GetUnlimited() {
			return checkedNetwork{}, invalidNetwork("不限时授权需要显式 unlimited=true")
		}
		if !policy.AllowUnlimited {
			return checkedNetwork{}, denied("network_unlimited_not_allowed", "租户策略不允许本任务不限时联网")
		}
	} else if req.GetUnlimited() {
		return checkedNetwork{}, invalidNetwork("unlimited=true 只能与 minutes=0 一起使用")
	}
	if !slices.Contains(grantMinutes, req.GetMinutes()) {
		return checkedNetwork{}, invalidNetwork("授权时长只能是 0（本任务不限时）、15、30、60 分钟")
	}
	if req.GetMinutes() > policy.maxMinutes() {
		return checkedNetwork{}, denied("grant_too_long", fmt.Sprintf("租户策略的授权最长 %d 分钟", policy.maxMinutes()))
	}
	switch policy.approver() {
	case "submitter":
	case "admin":
		if !slices.Contains(scopes, ScopeApprove) {
			return checkedNetwork{}, denied("network_needs_admin",
				"租户策略要求管理员批准联网：用带审批权限的身份提交，或在任务运行中由 agent 经 request_network 申请、管理员批")
		}
	default:
		return checkedNetwork{}, providererr.New(providererr.Invalid, "tenant_policy_malformed", "租户策略的 sandbox.network.approver 只能是 submitter 或 admin")
	}
	out.Minutes = req.GetMinutes()
	sort.Strings(out.Hosts)
	slices.Sort(out.Ports)
	return out, nil
}

// writeSubmitGrant 把提交时批准的申请写进 sandbox_grants（sandboxgrants；与 Worker 的 GrantStore 同一张表）。
func writeSubmitGrant(ctx context.Context, store relational.Provider, tenant *commonv1.TenantContext, taskID string,
	req checkedNetwork, policy NetworkPolicy, approvedBy string, now time.Time) error {
	return sandboxgrants.WriteSubmitGrant(ctx, store, tenant, sandboxgrants.Grant{
		TaskID: taskID, Scope: req.Scope, Hosts: req.Hosts, Ports: req.Ports, Minutes: req.Minutes, AllowUnlimited: policy.AllowUnlimited, Reason: req.Reason, ApprovedBy: approvedBy,
		MaxUpBytes: policy.MaxUpBytes, MaxDownBytes: policy.MaxDownBytes, MaxConns: policy.MaxConns, MaxRPS: policy.MaxRPS,
	}, now)
}
