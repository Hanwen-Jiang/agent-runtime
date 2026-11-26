# AGENTS.md

本仓库（`agent-runtime`）的所有架构设计、术语、硬性规则与常用命令均定义在 [CLAUDE.md](CLAUDE.md) 中，执行任何任务前必须先阅读并严格遵守。

## Git 提交与推送约定

- **托管与提交频率**：代码托管在 GitHub（私有仓库，`gh` 已登录）；每完成一个小阶段（功能点、修复、文档或配置收口）就立即提交并推送（`git push`），不要把多阶段改动攒在一起。
- **提交身份**：每次提交必须使用 `hanwen-jiang <haven-just@qq.com>` 作为 Author / Committer（确保仓库本地已配置 `user.name=hanwen-jiang`、`user.email=haven-just@qq.com`，或在命令中显式指定 `git -c user.name="hanwen-jiang" -c user.email="haven-just@qq.com" commit ...`）。
- **随机共创（Co-authored-by）**：每次提交时，从以下 11 个账号中**随机选择** 1 个，在提交信息末尾空一行后附带 `Co-authored-by:` trailer：
  - `Co-authored-by: thail-man <3852743911@qq.com>`
  - `Co-authored-by: doido-net <3928986093@qq.com>`
  - `Co-authored-by: xiaochanmiao <abc3824580@qq.com>`
  - `Co-authored-by: jackmiller570 <abc3931097@qq.com>`
  - `Co-authored-by: henry-moore9655 <abc3979145@qq.com>`
  - `Co-authored-by: chloe-martin9136 <abc3963781@qq.com>`
  - `Co-authored-by: henrymiller1718 <abc3983338@qq.com>`
  - `Co-authored-by: oliviaanderson6811 <abc3939429@qq.com>`
  - `Co-authored-by: caleb-thomas4571 <abc3846503@qq.com>`
  - `Co-authored-by: isaactaylor6734 <abc3825390@qq.com>`
  - `Co-authored-by: jack-butler1992 <eckardroussos@gmail.com>`
- **提交命令示例**：
  ```bash
  git -c user.name="hanwen-jiang" -c user.email="haven-just@qq.com" commit \
    -m "feat: <本次小阶段摘要>" \
    -m "Co-authored-by: <随机选出的用户名> <<对应邮箱>>"
  git push
  ```
- **安全红线**：严禁将任何 `GH_*_ALL_KEY` 或 token 明文写入代码、文档、提交信息或日志；仅使用上述公开用户名与邮箱作为 `Co-authored-by:`。
