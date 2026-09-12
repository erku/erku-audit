import json
from pathlib import Path
from defense.ingest import inspect_content


def load_cases():
    return json.loads((Path(__file__).parent / 'attacks' / 'cases.json').read_text(encoding='utf-8'))


def run_regression(extra_patterns=(), cases=None):
    cases = load_cases() if cases is None else cases
    missed, false_positives, caught = [], [], []
    for case in cases:
        blocked = not inspect_content(case['text'], extra_patterns=extra_patterns)['safe']
        if case['attack'] and not blocked: missed.append(case['id'])
        if case['attack'] and blocked: caught.append(case['id'])
        if not case['attack'] and blocked: false_positives.append(case['id'])
    total = len(caught) + len(missed)
    return {'score':len(caught)/total if total else None, 'caught':caught, 'missed':missed, 'false_positives':false_positives, 'total':len(cases)}
