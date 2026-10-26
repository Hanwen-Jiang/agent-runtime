package server

// english 是给用户看的说明的英文，键是代码里的中文原文（见 i18n.go）。新加一句中文就在这里加它的英文：
// TestEveryMessageHasEnglish 按源码核对每一句都有、参数个数对得上、这里没有用不到的。
// 用词与网页一致：“项目”在界面上叫 workspace。
var english = map[string]string{
	// 登录与请求
	"请先登录":                         "Please sign in first",
	"登录已过期，请重新登录":                  "Your session has expired. Please sign in again.",
	"缺少 X-Coding-Web 头":            "Missing the X-Coding-Web header",
	"请求体不是合法的 JSON":                "The request body isn't valid JSON",
	"没有打开开发登录（CODING_DEV_LOGIN=1）": "Dev sign-in isn't enabled (CODING_DEV_LOGIN=1)",
	"租户与用户名只能用小写字母、数字、- 与 _，以字母或数字开头":                "Tenant and username may only use lowercase letters, digits, - and _, and must start with a letter or digit",
	"gh-、gitee-、gl-、gitea-、bb-、google- 开头的租户留给第三方登录": "Tenants starting with gh-, gitee-, gl-, gitea-, bb- or google- are reserved for third-party sign-in",
	"写租户策略失败：%v":    "Couldn't write the tenant policy: %v",
	"租户策略文件格式不对：%v": "The tenant policy file is malformed: %v",

	// 第三方登录与账号
	"没有配置这个登录方式":                  "This sign-in method isn't configured",
	"先登录，再连接 %s":                  "Sign in first, then connect %s",
	"登录状态不匹配，请重新登录":               "Sign-in state doesn't match. Please sign in again.",
	"%s 没有授权：%s":                  "%s didn't grant access: %s",
	"%s 登录没有完成：%v":                "Signing in with %s didn't complete: %v",
	"读不到 %s 账号：%v":                "Couldn't read your %s account: %v",
	"%s 没有给出账号编号或登录名":             "%s didn't return an account ID or username",
	"登录已过期，请重新登录后再连接":             "Your session has expired. Sign in again, then connect.",
	"这个 %s 账号已经关联到别的账号了":          "This %s account is already linked to another account",
	"保存关联失败":                      "Couldn't save the link",
	"建账号失败":                       "Couldn't create the account",
	"这个账号已经连着另一个 %s 账号，先断开它":      "This account is already connected to a different %s account. Disconnect it first.",
	"保存 %s 账号失败":                  "Couldn't save the %s account",
	"没有连接这个平台":                    "This platform isn't connected",
	"这是这个账号唯一的登录方式，先连接另一家再断开":     "This is the account's only sign-in method. Connect another one before disconnecting it.",
	"没有配置这个 Git 平台":               "This Git platform isn't configured",
	"先在“账号与平台”里连接 %s":             "Connect %s under Account & platforms first",
	"先在“账号与平台”里连接 %s，才能推送到 %[1]s": "Connect %s under Account & platforms before pushing to %[1]s",
	"令牌记录损坏":                      "The stored token is corrupted",
	"令牌记录解不开":                     "The stored token can't be decrypted",
	"不认识的登录方式 %s":                 "Unknown sign-in method %s",

	// 与 Git 平台、Google 打交道
	"连不上 %s":         "Can't reach %s",
	"请求地址不对":         "Invalid request URL",
	"%s 返回的令牌格式不对":   "%s returned a malformed token",
	"%s 拒绝了授权：%s %s": "%s refused the authorization: %s %s",
	"授权过期且续不上：%v":    "Authorization expired and couldn't be renewed: %v",
	"%s 授权失效，请在“账号与平台”里重新连接 %[1]s": "%s authorization is no longer valid. Reconnect %[1]s under Account & platforms.",
	"%s：%s": "%s: %s",
	"这个 Google 账号的邮箱还没有验证": "This Google account's email address isn't verified yet",
	"%s 不是 Git 平台":         "%s isn't a Git platform",

	// 工作区、会话与文件
	"项目不存在": "Workspace not found",
	"项目名要有，且不超过 80 个字符":                "A workspace name is required (at most 80 characters)",
	"只支持 https 的 Git 地址，且地址里不能带用户名或密码": "Only https Git URLs are supported, and the URL can't contain a username or password",
	"分支名不合法":    "Invalid branch name",
	"分支名不合法：%s": "Invalid branch name: %s",
	"没有这个示例":    "No such sample",
	"建项目失败：%v":  "Couldn't create the workspace: %v",
	"会话不存在":     "Session not found",
	"开会话失败：%v":  "Couldn't start the session: %v",
	"标题不能为空":    "The title can't be empty",
	"路径不合法":     "Invalid path",
	"不能访问 .git": "Access to .git isn't allowed",
	"路径不在工作区里":  "The path is outside the workspace",
	"目录不存在":     "Folder not found",
	"文件不存在":     "File not found",

	// 任务
	"任务不存在": "Task not found",
	"任务描述不能为空，也不能超过 20000 个字":         "The task description can't be empty or longer than 20,000 characters",
	"沙箱等级只能是 read 或 write_reversible": "The sandbox level must be read or write_reversible",
	"模型倾向只能是 economy、balanced 或 best": "The model preference must be economy, balanced or best",
	"要给出 question_id 与 option":        "question_id and option are required",
	"连不上 task-service":                "Can't reach task-service",
	"用户在网页上取消":                        "Cancelled by the user in the web app",
	"还有任务在跑，等它结束或先取消":                 "A task is still running. Wait for it to finish or cancel it first.",
	"还有任务在跑，等它结束再提交":                  "A task is still running. Commit after it finishes.",
	"还有任务在跑，等它结束再推送":                  "A task is still running. Push after it finishes.",
	"还有任务在跑，等它结束再切换":                  "A task is still running. Switch branches after it finishes.",

	// 提交与推送
	"没有可提交的改动": "There are no changes to commit",
	"没有可推送的改动": "There are no changes to push",
	"只有从已配置的 Git 平台克隆的工作区能推送并开 PR":  "Only workspaces cloned from a configured Git platform can push and open a PR",
	"推送失败（是否有这个仓库的写权限？）：%v":         "Push failed (do you have write access to this repository?): %v",
	"由 Coding Agent 生成的改动，请审阅后再合并。": "Changes generated by Coding Agent. Please review before merging.",
	"还有未提交的改动：先推送、提交或丢弃":            "There are uncommitted changes. Push, commit or discard them first.",
	"不知道基线分支": "The base branch is unknown",
	"拉取失败：%v": "Fetch failed: %v",
}
