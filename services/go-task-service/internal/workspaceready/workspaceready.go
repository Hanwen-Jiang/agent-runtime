// Package workspaceready 记下正在建的工作区（WS-CREATE-RACE，docs/plan/all-plans-dag-2026-11-03.md）。
//
// Worker 建工作区时一开始就把业务库里那一行写成 active，环境、初始文件、提交与快照之后才陆续做完；这段时间里在它上面开的 Task
// 会另起一个空环境，之后的快照盖掉建工作区时写的文件。所以 CreateWorkspace 起工作流之前在 Cache 里记下“在建”，工作流真正结束时
// （不跟着客户端断开）清掉，SubmitTask 见到这个标记就拒。Cache 按租户绑定，键里只有工作区 ID；兜底的过期时间防止进程挂了留下标记。
package workspaceready

import (
	"context"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
)

func key(workspaceID string) string { return "workspace_creating:" + workspaceID }

// Mark 记下这个工作区正在由 operationID 这次操作建；ttl 是兜底（工作流的执行超时）。
func Mark(ctx context.Context, c cache.Cache, workspaceID, operationID string, ttl time.Duration) error {
	return c.Set(ctx, key(workspaceID), []byte(operationID), ttl)
}

// Clear 在 operationID 这次操作结束时清掉标记；标记已经换成别的操作（同一个工作区又建了一次）时不动。
func Clear(ctx context.Context, c cache.Cache, workspaceID, operationID string) error {
	v, ok, err := c.Get(ctx, key(workspaceID))
	if err != nil || !ok || string(v) != operationID {
		return err
	}
	_, err = c.Delete(ctx, key(workspaceID))
	return err
}

// Pending 返回这个工作区是不是还在建。
func Pending(ctx context.Context, c cache.Cache, workspaceID string) (bool, error) {
	_, ok, err := c.Get(ctx, key(workspaceID))
	return ok, err
}
