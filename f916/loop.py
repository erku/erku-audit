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
from .publisher import Publisher
from .seal import seal_artifact
from .payout import PayoutManager
from .opportunities import OpportunityRunner


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
    rows=[]; positions={}
    for bucket in ("replies","comments_on_your_posts","in_threads_you_joined","mentions_of_you"):
        for item in since.get(bucket, []) if isinstance(since, dict) else []:
            if not isinstance(item,dict): continue
            identity=item.get("comment_id",item.get("id"))
            if identity is not None and identity in positions:
                prior=rows[positions[identity]]
                prior["bucket"] = ",".join(dict.fromkeys(prior["bucket"].split(",")+[bucket]))
            else:
                if identity is not None: positions[identity]=len(rows)
                rows.append({"bucket":bucket, **item})
    return _compact(rows, ("bucket","id","post_id","comment_id","author","title","body","created_at"), 800, 15)


class Worker:
    def __init__(self, settings, db, client, brain, executor=None, publisher=None, opportunity_runner=None):
        self.settings, self.db, self.client, self.brain = settings, db, client, brain
        self.executor = executor or Executor(settings, db, client)
        self.publisher = publisher
        self.opportunity_runner = opportunity_runner
        if self.opportunity_runner is None and settings.api_key:
            self.opportunity_runner = OpportunityRunner(settings, db, client, self.executor, publisher)

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
        me = self._fetch("/api/me", "since_last_visit", params={"cursor_mode":"id"}) if self.settings.api_key else {}
        front = self._fetch("/api/front", "posts")
        newest = self._fetch("/api/new", "posts", params={"limit":"15"})
        changes_state=self.db.get_setting("changes_cursor") or {
            "since":str(int(time.time()*1000)-max(60,self.settings.cycle_seconds)*1000),
            "posts_since":"init","comments_since":"init","nulls_since":"done"}
        changes=self._fetch("/api/changes","posts",params=changes_state)
        listings = self._fetch("/api/listings", "listings")
        grants = self._fetch("/api/grants", "grants")
        # Bound untrusted context and cost even if the upstream response grows.
        combined=[]; seen=set()
        for row in _list(changes,"posts")+_list(newest,"posts")+_list(front,"posts"):
            if isinstance(row,dict) and row.get('id') not in seen:
                seen.add(row.get('id')); combined.append(row)
        posts = _compact(combined, ("id","author","title","body","weighted_votes","votes","comments","tags"), 600, 10)
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
        listing_details=[]
        now_seconds=int(time.time())
        for listing in reversed(_list(listings,'listings')):
            if len(listing_details)>=6: break
            if not isinstance(listing,dict) or listing.get('withdrawn_at') or int(listing.get('expiry') or 0)<=now_seconds: continue
            detail=self._fetch(f"/api/listings/{int(listing['id'])}","submissions")
            economics=detail.get('economics',{}) if isinstance(detail,dict) else {}
            if economics.get('available_award_capacity',1)>0: listing_details.append(detail)
        if self.opportunity_runner:
            for listing in listing_details:
                try:
                    self.opportunity_runner.process(listing)
                except Exception as exc:
                    self.db.log("opportunity_error", {"listing_id":listing.get("listing_id"),
                                "stage":"cycle", "error_type":type(exc).__name__})
        snapshot = {
            "items":safe_items,
            "inbox":_inbox(me),
            "standing":{"karma":me.get("karma"), "today":me.get("today")} if isinstance(me,dict) else {},
            "listings":_compact(listing_details, ("listing_id","title","condition","funder","amount_atomic","chain_id","token","expiry","funding_mode","settlement_mode","economics","post_id"), 900, 6),
            "grants":_compact(_list(grants,"grants"), ("slug","title","summary","state","sponsor","resource"), 600, 5),
        }
        digest = hashlib.sha256(json.dumps(redact(snapshot), sort_keys=True, separators=(",",":"), ensure_ascii=False).encode()).hexdigest()
        previous = self.db.get_setting("snapshot_hash")
        if digest == previous:
            self.db.log("cycle", {"status":"unchanged", "snapshot_hash":digest})
            return {"changed":False}
        last_ok=next((event for event in self.db.events('llm',1000) if event['data'].get('status')=='ok' and event['data'].get('task')=='triage'),None)
        urgent=any(item.get('bucket')!='in_threads_you_joined' for item in snapshot['inbox'])
        minimum_interval=getattr(self.settings,'urgent_cadence_seconds',1800) if urgent else getattr(self.settings,'ordinary_cadence_seconds',3600)
        if last_ok and time.time()-last_ok['created_at'] < minimum_interval:
            self.db.log('cycle',{'status':'throttled','snapshot_hash':digest,'urgent':urgent,'retry_after_seconds':int(minimum_interval-(time.time()-last_ok['created_at']))})
            return {'changed':True,'processed':False,'throttled':True}
        for incident in quarantined:
            self.db.log("quarantine", incident)
        self.db.log("inbox", {"snapshot_hash":digest, "posts":len(safe_items), "quarantined":len(posts)-len(safe_items)})
        for intent in self.brain.decide(snapshot, "triage"):
            self.executor.dispatch(intent)
        if getattr(self.brain, "last_status", "ok") != "ok":
            self.db.log("cycle", {"status":"deferred", "snapshot_hash":digest,
                                  "reason":getattr(self.brain,"last_status","unknown")})
            return {"changed":True,"processed":False}
        ack_cursor = me.get("ack_cursor") if isinstance(me,dict) else None
        if ack_cursor:
            try: self.client.post("/api/me/ack", {"up_to":ack_cursor})
            except Exception as exc:
                self.db.log("cycle", {"status":"ack_error", "error_type":type(exc).__name__})
                return {"changed":True,"processed":True,"acked":False}
        if isinstance(changes,dict):
            self.db.set_setting("changes_cursor",{
                "since":str(changes.get("next_since",changes_state["since"])),
                "posts_since":changes.get("next_posts_since") or changes_state["posts_since"],
                "comments_since":changes.get("next_comments_since") or changes_state["comments_since"],
                "nulls_since":changes.get("next_nulls_since") or "done",
            })
        self.db.set_setting("snapshot_hash", digest)
        self.db.log("radar", {"items":summarize(snapshot["listings"], snapshot["grants"])})
        self.db.log("cycle", {"status":"complete", "snapshot_hash":digest})
        return {"changed":True,"processed":True,"acked":bool(ack_cursor)}

    def daily_audit(self):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if self.db.get_setting("last_daily_audit") == day: return None
        artifact = run_skill("self-redteam", {}, {}, Path(self.settings.data_dir)/"artifacts")
        if self.publisher:
            try: artifact = self.publisher.publish(artifact)
            except Exception as exc:
                self.db.log("publisher", {"status":"error","error_type":type(exc).__name__})
                return None
        if self.db.get_setting('last_published_artifact_hash') == artifact['hash']:
            self.db.log('artifact_check',{'status':'unchanged','hash':artifact['hash'],'public_url':artifact.get('public_url')})
            self.db.set_setting("last_daily_audit", day)
            return artifact
        try:
            sealed=seal_artifact(self.client,self.settings,artifact['hash'],'self-redteam')
            artifact['seal_id']=sealed.get('id')
        except Exception as exc:
            self.db.log("seal", {"status":"error","error_type":type(exc).__name__,"hash":artifact['hash']})
        self.db.log("artifact", artifact)
        self.db.set_setting('last_published_artifact_hash',artifact['hash'])
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
            def summarized(value):
                if not isinstance(value,dict): return {"type":type(value).__name__}
                return {
                    "counts":{k:len(v) for k,v in value.items() if isinstance(v,list)},
                    "totals":{k:v for k,v in value.items() if not isinstance(v,(dict,list)) and any(word in k for word in ("total","amount","gmv","liability"))},
                }
            rail_data=self._fetch("/api/rail","listings")
            self.db.log("economy", {
                "rail":summarized(rail_data),
                "payouts":summarized(self._fetch("/api/payouts","bindings")),
                "history":summarized(self._fetch("/api/me/history","posts")) if self.settings.api_key else {},
                "tags":summarized(self._fetch("/api/tags","tags")),
            })
            if self.settings.api_key and self.settings.payout_address:
                try:
                    bindings=PayoutManager(self.settings,self.db,self.client).scan_awards(_list(rail_data,'listings'))
                    if bindings: self.db.log('payout_scan',{'results':bindings})
                except Exception as exc:
                    self.db.log('payout_scan',{'status':'error','error_type':type(exc).__name__})
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
    publisher = Publisher(settings) if settings.github_repo else None
    worker = Worker(settings, db, client, brain, publisher=publisher)
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
