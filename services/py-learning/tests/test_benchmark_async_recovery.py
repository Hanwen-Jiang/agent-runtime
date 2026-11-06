"""No-TCP regressions for durable workspace operations and ambiguous submissions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.benchmarks.client import ApiError, RuntimeClient, WorkspaceOperationFailed
from swebench_support import FakeRuntime, records, settings, write_instances

OPERATION = 'wso-' + 'a' * 64


class ProtocolClient(RuntimeClient):
    def __init__(self, replies, **kwargs):
        self.now = 0.0
        super().__init__('https://example.invalid', 'secret-token', clock=lambda: self.now, sleep=self.advance, **kwargs)
        self.replies = list(replies)
        self.calls = []

    def advance(self, seconds):
        self.now += seconds

    def _call(self, service, method, body, *, timeout=None):
        self.calls.append((method, body, timeout))
        expected, value = self.replies.pop(0)
        assert method == expected
        if isinstance(value, Exception):
            raise value
        return value


def state(status, **kwargs):
    return {'operationId': OPERATION, 'status': 'WORKSPACE_OPERATION_STATUS_' + status, **kwargs}


def test_creation_uses_short_start_and_poll_requests_and_checkpoints_before_poll():
    client = ProtocolClient([
        ('StartWorkspaceOperation', {'operationId': OPERATION}),
        ('GetWorkspaceOperation', state('RUNNING')),
        ('GetWorkspaceOperation', state('COMPLETED', result={'workspace': {'workspaceId': 'w1', 'head': 'abc'}})),
    ], timeout=660)
    saved = []
    result = client.create_workspace_operation('w1', repo_url='', ref='', files=[], request_id='persisted-request', checkpoint=saved.append)
    assert result['head'] == 'abc' and saved == [{'operation_id': OPERATION, 'transport': 'async'}]
    assert [method for method, _, _ in client.calls] == ['StartWorkspaceOperation', 'GetWorkspaceOperation', 'GetWorkspaceOperation']
    assert all(timeout <= 60 for _, _, timeout in client.calls)
    assert client.calls[0][1]['request_id'] == 'persisted-request'


def test_reconnect_polls_known_operation_and_never_starts_or_downgrades():
    client = ProtocolClient([
        ('GetWorkspaceOperation', ApiError(524, 'unknown', 'edge disconnected')),
        ('GetWorkspaceOperation', state('COMPLETED', result={'workspace': {'head': 'abc'}})),
    ])
    assert client.create_workspace_operation('w1', repo_url='', ref='', files=[], request_id='same', operation_id=OPERATION)['head'] == 'abc'
    assert all(call[0] == 'GetWorkspaceOperation' for call in client.calls)


def test_only_explicit_unimplemented_enables_legacy_workspace_compatibility():
    client = ProtocolClient([
        ('StartWorkspaceOperation', ApiError(404, 'unimplemented', 'old API')),
        ('CreateWorkspace', {'workspace': {'head': 'legacy'}}),
        ('DeleteWorkspace', {}),
    ])
    assert client.create_workspace('w1', repo_url='', ref='', files=[])['head'] == 'legacy'
    client.delete_workspace('w1')
    unknown = ProtocolClient([('StartWorkspaceOperation', ApiError(524, 'unknown', 'ambiguous start'))])
    with pytest.raises(ApiError):
        unknown.create_workspace('w1', repo_url='', ref='', files=[])
    assert len(unknown.calls) == 1


def test_polling_timeout_keeps_the_operation_recoverable_and_failed_status_is_terminal():
    client = ProtocolClient([
        ('StartWorkspaceOperation', {'operationId': OPERATION}),
        ('GetWorkspaceOperation', state('RUNNING')),
        ('GetWorkspaceOperation', state('RUNNING')),
    ], operation_timeout=3, poll_interval=2)
    saved = []
    with pytest.raises(ApiError, match='still pending'):
        client.create_workspace_operation('w1', repo_url='', ref='', files=[], request_id='same', checkpoint=saved.append)
    assert saved[0]['operation_id'] == OPERATION
    client = ProtocolClient([('GetWorkspaceOperation', state('FAILED', error={'category': 'ERROR_CATEGORY_INVALID', 'code': 'bad_ref', 'message': 'bad ref'}))])
    with pytest.raises(WorkspaceOperationFailed) as error:
        client.create_workspace_operation('w1', repo_url='', ref='', files=[], request_id='same', operation_id=OPERATION)
    assert error.value.status == 400 and error.value.code == 'bad_ref'


def test_poll_result_cannot_be_taken_from_another_operation():
    client = ProtocolClient([('GetWorkspaceOperation', {'operationId': 'other', 'status': 'WORKSPACE_OPERATION_STATUS_COMPLETED'})])
    with pytest.raises(ApiError, match='different operation'):
        client.delete_workspace_operation('w1', request_id='same', operation_id=OPERATION)


class DurableRuntime(FakeRuntime):
    def __init__(self, out, *, lose_create=False, lose_poll=False, lose_submit=False, lose_delete=False, reject_submit=False):
        super().__init__()
        self.out = out
        self.operations = {}
        self.operation_calls = []
        self.requests_by_key = {}
        self.submission_calls = []
        self.lose_create, self.lose_poll = lose_create, lose_poll
        self.lose_submit, self.lose_delete, self.reject_submit = lose_submit, lose_delete, reject_submit
        self.deny_recovery = False

    def _saved(self):
        return next(iter(records(self.out).values()))

    def create_workspace_operation(self, workspace_id, *, request_id, operation_id='', checkpoint, **kwargs):
        rec = self._saved()
        assert rec['workspace_operations']['create']['request_id'] == request_id, 'intent must be persisted before starting'
        self.operation_calls.append(('create', request_id, operation_id))
        if request_id not in self.operations:
            self.operations[request_id] = (f'op-{request_id}', super().create_workspace(workspace_id, **kwargs))
        op, result = self.operations[request_id]
        if self.lose_create:
            self.lose_create = False
            raise ApiError(524, 'unknown', 'start response lost')
        checkpoint({'operation_id': op, 'transport': 'async'})
        if self.lose_poll:
            self.lose_poll = False
            raise ApiError(0, 'unavailable', 'poll disconnected')
        return result

    def delete_workspace_operation(self, workspace_id, *, request_id, operation_id='', checkpoint):
        assert self._saved()['workspace_operations']['delete']['request_id'] == request_id
        self.operation_calls.append(('delete', request_id, operation_id))
        if request_id not in self.operations:
            super().delete_workspace(workspace_id)
            self.operations[request_id] = (f'op-{request_id}', None)
        checkpoint({'operation_id': self.operations[request_id][0], 'transport': 'async'})
        if self.lose_delete:
            self.lose_delete = False
            raise ApiError(0, 'unavailable', 'delete response lost')

    def submit(self, *, client_request_id, **kwargs):
        rec = self._saved()
        assert rec['client_request_id'] == client_request_id and rec['submission_state'] == 'pending'
        self.submission_calls.append(client_request_id)
        if self.deny_recovery:
            raise ApiError(403, 'permission_denied', 'auth changed after unknown response')
        if self.reject_submit:
            self.reject_submit = False
            raise ApiError(400, 'invalid_argument', '命令过长')
        if client_request_id in self.requests_by_key:
            return {**self.requests_by_key[client_request_id], 'deduplicated': True}
        result = super().submit(client_request_id=client_request_id, **kwargs)
        self.requests_by_key[client_request_id] = result
        if self.lose_submit:
            self.lose_submit = False
            raise ApiError(524, 'unknown', 'Task launched; response lost')
        return result


def instance(tmp_path):
    return sb.load_instances(write_instances(tmp_path / 'instances.jsonl'))[:1]


@pytest.mark.parametrize('failure', ['lose_create', 'lose_poll'])
def test_creation_recovers_same_request_or_operation_after_disconnect(tmp_path, failure):
    items = instance(tmp_path)
    out = tmp_path / 'run'
    runtime = DurableRuntime(out, **{failure: True})
    assert sb.run(runtime, items, out, settings()).errors == 1
    assert not runtime.deleted and not runtime.submits
    first = records(out)[items[0].instance_id]['workspace_operations']['create']
    assert first['state'] == 'pending'
    assert ('operation_id' in first) == (failure == 'lose_poll')
    assert sb.run(runtime, items, out, settings()).finished == 1
    created = [call for call in runtime.operation_calls if call[0] == 'create']
    assert created[0][1] == created[1][1] and len(runtime.create_calls) == 1
    if failure == 'lose_poll':
        assert created[1][2] == first['operation_id']


def test_lost_task_response_recovers_same_task_without_deleting_live_workspace(tmp_path):
    items = instance(tmp_path)
    out = tmp_path / 'run'
    runtime = DurableRuntime(out, lose_submit=True)
    assert sb.run(runtime, items, out, settings()).errors == 1
    before = records(out)[items[0].instance_id]
    assert before['submission_state'] == 'pending' and before['client_request_id']
    assert len(runtime.submits) == 1 and not runtime.deleted
    # A later rejection does not prove that the earlier ambiguous request failed.
    runtime.deny_recovery = True
    assert sb.run(runtime, items, out, settings()).errors == 1
    assert not runtime.deleted
    runtime.deny_recovery = False
    result = sb.run(runtime, items, out, settings())
    assert result.finished == 1 and result.submitted == 0
    assert len(runtime.submits) == 1 and len(runtime.create_calls) == 1
    assert len(set(runtime.submission_calls)) == 1
    assert records(out)[items[0].instance_id]['task_id'] == before.get('task_id', 'task-1')


def test_unknown_submission_stops_starting_more_cases_until_reconciled(tmp_path):
    items = sb.load_instances(write_instances(tmp_path / 'instances.jsonl'))[:2]
    out = tmp_path / 'run'
    runtime = DurableRuntime(out, lose_submit=True)
    summary = sb.run(runtime, items, out, settings(concurrency=1))
    assert summary.errors == 1 and len(runtime.submits) == 1 and len(runtime.create_calls) == 1
    assert not runtime.deleted


def test_definite_rejection_then_rebuild_uses_new_creation_and_submission_identity(tmp_path):
    items = instance(tmp_path)
    out = tmp_path / 'run'
    runtime = DurableRuntime(out, reject_submit=True)
    assert sb.run(runtime, items, out, settings()).errors == 1
    old = records(out)[items[0].instance_id]
    assert old['submission_state'] == 'rejected' and runtime.deleted
    assert sb.run(runtime, items, out, settings()).finished == 1
    new = records(out)[items[0].instance_id]
    assert new['workspace_generation'] != old['workspace_generation']
    assert new['client_request_id'] != old['client_request_id']
    creates = [call[1] for call in runtime.operation_calls if call[0] == 'create']
    assert len(set(creates)) == 2 and len(runtime.create_calls) == 2


def test_cleanup_disconnect_is_resumed_without_rerunning_task_or_reading_deleted_workspace(tmp_path):
    items = instance(tmp_path)
    out = tmp_path / 'run'
    runtime = DurableRuntime(out, lose_delete=True)
    assert sb.run(runtime, items, out, settings()).finished == 1
    old = records(out)[items[0].instance_id]
    assert old['cleanup_pending'] and old['workspace_operations']['delete']['operation_id']
    assert sb.run(runtime, items, out, settings()).finished == 1
    assert not records(out)[items[0].instance_id]['cleanup_pending']
    assert len(runtime.submits) == 1 and len(runtime.diffs) == 1 and len(runtime.deleted) == 1
    deletes = [call for call in runtime.operation_calls if call[0] == 'delete']
    assert len(deletes) == 2 and deletes[0][1] == deletes[1][1] and deletes[1][2]


def test_unknown_submission_is_reported_as_one_error_and_remaining_unrun(tmp_path):
    items = sb.load_instances(write_instances(tmp_path / 'instances.jsonl'))[:2]
    out = tmp_path / 'run'
    runtime = DurableRuntime(out, lose_submit=True)
    summary = sb.run(runtime, items, out, settings(concurrency=1))
    assert summary.errors == 1 and summary.not_run == 1 and summary.finished == 0


def test_resume_waits_for_ambiguous_submission_before_using_spare_concurrency(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    items = sb.load_instances(write_instances(tmp_path / 'instances.jsonl'))[:2]
    out = tmp_path / 'run'
    runtime = DurableRuntime(out, lose_submit=True)
    sb.run(runtime, items, out, settings(concurrency=1))
    entered, release = threading.Event(), threading.Event()
    original = runtime.submit

    def wait_then_refuse(**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        runtime.deny_recovery = True
        return original(**kwargs)

    runtime.submit = wait_then_refuse
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(sb.run, runtime, items, out, settings(concurrency=3))
        try:
            assert entered.wait(timeout=5)
            # Polling a small local event allows another scheduled worker to
            # expose accidental creation; it does not involve TCP or a model.
            assert not release.wait(timeout=.03)
            assert len(runtime.create_calls) == 1
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result.errors == 1 and result.not_run == 1 and len(runtime.create_calls) == 1


def test_generic_invalid_argument_can_follow_task_launch_and_is_not_cleanup_permission(tmp_path):
    items = instance(tmp_path)
    out = tmp_path / 'run'
    runtime = DurableRuntime(out)
    original = runtime.submit

    def launch_then_fail(**kwargs):
        original(**kwargs)
        raise ApiError(400, 'invalid_argument', 'post-dispatch store failure')

    runtime.submit = launch_then_fail
    assert sb.run(runtime, items, out, settings()).errors == 1
    rec = records(out)[items[0].instance_id]
    assert rec['submission_state'] == 'pending' and rec['submission_ambiguous']
    assert not runtime.deleted and len(runtime.submits) == 1
    runtime.submit = original
    assert sb.run(runtime, items, out, settings()).finished == 1
    assert len(runtime.submits) == 1


def test_definite_tenant_budget_admission_failure_is_not_replayed_past_local_budget(tmp_path):
    items = instance(tmp_path)
    out = tmp_path / 'run'
    runtime = DurableRuntime(out)
    original = runtime.submit
    runtime.submit = lambda **kwargs: (_ for _ in ()).throw(ApiError(403, 'permission_denied', 'tenant model budget exceeded'))
    assert sb.run(runtime, items, out, settings()).errors == 1
    rec = records(out)[items[0].instance_id]
    assert rec['submission_state'] == 'rejected'
    runtime.submit = original
    summary = sb.run(runtime, items, out, settings(budget_usd=0))
    assert not runtime.submits and summary.submitted == 0


@pytest.mark.parametrize('workspace', [{}, {'workspaceId': 'other', 'head': 'abc'}])
def test_successful_operation_must_return_the_expected_workspace_head(workspace):
    client = ProtocolClient([('GetWorkspaceOperation', state('COMPLETED', result={'workspace': workspace}))])
    with pytest.raises(ApiError, match='no head or belongs'):
        client.create_workspace_operation('w1', repo_url='', ref='', files=[], request_id='same', operation_id=OPERATION)


def test_checkpoint_failure_stops_scheduler_without_starting_remote_work(tmp_path, monkeypatch):
    items = instance(tmp_path)
    out = tmp_path / 'run'
    (out / 'records').mkdir(parents=True)
    runtime = DurableRuntime(out)
    runner = sb._Runner(runtime, items, out, settings())
    runner.reserved[items[0].instance_id] = 1

    def disk_full(*args, **kwargs):
        raise OSError('checkpoint disk full')

    monkeypatch.setattr(sb, '_write_text', disk_full)
    with pytest.raises(OSError, match='disk full'):
        runner._one(items[0])
    assert runner.stopping and not runner.reserved
    assert not runtime.create_calls and not runtime.submits and not runtime.operation_calls
