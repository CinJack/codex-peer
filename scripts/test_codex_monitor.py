"""Regression checks for cursors, reply correlation, and native delivery decisions."""
import importlib.util
import json
from pathlib import Path
import socket
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('peer', Path(__file__).with_name('codex_monitor.py'))
peer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer)


class PeerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.log = self.root / 'rollout.jsonl'
        self.log.touch()
        self.info = dict(id='fixture', title='test', cwd=str(self.root), model='test', log=self.log)
        self.patch = patch.object(peer, 'STATE', self.root / 'state')
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def append(self, kind, **payload):
        with self.log.open('a') as stream:
            stream.write(json.dumps({'type': 'event_msg', 'payload': {'type': kind, **payload}}) + '\n')

    def args(self, **values):
        args = dict(limit=2, chars=480, kind='all', query=None, wait=0, reader='test', state=None, after=None, until='reply')
        args.update(values)
        return SimpleNamespace(**args)

    def test_read_preserves_partial_lines_and_paginates_without_loss(self):
        self.append('agent_message', message='initial')
        peer.read_new(self.info, self.args())
        partial = json.dumps({'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'one'}})
        with self.log.open('a') as stream:
            stream.write(partial)
        self.assertEqual(peer.read_new(self.info, self.args())['events'], [])
        with self.log.open('a') as stream:
            stream.write('\n')
        self.append('agent_message', message='two')
        self.append('agent_message', message='three')
        page = peer.read_new(self.info, self.args())
        self.assertEqual([x['text'] for x in page['events']], ['one', 'two'])
        self.assertTrue(page['more'])
        self.assertEqual([x['text'] for x in peer.read_new(self.info, self.args())['events']], ['three'])

    def test_history_search_uses_full_text_before_truncation(self):
        self.append('agent_message', message='a' * 1000 + 'NEEDLE' + 'z' * 1000)
        result, _ = peer.history(self.info, self.args(chars=20, query='needle'))
        self.assertEqual(len(result['events']), 1)
        self.assertLess(len(result['events'][0]['text']), 30)

    def test_wait_ignores_unrelated_output_and_advances_across_calls(self):
        receipt = dict(id='0123456789ab', thread='fixture', after=0, marker='[Gemini:0123456789ab]', delivery='accepted', turn='turn1')
        self.append('agent_message', message='unrelated')
        self.append('user_message', message='[Gemini:0123456789ab] hello')
        self.append('agent_message', message='first')
        self.append('agent_message', message='second')
        self.append('task_complete', turn_id='turn1', last_agent_message='done')
        first = peer.wait_for(self.info, self.args(), receipt)
        second = peer.wait_for(self.info, self.args(), receipt)
        done = peer.wait_for(self.info, self.args(until='done'), receipt)
        self.assertEqual(first['event']['text'], 'first')
        self.assertEqual(second['event']['text'], 'second')
        self.assertEqual(first['turn'], 'turn1')
        self.assertEqual(done['event']['kind'], 'task_complete')
        self.assertEqual(peer.wait_for(self.info, self.args(), receipt)['state'], 'timeout')

    def test_status_distinguishes_running_finished_and_interrupted(self):
        with patch.object(peer, 'pending', return_value=[]):
            self.append('task_started', turn_id='a')
            self.assertEqual(peer.status(self.info)['state'], 'running')
            self.append('task_complete', turn_id='old')
            self.assertEqual(peer.status(self.info)['state'], 'running')
            self.append('task_complete', turn_id='a')
            self.assertEqual(peer.status(self.info)['state'], 'idle')
            self.append('task_started', turn_id='b')
            self.append('turn_aborted', turn_id='b')
            self.assertEqual(peer.status(self.info)['state'], 'interrupted')

    def test_live_send_and_idle_start(self):
        requests = []
        class FakeIPC:
            def owner(self, thread):
                return 'owner'
            def close(self):
                pass
            def request(self, method, params, owner):
                requests.append((method, params))
                return {'result': {'result': {'turnId': 'turn1'}}}
        with patch.object(peer, 'IPC', FakeIPC), patch.object(peer, 'pending', return_value=[]):
            self.append('task_started', turn_id='turn1')
            active = peer.send(self.info, 'active message')
            self.assertEqual(active['delivery'], 'accepted')
            self.assertEqual(active['operation'], 'steer')
            self.assertIn(active['marker'], requests[-1][1]['input'][0]['text'])
            self.append('task_complete', turn_id='turn1')
            idle = peer.send(self.info, 'idle message')
            self.assertEqual(idle['operation'], 'start')
            self.assertTrue(requests[-1][1]['turnStart']['context']['inheritThreadSettings'])

    def test_unknown_delivery_is_not_automatically_resent(self):
        class TimeoutIPC:
            def owner(self, thread):
                return 'owner'
            def close(self):
                pass
            def request(self, *args):
                raise TimeoutError('timeout after submit')
        with patch.object(peer, 'IPC', TimeoutIPC), patch.object(peer, 'pending', return_value=[]), patch.object(peer.subprocess, 'run') as queue:
            result = peer.send(self.info, 'message')
            self.assertEqual(result['delivery'], 'unconfirmed')
            queue.assert_not_called()

    def test_explicit_ended_turn_rejection_can_start_next_turn(self):
        operations = []
        class RaceIPC:
            def owner(self, thread):
                return 'owner'
            def close(self):
                pass
            def request(self, method, *args):
                operations.append(method)
                if method.endswith('steer-turn'):
                    raise peer.RemoteError('no active turn to steer')
                return {'result': {'result': {'turn': {'id': 'next'}}}}
        with patch.object(peer, 'IPC', RaceIPC), patch.object(peer, 'pending', return_value=[]):
            self.append('task_started', turn_id='turn1')
            result = peer.send(self.info, 'message')
            self.assertEqual(result['operation'], 'start')
            self.assertEqual(result['turn'], 'next')
            self.assertEqual(len(operations), 2)

    def test_ipc_reader_handles_split_frames(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        client = object.__new__(peer.IPC)
        client.socket = left
        client.client = None
        client.versions = {}
        # A fixed request UUID allows a complete wire-level round trip without a server.
        response = json.dumps({'type': 'response', 'requestId': 'id', 'resultType': 'success', 'result': {'ok': True}}).encode()
        frame = struct.pack('<I', len(response)) + response
        right.sendall(frame[:2])
        right.sendall(frame[2:9])
        right.sendall(frame[9:])
        with patch.object(peer.uuid, 'uuid4', return_value='id'):
            self.assertTrue(client.request('initialize', {})['result']['ok'])

    def test_status_tail_scan_matches_full_scan_and_falls_back(self):
        with patch.object(peer, 'pending', return_value=[]), patch.object(peer, 'STATUS_TAIL', 200):
            self.append('task_started', turn_id='a')
            self.append('agent_message', message='early')
            for _ in range(20):
                self.append('user_message', message='x' * 50)
            # Tail lacks task_started and agent_message, so status must rescan the whole log.
            result = peer.status(self.info)
            self.assertEqual(result['turn'], 'a')
            self.assertEqual(result['last_message'], 'early')
            self.append('task_complete', turn_id='a')
            self.assertEqual(peer.status(self.info)['state'], 'idle')

    def test_reply_wait_stops_when_turn_is_aborted(self):
        receipt = dict(id='0123456789ab', thread='fixture', after=0, marker='[Gemini:0123456789ab]', delivery='accepted')
        self.append('user_message', message='[Gemini:0123456789ab] hi')
        self.append('turn_aborted', turn_id='t')
        self.assertEqual(peer.wait_for(self.info, self.args(), receipt)['event']['kind'], 'turn_aborted')

    def test_rejected_message_does_not_wait_and_timeout_reports_progress(self):
        rejected = dict(id='0123456789ab', thread='fixture', after=0, marker='[Gemini:0123456789ab]', delivery='rejected')
        self.assertEqual(peer.wait_for(self.info, self.args(wait=30), rejected)['state'], 'not_delivered')
        receipt = dict(id='abcdefabcdef', thread='fixture', after=0, marker='[Gemini:abcdefabcdef]', delivery='accepted')
        self.append('user_message', message='[Gemini:abcdefabcdef] go')
        self.append('agent_message', message='working on it')
        receipt['sent_at'] = peer.time.time()
        result = peer.wait_for(self.info, self.args(until='done'), receipt)
        self.assertEqual(result['state'], 'timeout')
        self.assertEqual(result['progress']['text'], 'working on it')

    def test_ipc_shape_errors_fall_back_instead_of_crashing(self):
        class BrokenIPC:
            def __init__(self):
                raise KeyError('clientId')
        outcome = SimpleNamespace(returncode=0, stdout='ok', stderr='')
        with patch.object(peer, 'IPC', BrokenIPC), patch.object(peer, 'codex_binary', return_value='codex'), patch.object(peer.subprocess, 'run', return_value=outcome):
            self.assertEqual(peer.send(self.info, 'message', mode='queue')['delivery'], 'queued')
        with patch.object(peer, 'IPC', BrokenIPC), patch.object(peer, 'codex_binary', return_value='codex'), patch.object(peer, 'start_exec', return_value=(self.info, 1234)), patch.object(peer, 'pending', return_value=[]):
            resumed = peer.send(self.info, 'message', mode='auto')
            self.assertEqual(resumed['delivery'], 'accepted')
            self.assertEqual(resumed['operation'], 'resume')

    def test_spawn_and_fleet_registration(self):
        with patch.object(peer, 'codex_binary', return_value='codex'), patch.object(peer, 'start_exec', return_value=(self.info, 9999)):
            info, receipt = peer.spawn('do task', name='worker1', cwd='/tmp')
            self.assertEqual(receipt['name'], 'worker1')
            self.assertEqual(receipt['operation'], 'spawn')
            self.assertEqual(peer.fleet()['worker1']['thread'], 'fixture')
        with patch.object(peer, 'resolve', return_value=self.info), patch.object(peer, 'pending', return_value=[]):
            status = peer.fleet_status()
            self.assertEqual(len(status['fleet']), 1)
            self.assertEqual(status['fleet'][0]['name'], 'worker1')

    def test_stale_receipt_on_idle_thread_returns_immediately(self):
        stale = dict(id='0123456789ab', thread='fixture', after=0, marker='[Gemini:0123456789ab]', delivery='accepted', sent_at=0)
        self.append('task_started', turn_id='a')
        self.append('task_complete', turn_id='a')
        with patch.object(peer, 'pending', return_value=[]):
            result = peer.wait_for(self.info, self.args(wait=30, until='done'), stale)
        self.assertEqual(result['state'], 'idle')
        self.assertFalse(result['message_observed'])

    def test_item_completed_messages_are_observed(self):
        def item(kind, value, **extra):
            row = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'turn_id': 't', 'item': {'type': kind, 'content': [{'type': 'text', 'text': value}], **extra}}}
            with self.log.open('a') as stream:
                stream.write(json.dumps(row) + '\n')
        receipt = dict(id='0123456789ab', thread='fixture', after=0, marker='[Gemini:0123456789ab]', delivery='accepted', sent_at=peer.time.time())
        self.append('task_started', turn_id='t')
        item('UserMessage', '[Gemini:0123456789ab] hi')
        item('AgentMessage', 'on it', phase='commentary')
        result = peer.wait_for(self.info, self.args(), receipt)
        self.assertEqual(result['event']['text'], 'on it')
        self.assertEqual(result['event']['phase'], 'commentary')
        with patch.object(peer, 'pending', return_value=[]):
            self.assertEqual(peer.status(self.info)['last_message'], 'on it')

    def test_like_escapes_wildcards(self):
        self.assertEqual(peer.like('a_b%c'), 'a\\_b\\%c')

    def test_compact_receipt_hides_raw_fields(self):
        receipt = dict(id='x', delivery='accepted', operation='start', marker='m', after=1, result={'big': 1}, turn='t')
        self.assertEqual(peer.compact(receipt), {'id': 'x', 'delivery': 'accepted', 'operation': 'start', 'turn': 't'})


if __name__ == '__main__':
    unittest.main()
