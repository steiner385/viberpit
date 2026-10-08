"""Authenticated records remain observations, never transition authorization."""
import json
from dataclasses import replace
import pytest
from server.sdlc.framework_observe import policy
from server.sdlc.gates import observe_framework_evidence
from server.tests.test_framework_observe import snapshot


def configured(**kw):
    entry = {'id': 'impeccable', 'state': 'candidate', 'concerns': ['ui-design'],
             'observation': {'mode': 'observe', 'trusted_github_reviewers': ['bob'],
                'concerns': {'ui-design': {'tier': 'required_review', 'include_paths': ['web/**', 'server/api/**'],
                                         'exclude_paths': ['docs/**']}}}}
    return snapshot(frameworks=(entry,), base_sha='c'*40, changed_paths=('web/app.tsx',),
                    scope_complete=True, framework_digest='d'*64, policy_digest='e'*64, **kw)


def review(s, **updates):
    body = {'version': 1, 'framework': 'impeccable', 'concern': 'ui-design', 'repo': s.repo,
            'base_sha': s.base_sha, 'head_sha': s.head_sha, 'framework_digest': s.framework_digest,
            'policy_digest': s.policy_digest, 'scope_digest': policy.scope_digest(s.changed_paths),
            'artifacts': [{'path': 'report.md', 'sha256': 'f'*64}], 'findings': []}
    body.update(updates)
    return {'id': 'R1', 'author': {'login': 'bob'}, 'state': 'COMMENTED',
            'submittedAt': '2026-10-08T12:00:00Z', 'commit': {'oid': s.head_sha},
            'body': '```viberpit-framework-review\n'+json.dumps(body)+'\n```'}


def row(s):
    return observe_framework_evidence(s)['frameworks'][0]['obligations'][0]


@pytest.mark.parametrize('paths,complete,expected', [
    (('web/app.tsx',), False, 'applicable'), (('docs/a.md',), True, 'not_applicable'),
    (('server/db/a.py',), True, 'unknown'), (('docs/a.md',), False, 'unknown'),
    ((), True, 'unknown'), (('server/api/ui.py',), True, 'applicable')])
def test_conservative_scope(paths, complete, expected):
    assert row(replace(configured(), changed_paths=paths, scope_complete=complete))['applicability'] == expected


def test_authenticated_attestation_not_admission():
    s = configured()
    s = replace(s, reviews=(review(s),), reviews_complete=True)
    assert row(s)['evidenceStatus'] == 'review_recorded'
    assert observe_framework_evidence(s)['blocking'] is False
    assert observe_framework_evidence(s)['frameworks'][0]['state'] == 'candidate'


@pytest.mark.parametrize('field,value', [('repo','x/y'), ('base_sha','f'*40), ('head_sha','f'*40),
    ('framework_digest','f'*64), ('policy_digest','f'*64), ('scope_digest','f'*64),
    ('reviewer','bob'), ('findings',[{'id':'x','disposition':'open'}])])
def test_invalid_attestation(field,value):
    s = configured()
    assert row(replace(s, reviews=(review(s, **{field:value}),), reviews_complete=True))['evidenceStatus'] != 'review_recorded'


@pytest.mark.parametrize('change', ['self','untrusted','dismissed','stale','ordinary','incomplete','revoked','latest'])
def test_provider_and_policy_failures(change):
    s = configured()
    r = review(s)
    if change == 'self': r['author']['login'] = 'alice'
    if change == 'untrusted': r['author']['login'] = 'mallory'
    if change == 'dismissed': r['state'] = 'DISMISSED'
    if change == 'stale': r['commit']['oid'] = 'f'*40
    if change == 'ordinary': r['body'] = 'Approved'
    if change == 'incomplete': s = replace(s, scope_complete=False)
    if change == 'revoked': s = replace(s, frameworks=(dict(s.frameworks[0], state='revoked'),))
    reviews = (r,)
    if change == 'latest': reviews += (dict(r, id='R2', state='DISMISSED', submittedAt='2026-10-08T13:00:00Z'),)
    assert row(replace(s, reviews=reviews, reviews_complete=True))['evidenceStatus'] != 'review_recorded'


@pytest.mark.parametrize('updates', [
    {'artifacts':[]}, {'artifacts':[{'path':'../secret','sha256':'f'*64}]},
    {'artifacts':[{'path':'a.md','sha256':'bad'}]}, {'artifacts':None},
    {'findings':None}, {'findings':[{'id':'x','disposition':'fixed','extra':'bad'}]},
    {'findings':[{'id':'x','disposition':'fixed'}]*2}, {'version':True},
    {'findings':[{'id':'','disposition':'fixed'}]},
])
def test_strict_record_fields(updates):
    s=configured()
    assert row(replace(s,reviews=(review(s,**updates),),reviews_complete=True))['evidenceStatus'] != 'review_recorded'


@pytest.mark.parametrize('updates', [
    {'isMinimized':True}, {'state':'CHANGES_REQUESTED'}, {'state':'PENDING','submittedAt':None},
    {'lastEditedAt':'2026-10-08T13:00:00Z','editor':{'login':'mallory'}},
    {'lastEditedAt':'2026-10-08T13:00:00Z','editor':None},
    {'body':'```viberpit-framework-review\nnot JSON\n```'},
    {'body':'```viberpit-framework-review\n[]\n```'},
    {'submittedAt':'yesterday'}, {'submittedAt':'2026-10-08'},
    {'body':'x'*65537},
])
def test_provider_metadata_and_malformed_records(updates):
    s=configured();r=review(s);r.update(updates)
    assert row(replace(s,reviews=(r,),reviews_complete=True))['evidenceStatus'] != 'review_recorded'


def test_no_allowlist_and_other_tiers_never_record():
    s=configured();r=review(s)
    for tier in ('required_review','deterministic','advisory'):
        entry=json.loads(json.dumps(s.frameworks[0]));entry['observation']['trusted_github_reviewers']=[]
        entry['observation']['concerns']['ui-design']['tier']=tier
        assert row(replace(s,frameworks=(entry,),reviews=(r,),reviews_complete=True))['evidenceStatus']!='review_recorded'


def test_inclusion_wins_and_double_star_matches_zero_directories():
    rule={'include_paths':['docs/*.md'],'exclude_paths':['docs/**/*.md']}
    assert policy.applicability(rule,['docs/a.md'],True)=='applicable'
    rule['include_paths']=[]
    assert policy.applicability(rule,['docs/a.md'],True)=='not_applicable'
    assert policy.applicability(rule,['docs/nested/a.md'],True)=='not_applicable'


def test_latest_pending_or_invalid_timestamp_cannot_resurrect_earlier_success():
    s=configured();earlier=review(s)
    for updates in ({'submittedAt':None,'state':'PENDING'}, {'submittedAt':'garbage'}):
        later=dict(earlier,id='R2',**updates)
        assert row(replace(s,reviews=(earlier,later),reviews_complete=True))['evidenceStatus']!='review_recorded'


def test_example_or_quoted_review_cannot_attest():
    s=configured();r=review(s)
    for body in ('Example:\n'+r['body'], '> '+r['body'], r['body']+'\n'+r['body']):
        assert row(replace(s,reviews=(dict(r,body=body),),reviews_complete=True))['evidenceStatus']!='review_recorded'


def test_adversarial_glob_is_bounded():
    assert policy.applicability({'include_paths':['**/'*100+'*a'*100+'b'],'exclude_paths':[]},
                                ['a/'*100+'a'*1000], True)=='unknown'


def test_missing_binding_in_later_record_supersedes_old_record():
    s=configured();old=review(s);new=review(s)
    body=json.loads(new['body'].split('\n')[1]);body.pop('framework')
    new.update(id='R2', submittedAt='2026-10-08T13:00:00Z',body='```viberpit-framework-review\n'+json.dumps(body)+'\n```')
    assert row(replace(s,reviews=(old,new),reviews_complete=True))['evidenceStatus']!='review_recorded'


def test_duplicate_json_fields_rejected():
    s=configured();r=review(s);r['body']=r['body'].replace('"version": 1','"version": 2, "version": 1')
    assert row(replace(s,reviews=(r,),reviews_complete=True))['evidenceStatus']!='review_recorded'


@pytest.mark.parametrize('tolerance',['cautious','balanced','bold'])
def test_recorded_review_does_not_change_decisions(tolerance):
    from server.sdlc.explain import explain, guidance_to_wire
    from server.tests.test_sdlc_explain import _ctx, _view, _art
    ctx=_ctx(_view(),[_art('spec',sha='b')],tolerance=tolerance)
    before=guidance_to_wire(explain(ctx));s=configured()
    observed=observe_framework_evidence(replace(s,reviews=(review(s),),reviews_complete=True))
    after=guidance_to_wire(explain(replace(ctx,framework_observations=observed)))
    assert after.pop('frameworkEvidence')['blocking'] is False
    assert after==before


@pytest.mark.parametrize('reverse', [False, True])
def test_same_time_review_records_do_not_order_by_opaque_ids(reverse):
    s=configured()
    good=dict(review(s),id='R9')
    dismissed=dict(review(s),id='R10',state='DISMISSED')
    records=(dismissed,good) if reverse else (good,dismissed)
    assert row(replace(s,reviews=records,reviews_complete=True))['evidenceStatus']!='review_recorded'


@pytest.mark.parametrize('commit', [None, {}])
def test_missing_provider_review_commit_is_invalid(commit):
    s=configured();r=dict(review(s),commit=commit)
    assert row(replace(s,reviews=(r,),reviews_complete=True))['evidenceStatus']!='review_recorded'


@pytest.mark.parametrize('reverse', [False, True])
def test_newer_record_supersedes_older_timestamp_ambiguity(reverse):
    s=configured()
    old=(dict(review(s),id='R9'),dict(review(s),id='R10',state='DISMISSED'))
    newer=dict(review(s),id='R11',submittedAt='2026-10-08T13:00:00Z')
    records=(newer,*old) if reverse else (*old,newer)
    assert row(replace(s,reviews=records,reviews_complete=True))['evidenceStatus']=='review_recorded'


@pytest.mark.parametrize('path,expected_ui,expected_process', [
    ('docs/architecture.md', 'not_applicable', 'not_applicable'),
    ('server/sdlc/gates.py', 'unknown', 'applicable'),
])
def test_ui_and_process_obligations_route_docs_and_sdlc_independently(path, expected_ui, expected_process):
    import copy
    s=configured()
    process={'id':'process-guidance','state':'candidate','concerns':['process'],
             'observation':{'mode':'observe','trusted_github_reviewers':[],
                            'concerns':{'process':{'tier':'required_review',
                                'include_paths':['server/sdlc/**'],'exclude_paths':['docs/**']}}}}
    s=replace(s,frameworks=(*s.frameworks,process),changed_paths=(path,))
    declarations=copy.deepcopy(s.frameworks)
    observed=observe_framework_evidence(s)
    by_id={entry['id']:entry for entry in observed['frameworks']}
    assert by_id['impeccable']['obligations'][0]['applicability']==expected_ui
    assert by_id['process-guidance']['obligations'][0]['applicability']==expected_process
    assert by_id['impeccable']['state']=='candidate'
    assert s.frameworks==declarations
    assert observed['blocking'] is False
    # Candidate observations do not change the existing transition decision.
    from server.sdlc.explain import explain, guidance_to_wire
    from server.tests.test_sdlc_explain import _ctx, _view, _art
    ctx=_ctx(_view(),[_art('spec',sha='b')],tolerance='bold')
    before=guidance_to_wire(explain(ctx))
    after=guidance_to_wire(explain(replace(ctx,framework_observations=observed)))
    after.pop('frameworkEvidence')
    assert after==before
