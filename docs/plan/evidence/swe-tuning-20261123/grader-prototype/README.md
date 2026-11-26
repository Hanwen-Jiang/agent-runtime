# 评分器网络排查（已终止）

这次两组正式判分均使用主机原有 Docker。为缓解根盘压力，曾试用带 28 GiB 内存盘的独立 Docker 与 containerd。相同官方镜像能启动后，实际运行 Sphinx 8475 的 linkcheck 才发现该原型缺少容器网络，三个回归用例遇到 DNS 错误。对同一份补丁在原 Docker 重新评分，结果由未解决变为解决。五份原型评分留在云端各运行的 `grader-environment-history/prototype-no-eth0/`，重新判分沿用原有模型生成的补丁。[判分环境与逐题核对](../grader-network-recovery.json)

独立 daemon 初次启动时也影响了主 Docker 的默认网桥。已从主 Docker 持久网络配置恢复网桥并启动官方 Sphinx 容器检查默认路由、DNS 和 HTTPS。其后准备了独立 Linux 网络命名空间；宿主 root 用户的 inotify 实例用尽，独立 daemon 未能在该空间启动。两套正式运行随即回到主 Docker。该试验创建的私有服务、网络命名空间、用户态网络和内存盘已清理，原宿主的内核上限保持 128。
