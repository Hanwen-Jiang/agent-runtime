# 密钥与凭据：从 Git 到 Pod 再到代码

> 学习笔记，2026-11-02。对应代码：`libs/py-core/src/agentruntime_core/providers/secret_store.py`（接口）、`agentruntime_core/dev/secret_store.py`
> 与 `libs/go-core/dev/filesecrets`（现在线上用的实现）、`deploy/cluster/secrets/`（SOPS）、Chart 的 `_helpers.tpl`（`ar.storeEnv`）。
> 设计依据：设计书 3.6（SecretStore）、附录 D.4，`docs/plan/cloud-deploy.md` D-02、D-04。工具的短期凭据见 [tools-and-secrets.md](tools-and-secrets.md) 第 4 节。

## 1. 一句话回答：是 K8s 提供的吗？

**一半是。** 密钥要经过三层，每层管的事不一样：

| 层 | 用什么 | 管什么 | 现在的状态 |
| --- | --- | --- | --- |
| 仓库里（静态存放） | SOPS + age | Git 里只放密文 `deploy/cluster/secrets/secrets.enc.yaml`；age 私钥只在部署机的 `~/.config/sops/age/keys.txt` | 有 |
| 集群里（交付给 Pod） | K8s Secret | 把密钥交给 Pod：挂成文件或环境变量；谁能读由 RBAC 决定 | 有；k3s 的 `secrets-encryption` 已打开（etcd 里是密文） |
| 应用里（代码怎么用） | 我们自己的 **SecretStore**（Provider 接口） | 按作用域隔离（平台、各租户）、签发短期凭据、生成与解开数据密钥 | 用的是**开发实现**：PVC 上的一个 JSON 文件 |

K8s Secret 只是“快递”，把一包密钥送进 Pod；真正决定“哪个租户能拿到哪个密钥、拿到的是长期的还是 15 分钟的”是 SecretStore。

## 2. 一个密钥从哪来、到哪去

```text
人：sops deploy/cluster/secrets/secrets.enc.yaml（编辑器里解密，保存时重新加密）
  │  只有部署机有 age 私钥
  ▼
sops --decrypt … | kubectl apply -f -          → K8s Secret agent-runtime-secrets
  │                                               （键 secrets.json：SecretStore 的“种子”；键 dev-signing-key）
  ▼
Pod 启动：Secret 只读地挂在 /etc/agentruntime/secrets/
  │  AR_SECRETS_SEED_FILE 指着种子
  ▼
进程把种子并进可写文件 AR_SECRETS_FILE（PVC agent-runtime-secretstore 上的 secrets.json，各进程共用）
  │  种子里的值覆盖旧值；各作用域的 kek 已有的不换
  ▼
代码：SecretStore.read("model-provider/api-key")、issue_credential(角色)、generate_data_key(用途)
```

为什么不直接读只读的 Secret，而要“并进一个可写文件”？因为 SecretStore 要**写回**东西：每个作用域第一次用到时生成的 kek（见第 3 节），
以及几个进程（task-service、Worker、Memory Service）要共用同一份 kek，才能互相解开对方加密的数据。

## 3. 几个概念

**作用域。** 文件里按 `_platform`（平台自己的密钥）与各租户分开：`{"scopes": {"_platform": {"secrets": {…}}, "t1": {…}}}`。
代码先 `bind(租户)` 或 `bind_platform(用途)` 拿到一个客户端，之后只能读这个作用域里的东西（全仓统一的租户绑定）。

**信封加密（kek 与数据密钥）。** Temporal 的大载荷要卸到 ObjectStore（Claim-Check，S-06），而且要加密。做法是：

1. 每个作用域有一把 **kek**（密钥加密密钥），只在 SecretStore 里。
2. 要加密时 `generate_data_key`：随机生成一把**数据密钥**，用它加密载荷；再用 kek 把数据密钥“包”起来，包好的数据密钥跟着密文一起存。
3. 解密时 `unwrap_data_key`：用 kek 解开数据密钥，再解载荷。

这样 kek 从不离开 SecretStore，换 kek 也只要重新包数据密钥，不用重新加密全部数据。

**短期凭据。** 工具要调外部 API 时，Node Executor 按工具登记的角色 `issue_credential`，拿到一个最多 15 分钟的凭据，调用结束就吊销；
长期密钥不交给模型写的代码，也不进结果与日志（附录 D.4、CLAUDE.md 的硬性规则）。

**签名密钥。** `dev-signing-key`（环境变量 `AR_DEV_SIGNING_KEY`）签发 Task API 的令牌（`task-service dev-token --tenant … --ttl …`）。令牌带过期时间：
2026-11-02 SWE-bench 的那次 401 就是 10-30 签的令牌过期了。能拿到签名密钥的人能签任何租户的令牌，所以它和平台密钥一样要管好。

**封装的凭据。** Workspace API 带来的 Git 凭据由 task-service 用 `workspace/credential-key` 封起来，只有 Worker 能解开，中间经过 Temporal 时是密文（D-06）。
沙箱访问包镜像与出网网关的**环境凭据**由 `sandbox/env-credential-key` 签发，短期、按 Task 收窄（D-15、D-16）。

## 4. 线上现在有哪些 Secret

| Secret | 里面是什么 | 谁用 |
| --- | --- | --- |
| `agent-runtime-secrets` | `secrets.json`（SecretStore 的种子：模型供应商的 Key、Registry 发布令牌、沙箱令牌密钥、Cloudflare 桥令牌、环境凭据密钥、包镜像与出网网关的管理令牌、环境镜像仓库凭据、Workspace 凭据密钥）与 `dev-signing-key` | task-service、Worker、Memory Service、Model Gateway |
| `agent-runtime-postgres` | 业务库的连接串（含口令） | 同上，经 `AR_RELATIONAL_DSN` |
| `agent-runtime-jev` | Jev 的 API Key（键 `api-key`） | 4 个 Worker，挂成文件 `/etc/agentruntime/judge/api-key`（N-09） |
| `agent-runtime-env-registry` | 环境镜像仓库的 htpasswd | 镜像仓库 |
| `agent-runtime-langfuse` | Langfuse 的鉴权 | Collector |
| `coding-oauth` | GitHub、Gitee 等的 OAuth Client Secret | coding-web |
| `cloudflare-origin-tls` | 源站证书 | Traefik |
| `sh.helm.release.v1.ar.v*` | Helm 自己的发布记录（含渲染时的 values） | Helm；所以 values 里**不放**密钥，`tests/deploy` 会拦 |

Jev 为什么单独一个 Secret：要把它加进种子，就得用 age 私钥**解密全部平台密钥**再重新加密。单独一个 Secret 只碰这一把，Worker 每次调用时读文件，
建好之后约一分钟生效，不用重启；卷是 `optional` 的，没建时 Worker 照常启动，只是不检查交付。

## 5. 现在的不足

- **SecretStore 是开发实现**：PVC 上的文件是明文，谁能进 Pod 或读到卷，谁就能读到全部密钥。生产应换成 KMS 管 kek、Vault 或 OpenBao 管动态凭据
  （已登记，设计书 3.6）。接口不用变：代码只依赖 `SecretStore` 这个 Provider。
- **K8s Secret 的边界是 RBAC**：能 `kubectl get secret` 的人能读明文（etcd 里加密只防磁盘被拿走）。部署用的 root 与 kubeconfig 要当作最高权限。
- **轮换**：改种子（`sops …`、`kubectl apply`）之后要重启读它的进程；kek 不随种子换。签名密钥换了，所有旧令牌立刻失效。
- **AI 代理的边界**：这个会话里解密种子、建 Secret、签令牌都被拦下，由人来做。这是故意的：代理能读到的密钥越少，泄露面越小。

## 6. 面试速查

**Q：K8s Secret 安全吗？**
A：默认只是 base64，不是加密；要打开静态加密（k3s 的 `secrets-encryption`，我们已打开），并用 RBAC 限制谁能读。它适合“交付”，不适合当唯一的密钥系统：
没有短期凭据、没有按租户隔离、审计也粗。

**Q：为什么 Git 里能放密钥？**
A：放的是 SOPS 加密后的密文，只加密 `data` 与 `stringData`，键名可读、便于审查改了什么；解密要部署机上的 age 私钥，私钥不进仓库、CI 与聊天记录。

**Q：什么是信封加密，好处是什么？**
A：用数据密钥加密数据，用主密钥（kek）加密数据密钥。主密钥不出 KMS（我们这里是 SecretStore），轮换主密钥只要重新包数据密钥；每份数据有自己的密钥，泄露一份不影响别的。

**Q：模型写的代码能拿到 API Key 吗？**
A：拿不到长期的。工具调用时按角色签发短期凭据，只交给那一次调用，结束吊销；沙箱的环境变量是白名单，出口处还会把回显的凭据换成掩码。

**Q：令牌过期了怎么办？**
A：用签名密钥重新签（`task-service dev-token --tenant … --ttl …`）。长期跑的评测要么签足够长的 TTL，要么在跑之前现签；不要把签名密钥交给跑评测的进程。
