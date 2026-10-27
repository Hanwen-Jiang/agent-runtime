// Package sandboxgrants 是 task-service 对联网授权表 sandbox_grants 的两处写入（设计书改动稿 W8；docs/plan/cloud-deploy.md D-16）：
// 提交时批准的 network_request 写成授权（taskapi），Task 结束时收回它的全部授权（Session 的 FinishTask）。
//
// 表在业务库里，由 Worker 迁移（agentruntime_worker.sandbox.ledger，迁移 202、203）；出网网关读同一张表。
// 收回之后另可通知出网网关立即断开这个 Task 的连接（Notifier；网关缓存授权的时间本来就只有几秒）。
package sandboxgrants

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Grant 是要写的一条授权。
type Grant struct {
	TaskID     string
	Hosts      []string
	Ports      []int32
	Minutes    int32
	Reason     string
	ApprovedBy string
	// 单次授权的用量上限，取自租户策略；0 表示不限。
	MaxUpBytes, MaxDownBytes, MaxConns, MaxRPS int64
}

// SubmitGrantID：提交时的授权按 task_id 定号，重投递（上一次的信号没送达）时不会多写一份。
func SubmitGrantID(taskID string) string {
	sum := sha256.Sum256([]byte("submit-grant:" + taskID))
	return "sbg-" + hex.EncodeToString(sum[:8])
}

// ISO 是表里的时间写法（与 Worker 的 ledger.iso 相同，可以按字典序比较）。
func ISO(t time.Time) string { return t.UTC().Format("2006-01-02T15:04:05.000000Z") }

// WriteSubmitGrant 写一条提交时批准的授权（同一个 Task 只写一次）。表还没有（Worker 还没起来迁移）时 transient。
func WriteSubmitGrant(ctx context.Context, store relational.Provider, tenant *commonv1.TenantContext, g Grant, now time.Time) error {
	db, err := store.Bind(tenant)
	if err != nil {
		return err
	}
	hosts, _ := json.Marshal(g.Hosts)
	ports, _ := json.Marshal(g.Ports)
	expires := now.Add(time.Duration(g.Minutes) * time.Minute)
	err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, `INSERT INTO sandbox_grants (grant_id, task_id, workspace_id, session_id, hosts, ports, expires_at,
			max_up_bytes, max_down_bytes, max_conns, max_rps, up_bytes, down_bytes, preview_port, state, approved_by, reason, created_at,
			revoked_at, preview_env, preview_target)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 0, 0, 0, 'active', $12, $13, $14, '', '', '')
			ON CONFLICT (grant_id) DO NOTHING`,
			SubmitGrantID(g.TaskID), g.TaskID, tenant.GetWorkspaceId(), tenant.GetSessionId(), string(hosts), string(ports), ISO(expires),
			g.MaxUpBytes, g.MaxDownBytes, g.MaxConns, g.MaxRPS, g.ApprovedBy, g.Reason, ISO(now))
		return err
	})
	if err != nil && MissingTable(err) {
		return providererr.Wrap(providererr.Transient, "sandbox_unavailable", "沙箱的表还没建好（Worker 起来后会迁移），稍后再提交", err)
	}
	return err
}

// RevokeTask 收回一个 Task 的全部联网授权；返回收回了几个。表还没有（这个部署没有沙箱）时什么也不做。
func RevokeTask(ctx context.Context, store relational.Provider, tenant *commonv1.TenantContext, taskID string, now time.Time) (int64, error) {
	db, err := store.Bind(tenant)
	if err != nil {
		return 0, err
	}
	var n int64
	err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		var err error
		n, err = tx.Execute(ctx, `UPDATE sandbox_grants SET state = 'revoked', revoked_at = $1 WHERE task_id = $2 AND state = 'active'`,
			ISO(now), taskID)
		return err
	})
	if err != nil && MissingTable(err) {
		return 0, nil
	}
	return n, err
}

// MissingTable：SQLite 报 no such table，Postgres 报 relation does not exist（两个适配器都映射成 invalid，原因在包着的错误里）。
func MissingTable(err error) bool {
	for e := err; e != nil; e = errors.Unwrap(e) {
		text := strings.ToLower(e.Error())
		if strings.Contains(text, "no such table") || (strings.Contains(text, "relation") && strings.Contains(text, "does not exist")) {
			return true
		}
	}
	return false
}

// Notifier 通知出网网关立即收回一个 Task 的授权（POST <管理地址>/admin/revoke，带管理令牌）。尽力而为：
// 失败只影响“立即”，网关缓存的授权几秒内就会按业务库里的状态失效。
type Notifier struct {
	URL    string
	Token  string
	Client *http.Client
}

// Revoked 见 Notifier。
func (n *Notifier) Revoked(ctx context.Context, tenantID, taskID string) error {
	if n == nil || n.URL == "" {
		return nil
	}
	body, _ := json.Marshal(map[string]string{"tenant_id": tenantID, "task_id": taskID})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(n.URL, "/")+"/admin/revoke", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if n.Token != "" {
		req.Header.Set("Authorization", "Bearer "+n.Token)
	}
	client := n.Client
	if client == nil {
		client = &http.Client{Timeout: 3 * time.Second}
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	_ = resp.Body.Close()
	if resp.StatusCode >= 300 {
		return providererr.New(providererr.Transient, "egress_notify_failed", "出网网关没有接受收回通知："+resp.Status)
	}
	return nil
}
