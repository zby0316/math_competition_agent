"""Regression of failures observed in paired batch A; no network calls."""
import json
import importlib

import pytest

from nodes.assemble_step import assemble_step
from nodes.reason_step import _parse_reasoning_output, _looks_token_exhausted, reason_step
from utils.deps import Deps, MockClient
from utils.domain_loader import DomainLoader
from utils.llm_retry_guard import ChatResult
from utils.proof_review import proof_handoff
from config import CONFIG

reason_module = importlib.import_module('nodes.reason_step')


DRAFT = ('Thinking Process:\nI will use `## 最终答案` and identical summary.\n'
         '```markdown\n## 问题分析\n[分析]\n## 详细解题步骤\n[步骤]\n'
         '## 最终答案\n[Result]\n```\nThe text "## 最终答案" must match.\n')
PARTIAL = ('## 结论速览\n映射是同胚。\n## 问题分析\n用闭映射证明。\n'
           '## 详细解题步骤\n取闭集 F。F 紧致，连续像紧致。由目标空间 Hausdorff，')
COMPLETE = ('## 结论速览\n映射是同胚。\n## 问题分析\n用闭映射证明。\n'
            '## 详细解题步骤\n取闭集 F。F 紧致，连续像紧致。目标 Hausdorff，故像闭，逆映射连续。\n'
            '## 最终答案\n映射是同胚。\n## 关键验证点\n- 全部前提满足。')


@pytest.mark.parametrize('finish', ['length', 'stop', None])
def test_draft_quotes_and_fenced_template_are_not_proof(finish):
    raw = ChatResult(DRAFT, finish_reason=finish)
    parsed = _parse_reasoning_output(raw, 'proof')
    assert not parsed['answer'] and not parsed['steps']
    assert parsed['format_failure'] == 'draft_without_formal_proof'
    assert _looks_token_exhausted(raw, parsed) is (finish == 'length')


def test_real_answer_after_draft_template_is_preserved():
    parsed = _parse_reasoning_output(ChatResult(DRAFT + '\n' + COMPLETE, 'stop'), 'proof')
    assert parsed['answer'] == '映射是同胚。'
    assert '逆映射连续' in parsed['proof_body']
    assert '[步骤]' not in parsed['proof_body']


@pytest.mark.parametrize('retry', [ChatResult(DRAFT, 'stop'), ChatResult('', 'stop')])
def test_empty_retry_keeps_prior_proof_and_handoff_records(monkeypatch, retry):
    responses = iter([ChatResult(PARTIAL, 'length'), retry])
    prompts = []
    def chat(client, **kwargs):
        prompts.append(kwargs['messages'][0]['content'])
        return next(responses)
    monkeypatch.setattr(reason_module, 'chat_with_retry', chat)
    monkeypatch.setattr(reason_module, 'run_with_timeout', lambda fn, seconds: fn())
    deps = Deps(client=MockClient(), skills_loader=DomainLoader())
    result = reason_step({'problem': '证明紧致空间到 Hausdorff 空间的连续双射是同胚。',
                          'category': '拓扑学', 'question_mode': 'proof'},
                         {'configurable': {'deps': deps}})
    assert len(prompts) == result['reasoning_attempts'] == 2
    parsed = result['reasoning_result']
    assert '取闭集 F' in parsed['proof_body']
    assert parsed['finish_reason'] == 'length'
    assert result['reasoning_raw_response'] == PARTIAL
    assert len(parsed['proof_attempts']) == 2
    assert parsed['proof_attempts'][1]['structurally_complete'] is False
    handoff = json.loads(proof_handoff(result))
    assert handoff['attempts'][0]['visible_response'] == PARTIAL
    assert handoff['attempts'][1]['visible_response'] == ''
    assert 'Thinking Process' not in json.dumps(handoff)
    assert '必须完全一致' not in prompts[0]
    assert '假设检验' not in prompts[0]
    assert '数学一致' in prompts[0]


def test_full_retry_is_available_without_merging_conflicting_claims(monkeypatch):
    responses = iter([ChatResult(PARTIAL, 'length'), ChatResult(COMPLETE, 'stop')])
    monkeypatch.setattr(reason_module, 'chat_with_retry', lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(reason_module, 'run_with_timeout', lambda fn, seconds: fn())
    result = reason_step({'problem': '证明映射是同胚。', 'category': '拓扑学', 'question_mode': 'proof'},
                         {'configurable': {'deps': Deps(client=MockClient(), skills_loader=DomainLoader())}})
    assert result['reasoning_raw_response'] == COMPLETE
    assert len(result['reasoning_result']['proof_attempts']) == 2
    assert all(not item['mathematical_correctness_verified'] for item in result['reasoning_result']['proof_attempts'])


def test_proof_coordinator_gets_all_attempts_without_computation_template():
    parsed = _parse_reasoning_output(COMPLETE, 'proof')
    parsed['proof_attempts'] = [{'attempt': 1, 'visible_response': PARTIAL, 'finish_reason': 'length'},
                                {'attempt': 2, 'visible_response': COMPLETE, 'finish_reason': 'stop'}]
    client = MockClient(['证明有效。\n<!-- PROOF_REVIEW {"status":"none_reported","issues":[]} -->'])
    result = assemble_step({'problem': '证明映射是同胚。', 'category': '拓扑学',
                            'reasoning_result': parsed, 'reasoning_raw_response': COMPLETE,
                            'validation_details': {'problem_type': 'proof'}},
                           {'configurable': {'deps': Deps(client=client)}})
    assert len(client.calls) == 1
    prompt = client.calls[0]['messages'][0]['content']
    assert PARTIAL in prompt.replace('\\n', '\n')
    assert 'Python验证代码' not in prompt
    assert '数学一致' in prompt
    assert '量词顺序' in prompt and '开集原像' in prompt
    assert '交叉项' not in prompt and '非零虚部' not in prompt
    assert result['proof_review']['mathematical_correctness_verified'] is False


def test_draft_exhaustion_uses_existing_bounded_compressed_retry(monkeypatch):
    plain, compressed = [], []
    def first(*args, **kwargs):
        plain.append(kwargs)
        return ChatResult(DRAFT, 'length')
    def recover(*args, **kwargs):
        compressed.append(kwargs)
        return ChatResult(COMPLETE, 'stop')
    monkeypatch.setattr(reason_module, 'chat_with_retry', first)
    monkeypatch.setattr(reason_module, 'chat_prefilled', recover)
    monkeypatch.setattr(reason_module, 'run_with_timeout', lambda fn, seconds: fn())
    result = reason_step({'problem': '证明映射是同胚。', 'category': '拓扑学', 'question_mode': 'proof'},
                         {'configurable': {'deps': Deps(client=MockClient(), skills_loader=DomainLoader())}})
    assert len(plain) == len(compressed) == 1
    assert result['reasoning_attempts'] == 2
    assert result['reasoning_result']['proof_body']
    prompt = compressed[0]['messages'][0]['content']
    assert '最多 4 步' not in prompt and '每步 3 行' not in prompt
    assert '可信，直接复用' not in prompt and DRAFT not in prompt
    assert compressed[0]['max_tokens'] == CONFIG['max_tokens']['reasoning_compressed']
    assert compressed[0]['temperature'] == CONFIG['temperatures']['reasoning']
