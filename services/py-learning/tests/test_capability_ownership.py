"""Experiment ownership and exact evidence recovery, with real process locks."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

import capability_support as cs
from agentruntime_learning.capability import matrix as mx
from agentruntime_learning.capability.__main__ import main as capability_main
from test_capability_matrix import plan


@pytest.fixture(autouse=True)
def reset_evaluators():
    cs.reset()


def artifacts(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file() and p.name != mx.OWNER_LOCK}


async def seed_run(root: Path):
    config = plan(n=2, r=1, candidates=[cs.SONNET])
    await mx.MatrixRunner(config, cs.ServingGateway(), root).run()
    return config


def test_second_process_cannot_read_truncate_score_or_publish_active_run_without_budget(tmp_path):
    out = tmp_path / 'out'
    config = plan(n=2, r=1, candidates=[cs.SONNET])
    source = tmp_path / 'input-plan.json'
    source.write_text(json.dumps(config.raw))
    runner = mx.MatrixRunner(config, cs.ServingGateway(), out)  # no BudgetGateway
    try:
        (out / mx.PLAN_COPY).write_text(json.dumps(config.raw))
        (out / mx.RESULTS).write_bytes(b'{"incomplete":')
        before = artifacts(out)
        constructor = (
            'import sys; from pathlib import Path; import capability_support as cs; '
            'from agentruntime_learning.capability import matrix as mx; '
            'mx.MatrixRunner(mx.load_plan(Path(sys.argv[1])), cs.ServingGateway(), Path(sys.argv[2]))'
        )
        result = subprocess.run([sys.executable, '-c', constructor, str(source), str(out)], capture_output=True, text=True, timeout=15)
        assert result.returncode != 0 and 'active owner' in result.stderr
        for command in ('score', 'publish'):
            result = subprocess.run([sys.executable, '-m', 'agentruntime_learning.capability', command, '--out', str(out)],
                                    capture_output=True, text=True, timeout=15)
            assert result.returncode == 2 and 'active owner' in result.stderr
        with pytest.raises(mx.DirectoryBusy):
            mx.read_results(out)
        assert artifacts(out) == before, 'rejection must precede partial-line recovery or any plan/report mutation'
    finally:
        runner.close()
    with mx.output_owner(out):
        pass


def test_crashed_process_releases_os_owner_without_removing_lock_inode(tmp_path):
    code = (
        'import os,sys; from pathlib import Path; '
        'from agentruntime_learning.capability.matrix import DirectoryOwner; '
        'owner=DirectoryOwner(Path(sys.argv[1])); os._exit(17)'
    )
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True, timeout=15)
    assert result.returncode == 17
    inode = (tmp_path / mx.OWNER_LOCK).stat().st_ino
    with mx.output_owner(tmp_path):
        assert (tmp_path / mx.OWNER_LOCK).stat().st_ino == inode
    assert (tmp_path / mx.OWNER_LOCK).stat().st_ino == inode


@pytest.mark.parametrize('bad', [b'not-json\n', b'{"bad":\n', b'\xff\n', b'[]\n', b'{"score":NaN}\n'])
async def test_corrupt_complete_record_fails_without_touching_later_partial_tail(tmp_path, bad):
    config = await seed_run(tmp_path)
    original = (tmp_path / mx.RESULTS).read_bytes()
    (tmp_path / mx.RESULTS).write_bytes(bad + original + b'{"later-incomplete":')
    before = artifacts(tmp_path)
    with pytest.raises(mx.ResumeMismatch, match='line 1'):
        mx.MatrixRunner(config, cs.ServingGateway(), tmp_path)
    assert artifacts(tmp_path) == before
    for command in ('score', 'publish'):
        assert capability_main([command, '--out', str(tmp_path)]) == 2
        assert artifacts(tmp_path) == before
    with mx.output_owner(tmp_path):
        pass  # constructor and both CLI error paths released ownership


@pytest.mark.parametrize('suffix', [b'{"unfinished":', b'{"unicode":"\xe4'])
async def test_writer_recovers_only_incomplete_tail_and_never_repeats_completed_items(tmp_path, suffix):
    config = await seed_run(tmp_path)
    complete = (tmp_path / mx.RESULTS).read_bytes()
    (tmp_path / mx.RESULTS).write_bytes(complete + suffix)
    before = artifacts(tmp_path)
    assert capability_main(['score', '--out', str(tmp_path)]) == 2
    assert artifacts(tmp_path) == before, 'score does not recover or silently ignore an incomplete record'
    provider = cs.ServingGateway()
    resumed = mx.MatrixRunner(config, provider, tmp_path)
    assert (tmp_path / mx.RESULTS).read_bytes() == complete
    await resumed.run()
    assert provider.requests == [] and len(mx.read_results(tmp_path)) == 2


async def test_valid_final_object_without_newline_is_preserved(tmp_path):
    config = await seed_run(tmp_path)
    complete = (tmp_path / mx.RESULTS).read_bytes()
    (tmp_path / mx.RESULTS).write_bytes(complete.rstrip(b'\n'))
    provider = cs.ServingGateway()
    await mx.MatrixRunner(config, provider, tmp_path).run()
    assert not provider.requests
    assert (tmp_path / mx.RESULTS).read_bytes() == complete


async def test_configuration_rejection_does_not_repair_the_tail(tmp_path):
    await seed_run(tmp_path)
    with (tmp_path / mx.RESULTS).open('ab') as handle:
        handle.write(b'{"unfinished":')
    before = artifacts(tmp_path)
    with pytest.raises(mx.ResumeMismatch):
        mx.MatrixRunner(plan(n=2, r=1, candidates=[cs.SONNET], seed=1), cs.ServingGateway(), tmp_path)
    assert artifacts(tmp_path) == before
    with mx.output_owner(tmp_path):
        pass


async def test_budget_setup_failure_and_run_validation_failure_release_directory(tmp_path):
    invalid_budget = tmp_path / 'budget'
    runner = mx.MatrixRunner(plan(n=1, r=1), cs.ServingGateway(), invalid_budget, budget_usd=-1)
    with pytest.raises(ValueError, match='positive'):
        await runner.run()
    with mx.output_owner(invalid_budget):
        pass
    wrong_role = tmp_path / 'role'
    runner = mx.MatrixRunner(plan(n=1, r=1, options={'key': 'wrong-role', 'role': 'route'}), cs.ServingGateway(), wrong_role)
    with pytest.raises(mx.PlanError, match='route'):
        await runner.run()
    with mx.output_owner(wrong_role):
        pass


async def test_cancellation_drains_parallel_calls_before_releasing_directory(tmp_path):
    class Waiting(cs.ServingGateway):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.ready = asyncio.Event()
            self.wait = asyncio.Event()

        async def generate(self, request):
            self.active += 1
            if self.active == 3:
                self.ready.set()
            try:
                await self.wait.wait()
            finally:
                self.active -= 1

    provider = Waiting()
    runner = mx.MatrixRunner(plan(n=4, r=1, candidates=[cs.SONNET], concurrency=3), provider, tmp_path)
    running = asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(provider.ready.wait(), timeout=5)
        with pytest.raises(mx.DirectoryBusy):
            mx.DirectoryOwner(tmp_path)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert provider.active == 0
        with mx.output_owner(tmp_path):
            pass
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize('suffix', [b'{"bad":}', b'not-json', b'{"x":1}garbage', b'{"x":"\xff"}'])
async def test_malformed_complete_tail_is_not_misclassified_as_a_truncated_append(tmp_path, suffix):
    config = await seed_run(tmp_path)
    with (tmp_path / mx.RESULTS).open('ab') as handle:
        handle.write(suffix)
    before = artifacts(tmp_path)
    with pytest.raises(mx.ResumeMismatch, match='corrupt JSON'):
        mx.MatrixRunner(config, cs.ServingGateway(), tmp_path)
    assert artifacts(tmp_path) == before
