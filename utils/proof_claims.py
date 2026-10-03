"""Candidate locations and model-reported coverage, never mathematical validation."""
import hashlib
import json
import re

from utils.proof_review import proof_handoff

INDEX_VERSION = 1
MAX_SOURCES = 16
MAX_BLOCKS = 256
MAX_CLAIMS = 192
_HEADING = re.compile(r"(?m)^ {0,3}#{1,6}[ \t]+[^\n]+|^(?:结论速览|摘要|问题分析|详细解题步骤|正文|最终答案|关键验证点|逐问结论)[ \t]*$")
_DRAFT = re.compile(r"(?im)^[ \t]*(?:#{1,6}[ \t]+)?(?:Thinking Process|Thought Process|Let me think|思考过程|推理过程|分析过程)(?:\s*[:：]|\s*$)")
_FORMAL = re.compile(r"(?im)^ {0,3}(?:#{1,6}[ \t]+)?(?:结论速览|摘要|问题分析|详细解题步骤|正文|最终答案|关键验证点|逐问结论|summary|proof|conclusion|final answer)(?:[ \t]*$|[:：])")
_EXAMPLES = re.compile(r"(?ms)^ {0,3}```[^\n]*\n.*?(?:^ {0,3}```[^\n]*(?:\n|$)|\Z)|<!--.*?(?:-->|\Z)|^\s*>[^\n]*")
_MATH = re.compile(r"\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)|(?<!\$)\$(?!\$)[^$]*?\$(?!\$)|"
                   r"\\begin\{(align\*?|equation\*?|gather\*?)\}.*?\\end\{\1\}", re.S)
_CUES = {
    "relation": r"[=<>≤≥≠≈]|\\(?:leq?|geq?|ne[q]?|approx|subset(?:eq)?|supset(?:eq)?)\b",
    "condition": r"若|如果|否则|当.{0,60}时|(?i:\bif\b|\bwhen\b|\bunless\b)",
    "scope": r"任意|所有|每个|每一|充分大|对所有|参数|范围|\\forall\b|(?i:\bfor all\b|\bevery\b|\beventually\b)",
    "index": r"编号|下标|首项|第一项|最小正整数|(?i:\bindex\b|\bfirst term\b)",
    "validity": r"不再成立|不成立|成立|失效|失败|不满足|不保证|不能推出|(?i:\bfails?\b|\binvalid\b|\bdoes not hold\b)",
    "property": r"可测|紧致|连续|同胚|单射|满射|有界|完备|饱和|收敛|开集|闭集|"
                r"(?i:\bmeasurable\b|\bcompact\b|\bcontinuous\b|\bbounded\b|\bcomplete\b|\bsaturated\b)",
}
_TASK_CUES = {
    "inner_product": r"内积|Hilbert|inner.product|共轭",
    "measurability": r"可测|测度|积分|measur|Lebesgue",
    "tails_and_truncation": r"截断|尾部|一致可积|truncat|tail",
    "topology": r"开集|闭集|紧致|连续|拓扑|饱和|同胚|Hausdorff|quotient|topolog",
    "boundaries": r"端点|边界|开球|闭球|严格|boundary|endpoint",
    "counterexample": r"反例|构造|不成立|不再成立|失效|counterexample|construct|fail",
}


def _inside(position, spans):
    return any(start <= position < end for start, end in spans)


def _trim_span(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _blocks(text, formal_start, formal_end):
    """Blank lines/headings split prose, but not multi-line display mathematics."""
    excluded = [(m.start(), m.end()) for m in _EXAMPLES.finditer(text)]
    maths = [(m.start(), m.end()) for m in _MATH.finditer(text) if not _inside(m.start(), excluded)]
    headings = [m for m in _HEADING.finditer(text) if not _inside(m.start(), excluded + maths)]
    boundaries = {formal_start, formal_end}
    for heading in headings:
        if formal_start <= heading.start() < formal_end:
            boundaries.update((heading.start(), min(heading.end(), formal_end)))
    for blank in re.finditer(r"\n[ \t]*\n", text):
        if formal_start <= blank.start() < blank.end() <= formal_end and not _inside(blank.start(), maths + excluded):
            boundaries.update((blank.start(), blank.end()))
    ordered = sorted(boundaries)
    for begin, end in zip(ordered, ordered[1:]):
        begin, end = _trim_span(text, begin, end)
        if begin == end or any(h.start() <= begin and end <= h.end() for h in headings):
            continue
        section = next((h.group().strip() for h in reversed(headings) if h.start() <= begin), "(无标题)")
        yield begin, end, section, maths, excluded


def index_sources(sources, task_ids=()):
    """Sources contain text already in the handoff; serialized index never repeats it."""
    index = {"version": INDEX_VERSION, "sources": [], "blocks": [], "claims": [], "unknowns": [],
             "default_task_id": "premises_and_coverage",
             "limits": {"sources": MAX_SOURCES, "blocks": MAX_BLOCKS, "claims": MAX_CLAIMS},
             "mathematical_correctness_verified": False,
             "note": "只定位词法线索；未识别内容、候选缺失或截断均未知，记录完整也不认证数学正确。"}
    seen = {}
    sources_by_path = {s["path"]: s.get("text", "") for s in sources}
    for source in sources:
        text, path = source.get("text", ""), source["path"]
        if not isinstance(text, str) or not text.strip():
            continue
        if source.get("format_failure"):
            index["unknowns"].append({"path": path, "reason": "format_failure"})
            continue
        # Indexed boundaries are explicit formal headings or a parsed formal field.
        start, end = source.get("span", (0, len(text)))
        if not (type(start) is int and type(end) is int and 0 <= start < end <= len(text)):
            index["unknowns"].append({"path": path, "reason": "invalid_source_span"})
            continue
        if not source.get("parsed_formal"):
            excluded = [(m.start(), m.end()) for m in _EXAMPLES.finditer(text)]
            header = next((h for h in _FORMAL.finditer(text) if not _inside(h.start(), excluded)), None)
            if header is None:
                index["unknowns"].append({"path": path, "reason": "formal_boundary_unknown"})
                continue
            start = header.start()
            if text[:start].strip():
                index["unknowns"].append({"path": path, "start": 0, "end": start, "reason": "unclassified_prefix"})
        if _DRAFT.search(text[start:end]):
            index["unknowns"].append({"path": path, "reason": "draft_marker"})
            continue
        formal = text[start:end]
        if source.get("finish_reason") == "length" or source.get("missing_sections"):
            index["unknowns"].append({"path": path, "reason": "candidate_incomplete_or_truncated"})
        masked = _EXAMPLES.sub("", formal)
        for math in reversed(list(_MATH.finditer(masked))):
            masked = masked[:math.start()] + masked[math.end():]
        if re.search(r"(?<!\\)\$|\\\[|\\\]|\\begin\{(?:align|equation|gather)\*?\}", masked):
            index["unknowns"].append({"path": path, "reason": "unclosed_or_unparsed_math"})
        if formal in seen:
            index["sources"][seen[formal]]["aliases"].append({"path": path, "start": start})
            continue
        # Parsed body/sections duplicated inside a full formal response need no second index.
        duplicate = next((s for s in index["sources"] if source.get("parsed_formal")
                          and formal in sources_by_path[s["path"]][s["formal_start"]:s["formal_end"]]), None) if index["sources"] else None
        if duplicate:
            original = sources_by_path[duplicate["path"]]
            duplicate["aliases"].append({"path": path, "start": start,
                                         "target_start": original.find(formal, duplicate["formal_start"])})
            continue
        if len(index["sources"]) >= MAX_SOURCES:
            index["unknowns"].append({"path": path, "reason": "source_index_capacity"})
            continue
        sid = f"S{len(index['sources']) + 1:03d}"
        seen[formal] = len(index["sources"])
        index["sources"].append({"id": sid, "path": path, "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "characters": len(text), "formal_start": start, "formal_end": end, "aliases": [],
            "finish_reason": source.get("finish_reason"), "missing_sections": source.get("missing_sections", [])})
        for begin, block_end, section, maths, excluded in _blocks(text, start, end):
            if len(index["blocks"]) >= MAX_BLOCKS:
                index["unknowns"].append({"source_id": sid, "start": begin, "end": end, "reason": "block_index_capacity"})
                break
            bid = f"B{len(index['blocks']) + 1:03d}"
            block = {"id": bid, "source_id": sid, "start": begin, "end": block_end,
                     "section": section, "line": text.count("\n", 0, begin) + 1, "claim_ids": []}
            index["blocks"].append(block)
            if any(a < block_end and b > begin for a, b in excluded):
                block["recognition"] = "unknown"
                index["unknowns"].append({"block_id": bid, "reason": "quoted_or_unparsed_content"})
                continue
            boundaries = [begin]
            for match in re.finditer(r"[。；;]|(?<!\d)\.(?=\s|$)|\n", text[begin:block_end]):
                position = begin + match.start()
                if not _inside(position, maths):
                    boundaries.append(begin + match.end())
            boundaries.append(block_end)
            unrecognized = 0
            for a, b in zip(boundaries, boundaries[1:]):
                a, b = _trim_span(text, a, b)
                if a == b:
                    continue
                fragment = text[a:b]
                kinds = [kind for kind, pattern in _CUES.items() if re.search(pattern, fragment)]
                if not kinds:
                    unrecognized += 1
                    continue
                if len(index["claims"]) >= MAX_CLAIMS:
                    index["unknowns"].append({"block_id": bid, "start": a, "end": block_end, "reason": "claim_index_capacity"})
                    break
                cid = f"C{len(index['claims']) + 1:03d}"
                tasks = [tid for tid, pattern in _TASK_CUES.items()
                    if tid in task_ids and re.search(pattern, fragment, re.I)]
                claim = {"id": cid, "block_id": bid, "start": a, "end": b, "kinds": kinds}
                if tasks:
                    claim["task_ids"] = tasks
                index["claims"].append(claim)
                block["claim_ids"].append(cid)
            if unrecognized:
                index["unknowns"].append({"block_id": bid, "reason": "no_reliable_cue", "segments": unrecognized})
            block["recognition"] = "cues_only" if block["claim_ids"] else "unknown"
    if not index["sources"]:
        index["unknowns"].append({"reason": "no_formal_candidate"})
    return index


def indexed_handoff(state, task_ids=()):
    payload = json.loads(proof_handoff(state))
    result = state.get("reasoning_result") or {}
    sources = []
    for i, attempt in enumerate(payload["attempts"]):
        if isinstance(attempt, dict):
            sources.append({"path": f"attempts/{i}/visible_response", "text": attempt.get("visible_response", ""),
                "finish_reason": attempt.get("finish_reason"), "missing_sections": attempt.get("missing_sections", []),
                "format_failure": attempt.get("format_failure")})
    if payload["visible_response"] != "见 attempts 中的完整响应" or not sources:
        sources.append({"path": "visible_response", "text": payload["visible_response"],
                        "finish_reason": payload["finish_reason"], "format_failure": result.get("format_failure")})
    sources.append({"path": "proof_body", "text": payload["proof_body"], "parsed_formal": True,
                    "finish_reason": payload["finish_reason"], "missing_sections": payload["missing_sections"],
                    "format_failure": result.get("format_failure")})
    # Complete visible responses get the bounded index before historical truncations.
    def priority(source):
        return (0 if source.get('finish_reason') == 'stop' and not source.get('missing_sections')
                and not source.get('parsed_formal') else 1 if source.get('parsed_formal') else 2)
    sources.sort(key=priority)
    preliminary = index_sources(sources, task_ids)
    texts = {s["path"]: s.get("text", "") for s in sources}
    existing = [texts[s["path"]][s["formal_start"]:s["formal_end"]] for s in preliminary["sources"]]
    for key in ("summary", "analysis", "answer", "validation_points"):
        values = result.get(key, []) if key == "validation_points" else [result.get(key, "")]
        if not isinstance(values, list):
            continue
        for i, text in enumerate(values):
            if not isinstance(text, str) or not text.strip() or any(text in old for old in existing):
                continue
            target = f"{key}_{i}" if key == "validation_points" else key
            original = next((s for s in sources if isinstance(s.get("text"), str) and text in s["text"]), None)
            if original:
                offset = original["text"].find(text)
                sources.append({"path": original["path"], "text": original["text"], "parsed_formal": True,
                    "span": [offset, offset + len(text)], "finish_reason": payload["finish_reason"],
                    "format_failure": result.get("format_failure")})
                existing.append(text)
                continue
            payload.setdefault("parsed_sections", {})[target] = text
            existing.append(text)
            sources.append({"path": f"parsed_sections/{target}", "text": text, "parsed_formal": True,
                "finish_reason": payload["finish_reason"], "format_failure": result.get("format_failure")})
    sources.sort(key=priority)
    payload["candidate_index"] = index_sources(sources, task_ids)
    return payload


def compact_index(index):
    """Lossless location tables for the prompt; rich diagnostic metadata stays internal."""
    sections = list(dict.fromkeys(b['section'] for b in index['blocks']))
    kinds = list(_CUES)
    tasks = list(dict.fromkeys(t for c in index['claims'] for t in c.get('task_ids', [])))
    reasons = list(dict.fromkeys(u['reason'] for u in index['unknowns']))
    return {'version': index['version'], 'columns': {
        'sources': ['id', 'path', 'formal_start', 'formal_end'],
        'blocks': ['id', 'source_id', 'section_number', 'start', 'end'],
        'claims': ['id', 'block_id', 'start', 'end', 'kind_numbers', 'task_numbers'],
        'unknowns': ['reason_number', 'path_or_block_or_source', 'start', 'end', 'segments']},
        'sections': sections, 'kinds': kinds, 'tasks': tasks, 'reasons': reasons,
        'sources': [[s['id'], s['path'], s['formal_start'], s['formal_end']] for s in index['sources']],
        'blocks': [[b['id'], b['source_id'], sections.index(b['section']), b['start'], b['end']]
                   for b in index['blocks']],
        'claims': [[c['id'], c['block_id'], c['start'], c['end'], [kinds.index(k) for k in c['kinds']],
                    [tasks.index(t) for t in c.get('task_ids', [])]] for c in index['claims']],
        'unknowns': [[reasons.index(u['reason']), u.get('path', u.get('block_id', u.get('source_id'))),
                      u.get('start'), u.get('end'), u.get('segments')] for u in index['unknowns']],
        'default_task_id': index['default_task_id'], 'limits': index['limits'],
        'mathematical_correctness_verified': False, 'note': index['note']}


def claim_coverage(review, index, finish_reason=None):
    """Validate references and evidence fields only, never their mathematical truth."""
    claims = {c["id"]: c for c in index["claims"]}
    covered, uncertain, records = set(), set(), []
    checks = review.get("checks", [])
    if not isinstance(checks, list):
        checks = []
    for check in checks:
        flags = []
        cid = check.get("claim_id") if isinstance(check, dict) else None
        if not isinstance(cid, str) or cid not in claims:
            flags.append("unknown_or_missing_claim_id")
        else:
            if "span" in check:
                span = check["span"]
                if (not isinstance(span, list) or len(span) != 2 or any(type(p) is not int for p in span)
                        or span != [claims[cid]["start"], claims[cid]["end"]]):
                    flags.append("invalid_span")
        if not isinstance(check, dict):
            flags.append("record_object_required")
        else:
            for field in ("premises", "basis"):
                value = check.get(field)
                if not isinstance(value, str) or not value.strip() or value.strip() in {"同上", "已核查", "略", "checked"}:
                    flags.append(f"missing_concrete_{field}")
            if check.get("status") not in ("checked", "corrected", "unresolved"):
                flags.append("invalid_status")
            elif check["status"] == "unresolved":
                flags.append("model_unresolved")
        records.append({"original": check, "flags": flags, "mathematical_correctness_verified": False})
        if isinstance(cid, str) and cid in claims:
            (uncertain if flags else covered).add(cid)
    covered -= uncertain
    missing = set(claims) - covered
    flags = []
    if finish_reason != "stop":
        flags.append("truncated" if finish_reason == "length" else "completion_unknown")
    if review.get("record_parse_error"):
        flags.append("record_parse_failure")
    if review.get("status") not in ("none_reported", "issues_corrected"):
        flags.append("model_review_incomplete")
    if not claims:
        flags.append("no_reliable_claims")
    if index["unknowns"]:
        flags.append("candidate_has_unclassified_or_incomplete_content")
    if any(record["flags"] for record in records):
        flags.append("invalid_or_unresolved_records")
    status = "records_present_unverified" if not missing and not flags else "uncertain"
    return {"status": status, "recognized_claim_count": len(claims), "referenced_with_fields_count": len(covered),
        "missing_or_uncertain_claim_ids": sorted(missing), "unclassified_content": index["unknowns"],
        "flags": flags, "records": records, "mathematical_correctness_verified": False,
        "note": "分母仅为词法识别线索；字段完整或定位成功不证明依据正确，不参与候选正确性排序。"}
