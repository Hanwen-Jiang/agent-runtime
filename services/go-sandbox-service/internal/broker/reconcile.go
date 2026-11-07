package broker

import (
	"context"
	"regexp"
	"sort"
	"syscall"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
)

// 对账（W9；D-11；docs/plan/sandbox-platform.md 第 1 节）。SBX-01 起由 Sandbox Service 自己按间隔跑（原来是 Worker 上的
// 一个 Temporal 工作流）；多副本时靠环境表的状态转换幂等（谁先改成 destroying 谁删），重复跑不出错。一轮做的事：
//
//  1. 列出每个能列的后端在跑的环境（列不了的后端这一轮跳过，不据此判丢失）。
//  2. 平台目录里没结束的环境，逐个读所属租户的行：creating 超过宽限还没就绪记 failed；ready 而后端的列表里没有记 lost；
//     ready 且空闲到期或活满、没有命令在跑：销毁（存档模式的可写环境先存档）。例外（N-05）：会话级的环境空闲到期、而它最近
//     服务的 Task 还没结束时顺延一个空闲期，从最后一次使用算起最多 HoldMax，也不超过活满；destroying：再删一次。
//     目录里有、租户的行没有：超过宽限把目录记为 failed。
//  3. 后端列出来的环境，环境表里没有或已经结束的：超过宽限（按后端记的创建时间）就是孤儿，销毁。

// DefaultHoldMax 是等人期间保留会话级环境的上限。
const DefaultHoldMax = 2 * time.Hour

// Report 是一轮对账的结果。
type Report struct {
	Listed           int
	StaleFailed      int
	Lost             int
	Reaped           int
	Held             int
	DestroyRetried   int
	OrphansDestroyed int
	Cooled           int
	// VolumeUsedRatio 是挂卷的工作区所在卷的已用比例（DISK-A）；没有工作区根时为 -1。
	VolumeUsedRatio float64
	SkippedBackends []string
	Errors          []string
}

// VolumeMetric 是卷用量的指标名（Grafana 的面板与告警读它，deploy/data）。
const VolumeMetric = "runtime.workspace.volume_used_ratio"

// volumeUsedRatio 量工作区根所在的卷（不跟随里面的符号链接）；量不出时 -1。
func volumeUsedRatio(root string) float64 {
	if root == "" {
		return -1
	}
	var st syscall.Statfs_t
	if err := syscall.Statfs(root, &st); err != nil || st.Blocks == 0 {
		return -1
	}
	return float64(st.Blocks-st.Bfree) / float64(st.Blocks)
}

var ownerPattern = regexp.MustCompile(`^(_platform|[a-z0-9][a-z0-9_-]{0,62})$`)

// HoldUntil 是等人期间顺延到什么时候；已经到头时零值。
func HoldUntil(rec ledger.Record, now time.Time, holdMax time.Duration) time.Time {
	last := ledger.ParseISO(rec.LastUsedAt)
	if last.IsZero() {
		last = ledger.ParseISO(rec.ReadyAt)
	}
	if last.IsZero() || rec.SessionID == "" {
		return time.Time{}
	}
	until := now.Add(time.Duration(rec.IdleSeconds) * time.Second)
	if t := last.Add(holdMax); t.Before(until) {
		until = t
	}
	if end := ledger.ParseISO(rec.MaxUntil); !end.IsZero() && end.Before(until) {
		until = end
	}
	if !until.After(now) {
		return time.Time{}
	}
	return until
}

type listing struct {
	backend string
	item    sandbox.ListedEnvironment
}

// Reconcile 跑一轮对账。
func (b *Broker) Reconcile(ctx context.Context, grace, holdMax time.Duration) (Report, error) {
	report := Report{VolumeUsedRatio: volumeUsedRatio(b.reg.WorkspaceRoot())}
	if err := b.reconcileTransferUsage(ctx); err != nil {
		report.Errors = append(report.Errors, "transfer accounting: "+errText(err))
	}
	now := b.ledger.Now()
	listings := map[string]map[string]sandbox.ListedEnvironment{}
	for _, name := range b.reg.Names() {
		if !b.reg.Buildable(name) {
			continue
		}
		provider, err := b.reg.Provider(ctx, name)
		if err != nil {
			report.SkippedBackends = append(report.SkippedBackends, name+"："+errText(err))
			continue
		}
		lister, ok := provider.(sandbox.Lister)
		if !ok {
			report.SkippedBackends = append(report.SkippedBackends, name+"：不能列出")
			continue
		}
		items, err := lister.ListEnvironments(ctx)
		if err != nil {
			report.SkippedBackends = append(report.SkippedBackends, name+"："+errText(err))
			continue
		}
		m := map[string]sandbox.ListedEnvironment{}
		for _, it := range items {
			m[it.ID] = it
		}
		listings[name] = m
	}
	seen := map[string]listing{}
	names := make([]string, 0, len(listings))
	for n := range listings {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		for id, it := range listings[n] {
			if _, dup := seen[id]; !dup {
				seen[id] = listing{n, it}
			}
		}
	}
	report.Listed = len(seen)

	dir, err := b.ledger.Directory(ctx, true)
	if err != nil {
		return report, err
	}
	handled := map[string]bool{}
	for _, entry := range dir {
		if err := b.reconcileEntry(ctx, entry, now, grace, holdMax, listings, handled, &report); err != nil {
			report.Errors = append(report.Errors, entry.EnvRef+"："+errText(err))
		}
	}

	ids := make([]string, 0, len(seen))
	for id := range seen {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		if handled[id] {
			continue
		}
		l := seen[id]
		if !ownerPattern.MatchString(l.item.Owner) {
			report.Errors = append(report.Errors, id+"：后端记的作用域格式不对，不碰")
			continue
		}
		rec, err := b.ledger.ByBackendEnv(ctx, l.item.Owner, "", id)
		if err != nil {
			report.Errors = append(report.Errors, id+"："+errText(err))
			continue
		}
		if rec != nil && (rec.State == "ready" || rec.State == "creating" || rec.State == "destroying") {
			continue // 目录里漏了它也照样活着：不当孤儿
		}
		if rec == nil && (l.item.CreatedAt.IsZero() || now.Sub(l.item.CreatedAt) <= grace) {
			continue // 刚建的，可能还没来得及记进环境表
		}
		provider, err := b.reg.Provider(ctx, l.backend)
		if err != nil {
			report.Errors = append(report.Errors, id+"："+errText(err))
			continue
		}
		var box sandbox.Sandbox
		if l.item.Owner == ledger.PlatformID {
			box, err = provider.BindPlatform("sandbox-reconcile")
		} else {
			box, err = provider.Bind(&commonv1.TenantContext{TenantId: l.item.Owner})
		}
		if err == nil {
			err = box.Destroy(ctx, id)
		}
		if err != nil && providererr.CategoryOf(err) != providererr.NotFound {
			report.Errors = append(report.Errors, id+"："+errText(err))
			continue
		}
		report.OrphansDestroyed++
	}
	cooled, err := b.CoolIdleWorkspaces(ctx)
	report.Cooled = cooled
	if err != nil {
		report.Errors = append(report.Errors, "cold："+errText(err))
	}
	return report, nil
}

func (b *Broker) reconcileEntry(ctx context.Context, entry ledger.DirectoryEntry, now time.Time, grace, holdMax time.Duration,
	listings map[string]map[string]sandbox.ListedEnvironment, handled map[string]bool, report *Report) error {
	rec, err := b.ledger.Get(ctx, entry.Owner, entry.EnvRef)
	if err != nil {
		return err
	}
	if rec == nil {
		if stale := ledger.ParseISO(entry.UpdatedAt); !stale.IsZero() && now.Sub(stale) > grace {
			report.StaleFailed++
			return b.ledger.SetDirectory(ctx, entry.EnvRef, entry.Owner, entry.Backend, "failed")
		}
		return nil
	}
	if rec.BackendEnvID != "" {
		handled[rec.BackendEnvID] = true
	}
	list, listed := listings[rec.Backend]
	switch rec.State {
	case "creating":
		if created := ledger.ParseISO(rec.CreatedAt); !created.IsZero() && now.Sub(created) > grace {
			report.StaleFailed++
			_, err := b.ledger.MarkFailed(ctx, *rec, "creating 超过宽限还没就绪（服务中途退出？）")
			return err
		}
	case "ready":
		if _, alive := list[rec.BackendEnvID]; listed && !alive {
			report.Lost++
			b.forget(rec.EnvRef)
			_, err := b.ledger.MarkLost(ctx, *rec, "后端的列表里没有这个环境")
			return err
		}
		if rec.Expired(now) && b.ledger.IdleNow(*rec) {
			lifetime := rec.MaxUntil != "" && rec.MaxUntil <= ledger.ISO(now)
			if !lifetime {
				if until := HoldUntil(*rec, now, holdMax); !until.IsZero() && b.ledger.TaskRunning(ctx, rec.Owner, rec.TaskID) {
					report.Held++
					_, err := b.ledger.Hold(ctx, *rec, until)
					return err
				}
			}
			reason := "idle expired"
			if lifetime {
				reason = "max lifetime"
			}
			report.Reaped++
			_, err := b.DestroyRecord(ctx, *rec, reason, true, lifetime)
			return err
		}
	case "destroying":
		report.DestroyRetried++
		reason := rec.Reason
		if reason == "" {
			reason = "retry destroy"
		}
		_, err := b.DestroyRecord(ctx, *rec, reason, true, false)
		return err
	}
	return nil
}
