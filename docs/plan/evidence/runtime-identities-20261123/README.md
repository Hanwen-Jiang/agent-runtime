# 历史 Runtime 运行身份核对

本次冻结 evals 站点当时可按稳定 ID 定位的 35 次 SWE 运行、11 次 Terminal 运行、20 次 Task Loop、2 次 DeepEval、4 次 Worker 上下文实验，以及既有的 Mini 50 活动入口，共 73 项。站点的历史 `medium` 分组只枚举这些 ID，不作为模型推理档的证据。前端另为没有运行 ID 的历史报告记录生成固定指纹。金补丁检查、纯模型能力矩阵、无模型的工程对照未纳入这组。

运行身份表中，58 项有可追溯的 Runtime 发布号、Worker 镜像标识、Git 树/提交或 Reviewer 源码快照；43 项有运行协议、部署或逐调用证据支持路由模式；41 项有实际运行模型的来源；10 项有可关联的实际生成档位收据。发布号是运行时声明时，字段 `versionKind=reported-release`。Reviewer 组件版本取自身源码快照。模型名单来自可匹配的成功网关收据、按原始 Task ID 聚合的非嵌入生成结算，或原组件的模型调用统计。旧账本缺少推理档，因此不能单凭模型名单补出 low、medium 或 high。观察到的档位仅覆盖已保留的收据。

固定/自动协议先用原运行目录对应的 `paired-protocol.json`、`recovery-protocol.json`、`route-protocol.json` 和 Terminal 原实验 `frozen.json` 对账。两个早期 Sphinx 冒烟运行虽要求固定 Profile，但真实结算命中过其他模型，身份表标为 `mixed` 并保留原要求。两题有资金的原生路由试验同时观测到 low 和 medium。未取得模型 Task 的试跑最多能记录所请求的路由和档位。

[逐项证据及原文件 SHA](run-audit.json) · [前端运行身份](../../../../services/py-learning/src/agentruntime_learning/evals/site_ui/src/runtime-identities.json) · [独立的历史显示组](../../../../services/py-learning/src/agentruntime_learning/evals/site_ui/src/runtime-display-groups.json) · [早期部署记录](../../cloud-deploy.md) · [两题原生/固定对照](../swe-mini50-20261121/routing-pilot/funded/comparison.json)

线上用 Chrome 核对了 Mini 50 三个成绩点、固定模型与原生路由两条明细，以及模型矩阵的暂停状态。现有 Runtime 记录的分组徽标只显示 `medium`。[发布与验收](production-verification.json)
