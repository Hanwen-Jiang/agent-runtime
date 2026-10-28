"""T-16 的云上输入：Job 直接消费 trace 主题（细化见 docs/plan/learning-pipeline.md 第 7 节）。

KEDA 的 ScaledJob 按消费组在 telemetry.spans 上的延迟起 Job；Job 从各分区最早保留的记录读到起跑时的末尾（快照），
跑完（退出码 0）才把快照的末尾提交为这个消费组的位置。所以：
- 每次都看到主题里保留的全部历史（门槛要每组至少 100 条样本，J.3），不只是上次之后的新记录；
- 失败（Registry 写失败、坏记录、读不到主题）不提交，KEDA 看到的延迟不变，下一轮整条重跑；
- 快照之后才到的记录不提交，留作下一轮的延迟；dry-run 不改消费组的位置。
真的 broker 是 Docker 里的 Redpanda（redpanda_support）；命令行的提交规则另用假的读者测，不要 Docker。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agentruntime_learning.pipeline import stream
from agentruntime_learning.pipeline.__main__ import main

from pipeline_support import FAMILY, FakeRegistry, otlp_lines, population

GOOD = dict(a_success=115, a_fail=5, b_success=85, b_fail=35)
BAD = dict(a_success=100, a_fail=40, b_success=90, b_fail=2)
TOPIC = "telemetry.spans"
GROUP = "learning-t1"
BROKERS = "redpanda.data.svc:9093"


class Registries:
    """命令行里的 GrpcRegistry 换成记录调用的替身；每次运行一个新的。"""

    def __init__(self) -> None:
        self.made: list[FakeRegistry] = []
        self.reject_promote = False

    def __call__(self, address: str, publish_token: str | None = None) -> FakeRegistry:
        registry = FakeRegistry()
        registry.reject_promote = self.reject_promote
        self.made.append(registry)
        return registry


@pytest.fixture
def registries(monkeypatch: pytest.MonkeyPatch) -> Registries:
    made = Registries()
    monkeypatch.setattr("agentruntime_worker.registry.GrpcRegistry", made)
    return made


def cli(tmp_path: Path, *extra: str, topic: str = TOPIC, group: str = GROUP, brokers: str = BROKERS) -> int:
    argv = ["run", "--tenant", "t1", "--family", FAMILY, "--topic", topic, "--group", group, "--report-dir", str(tmp_path / "reports"), *extra]
    env = {stream.ENV_BROKERS: brokers}
    if "--dry-run" not in extra:
        env["AR_REGISTRY_ADDRESS"] = "registry:8092"
    return main(argv, env=env)


def summary(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def report_of(line: dict[str, Any]) -> dict[str, Any]:
    return json.loads(Path(line["report"]).read_text("utf-8"))


# --- 命令行的提交规则（假的读者） ---


def snapshot_of(values: list[str | bytes], *, partitions: int = 2) -> stream.Snapshot:
    """像 Collector 那样一条记录一个值，轮流写进各分区。"""
    records = [stream.Record(i % partitions, i // partitions, v.encode() if isinstance(v, str) else v) for i, v in enumerate(values)]
    ends = {p: sum(1 for r in records if r.partition == p) for p in range(partitions)}
    return stream.Snapshot(TOPIC, GROUP, starts=dict.fromkeys(range(partitions), 0), ends=ends, records=tuple(sorted(records, key=lambda r: (r.partition, r.offset))))


@dataclass
class FakeTopic:
    snapshot: stream.Snapshot | None = None
    read_error: str = ""
    commit_error: str = ""
    opened: list[tuple[str, str, str]] = field(default_factory=list)
    commits: list[stream.Snapshot] = field(default_factory=list)
    closed: int = 0


@pytest.fixture
def fake_topic(monkeypatch: pytest.MonkeyPatch) -> FakeTopic:
    topic = FakeTopic()

    class Reader:
        def __init__(self, brokers: str, name: str, group: str, **_: Any) -> None:
            topic.opened.append((brokers, name, group))

        def snapshot(self) -> stream.Snapshot:
            if topic.read_error:
                raise stream.StreamError(topic.read_error)
            assert topic.snapshot is not None
            return topic.snapshot

        def commit(self, snapshot: stream.Snapshot) -> None:
            if topic.commit_error:
                raise stream.StreamError(topic.commit_error)
            topic.commits.append(snapshot)

        def close(self) -> None:
            topic.closed += 1

    monkeypatch.setattr(stream, "TopicReader", Reader)
    return topic


def test_a_finished_run_commits_its_snapshot(tmp_path: Path, fake_topic: FakeTopic, registries: Registries, capsys: pytest.CaptureFixture[str]) -> None:
    fake_topic.snapshot = snapshot_of(otlp_lines(population(**GOOD)))
    assert cli(tmp_path) == 0
    assert summary(capsys)["status"] == "published"
    assert fake_topic.opened == [(BROKERS, TOPIC, GROUP)]
    assert fake_topic.commits == [fake_topic.snapshot], "跑完才把快照的末尾提交为消费组的位置"
    assert fake_topic.closed == 1


def test_a_rejected_candidate_is_also_a_finished_run(tmp_path: Path, fake_topic: FakeTopic, registries: Registries, capsys: pytest.CaptureFixture[str]) -> None:
    """门槛没过也算跑完：同一批 trace 再跑结论相同，不提交只会每一轮重算一遍。"""
    fake_topic.snapshot = snapshot_of(otlp_lines(population(**BAD)))
    assert cli(tmp_path) == 0
    assert summary(capsys)["status"] == "rejected"
    assert fake_topic.commits == [fake_topic.snapshot]
    assert all(r.writes == [] for r in registries.made)


def test_a_registry_failure_commits_nothing(tmp_path: Path, fake_topic: FakeTopic, registries: Registries, capsys: pytest.CaptureFixture[str]) -> None:
    fake_topic.snapshot = snapshot_of(otlp_lines(population(**GOOD)))
    registries.reject_promote = True
    assert cli(tmp_path) == 1
    assert summary(capsys)["status"] == "promote_failed"
    assert fake_topic.commits == [], "失败不提交：KEDA 看到的延迟不变，下一轮整条重跑"
    assert fake_topic.closed == 1


def test_dry_run_leaves_the_group_where_it_was(tmp_path: Path, fake_topic: FakeTopic, capsys: pytest.CaptureFixture[str]) -> None:
    fake_topic.snapshot = snapshot_of(otlp_lines(population(**GOOD)))
    assert cli(tmp_path, "--dry-run") == 0
    assert summary(capsys)["status"] == "dry_run"
    assert fake_topic.commits == []


@pytest.mark.parametrize(("bad", "message"), [("not json", "不是合法的 otlp_json"), (b"\xff\xfe", "UTF-8")], ids=["json", "utf-8"])
def test_a_bad_record_fails_the_run_and_says_where_it_is(bad: str | bytes, message: str, tmp_path: Path, fake_topic: FakeTopic, registries: Registries,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    good = otlp_lines(population(a_success=5, a_fail=1))
    fake_topic.snapshot = snapshot_of([*good, bad])
    assert cli(tmp_path) == 2
    err = capsys.readouterr().err
    i = len(good)  # snapshot_of 轮流写分区
    assert message in err and f"分区 {i % 2} 偏移 {i // 2}" in err, err
    assert fake_topic.commits == [] and fake_topic.closed == 1


@pytest.mark.parametrize("where", ["read", "commit"])
def test_stream_errors_exit_3_and_commit_nothing(where: str, tmp_path: Path, fake_topic: FakeTopic, registries: Registries,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    fake_topic.snapshot = snapshot_of(otlp_lines(population(**GOOD)))
    setattr(fake_topic, f"{where}_error", f"broker 不在：{where}")
    assert cli(tmp_path) == 3
    assert f"broker 不在：{where}" in capsys.readouterr().err
    assert fake_topic.commits == [] and fake_topic.closed == 1


def test_the_audit_names_the_snapshot_that_was_read(tmp_path: Path, fake_topic: FakeTopic, capsys: pytest.CaptureFixture[str]) -> None:
    values = otlp_lines(population(a_success=5, a_fail=1))
    fake_topic.snapshot = snapshot_of(values)
    assert cli(tmp_path, "--dry-run") == 0
    audits = [json.loads(line) for line in capsys.readouterr().err.splitlines() if "learning.read_traces" in line]
    assert len(audits) == 1
    ends = fake_topic.snapshot.ends
    assert audits[0]["attributes"]["learning.files"] == f"{TOPIC}#0:0-{ends[0]},{TOPIC}#1:0-{ends[1]}"


@pytest.mark.parametrize(
    ("argv", "env", "message"),
    [
        (["--topic", TOPIC], {stream.ENV_BROKERS: BROKERS}, "--group"),
        (["--group", GROUP, "--traces", "x"], {stream.ENV_BROKERS: BROKERS}, "--group"),
        (["--topic", TOPIC, "--group", GROUP, "--traces", "x"], {stream.ENV_BROKERS: BROKERS}, "--traces"),
        (["--topic", TOPIC, "--group", GROUP], {}, stream.ENV_BROKERS),
        (["--topic", TOPIC, "--group", "bad group"], {stream.ENV_BROKERS: BROKERS}, "消费组"),
    ],
    ids=["no-group", "group-without-topic", "both-sources", "no-brokers", "bad-group"],
)
def test_cli_topic_options(argv: list[str], env: dict[str, str], message: str, tmp_path: Path, fake_topic: FakeTopic,
                           capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["run", "--tenant", "t1", "--family", FAMILY, *argv, "--report-dir", str(tmp_path), "--dry-run"], env=env)
    assert code == 2
    assert message in capsys.readouterr().err
    assert fake_topic.opened == []


def test_an_unreachable_broker_is_a_stream_error() -> None:
    reader = stream.TopicReader("127.0.0.1:1", TOPIC, GROUP, timeout_s=3)
    try:
        with pytest.raises(stream.StreamError):
            reader.snapshot()
    finally:
        reader.close()


# --- 真的 broker（Docker 里的 Redpanda） ---


def fresh(redpanda: Any, *, partitions: int = 2) -> tuple[str, str]:
    """每个用例自己的主题与消费组。"""
    suffix = uuid.uuid4().hex[:8]
    topic = f"telemetry.spans.{suffix}"
    redpanda.create_topic(topic, partitions=partitions)
    return topic, f"learning-{suffix}"


def test_the_job_reads_the_whole_topic_and_commits_the_end(redpanda: Any, tmp_path: Path, registries: Registries, capsys: pytest.CaptureFixture[str]) -> None:
    topic, group = fresh(redpanda)
    values = otlp_lines(population(**GOOD)) + otlp_lines(population(a_success=30, a_fail=2, tenant="t2", salt="t2"))
    redpanda.produce(topic, values)
    assert redpanda.lag(topic, group) == len(values), "还没提交过：KEDA 把全部记录算作延迟"

    assert cli(tmp_path, topic=topic, group=group, brokers=redpanda.brokers) == 0
    line = summary(capsys)
    assert line["status"] == "published"
    report = report_of(line)
    assert report["tasks"] == 240 and report["other_tenant_spans"] > 0, "主题里混着别的租户：只取 --tenant 的，别的只计数"
    assert redpanda.committed(topic, group) == redpanda.ends(topic)
    assert redpanda.lag(topic, group) == 0


def test_a_later_run_still_sees_the_earlier_traces(redpanda: Any, tmp_path: Path, registries: Registries, capsys: pytest.CaptureFixture[str]) -> None:
    """提交只表示“评测到了哪里”：下一轮仍从最早保留的记录读，样本随历史累积，不只是上次之后的新记录。"""
    topic, group = fresh(redpanda)
    early = population(a_success=20, a_fail=1, b_success=5, salt="early")
    redpanda.produce(topic, otlp_lines(early))
    assert cli(tmp_path / "1", topic=topic, group=group, brokers=redpanda.brokers) == 0
    first = summary(capsys)
    assert first["status"] == "rejected" and any("样本" in r for r in first["reasons"])
    assert redpanda.lag(topic, group) == 0

    late = otlp_lines(population(**GOOD, salt="late"))
    redpanda.produce(topic, late)
    assert redpanda.lag(topic, group) == len(late), "上次评测之后新到的记录"
    assert cli(tmp_path / "2", topic=topic, group=group, brokers=redpanda.brokers) == 0
    second = report_of(summary(capsys))
    assert second["tasks"] == len(early) + 240
    assert redpanda.lag(topic, group) == 0


def test_a_failed_run_leaves_the_lag_for_the_next_round(redpanda: Any, tmp_path: Path, registries: Registries, capsys: pytest.CaptureFixture[str]) -> None:
    topic, group = fresh(redpanda)
    values = otlp_lines(population(**GOOD))
    redpanda.produce(topic, values)
    registries.reject_promote = True
    assert cli(tmp_path / "1", topic=topic, group=group, brokers=redpanda.brokers) == 1
    assert summary(capsys)["status"] == "promote_failed"
    assert all(offset < 0 for offset in redpanda.committed(topic, group).values()), "失败不提交"
    assert redpanda.lag(topic, group) == len(values)

    registries.reject_promote = False
    assert cli(tmp_path / "2", topic=topic, group=group, brokers=redpanda.brokers) == 0
    assert summary(capsys)["status"] == "published"
    assert redpanda.lag(topic, group) == 0


def test_a_bad_record_on_the_topic_commits_nothing(redpanda: Any, tmp_path: Path, registries: Registries, capsys: pytest.CaptureFixture[str]) -> None:
    topic, group = fresh(redpanda)
    redpanda.produce(topic, otlp_lines(population(a_success=5, a_fail=1)))
    redpanda.produce(topic, ["not json"], partition=1)
    assert cli(tmp_path, topic=topic, group=group, brokers=redpanda.brokers) == 2
    assert "分区 1 偏移" in capsys.readouterr().err
    assert all(offset < 0 for offset in redpanda.committed(topic, group).values())


def test_dry_run_on_the_topic_does_not_move_the_group(redpanda: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    topic, group = fresh(redpanda)
    values = otlp_lines(population(**GOOD))
    redpanda.produce(topic, values)
    assert cli(tmp_path, "--dry-run", topic=topic, group=group, brokers=redpanda.brokers) == 0
    assert summary(capsys)["status"] == "dry_run"
    assert redpanda.lag(topic, group) == len(values)


def test_records_after_the_snapshot_are_left_as_lag(redpanda: Any) -> None:
    topic, group = fresh(redpanda)
    redpanda.produce(topic, otlp_lines(population(a_success=5, a_fail=1)))
    before = redpanda.ends(topic)
    reader = stream.TopicReader(redpanda.brokers, topic, group)
    try:
        snapshot = reader.snapshot()
        assert snapshot.ends == before and snapshot.starts == dict.fromkeys(before, 0)
        assert len(snapshot.records) == sum(before.values())
        assert [(r.partition, r.offset) for r in snapshot.records] == sorted((r.partition, r.offset) for r in snapshot.records)
        redpanda.produce(topic, ["{}", "{}", "{}"])
        reader.commit(snapshot)
    finally:
        reader.close()
    assert redpanda.committed(topic, group) == before
    assert redpanda.lag(topic, group) == 3, "快照之后到的记录留给下一轮"

    again = stream.TopicReader(redpanda.brokers, topic, group)
    try:
        assert len(again.snapshot().records) == sum(before.values()) + 3, "下一轮仍从最早保留的记录读"
    finally:
        again.close()


def test_a_topic_that_does_not_exist_yet_reads_as_empty(redpanda: Any, tmp_path: Path, registries: Registries, capsys: pytest.CaptureFixture[str]) -> None:
    topic = f"telemetry.spans.missing-{uuid.uuid4().hex[:8]}"
    reader = stream.TopicReader(redpanda.brokers, topic, "learning-missing")
    try:
        snapshot = reader.snapshot()
    finally:
        reader.close()
    assert snapshot.records == () and snapshot.ends == {}
    assert cli(tmp_path, topic=topic, group="learning-missing", brokers=redpanda.brokers) == 0
    assert summary(capsys)["status"] == "no_candidate"
