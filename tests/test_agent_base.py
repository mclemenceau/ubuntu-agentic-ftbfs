"""Backend-neutral helpers: parsing a model's JSON answer."""

import pytest

from ftbfs.agents.base import AgentBackend

extract = AgentBackend.extract_json


@pytest.mark.parametrize("text", [
    '{"a": 1}',
    'Here it is:\n```json\n{"a": 1}\n```',
    '{"a": 1}\nHope this helps.',
])
def test_plain_fenced_and_trailing_text(text):
    assert extract(text) == {"a": 1}


def test_trailing_comma_before_closing_bracket():
    # The most common malformed answer in run 22 (12 of 51 diagnoses).
    text = '{\n "a": [1, 2,\n ],\n "b": "x",\n}'
    assert extract(text) == {"a": [1, 2], "b": "x"}


def test_commas_inside_strings_are_kept():
    text = '{"a": "x, }", "b": "y\\", ]",}'
    assert extract(text) == {"a": "x, }", "b": 'y", ]'}


def test_raw_newline_inside_string():
    assert extract('{"a": "line 1\nline 2"}') == {"a": "line 1\nline 2"}


@pytest.mark.parametrize("text", ["", "no json here", '{"a": }'])
def test_unparseable_is_none(text):
    assert extract(text) is None
