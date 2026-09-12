import hashlib, json, re, sqlite3, time
from contextlib import contextmanager
from pathlib import Path

def redact(value, secrets=()):
    if isinstance(value,dict): return {k:('[REDACTED]' if re.search(r'secret|password|api.?key|authorization|private.?key',k,re.I) else redact(v,secrets)) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [redact(v,secrets) for v in value]
    if isinstance(value,str):
        for secret in secrets:
            if secret: value=value.replace(secret,'[REDACTED]')
        value=re.sub(r'Bearer\s+\S+','Bearer [REDACTED]',value,flags=re.I)
    return value

def fingerprint(intent):
    return hashlib.sha256(json.dumps(intent,sort_keys=True,separators=(',',':')).encode()).hexdigest()

class Database:
    def __init__(self,path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True); self.secrets=[]
    @contextmanager
    def connect(self):
        conn=sqlite3.connect(self.path,timeout=30); conn.row_factory=sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback(); raise
        finally: conn.close()
    def initialize(self):
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,data TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS queue(id INTEGER PRIMARY KEY,fingerprint TEXT UNIQUE NOT NULL,intent TEXT NOT NULL,reason TEXT NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS action_reservations(fingerprint TEXT PRIMARY KEY,action TEXT NOT NULL,scope TEXT NOT NULL,created_at REAL NOT NULL);''')
    def get_setting(self,key,default=None):
        with self.connect() as c: row=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set_setting(self,key,value):
        with self.connect() as c: c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value)))
    def log(self,kind,data):
        with self.connect() as c: return c.execute('INSERT INTO events(kind,data,created_at) VALUES(?,?,?)',(kind,json.dumps(redact(data,self.secrets)),time.time())).lastrowid
    def events(self,kind=None,limit=100):
        with self.connect() as c:
            rows=c.execute('SELECT * FROM events '+('WHERE kind=? ' if kind else '')+'ORDER BY id DESC LIMIT ?',((kind,) if kind else ())+(min(max(limit,1),1000),)).fetchall()
        return [dict(r)|{'data':json.loads(r['data'])} for r in rows]
    def llm_tokens_since(self, cutoff):
        with self.connect() as c:
            rows=c.execute("SELECT data FROM events WHERE kind='llm' AND created_at>=?",(cutoff,)).fetchall()
        total=0
        for row in rows:
            data=json.loads(row[0])
            total += int(data.get('prompt_tokens') or 0) + int(data.get('output_tokens') or 0)
        return total
    def queue(self,intent,reason):
        clean=redact(intent,self.secrets); fp=fingerprint(clean)
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO queue(fingerprint,intent,reason,status,created_at) VALUES(?,?,?,?,?)',(fp,json.dumps(clean),reason,'pending',time.time()))
            return c.execute('SELECT id FROM queue WHERE fingerprint=?',(fp,)).fetchone()[0]
    def queue_items(self,status='pending'):
        with self.connect() as c: rows=c.execute('SELECT * FROM queue WHERE status=? ORDER BY id',(status,)).fetchall()
        return [dict(r)|{'intent':json.loads(r['intent'])} for r in rows]
    def get_queue(self,id):
        with self.connect() as c: r=c.execute('SELECT * FROM queue WHERE id=?',(id,)).fetchone()
        return dict(r)|{'intent':json.loads(r['intent'])} if r else None
    def resolve_queue(self,id,status):
        allowed={'approved':'pending','rejected':'pending','sent':'executing','blocked':'executing','error':'executing','uncertain':'executing'}
        if status not in allowed: raise ValueError('Invalid transition')
        with self.connect() as c: return c.execute('UPDATE queue SET status=? WHERE id=? AND status=?',(status,id,allowed[status])).rowcount==1
    def claim_queue(self,id):
        with self.connect() as c: return c.execute("UPDATE queue SET status='executing' WHERE id=? AND status='approved'",(id,)).rowcount==1
    def edit_queue(self,id,intent):
        from .models import Intent
        clean=redact(Intent.model_validate(intent).model_dump(exclude_none=True),self.secrets)
        with self.connect() as c: return c.execute("UPDATE queue SET intent=?,fingerprint=? WHERE id=? AND status='pending'",(json.dumps(clean),fingerprint(clean),id)).rowcount==1
    def reserve_action(self,intent,now=None):
        now=time.time() if now is None else now; action=intent['action']; fp=fingerprint(intent)
        scope=str(intent.get('slug','')) if action=='propose' else ''
        limit={'post':1,'comment':20,'vote':50,'submit':10,'propose':3,'tag':50,'cadence':1,'porch':20}.get(action,0)
        start=now-86400 if action in {'submit','propose'} else now-now%86400
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM action_reservations WHERE fingerprint=?',(fp,)).fetchone(): return False
            count=c.execute('SELECT COUNT(*) FROM action_reservations WHERE action=? AND scope=? AND created_at>=?',(action,scope,start)).fetchone()[0]
            if count>=limit: return False
            c.execute('INSERT INTO action_reservations VALUES(?,?,?,?)',(fp,action,scope,now)); return True
    def release_action(self,intent):
        """Only call on definite rejection, never on uncertain transport failures."""
        with self.connect() as c: c.execute('DELETE FROM action_reservations WHERE fingerprint=?',(fingerprint(intent),))
