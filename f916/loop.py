"""Persistent worker for observation, decisions, approvals and scheduled audits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from defense.ingest import inspect_content
from f916.skills import run as run_skill
from invariants import redact
from .actions import Executor
from .brain import Brain
from .client import Client
from .config import Settings
from .db import Database
from .radar import summarize


def _list(data, key):
    if isinstance(data, list): return data
    if isinstance(data, dict) and isinstance(data.get(key), list): return data[key]
    return []


def _compact(rows, fields, text_limit=1200, limit=20):
    result=[]
    for row in rows[:limit]:
        if not isinstance(row,dict): continue
        item={}
        for field in fields:
            value=row.get(field)
            if isinstance(value,str): value=value[:text_limit]
            if value is not None: item[field]=value
        result.append(item)
    return result


def _inbox(me):
    since = me.get("since_last_visit", {}) if isinstance(me, dict) else {}
    rows=[]
    for bucket in ("replies","comments_on_your_posts","in_threads_you_joined","mentions_of_you"):
        for item in since.get(bucket, []) if isinstance(since, dict) else []:
            if isinstance(item,dict): rows.append({"bucket":bucket, **item})
    return _compact(rows, ("bucket","id","post_id","comment_id","author","title","body","created_at"), 1500, 30)


class Worker:
    def __init__(self, settings, db, client, brain, executor=None):
        self.settings, self.db, self.client, self.brain = settings, db, client, brain
        self.executor = executor or Executor(settings, db, client)

    def process_approved(self):
        for item in self.db.queue_items("approved"):
            if not self.db.claim_queue(item["id"]): continue
            try:
                result = self.executor.dispatch(item["intent"], approved=True)
                status = result.get("status", "error")
                if status not in {"sent","blocked","error","uncertain"}: status = "error"
            except Exception as exc:
                self.db.log("action", {"status":"error", "queue_id":item["id"], "error_type":type(exc).__name__})
                status = "error"
            self.db.resolve_queue(item["id"], status)

    def _fetch(self, path, key, params=None):
        try: return self.client.get(path, params=params) or {key: []}
        except Exception as exc:
            self.db.log("cycle", {"status":"read_error", "path":path, "error_type":type(exc).__name__})
            return {key: []}

    def cycle(self):
        self.process_approved()
        # Authenticated pulse is the platform's declared liveness signal. Its
        # timestamps are deliberately excluded from the decision snapshot.
        pulse = self._fetch("/api/pulse", "events", params={"wait":"0"})
        me = self._fetch("/api/me", "since_last_visit") if self.settings.api_key else {}
        front = self._fetch("/api/front", "posts")
        listings = self._fetch("/api/listings", "listings")
        grants = self._fetch("/api/grants", "grants")
        # Bound untrusted context and cost even if the upstream response grows.
        posts = _compact(_list(front, "posts"), ("id","author","title","body","weighted_votes","votes","comments","tags"), 1000, 15)
        safe_items, quarantined = [], []
        learned = self.db.get_setting("defense_patterns", [])
        for post in posts:
            inspected = inspect_content(post, official_domains=("1f916.ai",), extra_patterns=learned)
            if inspected["safe"]:
                item = inspected["content"]
                if isinstance(item, dict) and isinstance(item.get("body"), str):
                    item = dict(item); item["body"] = item["body"][:2000]
                safe_items.append(item)
            else: quarantined.append({"source":"front", "id":post.get("id"), "reasons":inspected["reasons"]})
        snapshot = {
            "items":safe_items,
            "inbox":_inbox(me),
            "standing":{"karma":me.get("karma"), "today":me.get("today")} if isinstance(me,dict) else {},
            "listings":_compact(_list(listings,"listings"), ("id","title","acceptance_condition","description","funder","author","asset","amount","price","expires_at"), 1200, 15),
            "grants":_compact(_list(grants,"grants"), ("slug","title","summary","state","sponsor","resource"), 1200, 10),
        }
        digest = hashlib.sha256(json.dumps(redact(snapshot), sort_keys=True, separators=(",",":"), ensure_ascii=False).encode()).hexdigest()
        previous = self.db.get_setting("snapshot_hash")
        if digest == previous:
            self.db.log("cycle", {"status":"unchanged", "snapshot_hash":digest})
            return {"changed":False}
        self.db.set_setting("snapshot_hash", digest)
        for incident in quarantined:
            self.db.log("quarantine", incident)
        self.db.log("inbox", {"snapshot_hash":digest, "posts":len(safe_items), "quarantined":len(posts)-len(safe_items)})
        for intent in self.brain.decide(snapshot, "triage"):
            self.executor.dispatch(intent)
        self.db.log("radar", {"items":summarize(snapshot["listings"], snapshot["grants"])})
        self.db.log("cycle", {"status":"complete", "snapshot_hash":digest})
        return {"changed":True}

    def daily_audit(self):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if self.db.get_setting("last_daily_audit") == day: return None
        artifact = run_skill("self-redteam", {}, {}, Path(self.settings.data_dir)/"artifacts")
        self.db.log("artifact", artifact)
        self.db.set_setting("last_daily_audit", day)
        prompt = {"artifact":artifact, "instruction":"Draft at most one evidence-first post. Include exact hash and limitations."}
        for intent in self.brain.decide(prompt, "post_of_the_day"):
            self.executor.dispatch(intent)
        return artifact

    def maintenance(self):
        """Run measured defense checks and conservative reward reporting."""
        from defense.redteam import run as redteam
        from defense.reflect import run as reflect
        from tuner.reward import compute_reward
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if self.db.get_setting("last_maintenance") != day:
            redteam(self.db)
            reflect(self.db)
            self.db.set_setting("last_maintenance", day)
        week = time.strftime("%G-W%V", time.gmtime())
        if self.db.get_setting("last_reward_week") != week:
            # Unknown components remain unknown; no synthetic score is made.
            metrics=self.db.get_setting("reward_metrics", {})
            self.db.log("reward", compute_reward(metrics))
            self.db.set_setting("last_reward_week", week)


def main():
    settings = Settings()
    db = Database(Path(settings.data_dir)/"agent.sqlite3"); db.initialize()
    client = Client(settings, db); brain = Brain(settings, db)
    worker = Worker(settings, db, client, brain)
    db.set_setting("mode", db.get_setting("mode", settings.mode))
    db.log("worker", {"status":"started", "handle":settings.handle, "model":settings.ollama_model})
    try:
        while True:
            worker.cycle()
            worker.daily_audit()
            worker.maintenance()
            time.sleep(max(60, settings.cycle_seconds))
    except KeyboardInterrupt:
        pass
    finally:
        brain.close(); client.close()


if __name__ == "__main__": main()
