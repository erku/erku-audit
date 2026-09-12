import math

WEIGHTS = {'karma_delta_7d':1.0,'outside_funded_earnings_usd':0.5,'grant_progress':0.2,'llm_cost_usd':-1.0,'flags_received':-5.0,'defense_incidents_unhandled':-5.0}


def compute_reward(metrics, weights=None):
    weights = WEIGHTS if weights is None else weights
    if set(weights) != set(WEIGHTS): raise ValueError('All six objective weights required')
    missing = [key for key in weights if metrics.get(key) is None]
    if missing: return {'status':'unavailable','value':None,'missing':missing}
    components = {}
    for key, weight in weights.items():
        value = float(metrics[key]); weight = float(weight)
        if not math.isfinite(value) or not math.isfinite(weight): raise ValueError('Non-finite reward')
        if key != 'karma_delta_7d' and value < 0: raise ValueError('Negative count or cost')
        components[key] = value*weight
    return {'status':'observed','value':sum(components.values()),'components':components}
