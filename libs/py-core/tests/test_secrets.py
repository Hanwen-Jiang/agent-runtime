"""Secret：打印、格式化、序列化都只得到掩码。"""

from __future__ import annotations

import copy
import json
import logging
import pickle

import pytest

from agentruntime_core.secrets import MASK, Secret

VALUE = "sk-live-0123456789"


def test_reveal_returns_value():
    assert Secret(VALUE).reveal() == VALUE
    assert Secret(b"\x00\x01").reveal() == b"\x00\x01"


@pytest.mark.parametrize(
    "render",
    [str, repr, lambda s: f"{s}", lambda s: f"{s!r}", lambda s: f"{s:>40}", lambda s: "%s" % s, lambda s: "{}".format(s)],
)
def test_rendering_is_masked(render):
    text = render(Secret(VALUE))
    assert VALUE not in text and MASK in text


def test_logging_is_masked(caplog):
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("key=%s %r", Secret(VALUE), Secret(VALUE))
    assert VALUE not in caplog.text


def test_cannot_be_serialized_or_copied_out():
    with pytest.raises(TypeError):
        pickle.dumps(Secret(VALUE))
    with pytest.raises(TypeError):
        json.dumps({"k": Secret(VALUE)})
    with pytest.raises(TypeError):
        copy.copy(Secret(VALUE))


def test_equality_and_hashing():
    assert Secret(VALUE) == Secret(VALUE)
    assert Secret(VALUE) != Secret("other")
    assert Secret("a") != Secret(b"a")
    with pytest.raises(TypeError):
        hash(Secret(VALUE))


def test_rejects_non_text_values():
    with pytest.raises(TypeError):
        Secret(123)  # type: ignore[arg-type]
