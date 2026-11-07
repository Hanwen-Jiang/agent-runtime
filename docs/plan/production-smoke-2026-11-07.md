# 生产无模型验收（2026-11-07）

本次直接运行已部署服务。首批Worker与Sandbox Service为`goal-f1388e13`，coding-web为`goal-0fbf18e`；最终Kata网页流程使用修复后的Sandbox Service `goal-0ee834d8`，Worker/Web不变。FC manager SHA-256 为 `eed2e5449423357ca151393de659d3fdb14866b37d78409c44a693f4174be77b`。运行前检查 Deployment 镜像、更新代数和可用副本、manager 二进制摘要及 `AR_WORKSPACE_CHUNKED_SNAPSHOTS=true`。

所有操作限定新建的唯一测试租户/工作区，没有调用 SubmitTask、模型、Git push 或桶生命周期 API。报告记录的模型费用为 $0，且两个服务验收租户的 model_usage 均为空；沙箱用时与出网字节另留原始 usage，不把它们称为供应商实付费用。

## 已通过的生产链路

- WK-LITE-01：Rust agent 产生 1 MiB stdout 与 128 KiB stderr 并直接上传。Worker 收到两路共 16,044 字节摘录与对象引用，大小及 SHA-256 一致；context_read 按引用读取窗口匹配。保留当前单流最大 64 MiB 捕获边界，不宣称无限输出完整保留。
- WK-LITE-02：12 MiB 文件只登记一个 artifact，同幂等键结果相同，真实对象下载摘要正确。
- WK-LITE-02网页下载补验：另以同隔离Web租户的临时FC策略完成`deliver_file → coding-web → Task API → ObjectStore`实际链路，线上下载HTTP200、2944字节SHA一致；no-store、nosniff、CSP sandbox、attachment正确。同租户共享Session的Bob合法读取200，跨租户Eve及错误Task均404。仅Task与会话关联记录是无模型夹具，不伪称提交了真实Task。测试后删除对象、夹具Task行、VM、Web项目和2个准确校验的本次cache块，租户策略已恢复原有效Kata文档。记录见`web-artifact-report-b.json`及policy/cleanup证据。
- WS-PULL-01：文件包含3个有序块、2个唯一内容块。第二次 snapshot 的 new_blobs=0；两次新VM恢复后整文件摘要正确。篡改本次manifest后两次恢复均拒绝交付环境，修复本次对象后恢复成功。
- WS-BLOB-01：已验证 PUT write-through 的节点缓存供两台新VM恢复，每次 hits 增2、misses 不增加。没有破坏生产共享cache做损坏或LRU压力测试；这些故障已由专用真实FC/R2 harness验收。
- D-27 HOME：通过隔离任务夹具行固定user/v1及digest，在真实FC环境安装0600文本文件；已有同路径内容保留，用户默认配置更新为v2后新借用仍安装Task固定的v1。夹具不启动Workflow，公开PersonalConfig接口的身份/不可变版本另由Web验收覆盖。
- NET-OPEN：通过实际GrantStore、Broker、FC与egress链路发出public授权，公网HTTPS返回200；元数据、loopback、私网IP、localhost、集群域名与25端口全部拒绝。撤销与到期后访问返回403；1-byte下载配额中止连接，审计明确`down_bytes_exceeded`。到期测试只调整本次grant的期限为now+2秒，不更改宿主时钟。此内部授权夹具不冒充Task API Submit端到端。
- D-27网页入口：真实Chrome156经线上HTTPS和两名合法隔离用户Cookie分别打开账号→个人配置，指令/文件/Skill/MCP/环境/保存控件均呈现，截图及身份证据保留。此浏览器检查只读，不提交任务。

服务验收耗时：传输完整流程36.016秒；HOME/网络边界23.782秒。报告、截图、运行脚本和SHA清单见同目录`evidence/`、`scripts/`与`manifest.json`。

## 保留的失败与尚待复验

首个传输夹具以`reuse=false`创建写环境，导致不保存session_id，随后deliver_file按会话借到新空VM。已按生产会话工具语义修正夹具为`reuse=true`；原失败报告与其完整清理证据保留，没有改产品断言。

Web预检遇Cloudflare对默认Python User-Agent的1010拒绝，改用项目明确的`agentruntime-goal-acceptance/1.0`应用标识；未修改防火墙或认证策略。最初goal-*租户不在coding issuer允许范围，因此改用现有获准前缀下全新的`gh-goalaccept-20261107`，未扩大issuer ACL。

Web个人配置HTTP CRUD、旧版本no-store、owner隔离、明文凭据拒绝及38ms异步空多仓创建/重试/异owner404已通过。但首Attach遇到真实空manifest `entries:null`恢复错误，原FAILED operation及报告保留；应用负责人已以`e3c3fa6c`修复并完成先红后绿回归。Service更新`goal-c2e9fcb4`后，同流程复验越过空恢复，在Kata后端克隆成功后遇R2直传失败，原503操作、egress审计和完整清理报告保留，不能用FC下载通过替代Kata失败。此时未标为通过；最终原Kata条件复验结果见下文。

网页下载的第一次脚本断言把HTTP头按大小写敏感字典读取，原始应用HTTP200并非失败；已改为HTTP规范的大小写无关读取，原报告保留，随后同等安全断言全部通过。未修改应用以掩盖这一夹具错误。

Service `goal-96772b80`补齐Kata快照每块上传的有限重试及ACK对账后，原Web流程已成功挂载source/reference两个仓库并通过非法路径拒绝。下一步新会话复制暴露维护读取缺少审计任务作用域：`WorkspaceOperations._current()`借只读环境时未传task_id，Kata恢复所需临时storage grant被`transfer_gateway_required`拒绝。失败操作、响应和完整清理证据已保存为`web-copy-transfer-scope-failure.json`及`web-copy-failed-operation.json`；交实现负责人作最小修复，不通过修改测试策略或TLS绕过该边界。

## 清理

两个transfer-accept-*租户所有测试VM、对象、作用域业务行已删除；只按本次随机内容摘要验证并删除5个专属缓存块，没有清空生产cache。Web测试项目已通过API删除、个人默认包退役；不可变版本和operation审计保留以便追溯。浏览器进程和独立profile已清理，测试Cookie仅在0600私有文件中使用，未写入证据。

## 最终Kata网页复验通过

2026-11-07 11:28 UTC，Service `goal-0ee834d8`包含空清单、局部上传重试/ACK对账和无Task维护恢复的独立审计作用域修复后，原Kata策略下完整流程退出0。没有切换为FC来掩盖Kata故障。

八组检查全部通过：个人配置不可变版本/digest/旧版本no-store/owner隔离，已知明文凭据拒绝，异步创建/同幂等键/异owner操作隐藏，多仓attach/list及非法路径拒绝，新会话复制与只读仓commit403、本人显式空工具选择，复制会话，clean detach不改基线，dirty detach默认409且保留仓库、只有明确discard_changes=true才200卸下。基线和复制副本的两个仓库保持完整。

所有变更HTTP请求先返回202，最大受理耗时0.051262秒，操作号与终态逐步落盘；原请求关闭后由短轮询查询，包含长时间删除操作，不依赖Cloudflare持续连接。期望的400/403/409仍作为FAILED操作证据保留，未弱化断言。

最终原删除操作`who-4b6d3c7ddeff0ec5852091cef1e11908b86bbebf93cfb4a2e015bd07968e8a85`返回COMPLETED/200，个人默认配置退役。独立读取确认该隔离tenant剩余活动工作区0、活跃环境0、model_usage行0、对象为空；网页项目与会话0、active personal config=false。完整证据为`web-final-passed.json`、`web-final-dirty-fixture.json`、`web-final-runtime-cleanup.json`、`web-final-ui-cleanup.json`和`web-final-execution.json`。之前的所有失败报告保留作对照；未重跑已经通过的传输/网络/下载用例。
