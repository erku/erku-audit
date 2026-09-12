"""Closed-set topic bandit: a bounded SOFT bias over which approved topic the
model emphasizes this cycle. It can never change what actions are allowed,
alter limits, or bypass invariants -- it only nudges wording via a single
additive guidance line built elsewhere (see brain.decide).

Arms are exactly the approved topics from tuner.strategy.CHOICES['topic'];
nothing outside that closed set is representable. Reward is a coarse,
bounded [0,1] signal derived from measured karma delta over a >=72h window,
fed through the existing tuner.bandit.Bandit (which itself enforces the
normalized-reward bound, the 72h attribution floor, and action_id dedup).
"""
from __future__ import annotations

import math
import time

from tuner.bandit import Bandit

ARMS = ("audit", "gate", "leak", "rail")  # == sorted(tuner.strategy.CHOICES['topic'])


def choose_topic(db) -> str:
    """Thompson-sample an approved topic arm; returns an ARMS member. Records
    nothing itself (caller logs which arm it used)."""
    return Bandit(db, list(ARMS)).choose()


def _karma(me):
    """Extract numeric karma from an /api/me-style dict, else None."""
    if not isinstance(me, dict):
        return None
    karma = me.get('karma')
    if isinstance(karma, bool) or not isinstance(karma, (int, float)) or not math.isfinite(karma):
        return None
    return float(karma)


def attribute_and_update(db, settings, me) -> dict:
    """Turn measured engagement into per-arm bandit updates, safely.

    Never raises: any unexpected failure is swallowed and reflected only in
    the returned counts, so a learning-loop bug can never block or alter
    triage. See module docstring for the safety frame.
    """
    updated, skipped = 0, 0
    try:
        current_karma = _karma(me)
        observed = db.get_setting('learning_observed', [])
        if not isinstance(observed, list):
            observed = []
        observed_set = set(observed)
        newly_observed = []
        bandit = Bandit(db, list(ARMS))
        now = time.time()
        for event in db.events('llm', 1000):
            try:
                if not isinstance(event, dict):
                    continue
                data = event.get('data')
                if not isinstance(data, dict) or data.get('status') != 'ok':
                    continue
                arm = data.get('topic_arm')
                if arm not in ARMS:
                    continue
                eid = event.get('id')
                if eid is None or eid in observed_set:
                    continue
                created_at = event.get('created_at')
                if not isinstance(created_at, (int, float)):
                    continue
                age_hours = (now - created_at) / 3600
                if age_hours < 72:
                    continue  # not yet attributable; revisit on a later pass
                snapshot = db.get_setting(f'arm_karma:{eid}')
                old_karma = snapshot.get('karma') if isinstance(snapshot, dict) else None
                if old_karma is None or isinstance(old_karma, bool) or not isinstance(old_karma, (int, float)):
                    skipped += 1
                    continue  # no snapshot recorded at triage time -- never guess
                if current_karma is None:
                    skipped += 1
                    continue  # no measured signal available right now
                if current_karma > old_karma: reward = 1.0
                elif current_karma < old_karma: reward = 0.0
                else: reward = 0.5
                ok = bandit.update(arm, action_id=f'triage-{eid}', reward=reward, age_hours=age_hours)
                observed_set.add(eid); newly_observed.append(eid)
                updated += 1 if ok else 0
                skipped += 0 if ok else 1
            except Exception:
                skipped += 1
                continue
        if newly_observed:
            db.set_setting('learning_observed', (observed + newly_observed)[-500:])
        return {'updated': updated, 'skipped': skipped}
    except Exception:
        return {'updated': updated, 'skipped': skipped}
