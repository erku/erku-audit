import math
import random


class Bandit:
    def __init__(self, db, arms):
        if not arms or len(arms)>100 or len(set(arms))!=len(arms): raise ValueError('Invalid arms')
        self.db, self.arms = db, list(arms)

    def choose(self):
        state = self.db.get_setting('bandit_state', {})
        return max(self.arms, key=lambda arm: random.betavariate(*state.get(arm,[1.0,1.0])))

    def update(self, arm, action_id, reward, *, age_hours):
        if arm not in self.arms: raise ValueError('Unknown arm')
        if not math.isfinite(age_hours) or age_hours < 0: raise ValueError('Invalid attribution age')
        if not math.isfinite(reward) or not 0 <= reward <= 1: raise ValueError('Observed reward must be normalized to [0,1]')
        seen = self.db.get_setting('bandit_observations', [])
        if not action_id or action_id in seen or age_hours < 72: return False
        state = self.db.get_setting('bandit_state', {})
        alpha,beta = state.get(arm,[1.0,1.0])
        state[arm] = [alpha+reward,beta+1-reward]
        self.db.set_setting('bandit_state',state)
        self.db.set_setting('bandit_observations',seen+[action_id])
        self.db.log('bandit_update',{'arm':arm,'action_id':action_id,'reward':reward})
        return True
