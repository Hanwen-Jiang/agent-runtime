"""开发与测试用的 Provider 实现（设计书 4.1：每个接口带开发实现；docs/reference/runtime-quickref.md 第 6 节）。

只在本地开发与测试中使用：内存、SQLite、本地磁盘、本地子进程、命令行。不做性能优化，也不提供生产所需的隔离强度。
每个实现都接收 FaultInjector，在每个公开方法开头按方法名检查，供一致性测试注入故障；有过期语义的实现接收 Clock。
"""
