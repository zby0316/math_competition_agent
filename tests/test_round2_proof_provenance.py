"""Real batch A-prime regressions: proof provenance and selected knowledge, offline."""
import pytest
from types import SimpleNamespace

from nodes.assemble_step import assemble_step
from utils.deps import Deps, MockClient
from utils.domain_excerpt import select_skill_excerpt
from utils.domain_loader import DomainLoader
from utils.llm_retry_guard import ChatResult
from utils.response_builder import post_process_final_response


@pytest.mark.parametrize('problem,modules', [
    ('在复 Hilbert 空间讨论弱收敛与范数收敛。', ('模块4：内积', '模块17：弱收敛')),
    ('Complex Hilbert space: weak and strong convergence.', ('模块4：内积', '模块17：弱收敛')),
    ('用 Baire 定理说明 Banach 空间中逐点有界与一致有界的关系。', ('模块16：一致有界',)),
])
def test_functional_handbook_loads_relevant_real_modules(problem, modules):
    doc = DomainLoader().get_skill_document('泛函分析')
    excerpt = select_skill_excerpt(doc, problem, 3000)
    assert len(excerpt) <= 3000
    assert all(module in excerpt for module in modules)
    assert '第一变量线性' in excerpt and r'\sum_k u_k\overline{v_k}' in excerpt
    assert '仅知' in excerpt and r'\le r' in excerpt


@pytest.mark.parametrize('heading', ['## 模块', '### 模块'])
def test_module_level_does_not_hide_matching_section(heading):
    doc = f'前言\n{heading}1：无关\n' + '不相关内容\n' * 400
    doc += f'{heading}2：inner product\n正确的复内积约定。\n'
    excerpt = select_skill_excerpt(doc, 'inner product', 160)
    assert '正确的复内积约定' in excerpt
    assert len(excerpt) <= 160


CORRECTED = ('## 结论速览\n给定的映射连续。\n## 详细解题步骤\n'
             '先在中心平移，再使用严格小于原半径的点，利用定义得到所需界。\n'
             '## 最终答案\n使用平移后的点，不宣称原集合对平移不变。\n'
             '## 关键验证点\n边界严格不等式已核对。')
STALE = '原集合在任意平移下不变，故映射连续且收敛、极限存在唯一。'


def test_coordinator_correction_survives_assembly_without_old_summary():
    client = MockClient([CORRECTED + '\n<!-- PROOF_REVIEW {"status":"issues_corrected","issues":["平移"]} -->'])
    result = assemble_step({'problem': '证明给定映射连续。', 'category': '拓扑学',
                            'reasoning_result': {'answer': STALE, 'proof_body': '未核查的旧证明'},
                            'validated_answer': STALE,
                            'validation_details': {'problem_type': 'proof'}},
                           {'configurable': {'deps': Deps(client=client)}})
    assert len(client.calls) == 1
    assert result['final_response'] == CORRECTED
    assert STALE not in result['final_response']
    assert result['proof_review']['mathematical_correctness_verified'] is False


def test_truncated_coordinator_does_not_mix_in_old_conclusion():
    partial = ChatResult('## 详细解题步骤\n先取闭集，由紧性可得', 'length')
    client = MockClient([partial])
    client._tls = SimpleNamespace(finish_reason='length')
    result = assemble_step({'problem': '证明逆映射连续。', 'category': '拓扑学',
                            'reasoning_result': {'answer': STALE},
                            'validation_details': {'problem_type': 'proof'}},
                           {'configurable': {'deps': Deps(client=client)}})
    assert result['final_response'] == str(partial)
    assert result['proof_review']['finish_reason'] == 'length'
    assert result['proof_review']['mathematical_correctness_verified'] is False


def test_locked_proof_candidate_still_bypasses_generation():
    client = MockClient([])
    result = assemble_step({'problem': '证明映射连续。', 'validated_answer': '该映射连续。',
                            'answer_locked': True, 'arbiter_decision': 'reasoning',
                            'reasoning_result': {'proof_body': '任取开集，连续映射的原像为开集。' * 8},
                            'validation_details': {'problem_type': 'proof'}},
                           {'configurable': {'deps': Deps(client=client)}})
    assert not client.calls
    assert result['final_response'].startswith('结论：该映射连续。')
    assert '证明过程：' in result['final_response']


def test_empty_proof_response_explicitly_remains_unproved():
    result = post_process_final_response('', '结论仍需证明。', 'proof')
    assert '未取得完整证明' in result
    assert '结论仍需证明' in result
