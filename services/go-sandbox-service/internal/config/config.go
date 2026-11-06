// Package config 是 Sandbox Service 的配置（环境变量）。
//
//	AR_SANDBOX_SERVICE_ADDR            监听地址（gRPC h2c 与 /healthz 同一个端口），默认 127.0.0.1:8097
//	AR_SANDBOX_BACKENDS_FILE           沙箱后端的登记（必填；格式见 internal/registry）
//	AR_TOOL_WORKSPACE_ROOT             挂卷时的工作区根（读文件的快路径、本地后端、删工作区时删目录；集群里可写地挂工作区卷）
//	AR_SANDBOX_STATE_DIR               本地后端的环境目录，默认 <AR_DATA_DIR>/sandboxes
//	AR_SANDBOX_AGENT_BIN               本地后端起的 sandbox-agent，默认 PATH 上的 sandbox-agent
//	AR_DATA_DIR、AR_RELATIONAL_DSN     业务库：设了 DSN 用 Postgres（行级安全），否则 <AR_DATA_DIR>/relational 下的 SQLite
//	AR_RELATIONAL_MAX_CONNS            Postgres 连接池上限，默认 8
//	AR_OBJECT_STORE_DIR、AR_S3_*       工作区存档的对象存储（与 Worker、task-service 同一个）
//	AR_SECRETS_FILE、AR_SECRETS_SEED_FILE  SecretStore：调用方令牌 sandbox-service/tokens/<调用方>、sandbox/token-key 等
//	AR_SANDBOX_SERVICE_CALLERS         登记的调用方，逗号分隔，默认 worker,task-service（task-service 只能读）
//	AR_REGISTRY_ADDRESS                Registry（租户策略）；没设时都用部署的默认后端
//	AR_DEV_TENANT_POLICIES_FILE        开发用的租户策略文件（{"tenants": {...}}），列出的租户以文件为准
//	AR_PKG_MIRROR_URL、AR_EGRESS_GATEWAY_URL  包镜像与出网网关（注入给 K8s 与本地后端的命令）
//	AR_SANDBOX_RUNTIME                 服务自己借环境（git、转冷）用的运行环境，默认 python
//	AR_SANDBOX_RECONCILE_INTERVAL_S    对账间隔，默认 60；0 不对账
//	AR_SANDBOX_HOLD_MAX_S              等人期间保留会话级环境的上限，默认 7200
//	AR_SANDBOX_SETUP_TIMEOUT_S         环境配置的安装脚本（ENV-02 的 setup）的超时，默认 600
//	AR_WORKSPACE_COLD_AFTER_DAYS       挂卷工作区闲置多少天转冷（租户策略没写时），默认 0（不转冷）
//	AR_WORKSPACE_CHUNKED_SNAPSHOTS     写分块清单，默认 false；先部署兼容读取的回退版本再启用，关闭不影响读取已有分块
//	AR_SANDBOX_POLICY_TTL_S            租户策略的缓存秒数，默认 60；0 不缓存（测试用）
//	AR_SANDBOX_TMP_DIR                 临时文件（工作区存档、D-17 构建产物，可达几个 GB）的目录，默认系统的临时目录
//	AR_PROFILE                         production 时没有 AR_RELATIONAL_DSN 拒绝启动
package config

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Config 是服务的配置。
type Config struct {
	Addr             string
	BackendsFile     string
	WorkspaceRoot    string
	StateDir         string
	AgentBinary      string
	DataDir          string
	RelationalDSN    string
	MaxConns         int
	ObjectStoreDir   string
	SecretsFile      string
	SecretsSeedFile  string
	Callers          []string
	RegistryAddress  string
	PoliciesFile     string
	MirrorURL        string
	EgressURL        string
	Runtime          string
	ReconcileEvery   time.Duration
	HoldMax          time.Duration
	ColdDays         int
	ChunkedSnapshots bool
	// PolicyTTL 是租户策略的缓存时长；负数不缓存（AR_SANDBOX_POLICY_TTL_S=0，测试改策略即时生效）。
	PolicyTTL  time.Duration
	Production bool
	// TmpDir 是临时文件的目录（空时系统的临时目录）。
	TmpDir       string
	SetupTimeout time.Duration
}

// FromEnv 读环境变量。
func FromEnv(getenv func(string) string) (Config, error) {
	c := Config{
		Addr: or(getenv("AR_SANDBOX_SERVICE_ADDR"), "127.0.0.1:8097"), BackendsFile: getenv("AR_SANDBOX_BACKENDS_FILE"),
		WorkspaceRoot: getenv("AR_TOOL_WORKSPACE_ROOT"), StateDir: getenv("AR_SANDBOX_STATE_DIR"), AgentBinary: getenv("AR_SANDBOX_AGENT_BIN"),
		DataDir: getenv("AR_DATA_DIR"), RelationalDSN: getenv("AR_RELATIONAL_DSN"), ObjectStoreDir: getenv("AR_OBJECT_STORE_DIR"),
		SecretsFile: getenv("AR_SECRETS_FILE"), SecretsSeedFile: getenv("AR_SECRETS_SEED_FILE"), RegistryAddress: getenv("AR_REGISTRY_ADDRESS"),
		PoliciesFile: getenv("AR_DEV_TENANT_POLICIES_FILE"), MirrorURL: getenv("AR_PKG_MIRROR_URL"), EgressURL: getenv("AR_EGRESS_GATEWAY_URL"),
		Runtime: or(getenv("AR_SANDBOX_RUNTIME"), "python"), Production: getenv("AR_PROFILE") == "production", TmpDir: getenv("AR_SANDBOX_TMP_DIR"),
	}
	if c.BackendsFile == "" {
		return c, errors.New("要设 AR_SANDBOX_BACKENDS_FILE（沙箱后端的登记）")
	}
	if c.SecretsFile == "" {
		return c, errors.New("要设 AR_SECRETS_FILE（调用方令牌与后端的密钥在这里）")
	}
	if c.DataDir == "" {
		c.DataDir = filepath.Join(os.TempDir(), "agentruntime-sandbox-service")
	}
	if c.StateDir == "" {
		c.StateDir = filepath.Join(c.DataDir, "sandboxes")
	}
	if c.ObjectStoreDir == "" {
		c.ObjectStoreDir = filepath.Join(c.DataDir, "objects")
	}
	if c.Production && c.RelationalDSN == "" {
		return c, errors.New("生产模式要配 AR_RELATIONAL_DSN，不回落 SQLite")
	}
	for _, v := range strings.Split(or(getenv("AR_SANDBOX_SERVICE_CALLERS"), "worker,task-service"), ",") {
		if v = strings.TrimSpace(v); v != "" {
			c.Callers = append(c.Callers, v)
		}
	}
	var err error
	switch getenv("AR_WORKSPACE_CHUNKED_SNAPSHOTS") {
	case "", "false":
	case "true":
		c.ChunkedSnapshots = true
	default:
		return c, errors.New("AR_WORKSPACE_CHUNKED_SNAPSHOTS 要是 true 或 false")
	}
	if c.MaxConns, err = intVar(getenv, "AR_RELATIONAL_MAX_CONNS", 8); err != nil {
		return c, err
	}
	every, err := intVar(getenv, "AR_SANDBOX_RECONCILE_INTERVAL_S", 60)
	if err != nil {
		return c, err
	}
	c.ReconcileEvery = time.Duration(every) * time.Second
	hold, err := intVar(getenv, "AR_SANDBOX_HOLD_MAX_S", 7200)
	if err != nil {
		return c, err
	}
	c.HoldMax = time.Duration(hold) * time.Second
	setup, err := intVar(getenv, "AR_SANDBOX_SETUP_TIMEOUT_S", 600)
	if err != nil {
		return c, err
	}
	if setup == 0 {
		return c, errors.New("AR_SANDBOX_SETUP_TIMEOUT_S 要是正整数（秒）")
	}
	c.SetupTimeout = time.Duration(setup) * time.Second
	if c.ColdDays, err = intVar(getenv, "AR_WORKSPACE_COLD_AFTER_DAYS", 0); err != nil {
		return c, err
	}
	if c.ColdDays > 365 {
		return c, errors.New("AR_WORKSPACE_COLD_AFTER_DAYS 要是 0 到 365 的整数（天）")
	}
	ttl, err := intVar(getenv, "AR_SANDBOX_POLICY_TTL_S", 60)
	if err != nil {
		return c, err
	}
	c.PolicyTTL = time.Duration(ttl) * time.Second
	if ttl == 0 {
		c.PolicyTTL = -1
	}
	return c, nil
}

func or(v, fallback string) string {
	if v == "" {
		return fallback
	}
	return v
}

func intVar(getenv func(string) string, key string, fallback int) (int, error) {
	v := getenv(key)
	if v == "" {
		return fallback, nil
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < 0 {
		return 0, fmt.Errorf("%s 要是非负整数", key)
	}
	return n, nil
}
