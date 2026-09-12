"""Shadow rewards must be caller-supplied measured evaluation results.

No synthetic counterfactual reward is inferred from a model or local action.
Missing observations keep the candidate in shadow indefinitely.
"""
import math
from tuner.strategy import DEFAULT_POLICY, validate_policy


class PolicyManager:
    def __init__(self, db): self.db = db

    def current(self): return self.db.get_setting('live_policy',dict(DEFAULT_POLICY))

    def propose(self, patch):
        if not validate_policy(patch): return {'status':'rejected','reason':'Outside immutable tuning bounds'}
        candidate = {**self.current(),**patch}
        self.db.set_setting('shadow_policy',{'policy':candidate,'observations':[]})
        self.db.log('policy_proposal',{'policy':candidate})
        return {'status':'shadow','policy':candidate}

    def observe_shadow(self, evidence_id, live_reward, candidate_reward, *, violations=0):
        shadow = self.db.get_setting('shadow_policy')
        if not shadow: return {'status':'no_candidate'}
        if not evidence_id or any(v is None for v in (live_reward,candidate_reward)): return {'status':'insufficient_evidence'}
        if any(not math.isfinite(v) for v in (live_reward,candidate_reward)): raise ValueError('Non-finite observation')
        if evidence_id in [o['id'] for o in shadow['observations']]: return {'status':'duplicate'}
        if violations:
            self.db.set_setting('shadow_policy',None)
            return {'status':'rejected','reason':'Invariant violation'}
        shadow['observations'].append({'id':evidence_id,'live':live_reward,'candidate':candidate_reward})
        self.db.set_setting('shadow_policy',shadow)
        if len(shadow['observations'])<3: return {'status':'shadow'}
        live = sum(o['live'] for o in shadow['observations'])/len(shadow['observations'])
        candidate = sum(o['candidate'] for o in shadow['observations'])/len(shadow['observations'])
        if candidate < live: return {'status':'shadow','reason':'No measured improvement'}
        self.db.set_setting('previous_policy',self.current())
        self.db.set_setting('live_policy',shadow['policy'])
        self.db.set_setting('policy_baseline_reward',candidate)
        self.db.set_setting('policy_bad_windows',[])
        self.db.set_setting('shadow_policy',None)
        self.db.log('policy_version',{'policy':shadow['policy'],'evaluation':shadow['observations']})
        return {'status':'promoted'}

    def rollback(self):
        previous = self.db.get_setting('previous_policy')
        if previous is None: return {'status':'no_previous_version'}
        self.db.set_setting('live_policy',previous)
        self.db.set_setting('previous_policy',None)
        self.db.set_setting('policy_bad_windows',[])
        self.db.log('policy_rollback',{'policy':previous})
        return {'status':'rolled_back'}

    def record_live(self, window_id, reward):
        baseline = self.db.get_setting('policy_baseline_reward')
        if baseline is None or reward is None: return {'status':'insufficient_evidence'}
        if not math.isfinite(reward): raise ValueError('Non-finite observation')
        seen = self.db.get_setting('policy_live_windows',[])
        if window_id in seen: return {'status':'duplicate'}
        self.db.set_setting('policy_live_windows',seen+[window_id])
        bad = self.db.get_setting('policy_bad_windows',[])
        # Two distinct consecutive observed windows, at least 20% below baseline.
        bad = bad+[window_id] if reward < baseline-max(abs(baseline)*0.2,0.1) else []
        self.db.set_setting('policy_bad_windows',bad)
        self.db.log('reward_history',{'window':window_id,'reward':reward})
        return self.rollback() if len(bad)>=2 else {'status':'watch'}
