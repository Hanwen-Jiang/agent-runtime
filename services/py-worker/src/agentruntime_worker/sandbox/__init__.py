"""沙箱平台的 Worker 一侧（docs/plan/sandbox-platform.md）。借还环境、环境表、选后端、存档与恢复、对账、按项目构建环境都在
Sandbox Service 里（SBX-01，services/go-sandbox-service）；这里是它的客户端（client）、Worker 还要读的租户策略（policy）、
联网授权的记录（grants）、环境配置的解析（env_config）与搬走之后留下的 activity（retired）。"""
