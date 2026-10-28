"""Durable task state and append-only event history. No model calls here."""
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Store:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get('HARNESS_HOME', '~/.local/share/local-harness')).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(self.home / 'tasks.sqlite3'), timeout=10)
        os.chmod(self.home / 'tasks.sqlite3', 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, workspace TEXT NOT NULL, provider TEXT NOT NULL,
            prompt TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
            data TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, text TEXT NOT NULL,
            provider TEXT, at TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, contract_hash TEXT NOT NULL,
            target TEXT NOT NULL, question TEXT NOT NULL, status TEXT NOT NULL,
            reply TEXT, attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
            error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
            UNIQUE(task_id,contract_hash,target,question));
        ''')

    def create(self, workspace, provider, prompt):
        task_id = uuid.uuid4().hex[:12]
        stamp = now()
        data = {'contract': None, 'feedback': [], 'attempt': 0, 'pending': None,
                'sessions': {}, 'evidence': [], 'reason': '', 'contract_history': []}
        with self.db:
            self.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)',
                            (task_id, str(Path(workspace).resolve()), provider, prompt,
                             'received', 0, json.dumps(data), stamp, stamp))
            self._event(task_id, 'received', {'prompt': prompt})
        return self.get(task_id)

    def get(self, task_id):
        row = self.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown task: ' + task_id)
        item = dict(row)
        item['data'] = json.loads(item['data'])
        return item

    def list(self):
        return [dict(r) for r in self.db.execute(
            'SELECT id,workspace,provider,status,revision,updated FROM tasks ORDER BY updated DESC')]

    def _event(self, task_id, kind, payload):
        self.db.execute('INSERT INTO events(task_id,kind,payload,at) VALUES(?,?,?,?)',
                        (task_id, kind, json.dumps(payload, ensure_ascii=False), now()))

    def save(self, task, status, kind, payload=None, consumed=()):
        with self.db:
            changed = self.db.execute(
                'UPDATE tasks SET status=?,revision=revision+1,data=?,provider=?,updated=? WHERE id=? AND revision=?',
                (status, json.dumps(task['data'], ensure_ascii=False), task['provider'], now(),
                 task['id'], task['revision'])).rowcount
            if changed != 1:
                raise RuntimeError('Task changed concurrently; reload before continuing')
            self._event(task['id'], kind, payload or {})
            for item_id in consumed:
                self.db.execute('UPDATE inbox SET consumed=1 WHERE id=? AND task_id=?', (item_id, task['id']))
        task.update(self.get(task['id']))

    def events(self, task_id):
        self.get(task_id)
        return [dict(r, payload=json.loads(r['payload'])) for r in self.db.execute(
            'SELECT seq,kind,payload,at FROM events WHERE task_id=? ORDER BY seq', (task_id,))]

    def enqueue(self, task_id, text, provider):
        self.get(task_id)
        with self.db:
            row = self.db.execute('INSERT INTO inbox(task_id,text,provider,at) VALUES(?,?,?,?)',
                                  (task_id, text, provider, now()))
            self._event(task_id, 'feedback_queued', {'id': row.lastrowid, 'text': text, 'provider': provider})
        return row.lastrowid

    def inbox(self, task_id):
        return [dict(r) for r in self.db.execute('SELECT * FROM inbox WHERE task_id=? AND consumed=0 ORDER BY id', (task_id,))]

    def send(self, task, target, question):
        scope = task['data'].get('consultation_scope', task['data']['contract_hash'])
        existing = self.db.execute('SELECT * FROM messages WHERE task_id=? AND contract_hash=? AND target=? AND question=?',
                                  (task['id'], scope, target, question)).fetchone()
        if existing:
            raise ValueError('The same consultation was already requested; use its recorded reply')
        message_id, stamp = uuid.uuid4().hex, now()
        with self.db:
            self.db.execute('INSERT INTO messages(id,task_id,contract_hash,target,question,status,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                            (message_id, task['id'], scope, target, question, 'queued', stamp, stamp))
            self._event(task['id'], 'message_queued', {'id': message_id, 'target': target, 'question': question})
        return message_id

    def messages(self, task_id, contract=None):
        sql, values = 'SELECT * FROM messages WHERE task_id=?', [task_id]
        if contract:
            sql += ' AND contract_hash=?'
            values.append(contract)
        return [dict(r) for r in self.db.execute(sql + ' ORDER BY created', values)]

    def message_state(self, message, status, reply=None, error=None, retry_at=0):
        with self.db:
            self.db.execute('UPDATE messages SET status=?,reply=COALESCE(?,reply),error=?,retry_at=?,attempts=attempts+?,updated=? WHERE id=?',
                            (status, reply, error, retry_at, int(status == 'in_flight'), now(), message['id']))
            self._event(message['task_id'], 'message_' + status, {'id': message['id'], 'target': message['target'], 'error': error})

    @contextlib.contextmanager
    def lock(self, key):
        name = hashlib.sha256(key.encode()).hexdigest()
        path = self.home / (name + '.lock')
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Another harness process owns this task or workspace')
            yield
        finally:
            os.close(fd)

    def close(self):
        self.db.close()

    def preference(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)', (key, value))
        row = self.db.execute('SELECT value FROM preferences WHERE key=?', (key,)).fetchone()
        return row[0] if row else None
