#!/usr/bin/env python3
"""Command-line peer for the local Codex VS Code agent; standard library only."""
import argparse
from collections import deque
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import uuid

HOME = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
STATE = Path(os.environ.get('CODEX_PEER_STATE', f'/tmp/codex-peer-{os.getuid()}'))
EXTENSIONS = Path(os.environ.get('CODEX_PEER_EXTENSIONS', str(Path.home() / '.vscode-server/extensions')))
CALLS = {'function_call', 'custom_tool_call'}
OUTPUTS = {'function_call_output', 'custom_tool_call_output'}
LIFECYCLE = {'task_started', 'task_complete', 'turn_aborted'}

class PeerError(Exception):
    pass

class RemoteError(PeerError):
    """The plugin explicitly rejected a request, unlike a transport timeout."""
    pass

# Malformed or version-shifted IPC replies surface as KeyError/TypeError; treat them as transport failures.
LIVE_ERRORS = (OSError, PeerError, ValueError, KeyError, TypeError)

def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

def load(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.peer-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(dump(value))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

@contextmanager
def database(name):
    path = HOME / name
    if not path.exists():
        raise PeerError(f'database missing: {path}')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()

def like(value):
    """Escape SQL LIKE wildcards so user text such as '_' or '%' matches literally."""
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

def threads(limit=12, query=None):
    with database('state_5.sqlite') as c:
        sql = 'SELECT id,title,cwd,rollout_path,updated_at,model FROM threads WHERE archived=0'
        values = []
        if query:
            sql += " AND (title LIKE ? ESCAPE '\\' OR cwd LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')"
            values += [f'%{like(query)}%'] * 3
        return [dict(row) for row in c.execute(sql + ' ORDER BY updated_at DESC LIMIT ?', values + [limit])]

def brief_threads(items):
    """Compact thread rows: enough to choose a UUID without spending tokens on paths."""
    return [{'id': t['id'], 'title': short(t.get('title') or '', 70), 'cwd': t.get('cwd'),
             'updated': time.strftime('%m-%d %H:%M', time.localtime(t['updated_at'])) if t.get('updated_at') else None}
            for t in items]

def fleet():
    return load(STATE / 'fleet.json', {})

def save_fleet(workers):
    save(STATE / 'fleet.json', workers)

def resolve(target=None):
    workers = fleet()
    if target and target in workers:
        target = workers[target]['thread']
    target = target or os.environ.get('CODEX_PEER_THREAD') or load(STATE / 'selected.json', {}).get('thread')
    if not target:
        try:
            candidates = brief_threads(threads(3))
        except (PeerError, sqlite3.Error):
            candidates = []
        raise PeerError({'error': 'No thread selected; run: codex-peer spawn --name worker1 "task", or codex-peer use UUID', 'fleet': workers, 'candidates': candidates})
    with database('state_5.sqlite') as c:
        select = 'SELECT id,title,cwd,rollout_path,updated_at,model FROM threads WHERE '
        found = c.execute(select + 'id=?', (target,)).fetchall()
        if not found:
            found = c.execute(select + "id LIKE ? ESCAPE '\\' OR title=?", (like(target) + '%', target)).fetchall()
    if len(found) != 1:
        raise PeerError(f'thread {target!r}: {len(found)} matches; use threads and a UUID')
    info = dict(found[0])
    info['log'] = Path(info.pop('rollout_path'))
    if not info['log'].is_file():
        raise PeerError(f"rollout unavailable: {info['log']}")
    return info

ITEM_KINDS = {'UserMessage': 'user_message', 'AgentMessage': 'agent_message'}

def normalize(row):
    """Map newer `item_completed` message records onto legacy user_message/agent_message events.

    Recent Codex builds no longer write event_msg user_message/agent_message; without this,
    sent markers are never observed and replies are invisible.
    """
    p = row.get('payload') if isinstance(row, dict) else None
    if not isinstance(p, dict) or row.get('type') != 'event_msg' or p.get('type') != 'item_completed':
        return row
    item = p.get('item') if isinstance(p.get('item'), dict) else {}
    kind = ITEM_KINDS.get(item.get('type'))
    if not kind:
        return row
    content = item.get('content') if isinstance(item.get('content'), list) else []
    message = ''.join(c.get('text', '') for c in content if isinstance(c, dict) and isinstance(c.get('text'), str))
    payload = {'type': kind, 'message': message, 'turn_id': p.get('turn_id')}
    if item.get('phase'):
        payload['phase'] = item['phase']
    return {**row, 'payload': payload}

def rows(path, offset=0):
    """Return only complete JSONL lines with byte offsets, preserving a partial tail."""
    with path.open('rb') as stream:
        stream.seek(offset)
        while True:
            start = stream.tell()
            line = stream.readline()
            if not line.endswith(b'\n'):
                return
            try:
                row = normalize(json.loads(line))
            except ValueError:
                row = {}
            if not isinstance(row, dict):
                row = {}
            yield start, stream.tell(), row

def text(value):
    return value if isinstance(value, str) else dump(value)

def short(value, width=480):
    if width == 0 or len(value) <= width:
        return value
    left = width * 2 // 3
    return value[:left] + ' … ' + value[-(width-left):]

def event(row, offset, width=480):
    p = row.get('payload', {})
    if not isinstance(p, dict):
        return None
    kind = p.get('type', '')
    result = {'at': offset, 'time': row.get('timestamp'), 'kind': kind}
    if row.get('type') == 'event_msg' and kind in LIFECYCLE | {'user_message', 'agent_message'}:
        if kind in {'user_message', 'agent_message'}:
            result['text'] = short(text(p.get('message', '')), width)
            if p.get('phase'):
                result['phase'] = p['phase']
        else:
            result['turn'] = p.get('turn_id')
            if kind == 'task_complete':
                result['text'] = short(text(p.get('last_agent_message', '')), width)
    elif row.get('type') == 'response_item' and kind in CALLS | OUTPUTS:
        result['call'] = p.get('call_id')
        result['text'] = short(text(p.get('arguments', p.get('input', p.get('output', '')))), width)
        if kind in CALLS:
            result['tool'] = p.get('name')
    else:
        return None
    return result

def matches(item, kind='all', query=None):
    if kind == 'messages' and item['kind'] not in {'user_message', 'agent_message'}:
        return False
    if kind == 'tools' and item['kind'] not in CALLS | OUTPUTS:
        return False
    return not query or query.casefold() in dump(item).casefold()

def history(info, args):
    items, end = deque(maxlen=args.limit), 0
    for start, end, row in rows(info['log']):
        item = event(row, start, 0)
        if item and matches(item, args.kind, args.query):
            if 'text' in item:
                item['text'] = short(item['text'], args.chars)
            items.append(item)
    return {'thread': info['id'], 'events': list(items)}, end

def pending(thread):
    try:
        with database('queue_1.sqlite') as c:
            return [dict(row) for row in c.execute('SELECT id,created_at_ms FROM queued_items WHERE thread_id=? ORDER BY queue_order', (thread,))]
    except (PeerError, sqlite3.Error):
        return None

def tail_offset(path, size):
    """First line start within the last `size` bytes, so large rollouts are not rescanned fully."""
    total = path.stat().st_size
    if total <= size:
        return 0
    with path.open('rb') as stream:
        stream.seek(total - size - 1)
        stream.readline()
        return stream.tell()

STATUS_TAIL = 2 * 1024 * 1024

def status(info, verbose=False):
    start_at = tail_offset(info['log'], STATUS_TAIL)
    result = scan_status(info, start_at)
    if start_at and (result['turn'] is None or 'last_message' not in result):
        result = scan_status(info, 0)
    result.update(pending_queue=pending(info['id']))
    if verbose:
        result['log'] = str(info['log'])
    else:
        result['title'] = short(result.get('title') or '', 70)
    return result

def scan_status(info, offset):
    result = {k: info[k] for k in ('id', 'title', 'cwd', 'model')}
    result['thread'] = result.pop('id')
    result.update(state='unknown', turn=None)
    for start, _, row in rows(info['log'], offset):
        p = row.get('payload', {})
        if not isinstance(p, dict):
            continue
        kind = p.get('type')
        if row.get('type') == 'event_msg':
            if kind == 'task_started':
                result.update(state='running', turn=p.get('turn_id'))
            elif kind in {'task_complete', 'turn_aborted'} and p.get('turn_id') == result['turn']:
                result['state'] = 'interrupted' if kind == 'turn_aborted' else 'idle'
                # Some Codex builds only record the final answer here, not as an agent_message.
                if kind == 'task_complete' and p.get('last_agent_message'):
                    result['last_message'] = short(text(p['last_agent_message']), 400)
            elif kind == 'agent_message':
                result['last_message'] = short(text(p.get('message', '')), 400)
            elif kind == 'user_message':
                result['last_user_message'] = short(text(p.get('message', '')), 240)
        item = event(row, start, 240)
        if item:
            result['last_event'] = item
        if row.get('type') == 'response_item' and kind in CALLS:
            result['last_tool'] = item
    return result

def extension():
    candidates = list(EXTENSIONS.glob('openai.chatgpt-*/out/extension.js'))
    if not candidates:
        raise PeerError('Codex extension not found')
    return max(candidates, key=lambda p: p.stat().st_mtime).parent.parent

def codex_binary():
    if os.environ.get('CODEX_PEER_BIN'):
        return os.environ['CODEX_PEER_BIN']
    try:
        binaries = list((extension() / 'bin').glob('*/codex'))
        if binaries:
            return str(binaries[0])
    except PeerError:
        pass
    if shutil.which('codex'):
        return shutil.which('codex')
    raise PeerError('Codex executable not found')

class IPC:
    """Local extension follower protocol, matched to installed extension versions."""
    def __init__(self):
        source = (extension() / 'out/extension.js').read_text()
        self.versions = {}
        for name in ('thread-owner-discovery', 'thread-follower-start-turn', 'thread-follower-steer-turn', 'thread-follower-interrupt-turn', 'thread-follower-read-model-settings'):
            found = re.search(r'"' + re.escape(name) + r'":(\d+)', source)
            if not found:
                raise PeerError(f'IPC version unavailable: {name}')
            self.versions[name] = int(found[1])
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(8)
        self.client = None
        try:
            self.socket.connect(str(HOME / 'ipc/ipc.sock'))
            self.client = self.request('initialize', {'clientType': 'gemini-codex-peer'})['result']['clientId']
        except BaseException:
            self.socket.close()
            raise

    def close(self):
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def write(self, message):
        body = dump(message).encode()
        self.socket.sendall(struct.pack('<I', len(body)) + body)

    def exact(self, count, deadline):
        result = bytearray()
        while len(result) < count:
            self.socket.settimeout(max(.01, deadline - time.monotonic()))
            chunk = self.socket.recv(count - len(result))
            if not chunk:
                raise PeerError('Codex IPC connection closed')
            result.extend(chunk)
        return bytes(result)

    def request(self, method, params, target=None):
        request = str(uuid.uuid4())
        message = {'type': 'request', 'requestId': request, 'method': method, 'params': params, 'version': self.versions.get(method, 0), 'timeoutMs': 6000}
        if self.client:
            message['sourceClientId'] = self.client
        if target:
            message['targetClientId'] = target
        self.write(message)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            size = struct.unpack('<I', self.exact(4, deadline))[0]
            if not 0 < size <= 256 * 1024 * 1024:
                raise PeerError('invalid IPC frame size')
            response = json.loads(self.exact(size, deadline))
            if response.get('type') == 'client-discovery-request':
                self.write({'type': 'client-discovery-response', 'requestId': response['requestId'], 'response': {'canHandle': False}})
            if response.get('requestId') == request and response.get('type') == 'response':
                if response.get('resultType') != 'success':
                    raise RemoteError(response.get('error', 'IPC request rejected'))
                return response
        raise PeerError('Codex IPC request timed out')

    def owner(self, thread):
        return self.request('thread-owner-discovery', {'hostId': 'local', 'conversationId': thread})['handledByClientId']

def receipt_path(message):
    if not re.fullmatch(r'[a-f0-9]{12}', message):
        raise PeerError('invalid message ID')
    return STATE / 'messages' / f'{message}.json'

def start_exec(command):
    """Launch a detached codex exec process and wait for its thread.started JSONL line."""
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    deadline = time.monotonic() + 20
    thread_id = None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get('type') == 'thread.started' and row.get('thread_id'):
            thread_id = row['thread_id']
            break
    if not thread_id:
        err = (proc.stderr.read() if proc.poll() is not None else '').strip()
        raise PeerError(f'failed to start codex exec: {err or "no thread.started event"}')
    for _ in range(40):
        try:
            return resolve(thread_id), proc.pid
        except PeerError:
            time.sleep(0.25)
    raise PeerError(f'thread {thread_id} started (pid {proc.pid}) but rollout not yet registered in SQLite')

def spawn(prompt, name=None, cwd=None, model=None):
    message_id = uuid.uuid4().hex[:12]
    marker = f'[Gemini:{message_id}]'
    content = marker + ' ' + prompt
    cmd = [codex_binary(), 'exec', '--skip-git-repo-check', '--dangerously-bypass-approvals-and-sandbox', '--json']
    if cwd:
        cmd += ['-C', str(cwd)]
    if model:
        cmd += ['-m', model]
    cmd.append(content)
    info, pid = start_exec(cmd)
    receipt = {'id': message_id, 'thread': info['id'], 'marker': marker, 'after': 0, 'delivery': 'accepted', 'transport': 'codex-exec', 'operation': 'spawn', 'pid': pid, 'sent_at': time.time()}
    save(receipt_path(message_id), receipt)
    save(STATE / f"last-{info['id']}.json", {'id': message_id})
    save(STATE / 'selected.json', {'thread': info['id']})
    if name:
        workers = fleet()
        workers[name] = {'thread': info['id'], 'cwd': info['cwd'], 'created': int(time.time())}
        save_fleet(workers)
        receipt['name'] = name
    return info, receipt

def send(info, message, mode='auto'):
    message_id = uuid.uuid4().hex[:12]
    marker = f'[Gemini:{message_id}]'
    content = marker + ' ' + message
    receipt = {'id': message_id, 'thread': info['id'], 'marker': marker, 'after': info['log'].stat().st_size, 'delivery': 'not_sent', 'sent_at': time.time()}
    path = receipt_path(message_id)
    save(path, receipt)
    client = None
    if mode not in {'queue', 'exec'}:
        try:
            client = IPC()
            owner = client.owner(info['id'])
        except LIVE_ERRORS as error:
            if client:
                client.close()
            client = None
            receipt['live_unavailable'] = str(error)
            if 'no-client-found' in str(error):
                receipt['hint'] = LIVE_HINT
            if mode == 'steer':
                receipt.update(delivery='not_sent', error=f'live connection unavailable: {error}')
                save(path, receipt)
                return receipt
    if client:
        try:
            operation = 'steer' if mode == 'steer' or status(info)['state'] == 'running' else 'start'
            request_id = str(uuid.uuid4())
            inputs = [{'type': 'text', 'text': content, 'text_elements': []}]
            if operation == 'steer':
                method = 'thread-follower-steer-turn'
                restore = {'id': request_id, 'cwd': info['cwd'], 'text': content, 'context': {'workspaceRoots': [info['cwd']], 'commentAttachments': []}}
                params = {'conversationId': info['id'], 'input': inputs, 'restoreMessage': restore, 'clientUserMessageId': request_id, 'attachments': []}
            else:
                method = 'thread-follower-start-turn'
                params = {'conversationId': info['id'], 'turnStart': {'request': {'threadId': info['id'], 'input': inputs, 'clientUserMessageId': request_id}, 'context': {'inheritThreadSettings': True}}}
            receipt.update(delivery='submitting', transport='vscode-ipc', operation=operation)
            save(path, receipt)
            try:
                response = client.request(method, params, owner)['result']
            except RemoteError as error:
                if mode != 'auto' or operation != 'steer' or not any(phrase in str(error).lower() for phrase in ('no active turn', 'active turn already ended')):
                    raise
                # Only retry when the plugin confirms that steering was rejected.
                method = 'thread-follower-start-turn'
                params = {'conversationId': info['id'], 'turnStart': {'request': {'threadId': info['id'], 'input': inputs, 'clientUserMessageId': request_id}, 'context': {'inheritThreadSettings': True}}}
                receipt['operation'] = 'start'
                response = client.request(method, params, owner)['result']
            inner = response.get('result', {})
            receipt.update(delivery='accepted', result=response, turn=inner.get('turnId') or inner.get('turn', {}).get('id'))
        except RemoteError as error:
            receipt.update(delivery='rejected', error=str(error))
        except LIVE_ERRORS as error:
            receipt.update(delivery='unconfirmed', error=str(error))
        finally:
            client.close()
    elif mode != 'queue' and status(info)['state'] != 'running':
        receipt.update(delivery='submitting', transport='codex-exec', operation='resume')
        save(path, receipt)
        try:
            cmd = [codex_binary(), 'exec', 'resume', info['id'], content, '--skip-git-repo-check', '--dangerously-bypass-approvals-and-sandbox', '--json']
            _, pid = start_exec(cmd)
            receipt.update(delivery='accepted', pid=pid)
            receipt.pop('live_unavailable', None)
            receipt.pop('hint', None)
        except (OSError, PeerError) as error:
            receipt.update(delivery='rejected', error=str(error))
    else:
        receipt.update(delivery='submitting', transport='codex-queue', operation='queue')
        save(path, receipt)
        try:
            outcome = subprocess.run([codex_binary(), 'queue', '--thread', info['id'], '--message', content], capture_output=True, text=True, timeout=15)
            receipt['delivery'] = 'queued' if outcome.returncode == 0 else 'rejected'
            receipt['result'] = short((outcome.stdout + outcome.stderr).strip(), 500)
        except (OSError, subprocess.TimeoutExpired, PeerError) as error:
            receipt.update(delivery='unconfirmed', error=str(error))
    save(path, receipt)
    save(STATE / f"last-{info['id']}.json", {'id': message_id})
    return receipt

def read_new(info, args):
    cursor = args.state or STATE / f"cursor-{info['id']}-{args.reader}.json"
    previous = load(cursor, {})
    if not previous:
        result, end = history(info, args)
        save(cursor, {'offset': end})
        return result
    offset = previous if isinstance(previous, int) else previous.get('offset', 0)
    if offset > info['log'].stat().st_size:
        offset = 0
    deadline = time.monotonic() + args.wait
    while True:
        items = []
        for start, end, row in rows(info['log'], offset):
            offset = end
            item = event(row, start, 0)
            if item and matches(item, args.kind, args.query):
                if 'text' in item:
                    item['text'] = short(item['text'], args.chars)
                items.append(item)
                if len(items) >= args.limit:
                    break
        save(cursor, {'offset': offset})
        if items or time.monotonic() >= deadline:
            return {'thread': info['id'], 'events': items, 'more': offset < info['log'].stat().st_size, 'state': 'new_events' if items else 'no_new_events'}
        time.sleep(.5)

FAILED = {'rejected', 'not_sent'}
SETTLE_SECONDS = 20
LIVE_HINT = 'thread is not open in a Codex VS Code panel; open it there for live delivery, otherwise messages are queued'

def wait_for(info, args, receipt=None):
    if args.after:
        receipt = load(receipt_path(args.after))
        if receipt is None:
            raise PeerError('message receipt not found')
    if receipt is None:
        last = load(STATE / f"last-{info['id']}.json", {})
        receipt = load(receipt_path(last['id'])) if last.get('id') else None
    if receipt and receipt['thread'] != info['id']:
        raise PeerError('message belongs to a different thread; pass --thread')
    message = receipt['id'] if receipt else None
    cursor = STATE / (f"wait-{receipt['id']}.json" if receipt else f"wait-thread-{info['id']}.json")
    previous = load(cursor, {})
    offset = previous.get('offset', receipt['after'] if receipt else info['log'].stat().st_size)
    marker = receipt['marker'] if receipt else None
    observed = previous.get('observed', marker is None)
    current_turn = previous.get('turn', receipt.get('turn') if receipt else None)
    if offset > info['log'].stat().st_size:
        raise PeerError('log shrank; wait cursor is no longer valid')
    if receipt and not observed and receipt.get('delivery') in FAILED:
        # A rejected message never appears in the log; waiting on it would only burn the timeout.
        return {'message': message, 'state': 'not_delivered', 'delivery': receipt['delivery'], 'error': receipt.get('error')}
    def checkpoint():
        save(cursor, {'offset': offset, 'observed': observed, 'turn': current_turn})
    deadline = time.monotonic() + args.wait
    if not receipt and args.until == 'done':
        current = status(info)
        if current['state'] in {'idle', 'interrupted'}:
            return {'state': current['state'], 'last_message': current.get('last_message')}
    progress = None
    next_check = 0
    while True:
        for start, end, row in rows(info['log'], offset):
            offset = end
            p = row.get('payload', {})
            if not isinstance(p, dict):
                continue
            kind = p.get('type')
            if row.get('type') == 'event_msg' and kind == 'task_started':
                current_turn = p.get('turn_id')
            if marker and row.get('type') == 'event_msg' and kind == 'user_message' and marker in text(p.get('message', '')):
                observed = True
            item = event(row, start, args.chars)
            if not observed or not item:
                continue
            ended = kind in {'task_complete', 'turn_aborted'}
            # A reply wait also stops when the turn ends, so an aborted turn does not hang until timeout.
            ready = args.until == 'event' or (args.until == 'reply' and (kind == 'agent_message' or ended)) or (args.until == 'done' and ended)
            if ready:
                checkpoint()
                result = {'message': message, 'state': 'observed', 'turn': current_turn, 'event': item}
                if args.until != 'done' and not ended:
                    result['next'] = 'codex-peer wait'
                return result
            if kind == 'agent_message':
                progress = {'at': item['at'], 'text': short(item.get('text', ''), 240)}
            elif kind in CALLS:
                progress = {'at': item['at'], 'tool': item.get('tool'), 'text': short(item.get('text', ''), 120)}
        if receipt and time.monotonic() >= next_check:
            next_check = time.monotonic() + 15
            # Nothing left to wait for: Codex is idle, nothing is queued, and the message is not fresh.
            current = status(info)
            if current['state'] in {'idle', 'interrupted'} and not current.get('pending_queue') and time.time() - receipt.get('sent_at', 0) > SETTLE_SECONDS:
                checkpoint()
                return {'message': message, 'state': current['state'], 'message_observed': observed, 'last_message': current.get('last_message')}
        if time.monotonic() >= deadline:
            checkpoint()
            result = {'message': message, 'state': 'timeout', 'message_observed': observed, 'next': 'codex-peer wait'}
            if receipt:
                result['delivery'] = receipt.get('delivery')
            if progress:
                result['progress'] = progress
            return result
        time.sleep(.5 if args.wait <= 60 else 1)

def doctor(info):
    result = {'thread': info['id'], 'log': str(info['log']), 'codex': codex_binary(), 'live': False}
    try:
        with IPC() as client:
            owner = client.owner(info['id'])
            settings = client.request('thread-follower-read-model-settings', {'conversationId': info['id']}, owner)['result']
            result.update(live=True, owner=owner, settings=settings)
    except LIVE_ERRORS as error:
        result['live_error'] = str(error)
        if 'no-client-found' in str(error):
            result['hint'] = LIVE_HINT
    return result

def interrupt(info):
    current = status(info)
    if current['state'] != 'running' or not current['turn']:
        return {'thread': info['id'], 'state': current['state'], 'interrupted': False}
    with IPC() as client:
        owner = client.owner(info['id'])
        result = client.request('thread-follower-interrupt-turn', {'conversationId': info['id'], 'mode': 'user-stop', 'expectedTurnId': current['turn']}, owner)
    return {'thread': info['id'], 'result': result['result']}

def fleet_status(clean=False):
    workers = fleet()
    if clean:
        save_fleet({})
        return {'fleet': [], 'cleaned': True}
    items = []
    for name, meta in workers.items():
        try:
            info = resolve(meta['thread'])
            st = status(info)
            items.append({'name': name, 'thread': info['id'], 'state': st['state'], 'cwd': info['cwd'], 'last_message': short(st.get('last_message') or '', 160)})
        except PeerError as error:
            items.append({'name': name, 'thread': meta['thread'], 'state': 'missing', 'error': str(error)})
    return {'fleet': items}

def compact(receipt):
    """Caller-facing receipt; the full record (marker, raw IPC result) stays on disk."""
    keys = ('id', 'name', 'thread', 'delivery', 'operation', 'turn', 'pid', 'error', 'live_unavailable', 'hint')
    result = {k: receipt[k] for k in keys if receipt.get(k)}
    if receipt.get('delivery') == 'rejected' and receipt.get('transport') == 'codex-queue':
        result['result'] = receipt.get('result')
    return result

MAX_WAIT = 3600

def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('action', choices=('spawn', 'fleet', 'threads', 'use', 'status', 'read', 'history', 'event', 'send', 'ask', 'wait', 'interrupt', 'doctor'))
    ap.add_argument('message', nargs='?', help="message, UUID/alias for use, offset for event; '-' reads stdin")
    ap.add_argument('--name', help='worker alias in fleet (for spawn) or passed to --thread')
    ap.add_argument('--thread', help='worker alias, UUID, unique UUID prefix, or exact title')
    ap.add_argument('--cwd', help='working directory for spawn')
    ap.add_argument('--model', help='model override for spawn')
    ap.add_argument('--clean', action='store_true', help='clear fleet registry (with fleet action)')
    ap.add_argument('--wait', type=float, default=None, help=f'seconds, 0..{MAX_WAIT}; ask/wait default 45, spawn default 0')
    ap.add_argument('--after', help='message ID returned by send/ask/spawn')
    ap.add_argument('--until', choices=('reply', 'done', 'event'), default='done', help='ask/wait/spawn stop condition (default done)')
    ap.add_argument('--mode', choices=('auto', 'steer', 'queue', 'exec'), default='auto')
    ap.add_argument('--limit', type=int, default=12)
    ap.add_argument('--chars', type=int, default=480, help='characters per event; 0 = full')
    ap.add_argument('--full', action='store_true', help='full text; also verbose status')
    ap.add_argument('--kind', choices=('all', 'messages', 'tools'), default='all')
    ap.add_argument('--query', help='case-insensitive history/read search, or threads search')
    ap.add_argument('--reader', default='gemini', help='independent cursor name')
    ap.add_argument('--state', type=Path, help='custom read cursor path')
    return ap

def main(argv=None):
    ap = parser()
    args = ap.parse_args(argv)
    args.wait = (45 if args.action in {'ask', 'wait'} else 0) if args.wait is None else args.wait
    if not 0 <= args.wait <= MAX_WAIT or args.limit < 1 or args.chars < 0 or not re.fullmatch(r'[\w-]+', args.reader):
        ap.error(f'wait: 0..{MAX_WAIT}; limit > 0; chars >= 0; reader uses letters/digits/_/-')
    if args.name and not re.fullmatch(r'[\w-]+', args.name):
        ap.error('name uses letters/digits/_/-')
    if args.full:
        args.chars = 0
    if args.action == 'threads':
        return brief_threads(threads(args.limit, args.query))
    if args.action == 'fleet':
        return fleet_status(args.clean)
    if args.action == 'spawn':
        if args.message == '-':
            args.message = sys.stdin.read()
        if not args.message or not args.message.strip():
            ap.error("spawn requires initial task prompt or '-' for stdin")
        info, receipt = spawn(args.message, name=args.name, cwd=args.cwd, model=args.model)
        if args.wait == 0:
            return compact(receipt)
        return {'spawned': compact(receipt), 'reply': wait_for(info, args, receipt)}
    if args.action == 'use':
        target = args.message or args.name
        if not target:
            ap.error('use requires a worker name, UUID, or unique prefix')
        info = resolve(target)
        save(STATE / 'selected.json', {'thread': info['id']})
        return {'thread': info['id'], 'title': short(info['title'] or '', 70)}
    info = resolve(args.thread or args.name)
    if args.action == 'status':
        return status(info, verbose=args.full)
    if args.action == 'doctor':
        return doctor(info)
    if args.action == 'interrupt':
        return interrupt(info)
    if args.action == 'history':
        return history(info, args)[0]
    if args.action == 'read':
        return read_new(info, args)
    if args.action == 'event':
        if args.message is None or not args.message.isdigit():
            ap.error("event requires the numeric 'at' offset")
        for start, _, row in rows(info['log'], int(args.message)):
            item = event(row, start, 0)
            if item:
                return item
            break
        raise PeerError('no public event at that offset')
    if args.action in {'send', 'ask'}:
        if args.message == '-':
            args.message = sys.stdin.read()
        if not args.message or not args.message.strip():
            ap.error("send/ask requires message text or '-' for stdin")
        receipt = send(info, args.message, args.mode)
        if args.action == 'send' or receipt['delivery'] in {'rejected', 'not_sent', 'unconfirmed'}:
            return compact(receipt)
        return {'sent': compact(receipt), 'reply': wait_for(info, args, receipt)}
    return wait_for(info, args)

if __name__ == '__main__':
    try:
        result = main()
        print(dump(result), flush=True)
        if isinstance(result, dict) and result.get('delivery') in {'rejected', 'not_sent', 'unconfirmed'}:
            sys.exit(1)
    except (PeerError, OSError, sqlite3.Error, ValueError, KeyError) as error:
        detail = error.args[0] if error.args and isinstance(error.args[0], dict) else {'error': str(error)}
        print(dump(detail), file=sys.stderr)
        sys.exit(1)
