from f916.db import Database
from f916.config import Settings
from f916.outcomes import submitted_listing_ids, classify_outcome, usd_from_award, scan_outcomes


def test_submitted_listing_ids_extracts_dedupes_and_tolerates_junk(tmp_path):
    db = Database(tmp_path/'s.db'); db.initialize()
    db.log('action', {'intent': {'action': 'submit', 'listing_id': 5}, 'status': 'sent'})
    db.log('action', {'intent': {'action': 'submit', 'listing_id': 5}, 'status': 'sent'})
    db.log('action', {'intent': {'action': 'submit', 'listing_id': 7}, 'status': 'sent'})
    db.log('action', {'intent': {'action': 'vote', 'post_id': 1}, 'status': 'sent'})
    db.log('action', {'not_intent': True})
    db.log('action', {'intent': {'action': 'submit', 'listing_id': 'not-a-number'}})
    db.log('action', 'garbage')
    ids = submitted_listing_ids(db)
    assert sorted(ids) == [5, 7]


def test_classify_outcome_paid():
    detail = {
        'listing_id': 1,
        'submissions': [{'id': 354, 'handle': 'erku-audit'}],
        'awards': [{'award_id': 5, 'submission_id': 354, 'state': 'paid', 'amount_atomic': '1000000', 'token': 'USDC'}],
    }
    outcome = classify_outcome(detail, 'erku-audit')
    assert outcome == {'listing_id': 1, 'submission_id': 354, 'state': 'paid', 'amount_atomic': '1000000', 'token': 'USDC', 'paid': True}


def test_classify_outcome_awarded_unpaid():
    detail = {
        'listing_id': 2,
        'submissions': [{'id': 10, 'handle': 'erku-audit'}],
        'awards': [{'award_id': 6, 'submission_id': 10, 'state': 'awarded', 'amount_atomic': '500000', 'token': 'usdc'}],
    }
    outcome = classify_outcome(detail, 'erku-audit')
    assert outcome['state'] == 'awarded'
    assert outcome['paid'] is False


def test_classify_outcome_submitted_no_award():
    detail = {
        'listing_id': 3,
        'submissions': [{'id': 11, 'handle': 'erku-audit'}],
        'awards': [],
    }
    outcome = classify_outcome(detail, 'erku-audit')
    assert outcome == {'listing_id': 3, 'submission_id': 11, 'state': 'submitted', 'amount_atomic': None, 'token': None, 'paid': False}


def test_classify_outcome_only_other_handles_returns_none():
    detail = {
        'listing_id': 4,
        'submissions': [{'id': 12, 'handle': 'pepe-papi'}],
        'awards': [{'award_id': 7, 'submission_id': 12, 'state': 'paid', 'amount_atomic': '1000000', 'token': 'usdc'}],
    }
    assert classify_outcome(detail, 'erku-audit') is None


def test_classify_outcome_precedence_paid_over_awarded():
    detail = {
        'listing_id': 5,
        'submissions': [{'id': 20, 'handle': 'erku-audit'}, {'id': 21, 'handle': 'erku-audit'}],
        'awards': [
            {'award_id': 8, 'submission_id': 20, 'state': 'awarded', 'amount_atomic': '1', 'token': 'usdc'},
            {'award_id': 9, 'submission_id': 21, 'state': 'paid', 'amount_atomic': '2000000', 'token': 'usdc'},
        ],
    }
    outcome = classify_outcome(detail, 'erku-audit')
    assert outcome['state'] == 'paid'
    assert outcome['submission_id'] == 21


def test_classify_outcome_never_raises_on_malformed_input():
    assert classify_outcome(None, 'erku-audit') is None
    assert classify_outcome({'submissions': 'nope', 'awards': None}, 'erku-audit') is None
    assert classify_outcome({}, '') is None


def test_usd_from_award_paid_usdc_base():
    assert usd_from_award('paid', '1000000', 'usdc', 8453) == 1.0
    assert usd_from_award('paid', '1000000', 'USDC', None) == 1.0


def test_usd_from_award_non_paid_is_zero():
    assert usd_from_award('awarded', '1000000', 'usdc', 8453) == 0.0


def test_usd_from_award_non_usdc_is_zero():
    assert usd_from_award('paid', '1000000', 'dai', 8453) == 0.0


def test_usd_from_award_wrong_chain_is_zero():
    assert usd_from_award('paid', '1000000', 'usdc', 1) == 0.0


def test_usd_from_award_junk_never_raises():
    assert usd_from_award(object(), 'not-a-number', None, 'nope') == 0.0
    assert usd_from_award('paid', None, 'usdc', 8453) == 0.0
    assert usd_from_award('paid', '-5', 'usdc', 8453) == 0.0


class FakeClient:
    def __init__(self, details):
        self.details = details
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(path)
        listing_id = int(path.rsplit('/', 1)[-1])
        if listing_id not in self.details:
            raise RuntimeError('not found')
        return self.details[listing_id]


def _seed_submission(db, listing_id):
    db.log('action', {'intent': {'action': 'submit', 'listing_id': listing_id}, 'status': 'sent'})


def test_scan_outcomes_idempotent_no_double_count(tmp_path):
    db = Database(tmp_path/'s.db'); db.initialize()
    settings = Settings(handle='erku-audit')
    _seed_submission(db, 100)
    _seed_submission(db, 101)
    details = {
        100: {
            'listing_id': 100, 'chain_id': 8453,
            'submissions': [{'id': 1, 'handle': 'erku-audit'}],
            'awards': [{'award_id': 1, 'submission_id': 1, 'state': 'paid', 'amount_atomic': '1000000', 'token': 'usdc'}],
        },
        101: {
            'listing_id': 101, 'chain_id': 8453,
            'submissions': [{'id': 2, 'handle': 'erku-audit'}],
            'awards': [{'award_id': 2, 'submission_id': 2, 'state': 'awarded', 'amount_atomic': '500000', 'token': 'usdc'}],
        },
    }
    client = FakeClient(details)

    first = scan_outcomes(client, db, settings)
    assert first['scanned'] == 2
    assert first['new_outcomes'] == 2
    assert first['earnings_usd'] == 1.0
    assert db.get_setting('reward_metrics')['outside_funded_earnings_usd'] == 1.0
    assert len(db.events('outcome', 1000)) == 2

    second = scan_outcomes(client, db, settings)
    assert second['new_outcomes'] == 0
    assert second['earnings_usd'] == 1.0
    assert len(db.events('outcome', 1000)) == 2
    assert db.get_setting('reward_metrics')['outside_funded_earnings_usd'] == 1.0


def test_scan_outcomes_progression_relogs_new_state_but_earnings_stay_correct(tmp_path):
    db = Database(tmp_path/'s.db'); db.initialize()
    settings = Settings(handle='erku-audit')
    _seed_submission(db, 200)
    details = {
        200: {
            'listing_id': 200, 'chain_id': 8453,
            'submissions': [{'id': 5, 'handle': 'erku-audit'}],
            'awards': [{'award_id': 3, 'submission_id': 5, 'state': 'awarded', 'amount_atomic': '2000000', 'token': 'usdc'}],
        },
    }
    client = FakeClient(details)
    first = scan_outcomes(client, db, settings)
    assert first['new_outcomes'] == 1
    assert first['earnings_usd'] == 0.0

    details[200]['awards'][0]['state'] = 'paid'
    second = scan_outcomes(client, db, settings)
    assert second['new_outcomes'] == 1
    assert second['earnings_usd'] == 2.0
    assert len(db.events('outcome', 1000)) == 2

    third = scan_outcomes(client, db, settings)
    assert third['new_outcomes'] == 0
    assert third['earnings_usd'] == 2.0
    assert len(db.events('outcome', 1000)) == 2


def test_scan_outcomes_tolerates_client_errors_and_bounds_to_25(tmp_path):
    db = Database(tmp_path/'s.db'); db.initialize()
    settings = Settings(handle='erku-audit')
    for i in range(30):
        _seed_submission(db, i)
    client = FakeClient({})  # every get() raises
    result = scan_outcomes(client, db, settings)
    assert result['scanned'] <= 25
    assert result['new_outcomes'] == 0
    assert result['earnings_usd'] == 0.0
