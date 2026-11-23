"""Offline regression: no provider credentials or real model calls required."""
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dual_ai as d


def report(status='passed', ids=()):
    payload = {'status': status, 'summary': status, 'verification': ['运行检查：通过']}
    if ids:
        payload['tasks'] = [dict(payload, id=task_id) for task_id in ids]
    return d.AgentResult(ok=True, text='完成\nDUAL_RESULT: ' + json.dumps(payload, ensure_ascii=False))


def setUpModule():
    global _state_root, _root_patch
    _state_root = tempfile.TemporaryDirectory()
    _root_patch = patch.object(d, 'RUN_STATE_ROOT', Path(_state_root.name))
    _root_patch.start()


def tearDownModule():
    _root_patch.stop()
    _state_root.cleanup()


class Plans(unittest.TestCase):
    def test_review_setting_controls_planned_overhead_and_shares(self):
        for tier in ('L0', 'L1', 'L2', 'L3', 'L4'):
            policy = d.tier_policy(tier)
            for enabled in (True, False):
                for raw in ({}, {'tasks': [{'title': '执行', 'ops': ['execute'],
                                            'points': 3}]}):
                    with self.subTest(tier=tier, enabled=enabled, raw=raw):
                        plan = d.normalize_plan(raw, '目标', tier,
                                                review_enabled=enabled)
                        overhead = (2 if policy.plan else 0) + (
                            2 if enabled and policy.review == 'full' else 0)
                        master = overhead + sum(t['points'] for t in plan['tasks']
                                                if t['role'] == d.MASTER)
                        worker = sum(t['points'] for t in plan['tasks']
                                     if t['role'] == d.WORKER)
                        self.assertEqual(plan['overhead_points'], overhead)
                        self.assertEqual(plan['master_points'], master)
                        self.assertEqual(plan['worker_points'], worker)
                        self.assertEqual(plan['total_points'], master + worker)
                        self.assertEqual(plan['actual_share'],
                                         round(master / (master + worker) * 100))

    def test_all_tiers_and_invalid_plans(self):
        for tier in ('L0', 'L1', 'L2', 'L3', 'L4', 0, 30, 100):
            for raw in (None, {}, {'tasks': 42}, {'tasks': ['bad']},
                        {'tasks': [{'title': 'a', 'points': 'inf'}, {'title': 'b', 'id': 'T1'}]}):
                p = d.normalize_plan(raw, '目标', tier)
                policy = d.tier_policy(tier)
                self.assertEqual(p['master_points'] + p['worker_points'], p['total_points'])
                self.assertEqual(len({t['id'] for t in p['tasks']}), len(p['tasks']))
                self.assertEqual(p['tier'], d.normalize_tier(tier))
                for task in p['tasks']:
                    self.assertEqual(task['role'], policy.role_for(task['ops']))
                if policy.solo_role:
                    self.assertTrue(all(t['role'] == policy.solo_role for t in p['tasks']))
                else:
                    expected = 2 + (2 if policy.review == 'full' else 0)
                    self.assertEqual(p['overhead_points'], expected)

    def test_ops_decide_role_not_ratio(self):
        raw = {'tasks': [
            {'title': '思考方案', 'ops': ['think'], 'points': 5},
            {'title': '读取定位', 'ops': ['read', 'locate'], 'points': 2},
            {'title': '执行验证', 'ops': ['execute', 'monitor'], 'points': 5},
        ]}
        balanced = d.normalize_plan(raw, '目标', 'L2')
        self.assertEqual([t['role'] for t in balanced['tasks']],
                         [d.MASTER, d.WORKER, d.WORKER])
        master_led = d.normalize_plan(raw, '目标', 'L1')
        self.assertEqual([t['role'] for t in master_led['tasks']],
                         [d.MASTER, d.WORKER, d.MASTER])
        self.assertEqual(d.tier_policy('L1').role_for(['read', 'monitor']), d.WORKER)
        worker_led = d.normalize_plan(raw, '目标', 'L3')
        self.assertEqual([t['role'] for t in worker_led['tasks']],
                         [d.MASTER, d.WORKER, d.WORKER])

    def test_legacy_share_maps_to_tier(self):
        self.assertEqual(d.normalize_tier(0), 'L4')
        self.assertEqual(d.normalize_tier(100), 'L0')
        self.assertEqual(d.normalize_tier(20), 'L3')
        self.assertEqual(d.normalize_tier(50), 'L2')
        self.assertEqual(d.normalize_tier(90), 'L1')

    def test_unverified_is_not_accepted(self):
        self.assertFalse(d.accepted_result(d.AgentResult(ok=True, text='测试失败，无法完成')))
        self.assertFalse(d.accepted_result(report('blocked')))
        self.assertTrue(d.accepted_result(report()))
        r = report(); r.ok = False
        self.assertFalse(d.accepted_result(r))


class Scheduling(unittest.TestCase):
    def simulate(self, tier='L2', task=None, review=None, takeover=None,
                 cancel_at=None, review_enabled=True):
        run = d.DualRunThread(cwd=tempfile.gettempdir(), request='目标', tier=tier,
                              review_enabled=review_enabled)
        calls, done, failed = [], [], []
        plan = {'tasks': [{'title': '实现', 'role': 'worker', 'points': 8, 'ops': ['execute']},
                          {'title': '下一步', 'role': 'worker', 'points': 8, 'ops': ['execute']}]}
        def invoke(role, prompt, label, **kwargs):
            calls.append((role, label, kwargs, prompt))
            if cancel_at and cancel_at in label:
                run.cancel()
                return d.AgentResult(cancelled=True)
            if '规划' in label or '契约' in label:
                return d.AgentResult(ok=True, text=json.dumps(plan))
            if '验收' in label:
                return review if review is not None else report()
            if '接管' in label:
                return takeover if takeover is not None else report()
            return task if task is not None else report(ids=re.findall(r'T\d+', label))
        run._run_agent = invoke
        run.completed.connect(done.append)
        run.failed.connect(failed.append)
        run.run()
        return calls, done, failed

    def test_endpoints_only_one_role(self):
        for tier, role in (('L4', d.WORKER), ('L0', d.MASTER)):
            calls, done, failed = self.simulate(tier=tier)
            self.assertEqual([c[0] for c in calls], [role])
            self.assertTrue(done[0]['ok']); self.assertFalse(failed)
            self.assertTrue(done[0]['solo'])

    def test_review_failure_controls_completion(self):
        for review in (report('failed'), d.AgentResult(error='timeout')):
            _, done, _ = self.simulate(review=review)
            self.assertFalse(done[0]['ok'])
            self.assertFalse(done[0]['review_ok'])

    def test_worker_blocked_triggers_takeover(self):
        calls, done, _ = self.simulate(task=report('blocked'))
        self.assertTrue(any('接管' in c[1] for c in calls))
        self.assertTrue(done[0]['ok'])
        self.assertGreater(done[0]['performed_points'][d.MASTER], 4)

    def test_low_tier_skips_review_when_all_pass(self):
        calls, done, _ = self.simulate(tier='L3')
        self.assertEqual([c[0] for c in calls], [d.MASTER, d.WORKER])
        self.assertIsNone(done[0]['review_ok'])
        self.assertTrue(done[0]['ok'])

    def test_low_tier_audits_read_only_on_failure(self):
        calls, done, _ = self.simulate(
            tier='L3', task=report('blocked'), takeover=report('failed'))
        audit = next(c for c in calls if '验收' in c[1])
        self.assertTrue(audit[2]['read_only'])
        self.assertIn('禁止修改文件', audit[3])
        self.assertFalse(done[0]['ok'])

    def test_review_switch_skips_independent_review_across_tiers(self):
        for tier in ('L1', 'L2', 'L3'):
            with self.subTest(tier=tier, review=False):
                calls, done, _ = self.simulate(tier=tier, review_enabled=False)
                self.assertFalse(any('验收' in c[1] for c in calls))
                self.assertTrue(done[0]['ok'])
                self.assertIsNone(done[0]['review_ok'])
                self.assertFalse(done[0]['review_enabled'])
                self.assertEqual(done[0]['review_mode'], 'none')
                self.assertEqual(done[0]['master_points'],
                                 18 if tier == 'L1' else 2)
                self.assertEqual(done[0]['total_points'], 18)
            with self.subTest(tier=tier, review=True):
                calls, done, _ = self.simulate(tier=tier, review_enabled=True)
                # L1/L2 始终验收；L3 全部通过时按原策略跳过。
                self.assertEqual(any('验收' in c[1] for c in calls), tier != 'L3')
                self.assertTrue(done[0]['review_enabled'])
                self.assertEqual(done[0]['review_mode'],
                                 d.tier_policy(tier).review)

    def test_review_off_still_blocks_failures_and_keeps_takeover(self):
        # 执行失败但接管成功：关闭复查也必须完成任务且不调用验收。
        calls, done, _ = self.simulate(task=report('blocked'),
                                       review_enabled=False)
        self.assertTrue(any('接管' in c[1] for c in calls))
        self.assertFalse(any('验收' in c[1] for c in calls))
        self.assertTrue(done[0]['ok'])
        # 接管也失败：不能因跳过复查而误报成功。
        calls, done, _ = self.simulate(
            task=report('blocked'), takeover=report('failed'),
            review_enabled=False)
        self.assertFalse(any('验收' in c[1] for c in calls))
        self.assertFalse(done[0]['ok'])
        # 取消仍保持取消语义，不触发复查也不升级。
        calls, done, _ = self.simulate(
            tier='L2', cancel_at='T1', review_enabled=False)
        self.assertTrue(done[0]['cancelled'])
        self.assertFalse(done[0]['ok'])
        self.assertFalse(any('验收' in c[1] for c in calls))

    def test_plan_prompt_has_no_duplicate_review_task(self):
        enabled = d.DualRunThread(cwd=tempfile.gettempdir(), tier='L2')
        prompt = enabled._plan_prompt()
        self.assertIn('不要把单独的主 AI验收', prompt)
        disabled = d.DualRunThread(cwd=tempfile.gettempdir(), tier='L2',
                                   review_enabled=False)
        prompt = disabled._plan_prompt()
        self.assertIn('本轮已关闭主 AI 复查', prompt)
        self.assertIn('不要安排独立验收', prompt)
        self.assertNotIn('不要把单独的主 AI验收', prompt)

    def test_cancel_never_escalates(self):
        for stage, tier in (('规划', 'L2'), ('T1', 'L2'), ('验收', 'L2'),
                            ('独立执行', 'L0')):
            calls, done, _ = self.simulate(tier=tier, cancel_at=stage)
            self.assertTrue(done[0]['cancelled'])
            self.assertFalse(done[0]['ok'])
            self.assertFalse(any('接管' in c[1] for c in calls))

    def test_worker_ask_gets_guidance_then_continues(self):
        run = d.DualRunThread(cwd=tempfile.gettempdir(), request='目标', tier='L2')
        calls, done = [], []
        plan = {'tasks': [{'title': '实现', 'role': 'worker', 'points': 8,
                           'ops': ['execute']}]}
        ask = d.AgentResult(ok=True, text=(
            'DUAL_ASK: {"task":"T1","problem":"两种接口取舍","need":"选择兼容接口"}'))
        guided = report(ids=['T1'])
        prompts = []
        def invoke(role, prompt, label, **kwargs):
            calls.append((role, label))
            prompts.append(prompt)
            if '规划' in label:
                return d.AgentResult(ok=True, text=json.dumps(plan))
            if '指导' in label:
                return d.AgentResult(ok=True, text=(
                    'DUAL_GUIDE: {"decision":"保留旧接口","next":["加适配层"],'
                    '"done_when":"旧调用不受影响"}'))
            if '验收' in label:
                return report()
            return ask if len(calls) == 2 else guided
        run._run_agent = invoke
        run.completed.connect(done.append)
        run.run()
        self.assertEqual([c[0] for c in calls], [d.MASTER, d.WORKER, d.MASTER, d.WORKER, d.MASTER])
        self.assertIn('保留旧接口', prompts[3])
        self.assertEqual(done[0]['asks'], 1)
        self.assertTrue(done[0]['ok'])

    def test_long_context_precompressed_before_planning(self):
        run = d.DualRunThread(cwd=tempfile.gettempdir(), request='目标', tier='L2',
                              context='长上下文' + 'x' * 8000)
        done, prompts = [], []
        plan = {'tasks': [{'title': '实现', 'role': 'worker', 'points': 8,
                           'ops': ['execute']}]}

        def fake(agent, spec, prompt, *args, session_id="", **kwargs):
            prompts.append((spec.source, prompt))
            if '你是执行 AI。先为下一个角色压缩有效上下文' in prompt:
                return d.AgentResult(ok=True, session_id='w1', text=json.dumps(
                    {'summary': '压缩摘要', 'constraints': ['旧接口不变'],
                     'refs': ['a.py:1'], 'risks': []}))
            if '负责规划' in prompt:
                return d.AgentResult(ok=True, session_id='m1', text=json.dumps(plan))
            if '最终验收' in prompt:
                return d.AgentResult(ok=True, session_id='m1', text=report().text)
            return d.AgentResult(ok=True, session_id='w1',
                                 text=report(ids=['T1']).text)

        with patch.object(d.CliAgent, 'run', new=fake):
            run.completed.connect(done.append)
            run.run()
        self.assertIn('压缩有效上下文', prompts[0][1])
        self.assertIn('压缩摘要', prompts[1][1])
        self.assertNotIn('x' * 8000, prompts[1][1])
        self.assertNotIn('首次接手必须读取完整原始输入', prompts[1][1])
        self.assertIn('原始输入（按需定位核实）', prompts[1][1])
        brief = json.loads((run._journal.root / 'context-brief.json').read_text())
        self.assertEqual(brief['constraints'], ['旧接口不变'])
        self.assertLess(done[0]['context_after'], done[0]['context_before'])
        self.assertTrue(done[0]['ok'])

    def test_short_context_skips_compression_call(self):
        calls, done, _ = self.simulate(tier='L2')
        self.assertEqual([c[0] for c in calls][:2], [d.MASTER, d.WORKER])

    def test_compression_retains_constraints_and_rejects_invalid_or_expanded_briefs(self):
        complete = {'summary': '摘要', 'constraints': ['约束' + str(i) + 'x' * 550 for i in range(15)],
                    'refs': ['关键文件'], 'risks': ['待核实项']}
        expanded = dict(complete, summary='x' * 31000)
        for brief, usable in ((complete, True), ({}, False), (expanded, False)):
            with self.subTest(usable=usable, expanded=brief is expanded):
                run = d.DualRunThread(cwd=tempfile.gettempdir(), request='目标', tier='L2',
                                      context='原始历史' + 'h' * 30000)
                done, prompts = [], []
                plan = {'tasks': [{'title': '实施', 'ops': ['execute']}]}
                def fake(agent, spec, prompt, *args, **kwargs):
                    prompts.append(prompt)
                    if '你是执行 AI。先为下一个角色压缩有效上下文' in prompt:
                        return d.AgentResult(ok=True, text=json.dumps(brief))
                    if '负责规划' in prompt:
                        return d.AgentResult(ok=True, text=json.dumps(plan))
                    return report(ids=['T1'])
                with patch.object(d.CliAgent, 'run', new=fake):
                    run.completed.connect(done.append)
                    run.run()
                self.assertTrue(done[0]['ok'])
                path = run._journal.root / 'context-brief.json'
                self.assertEqual(path.exists(), usable)
                if usable:
                    self.assertEqual(json.loads(path.read_text()), complete)
                    self.assertIn(complete['constraints'][-1], prompts[1])
                    self.assertNotIn('必须读取完整原始输入', prompts[1])
                else:
                    self.assertIn('必须读取完整原始输入', prompts[1])
                    self.assertEqual(done[0]['context_after'], done[0]['context_before'])

    def test_task_receives_handoff_and_context(self):
        run = d.DualRunThread(context='用户约定', request='目标')
        run._records.append({'id': 'T1', 'ok': True, 'report': {
            'summary': '验证证据', 'interfaces': ['保持接口']}})
        p = d.normalize_plan({}, '目标', 'L2')
        prompt = run._task_prompt(p, p['tasks'][0])
        self.assertNotIn('用户约定', prompt)
        self.assertIn('验证证据', prompt)
        self.assertIn('保持接口', prompt)
        saved = json.loads((run._journal.root / 'input.json').read_text())
        self.assertEqual(saved['context'], '用户约定')


class Cli(unittest.TestCase):
    def cli(self, events, rc=0, source='codex'):
        agent = d.CliAgent(tempfile.gettempdir(), claude_path='claude', codex_path='codex')
        def stream(proc, handle):
            for e in events:
                handle(json.dumps(e))
            return rc, ['stderr'], False
        with patch.object(agent, '_popen', return_value=object()), patch.object(agent, '_stream', side_effect=stream):
            return agent.run(d.RoleSpec(source=source), 'test')

    def test_codex_nonzero_with_text(self):
        r = self.cli([{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'partial'}}], rc=1)
        self.assertFalse(r.ok)
        self.assertIn('退出码', r.error)

    def test_codex_turn_failed_with_text(self):
        r = self.cli([{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'partial'}},
                      {'type': 'turn.failed', 'error': {'message': '额度耗尽'}}])
        self.assertFalse(r.ok); self.assertEqual(r.error, '额度耗尽')

    def test_claude_nonzero_with_text(self):
        r = self.cli([{'type': 'result', 'result': 'partial'}], rc=1, source='claude')
        self.assertFalse(r.ok)

    def test_timeout_after_stdout_closed(self):
        agent = d.CliAgent(tempfile.gettempdir(), timeout=0.15)
        p = agent._popen([sys.executable, '-c', 'import os,time; os.close(1); time.sleep(10)'], '')
        start = time.monotonic()
        _, _, timed_out = agent._stream(p, lambda line: None)
        self.assertTrue(timed_out)
        self.assertLess(time.monotonic() - start, 3)
        self.assertIsNotNone(p.poll())

    def test_large_input_and_output_no_pipe_deadlock(self):
        agent = d.CliAgent(tempfile.gettempdir(), timeout=3)
        code = "import sys; print('x'*200000,flush=True); data=sys.stdin.read(); print(len(data),flush=True)"
        p = agent._popen([sys.executable, '-c', code], 'y' * 200000)
        lines = []
        rc, _, timed_out = agent._stream(p, lines.append)
        self.assertFalse(timed_out); self.assertEqual(rc, 0)
        self.assertEqual(lines[-1], '200000')


class Pipeline(unittest.TestCase):
    def test_role_efforts_reach_each_cli_stage_and_endpoints(self):
        with tempfile.TemporaryDirectory() as root:
            cli = Path(root) / 'mock-agent'
            cli.write_text('''#!/usr/bin/env python3
import json, pathlib, sys
prompt = sys.stdin.read()
with open('calls.jsonl', 'a') as log:
    log.write(json.dumps({'args': sys.argv[1:], 'prompt': prompt}) + '\\n')
if '只输出一个 JSON 对象' in prompt:
    text = json.dumps({'tasks': [{'id': 'T1', 'title': '实现', 'role': 'worker', 'points': 8}]})
else:
    solo = '端点模式' in prompt or '全从档' in prompt or '全主档' in prompt
    status = 'blocked' if any(x.endswith('-worker') for x in sys.argv) and not solo else 'passed'
    text = 'DUAL_RESULT: ' + json.dumps({'status': status, 'summary': 'done', 'verification': ['mock verified']})
if 'exec' in sys.argv:
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}}))
    print(json.dumps({'type': 'turn.completed'}))
else:
    print(json.dumps({'type': 'result', 'result': text, 'is_error': False}))
''')
            cli.chmod(0o755)
            for reverse in (False, True):
                for tier in ('L4', 'L2', 'L0'):
                    with self.subTest(tier=tier, reverse=reverse):
                        log = Path(root) / 'calls.jsonl'
                        log.unlink(missing_ok=True)
                        master = d.RoleSpec('claude', 'c-master', efforts=('low', 'high'), effort='low')
                        worker = d.RoleSpec('codex', 'x-worker', efforts=('minimal', 'ultra'), effort='ultra')
                        if reverse:
                            master = d.RoleSpec('codex', 'x-master', efforts=('minimal', 'ultra'), effort='ultra')
                            worker = d.RoleSpec('claude', 'c-worker', efforts=('low', 'high'), effort='low')
                        run = d.DualRunThread(cwd=root, request='检查独立档位', tier=tier,
                                             master=master, worker=worker, effort='high',
                                             claude_path=str(cli), codex_path=str(cli))
                        completed, failed = [], []
                        run.completed.connect(completed.append)
                        run.failed.connect(failed.append)
                        run.run()
                        self.assertFalse(failed)
                        self.assertTrue(completed[0]['ok'])
                        rows = [json.loads(line) for line in log.read_text().splitlines()]
                        for row in rows:
                            args = row['args']
                            if 'exec' in args:
                                self.assertIn('model_reasoning_effort="ultra"', args)
                                self.assertNotIn('--effort', args)
                            else:
                                self.assertEqual(args[args.index('--effort') + 1], 'low')
                        if tier == 'L2':
                            self.assertEqual(len(rows), 4)
                            self.assertTrue(any('只输出一个 JSON 对象' in row['prompt'] for row in rows))
                            self.assertTrue(any('接管' in row['prompt'] for row in rows))
                            self.assertTrue(any('验收' in row['prompt'] for row in rows))
                        else:
                            self.assertEqual(len(rows), 1)
                            self.assertEqual('exec' in rows[0]['args'],
                                             (tier == 'L4') != reverse)

    def test_real_subprocess_pipeline_in_temporary_project(self):
        with tempfile.TemporaryDirectory() as root:
            cli = Path(root) / 'mock-agent'
            cli.write_text('''#!/usr/bin/env python3
import json, pathlib, sys
prompt = sys.stdin.read()
if '只输出一个 JSON 对象' in prompt:
    text = json.dumps({'summary':'临时项目测试','tasks':[{'title':'生成文件','points':8,'role':'worker'}]}, ensure_ascii=False)
else:
    pathlib.Path('result.txt').write_text('verified')
    text = 'DUAL_RESULT: ' + json.dumps({'status':'passed','summary':'已完成','verification':['已检查 result.txt']}, ensure_ascii=False)
if 'exec' in sys.argv:
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}))
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':5,'output_tokens':2}}))
else:
    print(json.dumps({'type':'result','result':text,'is_error':False,'usage':{'input_tokens':10,'output_tokens':2}}))
''')
            cli.chmod(0o755)
            for tier in ('L4', 'L2', 'L0'):
                run = d.DualRunThread(cwd=root, request='生成文件并验证', tier=tier,
                                      master=d.RoleSpec('claude', 'mock-master'),
                                      worker=d.RoleSpec('codex', 'mock-worker'),
                                      claude_path=str(cli), codex_path=str(cli))
                completed, failed = [], []
                run.completed.connect(completed.append)
                run.failed.connect(failed.append)
                run.run()
                self.assertFalse(failed)
                self.assertTrue(completed[0]['ok'])
                self.assertEqual((Path(root) / 'result.txt').read_text(), 'verified')
                if tier == 'L2':
                    self.assertEqual(completed[0]['stats'][d.WORKER]['tokens'], 12)
                    self.assertEqual(completed[0]['stats'][d.MASTER]['calls'], 2)


class Progress(unittest.TestCase):
    def test_status_snapshot_tracks_stage_role_tools_and_finish(self):
        run = d.DualRunThread(
            cwd=tempfile.gettempdir(), request='进度检查', tier='L2',
            master=d.RoleSpec('claude', 'mock-master'),
            worker=d.RoleSpec('codex', 'mock-worker'))
        snap = run.status_snapshot()
        self.assertFalse(snap['busy'])
        self.assertEqual(snap['activity_label'], '准备协作…')
        run._emit_status('主 AI 正在规划…', role=d.MASTER, task='T1')
        run._status_tool('Bash')
        snap = run.status_snapshot()
        self.assertEqual(snap['tier'], 'L2')
        self.assertEqual(snap['role_label'], '主 AI')
        self.assertEqual(snap['task_label'], 'T1')
        self.assertEqual(snap['last_tool'], 'Bash')
        self.assertEqual(snap['tool_count'], 1)
        self.assertIsNotNone(snap['since_activity'])
        self.assertEqual(snap['activity_label'], '执行工具 Bash')
        run._status_finish()
        self.assertTrue(snap['busy'] is False)


if __name__ == '__main__':
    unittest.main()
