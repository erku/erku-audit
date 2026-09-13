"""Fixed deterministic audits. No command strings are executed."""
import hashlib
import json
import math
import shlex
import statistics
import uuid
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl
from invariants import redact
from defense.regress import run_regression

SKILLS = frozenset({'gate-probe','leak-probe','rail-audit','chain-verify','self-redteam','rail-report','rail-derivation-check','batch-cadence','receipt-report'})


def _gate(target, params):
    tokens = target.get('blocked_tokens')
    if not isinstance(tokens,list) or not tokens or any(not isinstance(t,str) or not t for t in tokens):
        return {'status':'inconclusive','findings':[], 'summary':'Provide blocked_tokens for the literal substring rule under test.'}
    variants = ['rm -rf /tmp/example', 'rm -r -f /tmp/example', "rm '-rf' /tmp/example", 'rm -fr /tmp/example', '/bin/rm -rf /tmp/example']
    findings = []
    for command in variants:
        blocked = any(t in command for t in tokens)
        if not blocked: findings.append({'variant':command, 'expected':'blocked', 'observed':'allowed'})
    return {'status':'findings' if findings else 'consistent','findings':findings,'summary':f'{len(findings)} literal-rule misses in {len(variants)} destructive-command variants; no execution.', 'scope':'Only the supplied literal substring rule, not a production shell gate.'}


def _leak(target, params):
    findings = []
    def walk(value, path='$'):
        if isinstance(value,dict):
            for key,item in value.items(): walk(item, path+'.'+str(key))
        elif isinstance(value,list):
            for i,item in enumerate(value): walk(item,path+f'[{i}]')
        elif isinstance(value,str):
            import re
            for url in re.findall(r'https?://[^\s<>"\x27]+', value):
                parsed = urlsplit(url)
                for key, secret in parse_qsl(parsed.query, keep_blank_values=True):
                    if any(t in key.lower() for t in ('token','key','secret','password','email','authorization')):
                        findings.append({'path':path,'location':'query','parameter':key,'host':parsed.hostname})
            if redact(value) != value: findings.append({'path':path,'location':'text','type':'credential_like'})
    walk(target)
    return {'status':'findings' if findings else 'consistent','findings':findings,'summary':f'{len(findings)} possible credential or personal-data leaks; heuristic inspection only.'}


def _rail(target, params):
    try:
        if not isinstance(target.get('awards'),list) or not isinstance(target.get('receipts'),list): raise ValueError()
        if not target['awards'] or not target['receipts']: raise ValueError()
        def asset(row):
            value=row.get('asset')
            if isinstance(value,dict): return (int(value['chain_id']),str(value['token']).lower())
            if row.get('chain_id') is not None and row.get('token'): return (int(row['chain_id']),str(row['token']).lower())
            if row.get('currency'): return ('legacy',str(row['currency']))
            raise ValueError()
        assets = {asset(row) for key in ('awards','receipts') for row in target[key]}
        if len(assets) != 1: raise ValueError()
        totals = []
        for key in ('awards','receipts'):
            raw=[str(row.get('amount_atomic',row.get('amount',''))) for row in target[key]]
            if any(not value.isdigit() for value in raw): raise ValueError()
            totals.append(sum(map(int,raw)))
    except (KeyError, TypeError, ValueError, AttributeError):
        return {'status':'inconclusive','findings':[], 'summary':'Expected awards and receipts with nonnegative integer atomic amounts in one common asset.'}
    findings = [] if totals[0] == totals[1] else [{'awards':str(totals[0]),'receipts':str(totals[1]),'difference':str(totals[0]-totals[1])}]
    return {'status':'findings' if findings else 'consistent','findings':findings,'summary':'Compared supplied award and receipt totals as atomic integers for one asset; no chain or source-authenticity verification.', 'asset':list(assets)[0], 'totals':[str(t) for t in totals]}


def _rail_derivation(target, params):
    economics = target.get('economics') if isinstance(target, dict) else None
    listing_id = target.get('listing_id') if isinstance(target, dict) else None
    if not isinstance(economics, dict):
        return {'status':'inconclusive','findings':[],'summary':'No checkable published economic identities in the supplied fields.'}

    def parse_nonneg_int(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    findings = []
    for field, value in economics.items():
        if field.endswith('_atomic') and parse_nonneg_int(value) is None:
            findings.append({'field': field, 'issue': 'not_a_nonnegative_integer', 'value': value})

    n_checked = 0

    outstanding = parse_nonneg_int(economics['outstanding_awarded_atomic']) if 'outstanding_awarded_atomic' in economics else None
    currently_due = parse_nonneg_int(economics['currently_due_atomic']) if 'currently_due_atomic' in economics else None
    overdue = parse_nonneg_int(economics['overdue_unpaid_atomic']) if 'overdue_unpaid_atomic' in economics else None
    if outstanding is not None and currently_due is not None and overdue is not None:
        n_checked += 1
        computed = currently_due + overdue
        if outstanding != computed:
            findings.append({
                'identity': 'outstanding_awarded_atomic == currently_due_atomic + overdue_unpaid_atomic',
                'served': str(outstanding),
                'computed': str(computed),
            })

    max_awards = parse_nonneg_int(economics['max_awards']) if 'max_awards' in economics else None
    awarded_slots_used = parse_nonneg_int(economics['awarded_slots_used']) if 'awarded_slots_used' in economics else None
    available = parse_nonneg_int(economics['available_award_capacity']) if 'available_award_capacity' in economics else None
    if (max_awards is not None and awarded_slots_used is not None and available is not None
            and awarded_slots_used <= max_awards):
        n_checked += 1
        computed = max_awards - awarded_slots_used
        if available != computed:
            findings.append({
                'identity': 'available_award_capacity == max_awards - awarded_slots_used',
                'served': str(available),
                'computed': str(computed),
            })

    if n_checked == 0:
        return {'status':'inconclusive','findings':[],'summary':'No checkable published economic identities in the supplied fields.'}

    return {
        'status': 'findings' if findings else 'consistent',
        'findings': findings,
        'summary': (f'Recomputed {n_checked} published rail identities for listing {listing_id}; '
                    f'{len(findings)} mismatch(es). Structured public fields only, no chain verification.'),
    }


def _rail_report(target, params):
    verdict = target.get('verdict')
    quoted = target.get('quoted')
    if verdict not in {'BID', 'CAUTION', 'SKIP'} or not isinstance(quoted, dict):
        return {'status':'inconclusive','findings':[],'summary':'rail-report requires quoted public fields and a verdict.'}
    summary = f"Stranger-checkable rail report for listing {target.get('listing_id')}: {verdict}. {target.get('verdict_basis','')}"
    return {'status':'consistent','findings':[],'summary':summary,'quoted':quoted,'verdict':verdict,'source':target.get('source')}


def _receipt_report(target, params):
    """Stranger-checkable receipt anatomy for a payout-binding.

    Unlike `_rail_report` (which quotes the LISTING's own economics),
    `target` here quotes fields from the referenced payout-binding/receipt
    itself (see opportunities.build_artifact's walk:'binding' handling,
    which populates `quoted`/`verdict`/`verdict_basis` from a live GET
    /api/payout-bindings/<id>). Pure formatting; executes nothing."""
    verdict = target.get('verdict')
    quoted = target.get('quoted')
    if verdict not in {'PAID', 'PAYABLE', 'UNPROVEN'} or not isinstance(quoted, dict):
        return {'status':'inconclusive','findings':[],'summary':'receipt-report requires quoted payout-binding fields and a verdict.'}
    summary = f"Stranger-checkable receipt anatomy for payout-binding {target.get('binding_id')}: {verdict}. {target.get('verdict_basis','')}"
    return {'status':'consistent','findings':[],'summary':summary,'quoted':quoted,'verdict':verdict,'source':target.get('source')}


def _batch_cadence(target, params):
    """Deterministic, stranger-checkable settlement-receipt batch cadence.

    `target['receipts']` is a pre-walked list of
    {'binding_id':int, 'receipt_id':.., 'block_timestamp':num, 'created_at':num}
    rows from GET /api/payouts (the walk itself lives outside this pure
    skill -- see opportunities.walk_payout_receipts). Executes nothing;
    deterministic; never raises.

    Batch definition (documented here so a stranger can reproduce it):
    normalize each receipt's block_timestamp to seconds (values > 1e12 are
    treated as milliseconds and divided by 1000); drop receipts without a
    finite numeric block_timestamp; sort ascending by normalized timestamp,
    tie-broken by binding_id; then greedily walk the sorted receipts,
    starting a new cluster whenever a receipt's timestamp is more than
    `window_seconds` after the PREVIOUS receipt's timestamp. A cluster of
    size >= 2 is a "batch"; its span_seconds is its last-minus-first
    timestamp.
    """
    receipts = target.get('receipts') if isinstance(target, dict) else None
    if not isinstance(receipts, list) or not receipts:
        return {'status': 'inconclusive', 'findings': [],
                'summary': 'batch-cadence requires a receipts list walked from /api/payouts.'}

    window = target.get('window_seconds', 60)
    if isinstance(window, bool) or not isinstance(window, (int, float)) or not math.isfinite(window) or window <= 0:
        window = 60

    normalized = []
    for row in receipts:
        if not isinstance(row, dict):
            continue
        ts = row.get('block_timestamp')
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
            continue
        if ts > 1e12:
            ts = ts / 1000
        binding_id = row.get('binding_id')
        normalized.append((ts, binding_id))

    def _sort_key(pair):
        ts, binding_id = pair
        if isinstance(binding_id, bool) or not isinstance(binding_id, (int, float)):
            binding_id = 0
        return (ts, binding_id)
    normalized.sort(key=_sort_key)

    receipt_count = len(normalized)
    clusters, current, prev_ts = [], [], None
    for ts, binding_id in normalized:
        if current and (ts - prev_ts) <= window:
            current.append((ts, binding_id))
        else:
            if current:
                clusters.append(current)
            current = [(ts, binding_id)]
        prev_ts = ts
    if current:
        clusters.append(current)

    batch_clusters = [c for c in clusters if len(c) >= 2]
    batch_count = len(batch_clusters)
    batches = [{'members': [binding_id for _, binding_id in c],
                'span_seconds': int(c[-1][0] - c[0][0])} for c in batch_clusters]

    if batch_count >= 2:
        starts = [c[0][0] for c in batch_clusters]
        diffs = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
        median = int(round(statistics.median(diffs)))
        cadence_statement = f"{batch_count} batches, n>=2, median inter-batch interval {median}s"
    else:
        cadence_statement = f"fewer than two batches exist, so cadence is not identifiable from this data (n={batch_count})"

    summary = (cadence_statement +
               f" ({receipt_count} receipts, {batch_count} batches; window {window}s; "
               f"clusters over transfer block_timestamp).")
    return {'status': 'consistent', 'findings': [], 'summary': summary,
            'receipt_count': receipt_count, 'batch_count': batch_count, 'batches': batches,
            'window_seconds': window, 'cadence_statement': cadence_statement,
            'source': target.get('source')}


def run(skill, target, params, output_dir, binding=None):
    if skill not in SKILLS: raise ValueError('Unsupported skill')
    encoded = json.dumps({'target':target,'params':params}, allow_nan=False).encode()
    if len(encoded)>256000: raise ValueError('Audit input exceeds 256 KB')
    if not isinstance(target,dict) or not isinstance(params,dict): raise ValueError('Object input required')
    if skill == 'gate-probe': result = _gate(target,params)
    elif skill == 'leak-probe': result = _leak(target,params)
    elif skill == 'rail-audit': result = _rail(target,params)
    elif skill == 'rail-report': result = _rail_report(target,params)
    elif skill == 'receipt-report': result = _receipt_report(target,params)
    elif skill == 'rail-derivation-check': result = _rail_derivation(target,params)
    elif skill == 'batch-cadence': result = _batch_cadence(target,params)
    elif skill == 'self-redteam':
        regression = run_regression()
        result = {'status':'findings' if regression['missed'] else 'consistent','summary':'Local deterministic attack corpus regression; not exhaustive security assurance.','findings':regression}
    else:
        result = {'status':'inconclusive','summary':'Unsupported proof format: an authenticated checkpoint and pinned protocol verifier are required. No inclusion verification performed.','findings':[]}
    destination = Path(output_dir).resolve() / uuid.uuid4().hex
    destination.mkdir(parents=True)
    # Sanitized input permits reproduction, except values intentionally redacted.
    binding = binding or {}
    evidence = {
        **redact(binding),
        'skill':skill,
        'input':redact({'target':target,'params':params}),
        'reproduction': {
            'skill': skill,
            'input_is_embedded': True,
            'executes_community_commands': False,
        },
        **redact(result),
    }
    content = (json.dumps(evidence,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf-8')
    path = destination / 'evidence.json'
    path.write_bytes(content)
    return {**result, **redact(binding), 'evidence_files':[str(path)], 'hash':hashlib.sha256(content).hexdigest(), 'repro_cmd':'python -m f916.tools.sandbox --input '+shlex.quote(str(path))+' --output artifacts/reproduced', 'limitations':'Inputs containing secrets are redacted; provide originals privately for exact reproduction.'}
