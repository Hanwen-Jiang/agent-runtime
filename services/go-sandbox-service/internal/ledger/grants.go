package ledger

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"sort"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
)

// Grant 是一条联网授权（sandbox_grants；D-16）。审批不归服务（Task 的 request_network 与 task-service 的核对）；服务只读它，
// 托管后端按授权的主机建按域名放行的环境，以及构建项目环境时给构建记一条平台批准的授权（D-17）。
type Grant struct {
	GrantID   string
	TaskID    string
	Hosts     []string
	ExpiresAt string
	State     string
	MaxUp     int64
	MaxDown   int64
	MaxConns  int64
	MaxRPS    int64
}

// Audited：授权带用量上限时只能落在经出网网关的后端（W11）。
func (g Grant) Audited() bool { return g.MaxUp > 0 || g.MaxDown > 0 || g.MaxConns > 0 || g.MaxRPS > 0 }

// GrantsForTask 是这个 Task 现在有效的授权。
func (l *Ledger) GrantsForTask(ctx context.Context, owner, taskID string) ([]Grant, error) {
	if taskID == "" || owner == PlatformID {
		return nil, nil
	}
	now := ISO(l.Now())
	var out []Grant
	err := l.Tx(ctx, owner, true, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, "SELECT grant_id, task_id, hosts, expires_at, state, max_up_bytes, max_down_bytes, max_conns, max_rps "+
			"FROM sandbox_grants WHERE task_id = $1 ORDER BY created_at", taskID)
		for _, r := range rows {
			g := Grant{GrantID: Str(r, "grant_id"), TaskID: Str(r, "task_id"), ExpiresAt: Str(r, "expires_at"), State: Str(r, "state"),
				MaxUp: Int(r, "max_up_bytes"), MaxDown: Int(r, "max_down_bytes"), MaxConns: Int(r, "max_conns"), MaxRPS: Int(r, "max_rps")}
			_ = json.Unmarshal([]byte(Str(r, "hosts")), &g.Hosts)
			if g.State == "active" && g.ExpiresAt > now {
				out = append(out, g)
			}
		}
		return err
	})
	return out, err
}

// UnionHosts 是这些授权的主机的并集，排好序。
func UnionHosts(grants []Grant) []string {
	set := map[string]bool{}
	for _, g := range grants {
		for _, h := range g.Hosts {
			set[h] = true
		}
	}
	out := make([]string, 0, len(set))
	for h := range set {
		out = append(out, h)
	}
	sort.Strings(out)
	return out
}

// RevokeTask 收回这个 Task 的全部授权。
func (l *Ledger) RevokeTask(ctx context.Context, owner, taskID string) error {
	return l.Tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, "UPDATE sandbox_grants SET state = 'revoked', revoked_at = $1 WHERE task_id = $2 AND state = 'active'", ISO(l.Now()), taskID)
		return err
	})
}

// RevokeAll 收回这个租户的全部授权（租户删除）。
func (l *Ledger) RevokeAll(ctx context.Context, owner string) error {
	return l.Tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, "UPDATE sandbox_grants SET state = 'revoked', revoked_at = $1 WHERE state = 'active'", ISO(l.Now()))
		return err
	})
}

// CreatePlatformGrant 记一条平台批准的授权（构建项目环境时放行镜像仓库与系统包的源，D-17）。
func (l *Ledger) CreatePlatformGrant(ctx context.Context, owner, taskID string, hosts []string, lifetime time.Duration, reason string) error {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	now := l.Now()
	hostsJSON, _ := json.Marshal(hosts)
	return l.Tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, "INSERT INTO sandbox_grants (grant_id, task_id, hosts, ports, expires_at, state, approved_by, reason, created_at) "+
			"VALUES ($1, $2, $3, $4, $5, 'active', 'platform:env-builder', $6, $7)",
			"sbg-"+hex.EncodeToString(b), taskID, string(hostsJSON), "[80,443]", ISO(now.Add(lifetime)), reason, ISO(now))
		return err
	})
}
