"""Q78：可追溯的任务族/阶段能力和发布日期，配置继续拒绝拼错字段。"""

from copy import deepcopy

import pytest

from contract_tests import schemas


def profile():
    p = deepcopy(schemas.example_files()["model_profile"]["valid"][0]["instance"])
    p.update({
        "released_at": "2026-10-01",
        "availability": True,
        "capability_scores": [{
            "task_family": "coding.patch", "role": "transform", "tier": p["tiers"][0]["name"],
            "score": 0.9, "source": "synthetic:contract-test", "at": "2026-11-01",
        }],
        "reliability": {"score": 0.99, "source": "synthetic:uptime", "at": "2026-11-01"},
    })
    return p


def test_routing_metadata_and_legacy_profile_are_accepted():
    schemas.validator("model_profile").validate(profile())
    schemas.validator("model_profile").validate(schemas.example_files()["model_profile"]["valid"][0]["instance"])


@pytest.mark.parametrize("field,value", [
    ("released_at", "2026-02-30"), ("released_at", ""), ("availability", "yes"),
    ("release_at", "2026-10-01"),
    ("reliability", {"score": 1.1, "source": "test", "at": "2026-11-01"}),
    ("reliability", {"source": "test", "at": "2026-11-01"}),
])
def test_invalid_routing_metadata_is_rejected(field, value):
    p = profile()
    p[field] = value
    assert list(schemas.validator("model_profile").iter_errors(p))


@pytest.mark.parametrize("field,value", [
    ("score", -0.1), ("score", 1.01), ("source", ""), ("at", "2026-02-30"),
    ("role", "judeg"), ("task_family", ""), ("tier", ""), ("socre", 0.9),
])
def test_invalid_capability_measurement_is_rejected(field, value):
    p = profile()
    p["capability_scores"][0][field] = value
    assert list(schemas.validator("model_profile").iter_errors(p))


@pytest.mark.parametrize("field", ["task_family", "role", "tier", "score", "source", "at"])
def test_capability_measurement_requires_dimension_and_evidence(field):
    p = profile()
    del p["capability_scores"][0][field]
    assert list(schemas.validator("model_profile").iter_errors(p))
