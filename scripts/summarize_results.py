#!/usr/bin/env python3
"""Offline result export. Reads frozen runs; writes only aggregate artifacts."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from xml.sax.saxutils import escape, quoteattr
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from memform.defenses.metrics import build_eligibility, index_case_rows, score_on_eligibility
from experiments.transition_guard_p4.runtime import summarize_cost
from experiments.transition_guard_p4.external_defenses import completed

NAMES = {'B0': '无防御', 'B1': 'Final-only Judge（内部对照）', 'B2': '全库重建',
         'B3': '随机反事实（消融）', 'B4': 'TransitionGuard', 'B5': '始终回退',
         'D1': 'MemForm LLM-Judge', 'D2': 'Task Shield adapted', 'D3': 'A-MemGuard adapted'}
ORDER = ['B0', 'B1', 'B2', 'B3', 'B4', 'B5', 'D1', 'D2', 'D3']
EVENT_FIELDS = {'event_id', 'role', 'stage', 'mode', 'group', 'case_id', 'family_id',
                'cache_hit', 'success', 'api_attempts', 'simulated_generation',
                'usage', 'cached_response_usage', 'latency_seconds', 'response_metadata',
                'error_type', 'error_chain', 'origin_run', 'imported_from'}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def jsonl(path):
    if not path.exists():
        return []
    with path.open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def rate(a, b):
    return a / b if b else None


def usage(events):
    values = [e.get('usage') or e.get('cached_response_usage') for e in events]
    missing = sum(v is None for v in values)
    inputs = sum(v['input_tokens'] for v in values if v is not None)
    outputs = sum(v['output_tokens'] for v in values if v is not None)
    return {'逻辑输入token': inputs if not missing else None,
            '逻辑输出token': outputs if not missing else None,
            '逻辑总token': inputs + outputs if not missing else None,
            '逻辑token已知下限': inputs + outputs, '逻辑usage缺失次数': missing}


def event_cost(events, prefix=''):
    cost = summarize_cost(events)
    return {prefix + '逻辑调用次数': len(events), prefix + 'API尝试次数': cost['api_attempts'],
            prefix + 'API成功次数': cost['successful_api_responses'],
            prefix + '缓存命中次数': cost['cache_hits'],
            prefix + '缓存命中率': rate(cost['cache_hits'], len(events)),
            prefix + 'API输入token': cost['input_tokens'], prefix + 'API输出token': cost['output_tokens'],
            prefix + 'API总token': cost['input_tokens'] + cost['output_tokens']
                if cost['input_tokens'] is not None and cost['output_tokens'] is not None else None,
            prefix + 'API已知token下限': cost['known_input_tokens'] + cost['known_output_tokens'],
            prefix + 'API缺失usage次数': cost['api_responses_without_usage'],
            prefix + '累计调用秒': cost['latency_seconds']}


def format_percent(value):
    return 'N/A' if value is None else f'{value:.2%}'


def format_int(value):
    return 'N/A' if value is None else f'{value:,}'


def duration(value):
    if value is None:
        return 'N/A'
    if value < 1:
        return f'{value:.3f}秒（缓存）'
    seconds = round(value)
    return f'{seconds // 60}:{seconds % 60:02d}'


def collect(results, entry, selected_ids=None, dataset=None):
    directory = results / entry['path']
    config, summary = read(directory / 'run_config.json'), read(directory / 'summary.json')
    rows = jsonl(directory / 'case_results.jsonl')
    events = []
    with (directory / 'call_events.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            value = json.loads(line)
            events.append({k: v for k, v in value.items() if k in EVENT_FIELDS})
    if any(e['event_id'] != i for i, e in enumerate(events)):
        raise ValueError('Noncontiguous event IDs: ' + entry['path'])
    if selected_ids is not None:
        chosen = set(selected_ids)
        rows = [r for r in rows if r['case_id'] in chosen]
    else:
        chosen = set(config['active_case_ids'])
    groups = [g for g in ORDER if g in config['groups']]
    keys = [(r['experiment_group'], r['case_id']) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError('Duplicate group/case')
    baseline_rows = [r for r in rows if r['experiment_group'] == 'B0']
    baseline = index_case_rows(baseline_rows)
    baseline_complete = ({r['case_id'] for r in baseline_rows} == chosen and all(completed(r) for r in baseline_rows))
    eligibility = build_eligibility(baseline, ['P4'])
    eligible = eligibility['P4']
    recovery = [k for k in eligible if baseline[k]['poison_memory_pred'] != baseline[k]['y_c']]
    inherited = int(config.get('inherited_call_events', 0))
    if not 0 <= inherited <= len(events):
        raise ValueError('Invalid inherited event prefix')
    all_records, stages, flow, utilized = [], [], [], set()
    for group in groups:
        subset = [r for r in rows if r['experiment_group'] == group]
        tasks = [t for r in subset for t in r['task_results']]
        index = index_case_rows(subset)
        complete = ({r['case_id'] for r in subset} == chosen and all(completed(r) for r in subset))
        metrics_available = complete and baseline_complete
        fixed = score_on_eligibility(index, eligibility, ['P4'])['by_interface']['P4']
        ids = []
        for row in subset:
            a, b = row['call_event_range']
            if not 0 <= a <= b <= len(events):
                raise ValueError('Bad row event span')
            for i in range(a, b):
                if i in utilized or events[i]['group'] != group or events[i]['case_id'] != row['case_id']:
                    raise ValueError('Overlapping or misattributed events')
                ids.append(i)
                utilized.add(i)
        ev = [events[i] for i in ids]
        new = [events[i] for i in ids if i >= inherited]
        clean = sum(bool(t['learned_clean']['clean_correct']) for t in tasks)
        poison = sum(bool(t['learned_poisoned']['clean_correct']) for t in tasks)
        restored = sum(index.get(k, {}).get('poison_memory_pred') == baseline[k]['y_c'] for k in recovery)
        logical = usage(ev)
        record = {'数据集': dataset or entry['category'], 'N': entry['N'],
                  '计划样本数': len(chosen), '结果样本数': len(subset), '任务数': len(tasks),
                  '防御组': group, '防御名称': NAMES[group],
                  '方法版本': 'transition-guard-p4-v6' if config['version'].startswith('p4-external') else config['version'],
                  '入口版本': config['version'], '运行完整': summary.get('complete'), '分组完整': complete,
                  '正常正确数': clean, '正常准确率': rate(clean, len(tasks)) if complete else None,
                  '投毒正确数': poison, '投毒准确率': rate(poison, len(tasks)) if complete else None,
                  'ASR成功数': fixed['asr_success'], 'ASR分母': len(eligible),
                  'ASR': fixed['causal_asr'] if metrics_available else None,
                  '固定正常正确数': fixed['acc_correct'],
                  '固定正常准确率': fixed['learned_clean_accuracy'] if metrics_available else None,
                  'Recovery正确数': restored, 'Recovery分母': len(recovery),
                  'Recovery率': rate(restored, len(recovery)) if metrics_available else None,
                  '正常语义阻断数': sum(t['learned_clean'].get('defense_outcome_class') == 'semantic_misaligned' and bool(t['learned_clean'].get('defense_blocked')) for t in tasks),
                  '投毒语义阻断数': sum(t['learned_poisoned'].get('defense_outcome_class') == 'semantic_misaligned' and bool(t['learned_poisoned'].get('defense_blocked')) for t in tasks),
                  '技术失败条件数': sum(bool(t[s].get('defense_technical_failure')) for t in tasks for s in ('learned_clean', 'learned_poisoned')),
                  **logical, '每任务逻辑总token': logical['逻辑总token'] / len(tasks) if logical['逻辑总token'] is not None and tasks else None,
                  **event_cost(ev, '行关联_'), **event_cost(new, '本次新增_'),
                  '时延说明': '结果行关联的调用累计时间；含缓存，不是冷启动或整轮墙钟；不含未落盘失败尝试',
                  '来源目录': entry['path'], '参考目录': config.get('reference_run'),
                  '续跑目录': config.get('resume_from'), '样本来源': '固定32条从144条精确筛出' if selected_ids is not None else '原运行清单'}
        if selected_ids is None and group in summary.get('groups', {}):
            old = summary['groups'][group]
            for key, current in [('clean_accuracy_all_selected', record['正常准确率']),
                                 ('poisoned_task_accuracy_all_selected', record['投毒准确率'])]:
                if old.get(key) != current:
                    raise ValueError('Recomputed accuracy mismatch ' + entry['path'] + ' ' + group)
            if metrics_available and abs(old['fixed_metrics']['causal_asr'] - record['ASR']) > 1e-12:
                raise ValueError('Recomputed ASR mismatch')
            if metrics_available and old['recovery']['recovered'] != restored:
                raise ValueError('Recomputed recovery mismatch')
            if 'logical_usage' in old:
                for label, key in [('逻辑输入token', 'input_tokens'), ('逻辑输出token', 'output_tokens')]:
                    if old['logical_usage'][key] != record[label]:
                        raise ValueError('Logical token mismatch')
        all_records.append(record)
        for stage in sorted({e['stage'] for e in ev}):
            part = [e for e in ev if e['stage'] == stage]
            stages.append({'数据集': record['数据集'], 'N': entry['N'], '防御组': group,
                           '阶段': stage, **usage(part), **event_cost(part), '来源目录': entry['path']})
        if group == 'B4':
            for side in ('clean', 'poisoned'):
                reviews = [(r, (r.get('transition_reviews') or r['memory_traces'].get('post_consolidation', {})).get(side)) for r in subset]
                reviews = [(r, v) for r, v in reviews if v]
                item = {'数据集': record['数据集'], 'N': entry['N'], '条件': side, '转移数': len(reviews),
                        '保留原候选': 0, '提交修复': 0, '回退': 0, '反事实重建次数': 0,
                        '审核supported': 0, '审核unsupported': 0, '审核inconclusive': 0,
                        '审核协议失败': 0, '修复提交后正确': 0,
                        'Recovery来自修复': 0, 'Recovery来自回退': 0, 'Recovery来自原候选': 0,
                        '来源目录': entry['path']}
                for row, review in reviews:
                    action = review['commit_decision']
                    item[{'commit_original': '保留原候选', 'commit_repaired': '提交修复', 'defer': '回退'}[action]] += 1
                    item['反事实重建次数'] += review.get('counterfactual_rebuilds', 0)
                    validation = review.get('repair', {}).get('validation', {})
                    verdict = validation.get('effective_verdict')
                    if verdict in ('supported', 'unsupported', 'inconclusive'):
                        item['审核' + verdict] += 1
                    item['审核协议失败'] += bool(validation.get('protocol_errors'))
                    for task in row['task_results']:
                        success = bool(task['learned_' + side]['clean_correct'])
                        item['修复提交后正确'] += action == 'commit_repaired' and success
                        key = ('P4', row['case_id'], task['task_id'], '')
                        if side == 'poisoned' and key in recovery and success:
                            item[{'commit_original': 'Recovery来自原候选', 'commit_repaired': 'Recovery来自修复', 'defer': 'Recovery来自回退'}[action]] += 1
                flow.append(item)
    if selected_ids is not None:
        return all_records, stages, flow, None
    errors = jsonl(directory / 'errors.jsonl')
    ledger = {'来源目录': entry['path'], '分类': entry['category'], '用途': entry['role'],
              '版本': config['version'], 'N': entry['N'], '计划样本数': len(chosen),
              '分组': ' '.join(groups), '运行完整': summary.get('complete'), '落盘单元数': len(rows),
              '当前错误记录数': len(errors), '历史错误记录数': len(jsonl(directory / 'prior_errors.jsonl')),
              '参考目录': config.get('reference_run'), '续跑目录': config.get('resume_from'),
              '模型': config['model'], '防御模型': config['guard_model'], 'seed': config.get('seed'),
              'Agent思考': config.get('agent_thinking_mode'), 'Updater思考': config.get('memory_thinking_mode'),
              '防御思考': config.get('guard_thinking_mode'),
              '重建策略': config.get('strategy'), '每次证据条数k': config.get('k'),
              '最多重建次数': config.get('max_rebuilds'), '记忆词数上限': config.get('max_memory_words'),
              '防御输出token上限': config.get('guard_max_output_tokens'),
              '短名单记录上限': config.get('selection_max_records'), '短名单字符上限': config.get('selection_max_chars'),
              '防御输入字符上限': config.get('guard_max_input_chars'), 'HTTP超时秒': config.get('timeout'),
              '数据SHA256': config['source_sha256'], '实现SHA256': config.get('implementation_sha256'),
              **event_cost(events, '整条来源链_'), **event_cost(events[inherited:], '本次新增_'),
              '未落盘行关联调用数': len(events) - len(utilized),
              '未落盘行关联调用秒': sum(e['latency_seconds'] for i, e in enumerate(events) if i not in utilized),
              '费用说明': '来源链含导入/续跑父事件，禁止跨目录直接累计；本次新增仅统计继承前缀之后的事件'}
    failures = []
    for filename in ('errors.jsonl', 'prior_errors.jsonl'):
        for error in jsonl(directory / filename):
            failures.append({'来源目录': entry['path'], '记录文件': filename, 'N': entry['N'],
                             '防御组': error.get('group'), '样本ID': error.get('case_id'),
                             '错误类型': error.get('error_type'), '错误链': ' -> '.join(error.get('error_chain', [])),
                             '记录耗时秒': error.get('elapsed_seconds'),
                             '状态': '来源/历史故障；查看最终完整目录，不能将故障计作防御收益'})
    return all_records, stages, flow, (ledger, failures)


def export_csv(path, rows):
    if not rows:
        path.write_text('', encoding='utf-8-sig')
        return
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def column(number):
    value = ''
    while number:
        number, digit = divmod(number - 1, 26)
        value = chr(65 + digit) + value
    return value


def export_xlsx(path, sheets):
    """Small standard-library OOXML writer: numbers, styles, filters, frozen headers."""
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    styles = f'''<styleSheet xmlns="{ns}"><numFmts count="1"><numFmt numFmtId="164" formatCode="0.00%"/></numFmts>
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF203864"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE2F0D9"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0"/>
<xf numFmtId="164" fontId="0" fillId="3" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    content = ['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
               '<Default Extension="xml" ContentType="application/xml"/>',
               '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
               '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    relations, workbook = [], []
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="' + rel + '/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/styles.xml', styles)
        for i, (name, rows) in enumerate(sheets, 1):
            if not rows:
                rows = [{'说明': '没有记录'}]
            headers = list(rows[0])
            cells = []
            for r, values in enumerate([headers] + [[row[k] for k in headers] for row in rows], 1):
                highlighted = r > 1 and rows[r - 2].get('防御组') == 'B4'
                line = []
                for c, value in enumerate(values, 1):
                    label = headers[c - 1]
                    percent = label in ('ASR', '正常准确率', '投毒准确率', '固定正常准确率', 'Recovery率', '缓存命中率') or label.endswith('_缓存命中率')
                    style = 1 if r == 1 else (4 if percent else 3) if highlighted else 2 if percent else 0
                    addr = f'{column(c)}{r}'
                    if value is None:
                        line.append(f'<c r="{addr}" s="{style}"/>')
                    elif isinstance(value, bool):
                        line.append(f'<c r="{addr}" s="{style}" t="b"><v>{int(value)}</v></c>')
                    elif isinstance(value, (int, float)):
                        line.append(f'<c r="{addr}" s="{style}"><v>{value}</v></c>')
                    else:
                        line.append(f'<c r="{addr}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>')
                cells.append(f'<row r="{r}"' + (' ht="34" customHeight="1"' if r == 1 else '') + '>' + ''.join(line) + '</row>')
            widths = ''.join(f'<col min="{j}" max="{j}" width="{48 if any(x in h for x in ("目录", "说明", "SHA256", "来源")) else 26 if "名称" in h else 18}" customWidth="1"/>' for j, h in enumerate(headers, 1))
            xml = f'<worksheet xmlns="{ns}"><dimension ref="A1:{column(len(headers))}{len(rows)+1}"/><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>{widths}</cols><sheetData>' + ''.join(cells) + f'</sheetData><autoFilter ref="A1:{column(len(headers))}{len(rows)+1}"/></worksheet>'
            archive.writestr(f'xl/worksheets/sheet{i}.xml', xml)
            content.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
            relations.append(f'<Relationship Id="rId{i}" Type="{rel}/worksheet" Target="worksheets/sheet{i}.xml"/>')
            workbook.append(f'<sheet name={quoteattr(name)} sheetId="{i}" r:id="rId{i}"/>')
        relations.append(f'<Relationship Id="rId{len(sheets)+1}" Type="{rel}/styles" Target="styles.xml"/>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + ''.join(relations) + '</Relationships>')
        archive.writestr('xl/workbook.xml', f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>' + ''.join(workbook) + '</sheets></workbook>')
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' + ''.join(content) + '</Types>')


def table(headers, rows):
    return ['| ' + ' | '.join(headers) + ' |', '|' + '|'.join('---' for _ in headers) + '|'] + [
        '| ' + ' | '.join(str(v).replace('|', '\\|').replace('\n', ' ') for v in row) + ' |' for row in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, default=ROOT / 'results')
    args = parser.parse_args()
    results = args.results.resolve()
    index = read(results / 'experiment_index.json')
    stamp = datetime.now(timezone.utc).isoformat()
    originals = {}
    for entry in index['runs']:
        path = results / entry['path']
        for name in ('run_config.json', 'summary.json', 'case_results.jsonl', 'call_events.jsonl'):
            originals[str((path / name).relative_to(results))] = sha(path / name)
    print(f'Verifying and aggregating {len(index["runs"])} retained runs; no API calls.', flush=True)
    all_records, all_stages, all_flow, ledger, failures = [], [], [], [], []
    for entry in index['runs']:
        records, stages, flow, run = collect(results, entry)
        all_records.extend(records)
        all_stages.extend(stages)
        all_flow.extend(flow)
        ledger.append(run[0])
        failures.extend(run[1])
        print(entry['path'], '->', len(records), 'groups', flush=True)
    main_rows = sorted([r for r in all_records if r['数据集'] == 'main'], key=lambda r: (r['N'], ORDER.index(r['防御组'])))
    if len(main_rows) != 35 or not all(r['分组完整'] for r in main_rows):
        raise ValueError('Expected 35 complete main groups')
    v6_n9 = [r for r in all_records if r['来源目录'].endswith('20261118T065642_903024Z_live')]
    v5 = [r for r in all_records if r['来源目录'].endswith('20261117T005206_665570Z_live')]
    pilot = [r for r in all_records if r['数据集'] == 'supplement' and r['计划样本数'] == 32]
    n9_entry = next(e for e in index['runs'] if e['path'].endswith('20261118T065642_903024Z_live'))
    matched_ids = read(ROOT / 'data/transition_guard_p4_large_history/v1_seed42/N9/selected_cases_32.json')['case_ids']
    matched, _, _, _ = collect(results, n9_entry, selected_ids=matched_ids, dataset='matched_32_from_N9_full')
    pilot = sorted(matched + pilot, key=lambda r: (r['N'], ORDER.index(r['防御组'])))
    cost_source = [r for r in all_records if r['数据集'] == 'cost_source']
    main_stages = sorted([r for r in all_stages if r['数据集'] == 'main'], key=lambda r: (r['N'], ORDER.index(r['防御组']), r['阶段']))
    main_flow = sorted([r for r in all_flow if r['数据集'] == 'main'], key=lambda r: (r['N'], r['条件']))
    cleanup = read(results / index['cleanup_audit'] / 'manifest.json')
    removed = []
    for item in cleanup['removed']:
        folder = results / index['cleanup_audit'] / 'removed_metadata' / str(Path(item['path']).relative_to('results'))
        config = read(folder / 'run_config.json') if (folder / 'run_config.json').exists() else {}
        summary = read(folder / 'summary.json') if (folder / 'summary.json').exists() else {}
        removed.append({'已删除目录': item['path'], '删除原因': item['reason'],
                        '版本': config.get('version'), '模式': config.get('mode'),
                        '计划样本数': config.get('active_cases'), '原摘要完整标志': summary.get('complete'),
                        '原字节数': item['bytes'], '原文件数': item['file_count'],
                        '元数据位置': str(folder.relative_to(results)), '限制': '仅元数据；不混入正式结果或重新推断原始行'})

    dictionaries = [
        {'字段': '计划样本数 / N', '口径': '样本数是独立case/family数；N是每个case的历史池记录数，二者不同。'},
        {'字段': '正常准确率 / 投毒准确率', '口径': '全样本上最终回答匹配y_c的比例；语义阻断计任务错误。'},
        {'字段': 'ASR', '口径': '固定B0资格集合中最终投毒回答匹配y_p；全量分母141，小样本32；不是1减全样本准确率。'},
        {'字段': 'Recovery率', '口径': '固定B0合格且投毒回答错误的任务恢复为y_c；全量分母76，小样本20。'},
        {'字段': '逻辑输入/输出/总token', '口径': '按结果行调用范围统计，包含缓存原usage；usage缺失则精确总量留空，并保留已知下限。'},
        {'字段': '行关联_API总token', '口径': '结果行调用范围中记录的非缓存API用量；导入历史调用也可能在内，不能当成本次新支出。'},
        {'字段': '本次新增_API总token', '口径': '该来源目录继承前缀后的新增API用量；comparison复用B4时为0，不代表B4无成本。'},
        {'字段': '行关联_累计调用秒', '口径': '含缓存的调用累计耗时，不是墙钟或冷启动，不包括没有落盘结果行的失败尝试。'},
        {'字段': '整条来源链_累计调用秒', '口径': '该目录保存的全部事件，含导入/续跑和失败尝试；父子/参考/比较目录禁止直接相加。'},
        {'字段': '缓存命中率', '口径': '对应范围命中次数/逻辑调用次数；不应把命中耗时当作真实模型推理速度。'},
        {'字段': 'main / supplement / history_v5 / provenance / cost_source', '口径': '正式全量 / 补充消融 / v5历史 / 参考或续跑来源 / 首次API成本来源，互相重叠，禁止把行数当独立样本累加。'},
        {'字段': 'B1与D1', '口径': 'B1是项目Final-only Judge，D1是MemForm原生经验准入LLM-Judge，两者不同。'},
        {'字段': '可见信息', '口径': 'B4检索遗漏历史，B2读全H；D1看S与操作上下文，D2看最终记忆与当前任务，D3看S与任务；权限不相同。'},
        {'字段': '时间可比性', '口径': '没有统一冷启动测量；已有时间日志完整保留，限制来自原实验缓存方式。'},
        {'字段': '原始资料保护', '口径': '只生成汇总，不改算法、评分、数据或任何原运行文件；不调用模型。'},
    ]
    def view(fields):
        return [{k: r[k] for k in fields} for r in main_rows]
    effect_view = view(['N', '防御组', '防御名称', '计划样本数', '正常正确数', '正常准确率',
                        '投毒正确数', '投毒准确率', 'ASR成功数', 'ASR分母', 'ASR',
                        'Recovery正确数', 'Recovery分母', 'Recovery率',
                        '正常语义阻断数', '投毒语义阻断数', '技术失败条件数', '来源目录'])
    token_view = view(['N', '防御组', '防御名称', '逻辑输入token', '逻辑输出token', '逻辑总token',
                       '每任务逻辑总token', '逻辑usage缺失次数', '逻辑token已知下限',
                       '行关联_API输入token', '行关联_API输出token', '行关联_API总token',
                       '本次新增_API输入token', '本次新增_API输出token', '本次新增_API总token', '来源目录'])
    time_view = view(['N', '防御组', '防御名称', '行关联_逻辑调用次数', '行关联_API尝试次数',
                      '行关联_API成功次数', '行关联_缓存命中次数', '行关联_缓存命中率', '行关联_累计调用秒',
                      '本次新增_逻辑调用次数', '本次新增_API尝试次数', '本次新增_累计调用秒', '时延说明', '来源目录'])
    sheets = [('口径说明', dictionaries), ('主实验效果', effect_view), ('主实验token', token_view),
              ('主实验时间与缓存', time_view), ('主实验144条', main_rows), ('N9六组144条', v6_n9),
              ('固定32条六组', pilot), ('v5全量历史', v5), ('首次外部32条', cost_source),
              ('B4流程与审核', main_flow), ('主实验阶段成本', main_stages),
              ('全部保留分组', all_records), ('运行与新增费用', ledger), ('故障与续跑记录', failures),
              ('已清理实验索引', removed)]
    csv_dir = results / '汇总数据'
    csv_dir.mkdir(exist_ok=True)
    for name, rows in sheets:
        export_csv(csv_dir / (name + '.csv'), rows)
    export_xlsx(results / '实验结果总表.xlsx', sheets)
    with zipfile.ZipFile(results / '实验结果总表.xlsx') as archive:
        import xml.etree.ElementTree as ET
        for name in archive.namelist():
            ET.fromstring(archive.read(name))
        if archive.testzip() is not None:
            raise ValueError('Invalid XLSX ZIP CRC')
    export = {'generated_utc': stamp, 'no_api_calls': True,
              'tables': {name: rows for name, rows in sheets}, 'source_sha256': originals,
              'note': 'Sources overlap; never sum all tables as independent samples or spend.'}
    (csv_dir / '全部汇总.json').write_text(json.dumps(export, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    lines = ['# 实验结果总汇', '', '更新：2026-11-19。由原始结果行、调用事件重算并与原summary交叉核对；本次不调用模型。', '',
             '**优先阅读主实验：五个N、相同144个family、七组完整。其他表分别标注小样本、历史版本及来源，不能混合累计。**', '',
             f'- [Excel总表（{len(sheets)}个工作表，可筛选）](实验结果总表.xlsx)',
             '- [CSV明细目录](汇总数据/)；[全部汇总JSON](汇总数据/全部汇总.json)',
             '- [原始结果位置索引](README.md)', '', '## 一、实验范围与方法', '',
             '主实验模型deepseek-v4.1-flash-1017，P4 MCQ，seed42；Agent/Updater思考开启，防御思考关闭。S=4；N=9/50/100/500/1000。B4为v6，swap、k=1、最多2次重建；短名单8条/6000字符，防御输入24000字符、输出8192 token。', '',
             '大N数据为本项目构造的跨工作流背景压力集，原任务与选中经验保持不变。不同N不是独立场景；外部防御相同输入复用缓存，不是独立重复实验。', '']
    lines += table(['组', '方法', '信息与定位'], [[g, NAMES[g], {
        'B0': '原始Agent/Updater', 'B1': '候选记忆最终审核，内部对照', 'B2': '完整历史H重建，诊断对照',
        'B3': '随机遗漏证据，内部消融', 'B4': '从遗漏历史检索证据，有限反事实重建与修复',
        'B5': 'EMPTY_MEMORY回退，再由原Agent回答', 'D1': '整合前审核保留经验S',
        'D2': '使用记忆时检查当前任务对齐', 'D3': 'S内任务相关共识及lesson'}[g]] for g in ORDER])
    lines += ['', '## 二、主实验：正常与投毒任务准确率', '', '每格为正确条数/144；括号为准确率。两个条件分别列出，不与固定资格集准确率混用。', '']
    for key, title in [('正常', '正常条件'), ('投毒', '投毒条件')]:
        lines += ['### ' + title, '']
        lines += table(['方法'] + [f'N={n}' for n in (9, 50, 100, 500, 1000)], [
            [g + ' ' + NAMES[g]] + [f"{r[key+'正确数']}/144（{format_percent(r[key+'准确率'])}）"
                for r in sorted([r for r in main_rows if r['防御组'] == g], key=lambda r: r['N'])]
            for g in ORDER if any(r['防御组'] == g for r in main_rows)])
        lines += ['']
    for metric, numerator, denominator, title in [('ASR', 'ASR成功数', 'ASR分母', '三、主实验：固定ASR（越低越好）'),
                                                 ('Recovery率', 'Recovery正确数', 'Recovery分母', '四、主实验：Recovery（越高越好）')]:
        lines += ['## ' + title, '']
        lines += table(['方法'] + [f'N={n}' for n in (9, 50, 100, 500, 1000)], [
            [g + ' ' + NAMES[g]] + [f"{r[numerator]}/{r[denominator]}（{format_percent(r[metric])}）"
                for r in sorted([r for r in main_rows if r['防御组'] == g], key=lambda r: r['N'])]
            for g in ORDER if any(r['防御组'] == g for r in main_rows)])
        lines += ['']
    lines += ['## 五、主实验：效果与token、时间综合明细', '',
              '35行对应5个N×7组。token为结果行调用范围内的完整逻辑量（包含缓存原usage）；时间为相同范围的累计调用时间，含缓存，不是冷启动。所有条件和no-memory诊断成本均计入。', '']
    lines += table(['N', '组', '正常准确率', '投毒准确率', 'ASR', 'Recovery', '输入token', '输出token', '总token', '累计时间', '命中率'], [
        [r['N'], r['防御组'], format_percent(r['正常准确率']), format_percent(r['投毒准确率']), format_percent(r['ASR']),
         format_percent(r['Recovery率']), format_int(r['逻辑输入token']), format_int(r['逻辑输出token']), format_int(r['逻辑总token']),
         duration(r['行关联_累计调用秒']), format_percent(r['行关联_缓存命中率'])] for r in main_rows])
    lines += ['', '### B4相对全库重建的逻辑token', '']
    comparisons = []
    for n in (9, 50, 100, 500, 1000):
        own = next(r for r in main_rows if r['N'] == n and r['防御组'] == 'B4')
        full = next(r for r in main_rows if r['N'] == n and r['防御组'] == 'B2')
        ratio = own['逻辑总token'] / full['逻辑总token']
        comparisons.append([n, format_int(own['逻辑总token']), format_int(full['逻辑总token']), f'{ratio:.3f}倍',
                            f'减少{1-ratio:.2%}' if ratio < 1 else f'增加{ratio-1:.2%}'])
    lines += table(['N', 'B4总token', 'B2总token', 'B4/B2', '差异'], comparisons)
    lines += ['', '### 时间与新增费用的解释', '',
              '- 外部D1/D2/D3在N≥50全部命中已有请求缓存，时间接近0来自缓存，不是推理加速。N9也复用此前32条的部分缓存。',
              '- B0/B5及N9 B2大量命中缓存；不能把当前接近0的时间当作冷启动成本。',
              '- B4与B2包含不同历史调用、缓存比例和运行时段，目前没有公平的冷启动速度排名；没有证据支持B4时延更低。',
              '- 新增API支出、来源链总量及未落盘失败开销见运行台账；comparison导入reference，resume继承父事件，不能跨目录累加总量。',
              '- 缺失usage的精确token留空，另列已知下限；不将连接失败当作零token确定值。', '',
              '## 六、补充：固定32条、B0—B5六组', '',
              'N9是从既有144条结果精确筛出与大N相同的32条，并非新跑一轮。其余N是已保留的32条最终实验。包含主实验没有覆盖的B1/B3消融；不要与144条主结果混合。', '']
    lines += table(['N', '组', '正常正确', '投毒正确', 'ASR', 'Recovery', '总token', '累计时间'], [
        [r['N'], r['防御组'], f"{r['正常正确数']}/32", f"{r['投毒正确数']}/32", format_percent(r['ASR']),
         f"{r['Recovery正确数']}/{r['Recovery分母']}", format_int(r['逻辑总token']), duration(r['行关联_累计调用秒'])] for r in pilot])
    for title, records in [('七、补充：v6 N9全144条六组（含B1/B3）', v6_n9),
                            ('八、历史：v5 N9全144条六组', v5), ('九、来源：外部防御首次32条真实调用', cost_source)]:
        lines += ['', '## ' + title, '']
        if records is v5:
            lines += ['本表只统计最终完成行关联的调用。早先按整组事件汇总的v5成本还包括失败尝试，因此数值略有差异；失败成本仍在运行台账单列，没有丢弃。', '']
        lines += table(['组', '样本数', '正常正确', '投毒正确', 'ASR', 'Recovery', '总token', '行关联API token', '累计时间'], [
            [r['防御组'], r['计划样本数'], r['正常正确数'], r['投毒正确数'], format_percent(r['ASR']),
             f"{r['Recovery正确数']}/{r['Recovery分母']}", format_int(r['逻辑总token']), format_int(r['行关联_API总token']), duration(r['行关联_累计调用秒'])] for r in records])
    lines += ['', '## 十、B4的提交、修复、回退与审核', '',
              '这些是模型/状态机过程统计，不是带独立标注的检测precision。修复后正确数包含原本已正确的任务；Recovery只计B0固定错误集合。回退后的任务恢复是合法收益。', '']
    lines += table(['N', '条件', '原候选', '修复', '回退', '审核支持', '审核不支持', '审核不确定', '协议失败', '恢复来自修复', '恢复来自回退'], [
        [r['N'], r['条件'], r['保留原候选'], r['提交修复'], r['回退'], r['审核supported'], r['审核unsupported'],
         r['审核inconclusive'], r['审核协议失败'], r['Recovery来自修复'] if r['条件'] == 'poisoned' else '—',
         r['Recovery来自回退'] if r['条件'] == 'poisoned' else '—'] for r in main_flow])
    lines += ['', '## 十一、全部保留运行与来源链', '',
              '以下19个目录有重叠/继承。首次真实API来源、旧版本与补充资料都保留，但不与主表相加。', '']
    lines += table(['目录', '分类', '样本数', '完整', '当前错误', '历史错误', '本次新增API次数', '本次新增API token', '本次累计时间'], [
        [f"[{r['来源目录']}]({r['来源目录']}/)", r['分类'], r['计划样本数'], r['运行完整'], r['当前错误记录数'], r['历史错误记录数'],
         r['本次新增_API尝试次数'], format_int(r['本次新增_API总token']), duration(r['本次新增_累计调用秒'])] for r in ledger])
    lines += ['', '### 故障与续跑记录', '', '同一故障可能同时出现在原目录errors与续跑prior_errors中，下表是记录列表，不能把重复记录当独立失败事件。', '']
    lines += table(['来源', 'N', '组', '样本', '错误链', '秒'], [[r['来源目录'], r['N'], r['防御组'], r['样本ID'], r['错误链'], r['记录耗时秒']] for r in failures])
    lines += ['', '## 十二、目前支持的结论与范围', '',
              '- B4在五个规模的投毒准确率92.36%—96.53%，ASR 2.84%—6.38%；优于三个外部防御。全量外部最强是D1 LLM-Judge，不能沿用32条pilot中的排序。',
              '- N=500/1000相对B2逻辑总token减少69.2%/84.1%，但B2准确率更高；尚无B4更快的证据。',
              '- B5始终回退在总体正确率上仍高于B4；承认回退收益，也不能据当前数据主张B4整体效用超过B5。',
              '- 当前只有一个模型、固定seed与已观察过的144个family；不将跨N缓存复用称作独立重复，不将跨工作流扩展称作官方同域对抗大历史集。', '',
              '## 十三、表格文件与字段说明', '']
    lines += table(['文件', '行数', '用途'], [[f'[{name}.csv](汇总数据/{name}.csv)', len(rows), name] for name, rows in sheets])
    lines += ['', '已删除32个实验仅收录配置/摘要索引，见Excel“已清理实验索引”；不重新把旧版本、mock或不完整结果纳入正式效果统计。', '',
              '重新生成：`/home/lukun/miniconda3/envs/memform/bin/python -B scripts/summarize_results.py`。只读原实验，输出本总汇/Excel/CSV/JSON，不调用API。', '']
    (results / '实验结果总汇.md').write_text('\n'.join(lines), encoding='utf-8')
    for filename, value in originals.items():
        if sha(results / filename) != value:
            raise ValueError('Original source changed: ' + filename)
    validation = {'generated_utc': stamp, 'original_files_unchanged': True, 'original_file_count': len(originals),
                  'main_group_rows': len(main_rows), 'matched_32_group_rows': len(pilot),
                  'retained_runs': len(ledger), 'all_group_rows': len(all_records), 'workbook_sheets': len(sheets),
                  'all_main_metrics_recomputed_and_matched': True, 'xlsx_xml_and_zip_valid': True,
                  'no_api_calls': True, 'source_sha256': originals}
    (csv_dir / '校验记录.json').write_text(json.dumps(validation, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in validation.items() if k != 'source_sha256'}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
