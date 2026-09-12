from defense.regress import run_regression, load_cases


def run(db=None):
    result = run_regression(db.get_setting('defense_patterns', []) if db else (), load_cases()+db.get_setting('defense_learned_cases',[]) if db else None)
    if db is not None:
        db.log('defense_score', result)
        for case in result['missed']: db.log('incident', {'type':'redteam_miss','case':case})
    return result
