package broker

import (
	"context"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/archive"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
)

// 挂卷工作区的冷热分层（DISK-B；docs/plan/workspace-read-latency-and-disk-2026-11-03.md）。SBX-01 从 Worker 的
// agentruntime_worker.workspace.cold 搬来：闲置超过天数的挂卷工作区先存一份快照、与环境里的清单核对一致，才删本地目录，状态记成
// cold；下次借用时 Broker 从快照恢复。存档成功之前绝不删本地。天数取租户策略的 workspace.cold_after_days，没写时用部署的默认值
// （AR_WORKSPACE_COLD_AFTER_DAYS），都没有时不转冷。对账最多一小时扫一次。

// CoolBusyFor 是转冷时借的环境最多占多久（导出整个工作区）。
const CoolBusyFor = 30 * time.Minute

// ColdOptions 是冷热分层的部署配置。
type ColdOptions struct {
	DefaultDays int
	Runtime     string
	Every       time.Duration
}

// SetCold 配置冷热分层。
func (b *Broker) SetCold(o ColdOptions) {
	if o.Every == 0 {
		o.Every = time.Hour
	}
	b.mu.Lock()
	b.cold = o
	b.mu.Unlock()
}

// CoolIdleWorkspaces 扫一遍工作区根，把闲置够天数的挂卷工作区转冷；返回转冷了几个。
func (b *Broker) CoolIdleWorkspaces(ctx context.Context) (int, error) {
	b.mu.Lock()
	cold := b.cold
	due := time.Since(b.lastCool) >= cold.Every
	if due {
		b.lastCool = time.Now()
	}
	b.mu.Unlock()
	root := b.reg.WorkspaceRoot()
	if !due || root == "" {
		return 0, nil
	}
	tenants, err := os.ReadDir(root)
	if err != nil {
		return 0, nil
	}
	now := b.ledger.Now()
	cooled := 0
	for _, t := range tenants {
		owner := t.Name()
		if !t.IsDir() || !sandbox.IsSlug(owner) {
			continue
		}
		policy, err := b.policies.For(ctx, owner)
		if err != nil {
			b.log.Warn("读租户的策略失败，这一轮不转冷", "tenant", owner, "error", errText(err))
			continue
		}
		days := policy.Workspace.ColdAfterDays
		if days == 0 {
			days = cold.DefaultDays
		}
		if days <= 0 || b.reg.Archive(policy.Backend) {
			continue
		}
		workspaces, _ := os.ReadDir(filepath.Join(root, owner))
		names := []string{}
		for _, w := range workspaces {
			if w.IsDir() && sandbox.IsSlug(w.Name()) {
				names = append(names, w.Name())
			}
		}
		sort.Strings(names)
		for _, ws := range names {
			ok, err := b.coolOne(ctx, owner, ws, filepath.Join(root, owner, ws), days, now, cold.Runtime)
			if err != nil {
				b.log.Warn("工作区转冷失败，本地目录留着", "workspace", ws, "error", errText(err))
				continue
			}
			if ok {
				cooled++
			}
		}
	}
	return cooled, nil
}

func mtime(p string) string {
	st, err := os.Stat(p)
	if err != nil {
		return ""
	}
	return ledger.ISO(st.ModTime())
}

func (b *Broker) coolOne(ctx context.Context, owner, ws, local string, days int, now time.Time, runtime string) (bool, error) {
	w, err := b.archiver.Get(ctx, owner, ws)
	if err != nil || w == nil || w.State != "active" {
		return false, err
	}
	used, err := b.ledger.WorkspaceLastUsed(ctx, owner, ws)
	if err != nil {
		return false, err
	}
	last := ""
	for _, s := range []string{used, w.UpdatedAt, mtime(local), mtime(filepath.Join(local, ".git", "index"))} {
		if s > last {
			last = s
		}
	}
	if t := ledger.ParseISO(last); !t.IsZero() && now.Sub(t) < time.Duration(days)*24*time.Hour {
		return false, nil
	}
	if envs, err := b.ledger.WorkspaceEnvs(ctx, owner, ws); err != nil || len(envs) > 0 {
		return false, err
	}
	if ok, err := b.archiver.Transition(ctx, owner, ws, "active", "cooling"); err != nil || !ok {
		return false, err
	}
	snapID, err := b.coolSnapshot(ctx, owner, ws, runtime)
	if err != nil {
		_, _ = b.archiver.Transition(context.WithoutCancel(ctx), owner, ws, "cooling", "active")
		return false, err
	}
	if ok, err := b.archiver.Transition(ctx, owner, ws, "cooling", "cold"); err != nil || !ok {
		return false, err
	}
	// 状态已经是 cold：以快照为准。删不干净的残留在下次恢复前由 Broker 清掉。
	_ = os.RemoveAll(local)
	b.log.Info("工作区闲置够天数，转冷", "workspace", ws, "days", days, "snapshot", snapID)
	return true, nil
}

func (b *Broker) coolSnapshot(ctx context.Context, owner, ws, runtime string) (string, error) {
	// 抢到之后再看一次：抢之前刚建的环境还在就让开。
	if envs, err := b.ledger.WorkspaceEnvs(ctx, owner, ws); err != nil || len(envs) > 0 {
		if err == nil {
			err = providererr.New(providererr.Invalid, "workspace_in_use", "工作区刚被用上")
		}
		return "", err
	}
	busy := CoolBusyFor
	lease, err := b.Acquire(ctx, &commonv1.TenantContext{TenantId: owner, WorkspaceId: ws},
		AcquireArgs{Spec: sandbox.Spec{Runtime: runtime, WorkspaceID: ws}, BusyFor: &busy, Cooling: true})
	if err != nil {
		return "", err
	}
	defer func() { _ = b.Release(context.WithoutCancel(ctx), lease, false, 0) }()
	snap, err := b.Snapshot(ctx, lease)
	if err != nil {
		return "", err
	}
	stored, _, err := b.archiver.Manifest(ctx, owner, snap)
	if err != nil {
		return "", err
	}
	arch, ok := lease.Box.(sandbox.Archive)
	if !ok {
		return "", providererr.New(providererr.Invalid, "unsupported_archive", "这个沙箱后端不能导出工作区")
	}
	live, err := arch.Manifest(ctx, lease.EnvID)
	if err != nil {
		return "", err
	}
	if !archive.SameShape(stored, live) {
		return "", providererr.New(providererr.Invalid, "workspace_snapshot_mismatch", "快照与本地的工作区对不上")
	}
	return snap.SnapshotID, nil
}
