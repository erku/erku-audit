"""Only a closed soft-policy space can be proposed by the model."""
import math

DEFAULT_POLICY = {'daily_comments':20,'daily_votes':50,'cycle_seconds':900,'radar_threshold':0.7,'style':'evidence-first','topic':'audit'}
BOUNDS = {'daily_comments':(0,20,1),'daily_votes':(0,50,1),'cycle_seconds':(300,3600,300),'radar_threshold':(0.5,0.95,0.05)}
CHOICES = {'style':{'evidence-first','concise'},'topic':{'audit','gate','leak','rail'}}


def validate_policy(patch):
    if not isinstance(patch,dict) or not patch: return False
    for key,value in patch.items():
        if key in BOUNDS:
            lo,hi,step = BOUNDS[key]
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not lo <= value <= hi: return False
            if abs((value-lo)/step-round((value-lo)/step))>1e-8: return False
        elif key in CHOICES:
            if not isinstance(value,str) or value not in CHOICES[key]: return False
        else: return False
    return True


def propose(patch, reason=''):
    return {'status':'candidate' if validate_policy(patch) else 'manual_review','patch':patch,'reason':str(reason)[:1000]}
