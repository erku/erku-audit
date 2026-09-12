"""Proposals cannot edit prompts, invariants or files on their own."""
from defense.regress import run_regression, load_cases


def propose(incidents):
    patterns = []
    for incident in incidents:
        phrase = incident.get('confirmed_attack_phrase')
        if isinstance(phrase,str) and 8 <= len(phrase) <= 160 and phrase not in patterns:
            patterns.append(phrase)
    return {'kind':'detector', 'patterns':patterns[:20], 'status':'proposal', 'reason':'Only explicitly confirmed attack phrases are candidates'}


def evaluate_proposal(proposal, baseline_patterns=(), cases=None):
    if proposal.get('kind') != 'detector':
        return {'accepted':False, 'status':'manual_review', 'reason':'Protected change'}
    patterns = proposal.get('patterns', [])
    if not isinstance(patterns,list) or not patterns or len(patterns)>20 or any(not isinstance(p,str) or not 8 <= len(p) <= 160 for p in patterns):
        return {'accepted':False, 'status':'rejected', 'reason':'Invalid literal patterns'}
    before = run_regression(baseline_patterns, cases)
    after = run_regression(tuple(baseline_patterns)+tuple(patterns), cases)
    accepted = (set(after['missed']) < set(before['missed']) and not (set(after['false_positives']) - set(before['false_positives'])))
    return {'accepted':accepted, 'status':'eligible' if accepted else 'rejected', 'before':before, 'after':after, 'patterns':list(baseline_patterns)+patterns}


def run(db, incidents=None):
    """Confirmed phrases are trusted operator annotations, never raw forum fields."""
    if incidents is None:
        incidents = [event['data'] for event in db.events(kind='confirmed_incident',limit=100)]
    proposal = propose(incidents)
    cases = load_cases() + db.get_setting('defense_learned_cases', [])
    known = {case['text'] for case in cases}
    for index, phrase in enumerate(proposal['patterns']):
        if phrase not in known:
            cases.append({'id':f'confirmed-{len(cases)}-{index}','text':phrase,'attack':True})
    result = evaluate_proposal(proposal, db.get_setting('defense_patterns', []), cases)
    if result['accepted']:
        db.set_setting('defense_patterns', result['patterns'])
        db.set_setting('defense_learned_cases', cases[len(load_cases()):])
    db.log('defense_reflection', result)
    return result
