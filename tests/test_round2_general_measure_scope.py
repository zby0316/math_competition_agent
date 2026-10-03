"""Most general measure-space assumptions reach both proof stages, offline."""
import importlib

import pytest

from config import CONFIG
from nodes.assemble_step import assemble_step
from nodes.reason_step import reason_step
from utils.deps import Deps, MockClient
from utils.domain_excerpt import select_skill_excerpt
from utils.domain_loader import DomainLoader
from utils.llm_retry_guard import ChatResult


PROBLEM = '可测函数列几乎处处收敛，讨论一般测度空间中极限的可测性。'
RESPONSE = ('## 结论速览\n需要核对极限的可测性。\n'
            '## 问题分析\n未假定空间完备。\n'
            '## 详细解题步骤\n选可测零测包络，在整个包络上固定赋值，'
            '并在其补集上论证代表的可测性；恢复原函数时将包络并入例外集。\n'
            '## 最终答案\n需要核对极限的可测性。\n'
            '## 关键验证点\n不能对未经论证的实际坏点集取测度。')


@pytest.mark.parametrize('limit', [2200, 3000])
def test_actual_measure_excerpt_preserves_general_scope_and_transfer(limit):
    doc = DomainLoader().get_skill_document('测度积分')
    excerpt = select_skill_excerpt(doc, PROBLEM, limit)
    assert len(excerpt) <= limit
    assert '可测零测包络' in excerpt
    assert '实际坏点集' in excerpt and '未必可测' in excerpt
    assert '整个包络' in excerpt and '恢复到原函数' in excerpt


def test_reasoning_receives_general_scope_without_extra_attempt(monkeypatch):
    reason_module = importlib.import_module('nodes.reason_step')
    calls = []
    def chat(*args, **kwargs):
        calls.append(kwargs)
        return ChatResult(RESPONSE, 'stop')
    monkeypatch.setattr(reason_module, 'chat_with_retry', chat)
    monkeypatch.setattr(reason_module, 'run_with_timeout', lambda fn, seconds: fn())
    result = reason_step({'problem': PROBLEM, 'category': '测度积分', 'question_mode': 'proof'},
                         {'configurable': {'deps': Deps(client=MockClient(), skills_loader=DomainLoader())}})
    assert len(calls) == result['reasoning_attempts'] == 1
    prompt = calls[0]['messages'][0]['content']
    assert '可测零测包络' in prompt and '恢复原函数' in prompt
    assert calls[0]['max_tokens'] == CONFIG['max_tokens']['reasoning']
    assert calls[0]['temperature'] == CONFIG['temperatures']['reasoning']
    assert all(not attempt['mathematical_correctness_verified']
               for attempt in result['reasoning_result']['proof_attempts'])


def test_coordinator_receives_scope_with_full_candidate_and_uncertified_review():
    client = MockClient([RESPONSE + '\n<!-- PROOF_REVIEW {"status":"none_reported","issues":[]} -->'])
    result = assemble_step({'problem': PROBLEM, 'category': '测度积分',
                            'reasoning_result': {'answer': '需要核对极限的可测性。',
                                                 'proof_body': RESPONSE,
                                                 'finish_reason': 'length',
                                                 'missing_sections': ['关键验证点']},
                            'validation_details': {'problem_type': 'proof'}},
                           {'configurable': {'deps': Deps(client=client)}})
    assert len(client.calls) == 1
    call = client.calls[0]
    prompt = call['messages'][0]['content']
    assert '可测零测包络' in prompt and '将包络并入例外集' in prompt
    assert RESPONSE in prompt.replace('\\n', '\n')
    assert 'length' in prompt and 'missing_sections' in prompt
    assert call['max_tokens'] == CONFIG['max_tokens']['coordinator']
    assert call['temperature'] == CONFIG['temperatures']['coordinator']
    assert result['proof_review']['mathematical_correctness_verified'] is False
