"""Protocol v2 regression, including real subprocess transport with deterministic CLIs."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dual_ai as d


def result(status="passed", ids=None, **extra):
    payload = dict(status=status, summary="checked", verification=["check: exit 0"], **extra)
    if ids is not None:
        payload["tasks"] = [dict(status=status, summary=task_id,
                                 verification=[task_id + ": exit 0"], id=task_id) for task_id in ids]
    return d.AgentResult(ok=True, text="DUAL_RESULT: " + json.dumps(payload, ensure_ascii=False))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.patch = patch.object(d, "RUN_STATE_ROOT", self.root / "states")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    def runner(self, **kwargs):
        return d.DualRunThread(cwd=str(self.root), request="原始目标不可丢失", **kwargs)


class Reports(Fixture):
    def test_latest_protocol_line_and_full_guidance(self):
        ask = 'DUAL_ASK: {"task":"T1","problem":"选择接口"}'
        self.assertIsNotNone(d.extract_ask(d.AgentResult(ok=True, text=ask)))
        self.assertIsNone(d.extract_ask(d.AgentResult(ok=True, text=ask + '\n' + result().text)))
        self.assertIsNone(d.extract_ask(d.AgentResult(ok=True, text=ask + '\n任务已结束')))
        self.assertEqual(d.extract_guide(d.AgentResult(ok=True, text='无有效指导')), '')
        guide = {'decision': '保留接口', 'next': ['x' * 1700], 'avoid': ['禁止删除末尾接口']}
        parsed = d.extract_guide(d.AgentResult(ok=True, text='DUAL_GUIDE: ' + json.dumps(guide)))
        self.assertIn('禁止删除末尾接口', parsed)

    def test_last_line_and_evidence_are_authoritative(self):
        good = result()
        self.assertFalse(d.accepted_result(d.AgentResult(ok=True, text=good.text + "\n后来发现失败")))
        for value in (None, [], [""], "exit 0"):
            bad = result()
            body = json.loads(bad.text.split(": ", 1)[1]); body["verification"] = value
            bad.text = "DUAL_RESULT: " + json.dumps(body)
            self.assertFalse(d.accepted_result(bad))
        self.assertFalse(d.accepted_result(result(open_issues=["未解决"])))
        good.error = "transport failed"
        self.assertFalse(d.accepted_result(good))

    def test_task_ids_missing_duplicate_unknown_and_out_of_order(self):
        run = self.runner()
        tasks = [{"id": "T1"}, {"id": "T2"}]
        for ids in ([], ["T2", "T1"], ["T1", "T1"], ["T1", "T3"], ["T1", "T2", "T3"]):
            with self.subTest(ids=ids):
                reports = run._task_reports(tasks, result(ids=ids))
                self.assertTrue(all(r["status"] == "blocked" for r in reports))
        reports = run._task_reports(tasks, result(ids=["T1"]))
        self.assertEqual([r["status"] for r in reports], ["passed", "blocked"])
        self.assertEqual([r["status"] for r in run._task_reports(tasks, result())], ["blocked", "blocked"])

    def test_failure_stops_later_reports_and_open_issues_block(self):
        run = self.runner()
        payload = json.loads(result(ids=["T1", "T2", "T3"]).text.split(": ", 1)[1])
        payload["status"] = "blocked"
        payload["tasks"][1].update(status="failed", open_issues=["接口冲突"])
        reports = run._task_reports([{"id": x} for x in ("T1", "T2", "T3")],
                                   d.AgentResult(ok=True, text="DUAL_RESULT: " + json.dumps(payload)))
        self.assertEqual([x["status"] for x in reports], ["passed", "blocked", "blocked"])
        self.assertFalse(d.accepted_result(result(ids=["T1"], open_issues=["仍阻塞"])))

    def test_large_state_uses_valid_index_and_retains_full_evidence(self):
        run = self.runner(context="早期约束" + "历史" * 12000 + "最后约定")
        plan = d.normalize_plan({"constraints": ["早期接口不变"], "tasks": [{"title": "a"}]}, "目标", 30)
        evidence = "check: " + "证据" * 15000
        run._records.append({"id": "T0", "ok": True, "report": {
            "summary": "previous", "verification": [evidence], "interfaces": ["接口 v1"]},
            "report_path": "完整记录.json"})
        prompt = run._task_prompt(plan, plan["tasks"][0])
        index = prompt.split("状态索引：\n", 1)[1].split("\n按任务 ID", 1)[0]
        self.assertEqual(json.loads(index)["handoffs"][0]["id"], "T0")
        state = json.loads((run._journal.root / "state.json").read_text())
        self.assertEqual(state["records"][0]["report"]["verification"], [evidence])
        self.assertEqual(state["plan"]["constraints"], ["早期接口不变"])
        source = json.loads((run._journal.root / "input.json").read_text())
        self.assertEqual(source["context"], run.context)
        self.assertLess(len(prompt), 5000)
        self.assertEqual(run._journal.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((run._journal.root / "input.json").stat().st_mode & 0o777, 0o600)

    def test_unlimited_history_retains_early_and_long_messages(self):
        blocks = [{"kind": "user", "text": "最早约束" + "a" * 10000}]
        blocks.extend({"kind": "worker", "text": str(n)} for n in range(30))
        text = d.history_context(blocks, limit=None)
        self.assertTrue(text.startswith("用户：最早约束"))
        self.assertIn("a" * 10000, text)
        self.assertTrue(text.endswith("29"))

    def test_next_turn_carries_snapshot_without_reusing_old_sessions(self):
        first = self.runner()
        first._plan = {"constraints": ["接口不可更名"]}
        first._records = [{"id": "T1", "ok": True, "report": {
            "interfaces": ["API v1"], "verification": ["passed"]}}]
        first._ensure_journal()
        second = self.runner(previous_state=str(first._journal.root))
        second._ensure_journal()
        snapshot = json.loads(Path(second._journal.previous).read_text())
        self.assertEqual(snapshot["plan"]["constraints"], ["接口不可更名"])
        self.assertEqual(snapshot["records"][0]["report"]["interfaces"], ["API v1"])
        self.assertEqual(second._sessions, {})
        self.assertIn(second._journal.previous, second._bootstrap())

    def test_missing_and_foreign_checkpoints_do_not_leak_between_projects(self):
        first = self.runner()
        first._ensure_journal()
        foreign = self.root / "another-project"
        foreign.mkdir()
        for cwd, previous in ((str(foreign), str(first._journal.root)),
                              (str(self.root), str(self.root / "missing"))):
            run = d.DualRunThread(cwd=cwd, previous_state=previous)
            notes = []; run.note.connect(notes.append)
            run._ensure_journal()
            self.assertEqual(run._journal.previous, "")
            self.assertTrue(notes)
            self.assertIn("未载入", run._bootstrap())
        (first._journal.root / 'state.json').write_text('[]')
        corrupted = self.runner(previous_state=str(first._journal.root))
        corrupted._ensure_journal()
        self.assertIn('格式损坏', corrupted._journal.previous_error)


class Sessions(Fixture):
    def test_resume_isolated_by_role_model_and_phase_permissions(self):
        spec = d.RoleSpec("codex", "same", effort="")
        run = self.runner(master=spec, worker=spec)
        seen = []
        def invoke(agent, spec, prompt, mode, readonly, *args, session_id=""):
            seen.append((prompt, readonly, session_id))
            r = result(); r.session_id = session_id or "session-" + str(len(seen))
            return r
        with patch.object(d.CliAgent, "run", new=invoke):
            run._run_agent(d.MASTER, "plan", "规划", display=False, read_only=True)
            run._run_agent(d.WORKER, "task", "T1")
            run._run_agent(d.MASTER, "review", "验收")
            run._run_agent(d.WORKER, "task2", "T2")
            run.worker = d.RoleSpec("codex", "changed", effort="")
            run._run_agent(d.WORKER, "task3", "T3")
        self.assertEqual([x[2] for x in seen], ["", "", "session-1", "session-2", ""])
        self.assertEqual([x[1] for x in seen], [True, False, False, False, False])
        self.assertNotIn("原始目标不可丢失", seen[2][0])
        self.assertIn("当前阶段：验收", seen[2][0])
        self.assertIn("原始目标不可丢失", seen[4][0])
        self.assertEqual(run._stats[d.MASTER]["calls"], 2)

    def test_expired_session_rebuilds_once_and_preserves_evidence(self):
        run = self.runner()
        seen = []
        def invoke(agent, spec, prompt, *args, session_id=""):
            seen.append((prompt, session_id))
            if len(seen) == 2:
                return d.AgentResult(error="No conversation found with session ID: expired")
            r = result(); r.session_id = "old" if len(seen) == 1 else "new"
            return r
        with patch.object(d.CliAgent, "run", new=invoke):
            run._run_agent(d.MASTER, "plan", "规划", display=False)
            r = run._run_agent(d.MASTER, "review", "验收")
        self.assertTrue(r.ok)
        self.assertEqual([x[1] for x in seen], ["", "old", ""])
        self.assertIn("原始目标不可丢失", seen[2][0])
        self.assertEqual(run._stats[d.MASTER]["calls"], 3)
        self.assertEqual(run._session_resets, 1)
        self.assertEqual(len(list(run._journal.root.glob("call-*.json"))), 3)
        self.assertEqual(run._prompt_chars, sum(len(prompt) for prompt, _ in seen))

    def test_error_or_partial_side_effect_never_replayed(self):
        for error, tools, text in (("额度耗尽", [], ""),
                                   ("session x not found", [{"name": "Edit"}], ""),
                                   ("session x not found", [], "已经修改")):
            run = self.runner()
            initial = result(); initial.session_id = "old"
            failed = d.AgentResult(error=error, tools=tools, text=text)
            with patch.object(d.CliAgent, "run", side_effect=[initial, failed]) as cli:
                run._run_agent(d.MASTER, "p", "规划", display=False)
                run._run_agent(d.MASTER, "t", "任务")
                self.assertEqual(cli.call_count, 2)
                self.assertEqual(run._session_resets, 0)

    def test_stale_session_retry_is_bounded(self):
        run = self.runner()
        initial = result(); initial.session_id = "old"
        with patch.object(d.CliAgent, "run", side_effect=[initial,
                d.AgentResult(error="session x not found"),
                d.AgentResult(error="session x not found")]) as cli:
            run._run_agent(d.MASTER, "p", "规划", display=False)
            r = run._run_agent(d.MASTER, "t", "任务")
            self.assertFalse(r.ok)
            self.assertEqual(cli.call_count, 3)

    def test_cli_resume_arguments_and_session_events(self):
        for source in ("claude", "codex"):
            for readonly in (True, False):
                for mode in ("acceptEdits", "bypassPermissions"):
                    agent = d.CliAgent(str(self.root), claude_path="claude", codex_path="codex")
                    captured = []
                    def stream(proc, handle):
                        if source == "claude":
                            handle(json.dumps({"type": "system", "subtype": "init", "session_id": "s-new"}))
                            handle(json.dumps({"type": "result", "result": result().text}))
                        else:
                            handle(json.dumps({"type": "thread.started", "thread_id": "s-new"}))
                            handle(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": result().text}}))
                        return 0, [], False
                    with patch.object(agent, "_popen", side_effect=lambda cmd, prompt: captured.append(cmd)), \
                         patch.object(agent, "_stream", side_effect=stream):
                        r = agent.run(d.RoleSpec(source, "test"), "prompt", mode, readonly, session_id="s-old")
                    cmd = captured[0]
                    self.assertTrue(r.ok); self.assertEqual(r.session_id, "s-new")
                    if source == "claude":
                        self.assertEqual(cmd[cmd.index("--resume") + 1], "s-old")
                        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan" if readonly else mode)
                    else:
                        self.assertLess(cmd.index("-C"), cmd.index("resume"))
                        self.assertEqual(cmd[-2:], ["s-old", "-"])
                        if readonly or mode != "bypassPermissions":
                            self.assertLess(cmd.index("--sandbox"), cmd.index("resume"))
                            self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only" if readonly else "workspace-write")
                        else:
                            self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)


class Scheduler(Fixture):
    def simulate(self, responses, tier="L2", plan=None):
        run = self.runner(tier=tier)
        if plan is None:
            plan = {"tasks": [{"title": "task " + str(i), "points": 8,
                               "role": "worker", "ops": ["execute"]} for i in range(3)]}
        calls, completed, failed = [], [], []
        def invoke(role, prompt, label, **kwargs):
            calls.append((role, prompt, label, kwargs))
            if "规划" in label:
                return d.AgentResult(ok=True, text=json.dumps(plan))
            return responses.pop(0)
        run._run_agent = invoke
        run.completed.connect(completed.append); run.failed.connect(failed.append)
        run.run()
        self.assertFalse(failed, failed)
        return run, calls, completed[0]

    def test_consecutive_tasks_three_calls_instead_of_five(self):
        run, calls, done = self.simulate([result(ids=["T1", "T2", "T3"]), result()])
        self.assertEqual(len(calls), 3)
        self.assertTrue(done["ok"])
        self.assertEqual([r["id"] for r in done["records"]], ["T1", "T2", "T3"])

    def test_old_ask_before_final_report_does_not_add_model_calls(self):
        completed = result(ids=['T1', 'T2', 'T3'])
        completed.text = 'DUAL_ASK: {"task":"T1","problem":"旧问题"}\n' + completed.text
        run, calls, done = self.simulate([completed, result()])
        self.assertTrue(done['ok'])
        self.assertEqual(len(calls), 3)
        self.assertEqual(done['asks'], 0)

    def test_partial_batch_resumes_remainder_without_replaying_prefix(self):
        partial = json.loads(result(ids=["T1", "T2", "T3"]).text.split(": ", 1)[1])
        partial.update(status="blocked")
        partial["tasks"][1].update(status="blocked", summary="fix needed")
        partial["tasks"][2].update(status="blocked", summary="not attempted")
        run, calls, done = self.simulate([
            d.AgentResult(ok=True, text="DUAL_RESULT: " + json.dumps(partial)),
            result(), result(ids=["T3"]), result()])
        self.assertTrue(done["ok"])
        self.assertIn("接管 T2", calls[2][2])
        self.assertEqual(calls[3][2], "执行 AI · T3")
        self.assertEqual([r["id"] for r in done["records"]], ["T1", "T2", "T3"])
        self.assertEqual(done["performed_points"][d.WORKER], 24)

    def test_final_generic_pass_cannot_hide_unfinished_tasks(self):
        _, _, done = self.simulate([result("blocked"), result("blocked"), result()])
        self.assertFalse(done["ok"])
        self.assertFalse(done["review_ok"])

    def test_final_explicit_repairs_reconcile_state(self):
        _, _, done = self.simulate([result("blocked"), result("blocked"), result(ids=["T1", "T2", "T3"])])
        self.assertTrue(done["ok"])
        self.assertEqual(len(done["records"]), 3)
        self.assertTrue(all(r["resolved_by_review"] for r in done["records"]))
        self.assertTrue(all(r["ok"] for r in done["records"]))

    def test_takeover_must_report_the_failed_task_id(self):
        _, _, done = self.simulate([result("blocked"), result(ids=["T9"]), result()])
        self.assertFalse(done["ok"])
        self.assertFalse(done["records"][0]["takeover"])

    def test_role_order_is_preserved_and_does_not_batch_across_roles(self):
        plan = {"tasks": [
            {"title": "task", "points": 8, "role": "worker", "ops": ["read"]},
            {"title": "task", "points": 8, "role": "master", "ops": ["think"]},
            {"title": "task", "points": 8, "role": "worker", "ops": ["execute"]},
            {"title": "task", "points": 8, "role": "master", "ops": ["design"]},
        ]}
        _, calls, done = self.simulate([result(ids=["T1"]), result(ids=["T2"]),
                                      result(ids=["T3"]), result(ids=["T4"]), result()],
                                     tier="L2", plan=plan)
        self.assertEqual([c[0] for c in calls], ["master", "worker", "master", "worker", "master", "master"])
        self.assertTrue(done["ok"])


class Transport(Fixture):
    def test_all_five_tiers_real_cli_call_order_and_monitor_allocation(self):
        script = self.root / 'tier-agent'
        script.write_text(FAKE_CLI.replace(
            '{"title": "task " + str(i), "points": 8, "role": "worker"} for i in range(3)',
            '{"title": "inspect", "points": 3, "ops": ["read", "monitor"]}, '
            '{"title": "execute", "points": 5, "ops": ["execute"]}'))
        script.chmod(0o755)
        for tier, sources in (
                ('L0', ['claude']),
                ('L1', ['claude', 'codex', 'claude', 'claude']),
                ('L2', ['claude', 'codex', 'claude']),
                ('L3', ['claude', 'codex']),
                ('L4', ['codex'])):
            with self.subTest(tier=tier):
                log = self.root / 'calls.jsonl'
                log.unlink(missing_ok=True)
                run = self.runner(master=d.RoleSpec('claude', 'main'),
                                  worker=d.RoleSpec('codex', 'worker'), tier=tier,
                                  claude_path=str(script), codex_path=str(script))
                done, errors = [], []
                run.completed.connect(done.append); run.failed.connect(errors.append)
                run.run()
                self.assertFalse(errors, errors)
                self.assertTrue(done[0]['ok'])
                rows = [json.loads(x) for x in log.read_text().splitlines()]
                self.assertEqual(['codex' if 'exec' in r['args'] else 'claude' for r in rows], sources)
                self.assertEqual(sum(s['calls'] for s in done[0]['stats'].values()), len(sources))
                if tier == 'L1':
                    self.assertEqual([r['role'] for r in done[0]['records']], [d.WORKER, d.MASTER])

    def test_real_subprocess_batch_sessions_artifacts_and_input_budget(self):
        script = self.root / "fake-agent"
        script.write_text(FAKE_CLI)
        script.chmod(0o755)
        for source in ("claude", "codex"):
            log = self.root / "calls.jsonl"
            log.unlink(missing_ok=True)
            run = self.runner(master=d.RoleSpec(source, "same"), worker=d.RoleSpec(source, "same"),
                              tier="L2", context="早期约定" + "x" * 15000 + "末尾约定",
                              claude_path=str(script), codex_path=str(script))
            done, errors = [], []
            run.completed.connect(done.append); run.failed.connect(errors.append)
            run.run()
            self.assertFalse(errors, errors)
            self.assertTrue(done[0]["ok"])
            rows = [json.loads(x) for x in log.read_text().splitlines()]
            # 长上下文多一次执行 AI 压缩调用，随后主 AI 只读压缩结果。
            self.assertEqual(len(rows), 4)
            self.assertEqual([x["resume"] for x in rows],
                             ["", "", "session-1", "session-2"])
            self.assertNotIn("原始目标不可丢失", rows[-1]["prompt"])
            self.assertLess(len(rows[1]["prompt"]), 8000)
            self.assertNotIn('首次接手必须读取完整原始输入', rows[1]['prompt'])
            state_dir = Path(done[0]["state_dir"])
            state = json.loads((state_dir / "state.json").read_text())
            brief = json.loads((state_dir / 'context-brief.json').read_text())
            self.assertEqual(brief['constraints'], ['早期约定', '末尾约定'])
            self.assertEqual(state['context_brief'], str(state_dir / 'context-brief.json'))
            self.assertEqual(len(state["records"]), 3)
            self.assertEqual(state["records"][0]["report"]["interfaces"], ["result.txt: UTF-8"])
            self.assertEqual(json.loads((state_dir / "input.json").read_text())["context"], run.context)
            self.assertTrue(json.loads((state_dir / "result.json").read_text())["ok"])
            self.assertEqual(len(list(state_dir.glob("call-*.json"))), 4)
            self.assertEqual((self.root / "result.txt").read_text(), "verified")
            self.assertNotEqual(state["sessions"][0]["id"], state["sessions"][1]["id"])


class ReviewSwitch(Fixture):
    def test_disabled_review_skips_master_acceptance_and_records_setting(self):
        script = self.root / "fake-agent"
        script.write_text(FAKE_CLI)
        script.chmod(0o755)
        run = self.runner(master=d.RoleSpec("claude", "main"),
                          worker=d.RoleSpec("codex", "worker"), tier="L2",
                          review_enabled=False,
                          claude_path=str(script), codex_path=str(script))
        done, errors = [], []
        run.completed.connect(done.append); run.failed.connect(errors.append)
        run.run()
        self.assertFalse(errors, errors)
        self.assertTrue(done[0]["ok"])
        self.assertFalse(done[0]["review_enabled"])
        self.assertEqual(done[0]["review_mode"], "none")
        self.assertIsNone(done[0]["review_ok"])
        rows = [json.loads(x) for x in (self.root / "calls.jsonl").read_text().splitlines()]
        # 规划(M) → 任务(W)；不产生第三次主 AI 验收调用。
        self.assertEqual(['codex' if 'exec' in r['args'] else 'claude' for r in rows],
                         ['claude', 'codex'])
        self.assertNotIn("最终验收", rows[-1]["prompt"])
        self.assertEqual(done[0]["stats"][d.MASTER]["calls"], 1)
        state = json.loads((Path(done[0]["state_dir"]) / "state.json").read_text())
        self.assertFalse(state["review_enabled"])
        self.assertEqual(state["review_mode"], "none")
        result = json.loads((Path(done[0]["state_dir"]) / "result.json").read_text())
        self.assertFalse(result["review_enabled"])
        self.assertEqual(result["review_mode"], "none")
        plan = json.loads((Path(done[0]["state_dir"]) / "plan.json").read_text())
        self.assertEqual(plan['overhead_points'], 2)
        for saved in (plan, state['plan'], result):
            self.assertEqual(saved['master_points'], 2)
            self.assertEqual(saved['master_points'] + saved['worker_points'],
                             saved['total_points'])
            self.assertEqual(saved['actual_share'],
                             round(2 / saved['total_points'] * 100))


class AskFlow(Fixture):
    def test_l3_two_guidance_rounds_then_skips_review(self):
        script = self.root / 'ask-twice'
        extra = '''elif "主 AI 的二次指导" in prompt and sum("你是主 AI（M）。执行 AI 在任务" in r["prompt"] for r in rows) < 2:
    resp('DUAL_ASK: {"task":"T1","problem":"还有一个兼容性决定","need":"确认边界"}')
'''
        script.write_text(ASK_CLI.replace('elif "主 AI 的二次指导" in prompt:',
                                          extra + 'elif "主 AI 的二次指导" in prompt:'))
        script.chmod(0o755)
        run = self.runner(master=d.RoleSpec('claude', 'same'),
                          worker=d.RoleSpec('claude', 'same'), tier='L3', claude_path=str(script))
        done, errors = [], []
        run.completed.connect(done.append); run.failed.connect(errors.append)
        run.run()
        self.assertFalse(errors, errors)
        self.assertTrue(done[0]['ok'])
        self.assertEqual(done[0]['asks'], 2)
        rows = [json.loads(x) for x in (self.root / 'ask-calls.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 6)
        self.assertNotIn('最终验收', rows[-1]['prompt'])
        self.assertIn('本轮剩余询问次数：0', rows[-1]['prompt'])
        self.assertEqual(rows[3]['resume'], rows[1]['session'])
        self.assertEqual(rows[5]['resume'], rows[1]['session'])

    def test_real_subprocess_ask_gets_guidance_and_resumes_worker(self):
        script = self.root / "ask-agent"
        script.write_text(ASK_CLI)
        script.chmod(0o755)
        log = self.root / "ask-calls.jsonl"
        run = self.runner(master=d.RoleSpec("claude", "same"),
                          worker=d.RoleSpec("claude", "same"), tier="L2",
                          claude_path=str(script), codex_path="")
        done, errors = [], []
        run.completed.connect(done.append)
        run.failed.connect(errors.append)
        run.run()
        self.assertFalse(errors, errors)
        self.assertTrue(done[0]["ok"])
        self.assertEqual(done[0]["asks"], 1)
        rows = [json.loads(x) for x in log.read_text().splitlines()]
        self.assertEqual(len(rows), 5)
        # 规划(M) → 任务(W, 主动询问) → 指导(M) → 原会话续做(W) → 验收(M)
        self.assertIn("当前任务包", rows[1]["prompt"])
        self.assertIn("两种接口取舍", rows[2]["prompt"])
        self.assertIn("保留旧接口", rows[3]["prompt"])
        self.assertEqual(rows[3]["resume"], rows[1]["session"])
        self.assertIn("最终验收", rows[4]["prompt"])


FAKE_CLI = r'''#!/usr/bin/env python3
import json, pathlib, re, sys
prompt = sys.stdin.read()
args = sys.argv[1:]
log = pathlib.Path("calls.jsonl")
rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
resume = args[args.index("--resume") + 1] if "--resume" in args else args[-2] if "resume" in args else ""
session = resume or "session-" + str(len(rows) + 1)
with log.open("a") as output:
    output.write(json.dumps({"args": args, "prompt": prompt, "resume": resume}) + "\n")
if "你是执行 AI。先为下一个角色压缩有效上下文" in prompt:
    text = json.dumps({"summary": "有效上下文摘要", "constraints": ["早期约定", "末尾约定"],
                       "refs": [], "risks": []})
elif "只输出一个 JSON 对象" in prompt:
    text = json.dumps({"constraints": ["早期约定"], "tasks": [
        {"title": "task " + str(i), "points": 8, "role": "worker"} for i in range(3)]})
else:
    pathlib.Path("result.txt").write_text("verified")
    report = {"status": "passed", "summary": "done", "verification": ["result.txt checked"]}
    if "当前任务包：" in prompt:
        tasks = json.loads(prompt.split("当前任务包：\n")[1].split("\n\n")[0])
        report["tasks"] = [dict(report, id=t["id"], changed_files=["result.txt"],
                                decisions=["保持格式"], interfaces=["result.txt: UTF-8"], open_issues=[]) for t in tasks]
    text = "DUAL_RESULT: " + json.dumps(report)
if "exec" in args:
    print(json.dumps({"type": "thread.started", "thread_id": session}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}))
else:
    print(json.dumps({"type": "system", "subtype": "init", "session_id": session}))
    print(json.dumps({"type": "result", "result": text, "session_id": session}))
'''

ASK_CLI = r'''#!/usr/bin/env python3
import json, pathlib, sys
prompt = sys.stdin.read()
args = sys.argv[1:]
log = pathlib.Path("ask-calls.jsonl")
rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
resume = args[args.index("--resume") + 1] if "--resume" in args else ""
session = resume or "s-" + str(len(rows) + 1)
with log.open("a") as output:
    output.write(json.dumps({"args": args, "prompt": prompt,
                             "resume": resume, "session": session}) + "\n")
def resp(text):
    print(json.dumps({"type": "system", "subtype": "init", "session_id": session}))
    print(json.dumps({"type": "result", "result": text, "session_id": session}))
if "只输出一个 JSON 对象" in prompt:
    resp(json.dumps({"summary": "计划", "constraints": [], "tasks": [
        {"title": "实现", "ops": ["execute"], "points": 8}]}))
elif "你是主 AI（M）。执行 AI 在任务" in prompt:
    resp('DUAL_GUIDE: {"decision":"保留旧接口","next":["加适配层"],"done_when":"旧调用不受影响"}')
elif "主 AI 的二次指导" in prompt:
    report = {"status": "passed", "summary": "done",
              "verification": ["result.txt checked"], "tasks": [
                  {"id": "T1", "status": "passed", "summary": "done",
                   "changed_files": [], "decisions": [], "interfaces": [],
                   "verification": ["check"], "open_issues": []}]}
    resp("DUAL_RESULT: " + json.dumps(report, ensure_ascii=False))
elif "当前任务包：" in prompt:
    resp('DUAL_ASK: {"task":"T1","problem":"两种接口取舍","need":"选择兼容接口"}')
elif "最终验收" in prompt:
    resp("DUAL_RESULT: " + json.dumps({"status": "passed", "summary": "done",
                                       "verification": ["integration checked"]}))
else:
    resp("DUAL_RESULT: " + json.dumps({"status": "blocked", "summary": "unexpected",
                                       "verification": ["unexpected prompt"]}))
'''

if __name__ == "__main__":
    unittest.main()
