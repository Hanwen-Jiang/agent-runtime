package broker

import (
	"context"
	"sort"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
)

// BuildNetwork 是构建项目环境时的联网（D-17）：在环境表里记一行（后端 env-builder、隔离 microvm），给它一条平台批准的授权
// （只放行部署登记的 build_hosts：镜像仓库与系统包的源），再签发出网与包镜像的凭据。出网网关于是按同一套规则放行构建
// 拉基础镜像，用量与审计照记。构建结束（成功或失败）收回授权、记为销毁。
type BuildNetwork struct {
	b     *Broker
	hosts []string

	mu      sync.Mutex
	records map[string]ledger.Record
}

// NewBuildNetwork 构造。
func NewBuildNetwork(b *Broker, hosts []string) *BuildNetwork {
	seen := map[string]bool{}
	var sorted []string
	for _, h := range hosts {
		if !seen[h] {
			seen[h] = true
			sorted = append(sorted, h)
		}
	}
	sort.Strings(sorted)
	return &BuildNetwork{b: b, hosts: sorted, records: map[string]ledger.Record{}}
}

// BuildTaskID 是构建在环境表与授权里记的 task_id。
func BuildTaskID(key string) string { return "envbuild-" + key[:16] }

func buildEnvID(key string) string { return "envb-" + key[:16] }

// Open 记环境、给授权、签发凭据；返回构建要的代理与镜像源变量。
func (n *BuildNetwork) Open(ctx context.Context, owner, key string, timeout time.Duration) (map[string]string, error) {
	led := n.b.ledger
	lifetime := timeout + 10*time.Minute
	task := BuildTaskID(key)
	rec, err := led.BeginCreate(ctx, owner, ledger.CreateArgs{Backend: BuildBackend, Kind: "k8s", Isolation: "microvm", TaskID: task,
		Idle: lifetime, MaxLifetime: lifetime})
	if err != nil {
		return nil, err
	}
	if rec, err = led.MarkReady(ctx, rec, buildEnvID(key), "", false); err != nil {
		return nil, err
	}
	n.mu.Lock()
	n.records[owner+"\x00"+key] = rec
	n.mu.Unlock()
	if len(n.hosts) > 0 {
		minutes := int(lifetime.Minutes()) + 1
		if err := led.CreatePlatformGrant(ctx, owner, task, n.hosts, time.Duration(minutes)*time.Minute, "构建项目环境"); err != nil {
			return nil, err
		}
	}
	if n.b.creds == nil {
		return map[string]string{}, nil
	}
	return n.b.creds.ExecEnv(ctx, rec, registry.Spec{"kind": "k8s"}, registry.Quota{})
}

// Close 收回授权、记为销毁。
func (n *BuildNetwork) Close(ctx context.Context, owner, key string) error {
	n.mu.Lock()
	rec, ok := n.records[owner+"\x00"+key]
	delete(n.records, owner+"\x00"+key)
	n.mu.Unlock()
	if !ok {
		found, err := n.b.ledger.ByBackendEnv(ctx, owner, BuildBackend, buildEnvID(key))
		if err != nil || found == nil {
			return err
		}
		rec = *found
	}
	if rec.State != "ready" {
		return nil
	}
	_, err := n.b.DestroyRecord(ctx, rec, "build finished", false, true)
	return err
}
