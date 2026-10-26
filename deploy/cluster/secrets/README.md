# 集群的密钥（SOPS + age）

依据：`docs/plan/cloud-deploy.md` D-02（“密钥用 SOPS + age 加密进仓库，解密只在部署机上做”）。

- `secrets.example.yaml`：集群要的全部 Secret，只有占位符 `REPLACE_ME`。
- `secrets.enc.yaml`：加密后的真东西，可以提交。明文的 `secrets.yaml` 不进仓库（`.gitignore`）。
- `.sops.yaml`：只加密 `data` 与 `stringData`，收件人是部署机的 age 公钥。

## 第一次

在部署机上：

```bash
age-keygen -o ~/.config/sops/age/keys.txt     # 私钥只留在这台机器上
grep 'public key' ~/.config/sops/age/keys.txt  # 把公钥填进 .sops.yaml
cp secrets.example.yaml secrets.yaml           # 填好每个 REPLACE_ME
sops --encrypt secrets.yaml > secrets.enc.yaml
shred -u secrets.yaml
```

## 部署与轮换

```bash
sops --decrypt deploy/cluster/secrets/secrets.enc.yaml | kubectl apply -f -
sops deploy/cluster/secrets/secrets.enc.yaml   # 改值（在编辑器里解密，保存时重新加密）
```

改了 `agent-runtime-secrets` 之后重启 task-service、Worker 与 Memory Service：它们启动时把 `secrets.json` 并进可写的密钥文件
（Chart 的 `secrets.storeClaim`），种子里的值覆盖旧值，已经生成的各租户 kek 不变。

## 不要

- 不要把密钥写进 Chart 的 values（Chart 只认 Secret 的名字，`tests/deploy` 会拦）。
- 不要把 age 私钥放进仓库、CI 或聊天记录。
