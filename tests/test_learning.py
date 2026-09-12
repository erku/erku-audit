import time

from f916.db import Database
from f916.config import Settings
from f916.learning import ARMS, choose_topic, attribute_and_update, _karma


def test_arms_match_the_closed_topic_set():
    from tuner.strategy import CHOICES
    assert list(ARMS) == sorted(CHOICES['topic'])


def test_choose_topic_returns_an_arms_member(tmp_path):
    db = Database(tmp_path / 's.db'); db.initialize()
    for _ in range(20):
        assert choose_topic(db) in ARMS


def test_karma_extraction_is_defensive():
    assert _karma({'karma': 42}) == 42.0
    assert _karma({'karma': 3.5}) == 3.5
    assert _karma({'karma': True}) is None
    assert _karma({'karma': 'lots'}) is None
    assert _karma({}) is None
    assert _karma(None) is None
    assert _karma([1, 2]) is None


def _log_triage(db, arm, karma_snapshot, age_hours):
    eid = db.log('llm', {'status': 'ok', 'task': 'triage', 'topic_arm': arm})
    with db.connect() as c:
        c.execute('UPDATE events SET created_at=? WHERE id=?', (time.time() - age_hours * 3600, eid))
    if karma_snapshot is not None:
        db.set_setting(f'arm_karma:{eid}', {'karma': karma_snapshot, 'ts': time.time()})
    return eid


def test_attribute_and_update_moves_state_toward_rising_karma(tmp_path):
    db = Database(tmp_path / 's.db'); db.initialize()
    _log_triage(db, 'audit', karma_snapshot=10, age_hours=100)
    result = attribute_and_update(db, Settings(data_dir=tmp_path), {'karma': 15})
    assert result == {'updated': 1, 'skipped': 0}
    alpha, beta = db.get_setting('bandit_state')['audit']
    assert alpha == 2.0 and beta == 1.0  # reward 1.0: alpha+=1, beta+=0


def test_attribute_and_update_moves_state_away_on_falling_karma(tmp_path):
    db = Database(tmp_path / 's.db'); db.initialize()
    _log_triage(db, 'gate', karma_snapshot=10, age_hours=100)
    result = attribute_and_update(db, Settings(data_dir=tmp_path), {'karma': 4})
    assert result == {'updated': 1, 'skipped': 0}
    alpha, beta = db.get_setting('bandit_state')['gate']
    assert alpha == 1.0 and beta == 2.0  # reward 0.0: alpha+=0, beta+=1


def test_attribute_and_update_uses_half_reward_when_karma_is_unchanged(tmp_path):
    db = Database(tmp_path / 's.db'); db.initialize()
    _log_triage(db, 'leak', karma_snapshot=10, age_hours=100)
    result = attribute_and_update(db, Settings(data_dir=tmp_path), {'karma': 10})
    assert result == {'updated': 1, 'skipped': 0}
    alpha, beta = db.get_setting('bandit_state')['leak']
    assert (alpha, beta) == (1.5, 1.5)  # reward 0.5: alpha+=0.5, beta+=0.5 from the [1,1] prior


def test_attribute_and_update_skips_events_under_72h(tmp_path):
    db = Database(tmp_path / 's.db'); db.initialize()
    _log_triage(db, 'leak', karma_snapshot=10, age_hours=1)
    result = attribute_and_update(db, Settings(data_dir=tmp_path), {'karma': 20})
    assert result == {'updated': 0, 'skipped': 0}
    assert db.get_setting('bandit_state', {}) == {}


def test_attribute_and_update_skips_and_never_guesses_without_a_snapshot(tmp_path):
    db = Database(tmp_path / 's.db'); db.initialize()
    _log_triage(db, 'rail', karma_snapshot=None, age_hours=200)
    result = attribute_and_update(db, Settings(data_dir=tmp_path), {'karma': 20})
    assert result == {'updated': 0, 'skipped': 1}
    assert db.get_setting('bandit_state', {}) == {}


def test_attribute_and_update_does_not_double_count_already_observed_ids(tmp_path):
    db = Database(tmp_path / 's.db'); db.initialize()
    _log_triage(db, 'audit', karma_snapshot=10, age_hours=100)
    first = attribute_and_update(db, Settings(data_dir=tmp_path), {'karma': 15})
    second = attribute_and_update(db, Settings(data_dir=tmp_path), {'karma': 15})
    assert first == {'updated': 1, 'skipped': 0}
    assert second == {'updated': 0, 'skipped': 0}
    alpha, beta = db.get_setting('bandit_state')['audit']
    assert (alpha, beta) == (2.0, 1.0)


def test_attribute_and_update_skips_when_current_karma_unavailable(tmp_path):
    db = Database(tmp_path / 's.db'); db.initialize()
    _log_triage(db, 'audit', karma_snapshot=10, age_hours=100)
    result = attribute_and_update(db, Settings(data_dir=tmp_path), {})
    assert result == {'updated': 0, 'skipped': 1}
    assert db.get_setting('bandit_state', {}) == {}


def test_attribute_and_update_never_raises_on_garbage_events(tmp_path):
    db = Database(tmp_path / 's.db'); db.initialize()
    db.log('llm', 'not-a-dict-once-parsed-back-it-still-is-a-string-value')
    db.log('llm', {'status': 'ok'})  # no topic_arm
    db.log('llm', {'status': 'error', 'topic_arm': 'audit'})  # wrong status
    db.log('llm', {'status': 'ok', 'topic_arm': 'not-an-arm'})
    result = attribute_and_update(db, Settings(data_dir=tmp_path), {'karma': 5})
    assert result == {'updated': 0, 'skipped': 0}


def test_attribute_and_update_never_raises_even_if_bandit_state_is_corrupt(tmp_path):
    db = Database(tmp_path / 's.db'); db.initialize()
    _log_triage(db, 'audit', karma_snapshot=10, age_hours=100)
    db.set_setting('bandit_state', 'not-a-dict')
    result = attribute_and_update(db, Settings(data_dir=tmp_path), {'karma': 20})
    assert isinstance(result, dict) and 'updated' in result and 'skipped' in result
