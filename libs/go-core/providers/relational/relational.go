// Package relational 定义 RelationalStore 接口（设计书 4.3）：事务读写、按租户分区的表、行级安全。
//
// 两种语言共用：Python 侧见 libs/py-core/src/agentruntime_core/providers/relational_store.py，方法一一对应，语义一致：
//   - 租户隔离由存储负责，应用 SQL 不写租户条件，也不出现租户列。开发实现（SQLite）每个作用域一个库；
//     生产实现（Postgres）用行级安全，事务开始时设置租户会话变量、连接归还时清除（S-12a）。
//   - SQL 用可移植子集：参数占位符写 $1、$2…；列类型只用 TEXT、BIGINT、DOUBLE PRECISION，取回的值为
//     string、int64、float64 或 nil。布尔、JSON、时间由调用方编码。
//   - Transaction：fn 返回 nil 时提交；返回错误或 panic 时回滚，错误原样返回。只读事务里写入：Invalid。
//   - 错误分类：唯一约束冲突 Conflict；语法错误、表或列不存在、QueryOne 得到多行 Invalid；
//     死锁、序列化失败、连接中断 Transient。
//   - Migrate 对所有作用域生效（已有的与以后绑定的），按 Version 递增执行，已执行过的版本跳过。
package relational

import (
	"context"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Provider 是长期存在的工厂。
type Provider interface {
	Bind(tenant *commonv1.TenantContext) (Store, error)
	BindPlatform(purpose string) (Store, error)
	Migrate(ctx context.Context, migrations []Migration) error
	Close() error
}

// Migration 是一次结构变更。
type Migration struct {
	Version    int
	Name       string
	Statements []string
}

// Row 是一行结果，列名到值。
type Row map[string]any

// TxOptions 是事务参数。
type TxOptions struct {
	ReadOnly bool
}

// Tx 是一个进行中的事务。
type Tx interface {
	// Execute 返回受影响的行数。
	Execute(ctx context.Context, sql string, args ...any) (int64, error)
	Query(ctx context.Context, sql string, args ...any) ([]Row, error)
	// QueryOne 没有结果时 ok 为 false；多于一行时返回 Invalid。
	QueryOne(ctx context.Context, sql string, args ...any) (row Row, ok bool, err error)
}

// Store 是绑定了作用域的客户端。
type Store interface {
	Transaction(ctx context.Context, opts TxOptions, fn func(ctx context.Context, tx Tx) error) error
}
