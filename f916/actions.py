import hashlib
from pathlib import Path
import httpx
from .models import Intent
from invariants import check_intent

class Executor:
    def __init__(self,settings,db,client):
        self.settings=settings; self.db=db; self.client=client
        self.db.secrets.extend([settings.api_key,settings.dashboard_password])
    def _artifact_valid(self,intent):
        if not intent.hash or intent.hash not in (intent.body or ''): return False
        root=(self.settings.data_dir/'artifacts').resolve()
        for event in self.db.events('artifact',1000):
            artifact=event['data']
            if artifact.get('hash')!=intent.hash: continue
            for filename in artifact.get('evidence_files',[]):
                path=Path(filename).resolve()
                try:
                    if path.is_relative_to(root) and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==intent.hash: return True
                except OSError: continue
        return False
    def dispatch(self,intent,approved=False):
        intent=Intent.model_validate(intent) if isinstance(intent,dict) else intent
        data=intent.model_dump(exclude_none=True); action=intent.action
        def result(status,reason=None,**extra):
            out={'status':status,**extra}
            if reason: out['reason']=reason
            self.db.log('action',{'intent':data,**out}); return out
        if action=='noop': return result('blocked','noop')
        invariant_reasons=check_intent(data)
        if invariant_reasons: return result('blocked','invariant',invariant_reasons=invariant_reasons)
        mode=self.db.get_setting('mode',self.settings.mode)
        if mode not in {'auto','approve'}: return result('blocked','off')
        if not self.settings.api_key: return result('blocked','observation_only')
        if action=='post' and not self._artifact_valid(intent): return result('blocked','verified_artifact_required')
        if not approved and mode=='approve' and action!='vote':
            return result('queued','manual_review',queue_id=self.db.queue(data,'manual_review'))
        if action=='vote':
            kind='comment' if intent.comment_id else 'post'; target=intent.comment_id or intent.post_id
            try:
                detail=self.client.get(f'/api/{kind}/{target}')
                item=detail.get(kind,detail) if isinstance(detail,dict) else {}
                author=item.get('author')
                if isinstance(author,dict): author=author.get('handle')
                if not isinstance(author,str) or not author or not self.settings.handle or author.casefold()==self.settings.handle.casefold():
                    return result('blocked','self_vote_or_unknown_author')
            except Exception: return result('blocked','author_lookup_failed')
        if not self.client.check_contract(): return result('blocked','contract')
        fields={'post':['title','body','url'],'comment':['post_id','body'],'vote':[],'tag':['tag'],'cadence':['interval_seconds'],'porch':['body'],'submit':['artifact','note'],'propose':['title','summary','body','wants_to_build']}
        payload={k:data[k] for k in fields[action] if k in data}
        if action=='vote': payload={'target_id':intent.comment_id or intent.post_id,'target_type':'comment' if intent.comment_id else 'post'}
        if action=='tag': payload['post_id']=intent.post_id
        if action=='comment': payload['post_id']=intent.post_id
        path={'cadence':'/api/me/cadence','submit':f'/api/listings/{intent.listing_id}/submissions','propose':f'/api/grants/{intent.slug}/proposals'}.get(action,f'/api/{action}')
        reservation={'action':action,'path':path,'payload':payload}
        # Preserve the scope fields used by the database's per-target limits.
        if intent.post_id is not None: reservation['post_id']=intent.post_id
        if intent.slug is not None: reservation['slug']=intent.slug
        if not self.db.reserve_action(reservation): return result('blocked','duplicate_or_limit')
        try:
            response=self.client.post(path,payload)
            return result('sent',response=response)
        except httpx.HTTPStatusError as exc:
            if 400<=exc.response.status_code<500 and exc.response.status_code not in {408,409,425}:
                self.db.release_action(reservation)
                return result('error','rejected',http_status=exc.response.status_code)
            return result('uncertain','uncertain_write_no_retry',error_type=type(exc).__name__)
        except Exception as exc:
            return result('uncertain','uncertain_write_no_retry',error_type=type(exc).__name__)
