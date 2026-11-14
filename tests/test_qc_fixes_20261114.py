"""Native response boundaries and truthful readiness after the November QC pass."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from algo_cli import decision_layer as d
from algo_cli.config import Config
from algo_cli.runtime_diagnostics import runtime_diagnostics

QUESTIONS = {'pick': {'type': 'choice', 'instructions': 'Choose the successful fixture.',
                      'criteria': {'yes': 'Successful fixture.', 'no': 'Failed fixture.'}}}
ANSWER = {'type': 'choice', 'choice': 'yes', 'probabilities': {'yes': 0.9, 'no': 0.1},
          'confidence': 0.8, 'certainty': 0.4, 'x_p_max': 0.9}


@pytest.mark.parametrize('answers', [[], None, 'invalid', 7, {}, {'unknown': ANSWER},
                                     {'pick': []}, {'pick': None}, {'pick': 'choice'}])
def test_decider_malformed_answers_return_typed_failure(monkeypatch, answers):
    request = {'state': 'public fixture', 'questions': QUESTIONS}
    response = {'ok': True, 'runtime': {'model_revision': 'eb5fbdfc9448473ec25e399882912863afbdb70e'},
                'input_sha256': d.digest(request).removeprefix('sha256:'),
                'response': {'model': 'decider-4b-v2.1', 'answers': deepcopy(answers)}}
    monkeypatch.setattr(d.CompanionProvider, '_path', lambda _: Path('/fixture/decider_local.py'))
    monkeypatch.setattr(d, '_cli_json', lambda *_: response)
    result = d.decide(Config(decision_routing_mode='direct'), 'public fixture', QUESTIONS, provider='decider')
    assert result['ok'] is False
    assert result['answers'] == {} and result['action_authority'] is False
    assert result['error'] in {'answer_ids_mismatch', 'answer_type_mismatch'}


def test_decider_valid_native_uncertainty_remains_separate(monkeypatch):
    request = {'state': 'public fixture', 'questions': QUESTIONS}
    response = {'ok': True, 'runtime': {'model_revision': 'eb5fbdfc9448473ec25e399882912863afbdb70e'},
                'input_sha256': d.digest(request).removeprefix('sha256:'),
                'response': {'model': 'decider-4b-v2.1', 'answers': {'pick': deepcopy(ANSWER)}}}
    monkeypatch.setattr(d.CompanionProvider, '_path', lambda _: Path('/fixture/decider_local.py'))
    monkeypatch.setattr(d, '_cli_json', lambda *_: response)
    result = d.decide(Config(decision_routing_mode='direct'), 'public fixture', QUESTIONS, provider='decider')
    assert result['ok']
    assert 'certainty' not in result['answers']['pick']
    assert result['provider_metadata']['native_uncertainty']['pick'] == {'certainty': 0.4, 'x_p_max': 0.9}


@pytest.mark.parametrize('status,available', [
    ({'configured': True, 'ready': False}, False),
    ({'configured': True, 'ready': True}, True),
    ({'configured': False}, False),
    ({'configured': True, 'availability': 'not_probed', 'local': False}, None),
    ({'available': False}, False), ({'available': True}, True),
    ({'available': True, 'ready': False}, False),
])
def test_diagnostics_preserve_unavailable_and_unknown(monkeypatch, status, available):
    monkeypatch.setattr(d, 'provider_for', lambda *_: SimpleNamespace(
        model='fixture', local=True, capabilities=d.Capabilities(), status=lambda: status))
    data = json.loads(runtime_diagnostics('decisions', provider='clm', cfg=Config()))['data']
    assert data['available'] is available
    assert data['inference'] == 'not_probed_by_status'


def test_remote_status_stays_unknown_without_contacting_endpoint(monkeypatch):
    cfg = Config(decision_provider_settings={'clef-flash': {'base_url': 'https://unprobed.invalid'}})
    monkeypatch.setattr(d, '_http_json', lambda *_: pytest.fail('Must not contact remote status endpoint'))
    data = json.loads(runtime_diagnostics('decisions', provider='clef-flash', cfg=cfg))['data']
    assert data['available'] is None and data['provider_health'] == 'not_verified'
    assert data['local'] is False


def test_go_toolchain_and_ci_pin_are_consistently_patched():
    root = Path(__file__).resolve().parents[1]
    assert '\ntoolchain go1.26.11\n' in (root / 'harness-gateway/go.mod').read_text()
    workflow = (root / '.github/workflows/oliver-ci.yml').read_text()
    assert 'go-version: "1.26.11"' in workflow
    assert 'go-version: "1.26.10"' not in workflow
