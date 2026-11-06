from __future__ import annotations

import asyncio
import copy
import json

import pytest
from temporalio.testing import ActivityEnvironment

from agentruntime_worker.reviewer.judge import JevJudge, capture_usage
from agentruntime_worker.reviewer.activities import ReviewerActivities, StaticCatalogSource
from agentruntime_worker.reviewer.reviewer import Reviewer
from agentruntime_worker.activities.types import StageResult
from .test_n09_jev_judge import ANSWER, FAST, Recorder
from .test_reviewer_activity import review_input
from .reviewer_support import CATALOG, FakeReviewGateway, done


async def key():
    return 'test-secret-not-in-evidence'


@pytest.mark.parametrize('status,payload,known,tokens,attempted', [
    (200, ANSWER, True, 529, True),
    (200, {**ANSWER, 'usage': {}}, False, None, True),
    (200, {**ANSWER, 'usage': {'input_tokens': -1}}, False, None, True),
    (503, {}, False, None, True),
    (200, TimeoutError('private-response'), False, None, True),
])
async def test_jev_every_attempt_has_known_or_explicit_unknown_usage(status, payload, known, tokens, attempted):
    judge = JevJudge(key, post=Recorder(status, payload))
    with capture_usage() as rows:
        await judge.check('message', 'reply')
    assert len(rows) == 1 and rows[0]['provider_attempted'] is attempted
    assert rows[0]['usage_known'] is known and rows[0]['input_tokens'] == tokens
    assert 'test-secret' not in json.dumps(rows) and 'private-response' not in json.dumps(rows)


async def test_usage_contexts_are_isolated_and_missing_key_is_explicitly_free():
    async def one(token_count):
        async def post(*args):
            await asyncio.sleep(0)
            return 200, json.dumps({**ANSWER, 'usage': {'input_tokens': token_count}}).encode()
        with capture_usage() as rows:
            await JevJudge(key, post=post).check('x', 'y')
        return rows
    left, right = await asyncio.gather(one(1), one(2))
    assert left[0]['input_tokens'] == 1 and right[0]['input_tokens'] == 2
    async def no_key():
        return None
    with capture_usage() as rows:
        assert await JevJudge(no_key).check('x', 'y') is None
    assert rows[0]['provider_attempted'] is False and rows[0]['input_tokens'] == 0


async def test_activity_persists_successful_check_even_when_veto_rule_is_disabled():
    reviewer = Reviewer(FakeReviewGateway([done('answer')]), judge=JevJudge(key, post=Recorder(200, ANSWER)))
    acts = ReviewerActivities(reviewer, StaticCatalogSource(CATALOG), None)
    inp = review_input(stage=FAST, results={'answer': StageResult(role='deliver', status='succeeded', result={'text': 'An answer with verified information'})})
    inp.verification = {'judge_blocked': 1}
    out = await ActivityEnvironment().run(acts.review_round, inp)
    usage = out.judge_usage
    assert usage and usage['usage_known'] and usage['provider_attempted']
    assert usage['attempts'][0]['veto_rule_disabled'] is True
    assert usage['attempts'][0]['input_tokens'] == 529
    assert usage['attempts'][0]['task_id'] == inp.task_id
