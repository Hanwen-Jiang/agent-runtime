# 全部20项交付结果（2026-11-08）

**总Goal未全部完成，阻塞于外部历史账单。** 实现项已部署并完成对应真实验收；Terminal-Bench的新可比40题、DeepEval新轨迹重判尚未执行。N16-R/EV-REAL的原22个Task及统一配置22份评分、LongMemEval固定50题已完成并交付负面/受限结论，不把门槛未达或费用未知改为通过。原始结果、失败及费用预占均保留。

代码交付入口为两仓库的`main`：Runtime已正式纳入最终评测提交`e4d1133a`及其全部恢复代码，coding-agent的Web实现与交付文档同步。本报告及最终清理记录的准确提交见Git历史；宿主最终SHA/远端核验保存在`/root/ar-release/goal-full-20261106/final-delivery-state.json`。源代码整理提交与实际生产镜像分别记录。工作目录为`/root/proj/agent-projs/{agent-runtime,coding-agent}`。

生产版本按[已有部署映射][deployment]：Helm44；Task `goal-f04660cc`、Sandbox Service `goal-0ee834d8`、Worker/egress/四沙箱镜像 `goal-f1388e13`、coding-web `goal-0fbf18e`、Gateway `goal-9ef23021`、Memory `goal-60a266a3`、评测网站 `goal-87051e2f`。FC manager SHA256为 `eed2e5449423357ca151393de659d3fdb14866b37d78409c44a693f4174be77b`；Cloudflare桥版本 `3ae9361b-1b1a-45a1-ae79-bcba8566d553`、容器 `goal-cf-20261107`。

| # / 任务 | 结论 | 关键提交或生产版本 | 可复查证据与明确限制 |
| --- | --- | --- | --- |
| 1 D-25 多仓工作区 | **已部署验收**：attach/list/detach、逐仓权限、复制、恢复及网页入口完成；dirty默认409，明确discard后才卸下。 | Task `goal-f04660cc`；Service `goal-0ee834d8`；Web `goal-0fbf18e` | [生产冒烟][smoke]、`web-final-passed.json`；原Kata路径通过，未用FC替代失败条件。只读仓库沿已批准Git API策略，不宣称任意shell文件级只读。 |
| 2 NET-OPEN 公网档位 | **已部署验收**：公网HTTPS允许，内网/元数据/loopback/非法端口拒绝，撤销、到期和配额生效。 | egress/Worker `goal-f1388e13`；Task/Service见部署映射 | [网络与HOME边界][boundaries]；真实GrantStore/Broker/FC/egress通过。内部授权夹具不冒充完整SubmitTask生产流程，Task API行为另有回归。 |
| 3 WS-CF-524 长操作异步化 | **已部署验收**：快速202、稳定操作号、查询失败/结果、幂等、断连和重启恢复。 | Web `goal-0fbf18e`；Task `goal-f04660cc` | [生产冒烟][smoke]；最大受理51.262ms，长删除按独立轮询完成；不同owner404，旧Temporal历史保留。 |
| 4 R-06 Registry修订成本账本 | **已部署真实验收**：真实Registry版本及digest进入每次attempt，单价与实际用量冻结。 | `771efed7`；Gateway `goal-9ef23021`；immutable CM `agent-runtime-model-profiles-published20261107` | [部署映射][deployment]、[执行记录][goal]；首97个已结算attempt的Registry/价格/Token/整数费用复算97/97；历史NULL未倒填，不以Git SHA或文件格式号冒充修订。 |
| 5 D-27 个人配置包 | **已部署验收**：私有指令、小型HOME文件、默认引用、不可变版本、Task pin/digest、冲突优先及网页配置完成。 | Runtime `f2daba6f`及后续修复；Task/Service/Web最终版本 | [生产冒烟][smoke]、[边界证据][boundaries]；0600、不覆盖既有文件、v2保存不影响Task的v1、同租户其他用户隔离、明文凭据拒绝。密钥识别仅覆盖已知格式，不冒称通用DLP。 |
| 6 WK-LITE-01 命令输出直传 | **已部署验收**：agent处理与上传，上层仅摘要及引用，context_read可回读；失败pending不重跑命令。 | Worker `goal-f1388e13`；Service `goal-0ee834d8`；CF `goal-cf-20261107` | [传输生产证据][transfer]、[CF生产证据][cfprod]；FC 1MiB+128KiB仅16044B摘要，CF 20KiB仅8022B摘要。保留单流最大64MiB捕获/尾部截断边界，非无限完整输出。 |
| 7 WK-LITE-02 交付文件直传 | **已部署验收**：路径校验、预占、不可变暂存、签名/核验、丢ACK恢复、幂等登记、网页下载完成。 | Service `goal-0ee834d8`、Worker `goal-f1388e13`、Web `goal-0fbf18e` | [传输证据][transfer]、[线上下载证据][download]；12MiB只登记一条artifact，SHA一致；跨租户/错Task404。生产下载Task关联为零模型夹具，不声称运行了模型任务。 |
| 8 WS-PULL-01 缺块恢复 | **已部署，FC生产通过**：有序分块、缺块判断、逐块与全文件校验、失败不交付半成品。 | 分块 `49133bfa`及后续严格清单修复；Service `goal-0ee834d8` | [传输证据][transfer]；3块/2唯一块、第二snapshot新增0、两次恢复整文件一致，损坏清单拒绝后修复恢复。分块写入在兼容reader回退点保存后才启用。 |
| 9 WS-BLOB-01 节点块缓存 | **已部署，FC生产通过**：CAS、LRU、容量、并发去重、损坏重取、授权先于命中完成。 | FC manager上述SHA；10GiB缓存，ObjectStore持久权威 | [传输证据][transfer]、[CF/FC传输记录][transferdoc]；两次恢复hits各+2、miss不增。损坏/LRU在专用真实FC/R2 harness测试，未破坏生产共享cache；仅清本次测试hash。 |
| 10 D-26 内存快照 | **实现、部署与真实隔离验收完成；默认关闭**。真正生成/恢复memory/state，凭据/身份/随机状态/盘与失效pin配额验证完成。 | `5726d1f1`、`17f1c7b2`；FC manager最终SHA | [真实快照与性能证据][fc]；快照未产生加速收益，生产snapshot cache=0，不能把预热池算作内存快照。 |
| 11 补测试/兼容/重放 | **必要关键回归与最终检查通过**。 | Runtime当前main；Web `d6444c9` | [最终检查目录][checks]；Go/Rust共同HTTP242、Service90、Registry61及并发、Web全套/race、Stage+重放40、Workspace旧golden、契约613；独立CI go.work全部build/vet/test通过181.17s。环境失败/可选真实用例未执行不冒充通过。 |
| 12 EV-TB Terminal-Bench | **未完成，历史费用对账阻塞**。23完成、4已执行错误、13未运行实物已核，原40题官方镜像digest已固定。 | 历史revision29与当前配置分开；新版runner已适配ops JWT/异步与恢复 | [TB核对与配对记录][pairedrecord]、`.goal-evidence/terminalbench-preflight-20261106/official-image-manifests.json`；不能混不同版本成40题基线。已结算$42.786647，206个pending预占$142.677378，不能据旧report推断剩余额度。 |
| 13 N16-R off/rules及成本 | **规定配对与统一评分已完成；综合门槛未通过**。11对Task/22份v2评分全部保留。 | 受试运行`goal-fdd0f202`，只切off/rules；裁判Opus v3、wire v2、统一提取v1 | [最终配对报告][pairedfinal]、[分析限定][analysis]；工具重复17/19、额外轮次7/10；仅原HTTP rules派3子Agent、已知费用$0.002109且Task aborted。原始均值0.94545/0.96364含一处条件误用，不发布为可信能力分；总费用/比率未知。 |
| 14 FC-REAL 实测 | **完成实测并交付限制**。 | 最新20×3/复核20：source `17f1c7b2`；96台密度较早source `5e1037e5`单列 | [原始样本与比较][fc]；1000m/256MiB/256MiB：cold p50/p95=1025.287/1227.864ms，warm=24.530/52.358ms，snapshot复核=9649.908/10319.383ms。96台是配置边界下idle VM观测下界，非物理极限；背景编译负载完整保留。 |
| 15 EV-REAL 规划/拆分 | **同批真实实验完成；规划/拆分门槛未通过**，未另跑受试任务。 | 同N16固定Task模型/Registry/Memory；全部22份裁判同wire v2 | [最终配对报告][pairedfinal]；各自策略结构条件off11/11、rules6/11；原HTTP真实三子Agent轨迹保留，aborted不记全解。五维原始评分有已标注裁判条件误用和一份raw schema偏差，未调分/改门槛。 |
| 16 EV-DE DeepEval重判 | **未完成，历史费用对账阻塞**。新鲜轨迹及run/score/report、按真实Task账本评分门禁已实现。 | `42a61d64`等账本门禁；新付费全套未启动 | [DeepEval账本说明][deepeval]、[评测总记录][evalrecord]；旧记录保留对照，不能只重判缺轨迹旧数据。已知$3.025569、pending预占$63.997193，原$10总限内余额无法证明。 |
| 17 MX-PUB 能力分发布 | **仅有效分已发布**：10个已核清费用act候选真实Publish→Promote→导出→Gateway加载。 | Opus/GLM/MiMo升v3，其他7个升v2；CM `...published20261107` | [发布回执与门槛][mx]、[部署映射][deployment]；13候选全部保留，2费用unknown、1淘汰不发；旧plan证据无效、route/judge/transform覆盖不足均不发布，未放松标准。 |
| 18 LongMemEval Memory检索 | **50题执行/清理完成；质量未达标，费用未全结**。原36份逐字节保留，只补原14未运行题，0timeout。 | runner `7dd4ea10`；Memory `goal-60a266a3`；Gateway `goal-9ef23021` | [最终验收][memory]、[完整指标][evalrecord]；recall@10 live0.94/lexical0.96，NDCG@10 0.7804/0.8200，gain−0.02、p=0.875，原+0.10/p≤.05门槛未改。六题型及写入/清理齐，所有对象/队列/墓碑0；cohort已知$0.760814，152微美元pending保留。 |
| 19 页面冒烟 | **已部署真实浏览器验收完成**。复用原测试补桌面长标签溢出断言，3页×desktop/mobile六组通过。 | learning `goal-87051e2f`；线上 `evals.cgent.link` | [线上浏览器证据][pages]；真实19数据集/517题/32报告与API一致，语言、分组、搜索、展开通过；保留原443px溢出失败对照，浏览器资源清理。 |
| 20 STOR-SEC R2安全 | **真实R2验收完成**。checksum同长篡改、If-None-Match、Content-Length、重复、过期、改key/method均核验。 | 真实当前R2账号与Rust agent；原签名契约未降低 | [11项原始日志][r2]、[执行说明][transferdoc]；仅隔离测试前缀，产物清理，无桶级生命周期修改；不能以mock代替这份证据。 |

费用交付口径：本轮相关配对（含无效pilot）已知账本成本$3.849185294，Memory（含准备运行）$0.837277，合计**$4.686462294**；五笔未知预占另计$0.331718，不当实付、免费或确定的实际费用上界。固定11对/裁判/Jev的已知部分为$3.763598496。口径是冻结Registry价格×实际用量及Jev固定费率，不是完整供应商付款发票；完整实际总额仍未知。旧“$1.97任务”为已结算$0.273624加未结算预占$1.695528，不是当前异常实付结论。CF/传输零模型验收的模型费用为0，基础设施账单未核为零。详见[本轮成本表][costs]。

交付与清理：生产回退点`/root/ar-release/goal-full-20261106/rollback`、分块兼容回退点`compatible-readers-rollback`均保留。最终按预存UID删除本轮8 Deployment、3 Service、3 ConfigMap及临时Task API令牌；专用Pod为0，所有非目标模板/配置/Service spec不变，其余17 Deployment均Ready。68个本次环境及目录记录均destroyed、busy租约0、对应真实firecracker/jailer进程0。生产测试VM/R2对象/专属cache/浏览器会话此前已清理。共享Secret/PVC、生产Gateway/Memory、Temporal历史、Registry、全部账本和原始评测产物保留，见[最终清理证据][cleanup]。已完成工作树先bundle并校验后收拢，含未提交数据/无法证明等价的旧树保留。

尚需外部输入：A6 2026-11-02至11-07 UTC完整消费/失败/退款明细或可读历史入口，才能核定TB/DeepEval在原$80/$10上限内的余额并执行缺失实验，及核清当前未结算成本。现有接口只有最近1000条；该需求已提出，不重复请求执行权限，不擅自扩额。质量门槛未达的固定实验不通过改门槛、删题或追加调分实验改成成功。

[deployment]: evidence/core-deployment-20261107.json
[goal]: goal-full-20-2026-11-07.md
[smoke]: production-smoke-2026-11-07.md
[boundaries]: evidence/production-smoke-20261107/evidence/boundaries-passed.json
[transfer]: evidence/production-smoke-20261107/evidence/transfer-passed.json
[cfprod]: evidence/production-cf-transfer-20261107/report.json
[download]: evidence/production-smoke-20261107/evidence/web-artifact-report-b.json
[transferdoc]: transfer-acceptance-2026-11-07.md
[fc]: evidence/fc-pin-20261107/comparison.json
[checks]: evidence/final-checks-20261107
[pairedrecord]: goal-paired-evaluation-2026-11-06.md
[pairtransition]: evidence/paired-off-to-rules-20261107/verified.json
[semantic]: B-paired-semantic-judge-2026-11-08.md
[paircomplete]: paired-completion-2026-11-07.md
[evalrecord]: goal-evaluation-2026-11-06.md
[deepeval]: goal-deepeval-ledger-2026-11-06.md
[mx]: goal-mx-publication-2026-11-06.md
[memory]: evidence/longmemeval-final-20261107/final-acceptance.json
[pages]: goal-page-live-2026-11-06.md
[r2]: evidence/transfer-r2-live-20261106.log

[pairedfinal]: evidence/paired-final-20261107/paired-completion-final.json
[analysis]: evidence/paired-final-20261107/analysis-notes.json
[costs]: evidence/paired-final-20261107/current-goal-costs.json
[cleanup]: evidence/final-eval-cleanup-20261107/verified.json
