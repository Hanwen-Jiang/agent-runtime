"""测试里拉起临时的 Redpanda：Learning Pipeline 在集群里直接消费 trace 主题（T-16 的云上输入，docs/plan/learning-pipeline.md 第 7 节）。

用 docker 起一个单 broker 的开发模式容器。镜像与集群里的同版（deploy/cluster/versions.conf 的 Redpanda Chart 26.2.4 带的
v26.2.4），可用 AR_REDPANDA_IMAGE 覆盖。docker 取自 AR_DOCKER_BIN 指定的文件或目录，或 PATH 上的 docker（同 Milvus 的测试）；
找不到 docker 或 Docker 没在运行时测试失败，设 AR_ALLOW_MISSING_TOOLS=1 改为跳过。

- Kafka 端口映射到 127.0.0.1 上事先挑好的空闲端口，并按同一个地址对外宣告：客户端拿到元数据后连的是宣告的地址。
- 数据只在容器里，结束时删除容器。
- 消费组的延迟按 KEDA 的 kafka 触发器算（offsetResetPolicy: earliest）：没提交过的分区，延迟是全部记录。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

from confluent_kafka import Consumer, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

ENV_BIN = "AR_DOCKER_BIN"
ENV_IMAGE = "AR_REDPANDA_IMAGE"
DEFAULT_IMAGE = "redpandadata/redpanda:v26.2.4"


class MissingTool(RuntimeError):
    """找不到 docker，或 Docker 没在运行。"""


def _binary() -> str:
    exe = "docker" + (".exe" if os.name == "nt" else "")
    configured = os.environ.get(ENV_BIN, "")
    if configured:
        path = Path(configured)
        if path.is_dir():
            path = path / exe
        if not path.exists():
            raise MissingTool(f"{ENV_BIN} 指向的位置没有 {exe}")
        return str(path)
    found = shutil.which("docker")
    if found is None:
        raise MissingTool("找不到 docker（Learning Pipeline 消费主题的测试用 Docker 拉起 Redpanda），见 CLAUDE.md 的常用命令")
    return found


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Redpanda:
    """一个临时的 Redpanda 容器。brokers 是 Kafka 的地址（host:port）。"""

    def __init__(self, *, ready_timeout: float = 180) -> None:
        self.docker = _binary()
        # docker 的凭据助手（docker-credential-desktop）与 docker 在同一目录，子进程的 PATH 要带上它。
        self._env = {**os.environ, "PATH": str(Path(self.docker).parent) + os.pathsep + os.environ.get("PATH", "")}
        self.image = os.environ.get(ENV_IMAGE) or DEFAULT_IMAGE
        if self._docker("info", "--format", "{{.ServerVersion}}", check=False).returncode != 0:
            raise MissingTool("Docker 没在运行（docker info 失败），先启动 Docker Desktop")
        if self._docker("image", "inspect", self.image, check=False).returncode != 0:
            self._docker("pull", self.image, timeout=1800)
        port = _free_port()
        self.brokers = f"127.0.0.1:{port}"
        self.name = f"ar-redpanda-{uuid.uuid4().hex[:8]}"
        self._docker(
            "run", "-d", "--rm", "--name", self.name, "-p", f"127.0.0.1:{port}:{port}", self.image,
            "redpanda", "start", "--mode", "dev-container", "--smp", "1", "--memory", "1G", "--default-log-level=warn",
            "--kafka-addr", f"PLAINTEXT://0.0.0.0:{port}", "--advertise-kafka-addr", f"PLAINTEXT://{self.brokers}",
        )
        try:
            self._wait_ready(ready_timeout)
        except BaseException:
            self.stop()
            raise

    def _docker(self, *args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([self.docker, *args], env=self._env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if check and result.returncode != 0:
            raise RuntimeError(f"docker {args[0]} 失败：{result.stderr.strip() or result.stdout.strip()}")
        return result

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        admin = AdminClient({"bootstrap.servers": self.brokers})
        while True:
            if self._docker("inspect", "--format", "{{.State.Running}}", self.name, check=False).stdout.strip() != "true":
                raise RuntimeError(f"redpanda 容器退出了：{self.logs()}")
            try:
                if admin.list_topics(timeout=2).brokers:
                    return
            except Exception:  # noqa: BLE001 — 还没就绪时各种连接错误都有
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"redpanda 没有在时限内就绪：{self.logs()}")
            time.sleep(1)

    def create_topic(self, name: str, *, partitions: int = 2) -> None:
        admin = AdminClient({"bootstrap.servers": self.brokers})  # 等到结果之前不能被回收，否则请求被取消（_DESTROY）
        [future] = admin.create_topics([NewTopic(name, num_partitions=partitions, replication_factor=1)]).values()
        future.result(timeout=30)

    def produce(self, topic: str, values: list[str | bytes], *, partition: int | None = None, partitions: int = 2) -> None:
        """像 Collector 的 kafka 导出器一样一条记录一个值；partition 不给时轮流写各分区。"""
        producer = Producer({"bootstrap.servers": self.brokers})
        for i, value in enumerate(values):
            producer.produce(topic, value=value.encode() if isinstance(value, str) else value, partition=i % partitions if partition is None else partition)
        assert producer.flush(30) == 0, "还有没送达的记录"

    def ends(self, topic: str) -> dict[int, int]:
        consumer = Consumer({"bootstrap.servers": self.brokers, "group.id": f"probe-{uuid.uuid4().hex[:8]}"})
        try:
            info = consumer.list_topics(topic, timeout=10).topics[topic]
            return {p: consumer.get_watermark_offsets(TopicPartition(topic, p), timeout=10, cached=False)[1] for p in sorted(info.partitions)}
        finally:
            consumer.close()

    def committed(self, topic: str, group: str) -> dict[int, int]:
        """消费组在各分区提交的位置；没提交过是负数（librdkafka 的 OFFSET_INVALID）。"""
        consumer = Consumer({"bootstrap.servers": self.brokers, "group.id": group})
        try:
            parts = [TopicPartition(topic, p) for p in sorted(self.ends(topic))]
            return {tp.partition: tp.offset for tp in consumer.committed(parts, timeout=10)}
        finally:
            consumer.close()

    def lag(self, topic: str, group: str) -> int:
        """KEDA 的 kafka 触发器（offsetResetPolicy: earliest）看到的延迟。"""
        ends, committed = self.ends(topic), self.committed(topic, group)
        return sum(end - committed[p] if committed[p] >= 0 else end for p, end in ends.items())

    def logs(self, tail: int = 40) -> str:
        result = self._docker("logs", "--tail", str(tail), self.name, check=False)
        return (result.stdout + result.stderr).strip()

    def stop(self) -> None:
        self._docker("rm", "-f", self.name, check=False)
