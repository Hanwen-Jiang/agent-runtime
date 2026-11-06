# D-27 个人配置实现与验收（2026-11-07）

- `personalconfig.v1.PersonalConfigService` 在 Task API 的公共端口上提供本人的 Get/Put/Retire。tenant_id 只校验认证身份；没有指定用户的入口。Registry kind=8、`~<user>/personal-config`、`user/vN` 不可变内容；退役后固定版本仍可读取。通用 Publish 不接受这个私有 kind。
- Migration 215 在 tasks 保存 `personal_config_pin`、`personal_config_selection`。提交时按真实 submitted_by 原子固定版本+SHA256 或 `none`；重试不随新配置变化。Worker 与 Sandbox 只在 activity/服务代码读业务库，Workflow 内不读 DB。
- instructions 最多 4KiB；进入 Planner 的 user message 与 ContextBuilder 已有 profile 分区（user 角色、计入 token）。当前明确要求优先。trace 仅带版本/digest，不记录正文。
- files 最多10份、合计256KiB UTF-8文本，拒绝绝对/穿越/重复/祖先冲突路径、启动脚本与明显明文凭据。所有文件校验和路径预检先完成，之后逐份以0600/noclobber写入HOME；已有文件/符号链接不覆盖。shell_run 的 environment.personal_config 返回 applied/skipped/errors；新的固定文件包不能复用旧包的HOME。local backend使用环境独占临时HOME，绝不写宿主用户HOME。
- defaults.skill_names / mcp_connection_ids 在提交时与已有可见Skills/用户已启用MCP工具求交，只缩小集合。引用不可用时返回 personal_skill_unavailable / personal_mcp_unavailable。`SubmitTask.inputs.personal_config` 的同名数组可覆盖：显式[]禁用该类；字段缺省用包默认；包未设置该默认时保留旧全可见行为。选择同样固定，重复提交不能换选择。
- environment_id 是 coding-web 本人配置目录的引用；网页新会话预选后通过原有 WorkspaceEnvironment 快照创建链路应用。已选环境/已有会话优先，Runtime 不把ID当可读取任意机密配置的授权。

验证：Registry 全套 SQLite/PostgreSQL（非root PostgreSQL）通过；新增版本/私有隔离/none/旧版本/digest/并发/选择重试测试在两种数据库通过。PersonalConfig HTTP 真实认证与正文不进日志通过。MCP真实SQLite租约筛选、空选择、权限交集与失败整事务回滚通过；HOME真实shell预检/不覆盖/0600通过。taskapi、cmd/task-service、broker与相关Go包通过。Python个人pin+Planner/tool_loop相关43条通过。

凭据格式复核：文件内容增加 GitHub `github_pat_`、GitLab `glpat-`、npm
`_auth`、Authorization 的 Bearer/Basic 字面值，以及 URL userinfo 中明文密码或
直接令牌的识别；百分号编码的 userinfo 先解码。`${VAR}` 及已有命名引用形式仍允许，
包括 Authorization 引用、npm 引用和固定用户名搭配引用密码的 URL。JSON 同行多个
配置项逐项检查，不能用后面的引用遮住前面的明文值。这里只覆盖明确已知格式和字段，
不承诺识别任意密钥，不解析或展开变量，不扩展为通用内容防泄漏系统。

路径复核补齐已有 Bash/Zsh 启动文件禁用列表的 `.bash_login`、`.bash_logout`、
`.zshenv`、`.zlogin`、`.zlogout`。穿越、绝对路径、Windows 路径、大小写变体、
保留目录和脚本后缀补回归；普通 `.npmrc` 和 `.config` 文本配置仍可用。
本轮仅静态审查、格式检查与编写回归，单元执行等待 Firecracker 独占测量窗口释放。

Firecracker 测量窗口释放后，执行 `go test ./personalconfig -run
'Test(CommonPlaintextCredentialFormatsAreRejected|CredentialVariableReferencesRemainConfiguration|ConfigurationPathsStayInsideNonExecutableHomeFiles)'
-count=1 -v`：3组、38个子用例全部通过，包耗时0.013s。证据保存在
`/root/proj/agent-projs/.goal-tools/personal-secret-review.log`；未运行模型或产生评测费用。
