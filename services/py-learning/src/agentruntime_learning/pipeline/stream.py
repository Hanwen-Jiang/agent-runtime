"""从 EventStream 的主题读 trace：Learning Pipeline 在集群里的输入（T-16；设计书 3.5、4.6；细化见 docs/plan/learning-pipeline.md 第 7 节）。

Collector 的 kafka 导出器把语义 span 写进主题 telemetry.spans（一条记录一个 OTLP JSON 的 ExportTraceServiceRequest），
KEDA 的 ScaledJob 按消费组在这个主题上的延迟起 Job。一次运行：
1. 快照：记下各分区当前最早保留的位置与末尾（high watermark）；
2. 从最早保留的记录读到快照的末尾。每次都从头读：门槛要每组至少 100 条样本（J.3），只读上次之后的新记录凑不够；
   重复投递与重读的 Task 由 traces.task_traces 去重；
3. 跑完（命令行的退出码 0）才把快照的末尾提交为消费组的位置（commit）。失败不提交：KEDA 看到的延迟不变，下一轮整条重跑。
   消费组的位置因此只表示“评测到了哪里”，KEDA 的延迟就是上次评测成功之后新到的记录。

不加入消费组（assign，不 subscribe）：不等再均衡，不与别的成员抢分区；提交时用消费组的名字，KEDA 读到的就是它。
主题里混着所有租户的 span（Collector 不分租户），没有 EventStream 记录上的作用域头，所以不经租户绑定的 EventStream 接口读；
按租户过滤与审计同文件输入（traces.task_traces、run 的 learning.read_traces）。
历史只到主题的保留期；更长的历史要把 trace 原文存进 ObjectStore（设计书 3.5，登记在后续）。
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agentruntime_learning.pipeline.traces import TraceFormatError

ENV_BROKERS = "AR_EVENT_STREAM_BROKERS"
# Kafka 的主题与消费组名允许的字符（主题最长 249）。
NAME = re.compile(r"^[A-Za-z0-9._-]{1,249}$")
DEFAULT_TIMEOUT_S = 300.0
_BATCH = 500


class StreamError(RuntimeError):
    """读不到主题或提交失败（broker 不通、超时、分区出错）。没有提交，下一轮重跑。"""


@dataclass(frozen=True)
class Record:
    partition: int
    offset: int
    value: bytes


@dataclass(frozen=True)
class Snapshot:
    """一次运行读到的东西：各分区 [starts, ends) 之间的记录，按（分区，偏移）排好。提交的是 ends。"""

    topic: str
    group: str
    starts: Mapping[int, int]
    ends: Mapping[int, int]
    records: tuple[Record, ...]

    @property
    def sources(self) -> list[str]:
        """写进审计（learning.read_traces 的 learning.files）：主题#分区:起-止。"""
        return [f"{self.topic}#{p}:{self.starts[p]}-{self.ends[p]}" for p in sorted(self.ends)]

    def lines(self) -> list[str]:
        """一条记录一行，交给 traces.read_spans；行号与 where 对得上。"""
        out = []
        for number, record in enumerate(self.records, start=1):
            try:
                out.append(record.value.decode("utf-8"))
            except UnicodeDecodeError:
                raise TraceFormatError(f"第 {number} 条记录不是 UTF-8", number) from None
        return out

    def where(self, line: int) -> str:
        if not 1 <= line <= len(self.records):
            return ""
        record = self.records[line - 1]
        return f"{self.topic} 分区 {record.partition} 偏移 {record.offset}"


class TopicReader:
    """读一个主题的快照，跑完后把快照的末尾提交为消费组的位置。timeout_s 是读完整个快照的时限。"""

    def __init__(self, brokers: str, topic: str, group: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        from confluent_kafka import Consumer

        self.topic, self.group, self._timeout = topic, group, timeout_s
        self._consumer = Consumer(
            {
                "bootstrap.servers": brokers,
                "group.id": group,
                "client.id": "learning-pipeline",
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
                "auto.offset.reset": "earliest",
            }
        )

    def snapshot(self) -> Snapshot:
        from confluent_kafka import KafkaError, KafkaException, TopicPartition

        consumer = self._consumer
        deadline = time.monotonic() + self._timeout
        step = min(30.0, self._timeout)
        try:
            info = consumer.list_topics(self.topic, timeout=step).topics.get(self.topic)
        except KafkaException as err:
            raise StreamError(f"读不到主题 {self.topic} 的元数据：{_describe(err)}") from None
        if info is None or (info.error is not None and info.error.code() == KafkaError.UNKNOWN_TOPIC_OR_PART):
            # Collector 还没写过：没有 trace。
            return Snapshot(self.topic, self.group, starts={}, ends={}, records=())
        if info.error is not None:
            raise StreamError(f"主题 {self.topic} 的元数据出错：{info.error.str()}")
        starts: dict[int, int] = {}
        ends: dict[int, int] = {}
        try:
            for p in sorted(info.partitions):
                starts[p], ends[p] = consumer.get_watermark_offsets(TopicPartition(self.topic, p), timeout=step, cached=False)
        except KafkaException as err:
            raise StreamError(f"读不到主题 {self.topic} 的末尾：{_describe(err)}") from None
        pending = {p for p in ends if ends[p] > starts[p]}
        records: list[Record] = []
        if pending:
            consumer.assign([TopicPartition(self.topic, p, starts[p]) for p in sorted(pending)])
            try:
                while pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        left = "、".join(f"分区 {p}" for p in sorted(pending))
                        raise StreamError(f"{self._timeout:g} 秒内没读完主题 {self.topic} 的快照（{left}）")
                    for message in consumer.consume(num_messages=_BATCH, timeout=min(1.0, remaining)):
                        if message.error() is not None:
                            raise StreamError(f"读主题 {self.topic} 出错：{message.error().str()}")
                        p, offset = message.partition(), message.offset()
                        if p in pending and offset < ends[p]:
                            records.append(Record(p, offset, message.value() or b""))
                    # 位置越过快照的末尾就读完了（中间被删除或压缩掉的偏移不会送来）。
                    for tp in consumer.position([TopicPartition(self.topic, p) for p in sorted(pending)]):
                        if tp.offset >= ends[tp.partition]:
                            pending.discard(tp.partition)
            except KafkaException as err:
                raise StreamError(f"读主题 {self.topic} 出错：{_describe(err)}") from None
            finally:
                consumer.unassign()
        records.sort(key=lambda r: (r.partition, r.offset))
        return Snapshot(self.topic, self.group, starts=starts, ends=ends, records=tuple(records))

    def commit(self, snapshot: Snapshot) -> None:
        from confluent_kafka import KafkaException, TopicPartition

        if not snapshot.ends:
            return
        offsets = [TopicPartition(snapshot.topic, p, end) for p, end in sorted(snapshot.ends.items())]
        try:
            done = self._consumer.commit(offsets=offsets, asynchronous=False) or []
        except KafkaException as err:
            raise StreamError(f"提交消费组 {snapshot.group} 的位置失败：{_describe(err)}") from None
        failed = [f"分区 {tp.partition}：{tp.error.str()}" for tp in done if tp.error is not None]
        if failed:
            raise StreamError(f"提交消费组 {snapshot.group} 的位置失败：{'；'.join(failed)}")

    def close(self) -> None:
        self._consumer.close()


def _describe(err: Any) -> str:
    inner = err.args[0] if err.args else err
    return inner.str() if hasattr(inner, "str") else str(inner)
