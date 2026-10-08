"""Pinned GitHub read pagination and revision boundaries."""
import json
from server.sdlc.framework_observe import load_policy_snapshot
from server.tests.test_framework_observe import runner_with
from server.tests.test_framework_receipts import configured, review, row


def response(data):
    return {'data': {'repository': data}}


def subject(**kw):
    return {'headRefOid': 'b'*40, 'baseRefOid': 'c'*40, **kw}


def page(field, nodes, more=False, cursor=None, **kw):
    return response({'pullRequest': subject(**{field:{'nodes':nodes,'pageInfo':{'hasNextPage':more,'endCursor':cursor}}}, **kw)})


def prefix():
    return [response({'defaultBranchRef':{'target':{'oid':'a'*40}},'pullRequest':subject(author={'login':'alice'})}),
            response({'object':{'oid':'d'*40,'text':json.dumps({'frameworks':{'lock':'.agents/frameworks.lock.json'}})}}),
            response({'object':{'oid':'e'*40,'text':json.dumps({'schema_version':1,'frameworks':list(configured().frameworks)})}})]


def load(tail):
    run, calls = runner_with(prefix()+tail)
    return load_policy_snapshot('o/r', pull_request=42, run=run), calls


def test_complete_pagination_and_binding():
    s, calls = load([page('files',[{'path':'docs/readme.md','changeType':'MODIFIED'}], True, 'next'),
                     page('files',[{'path':'web/a.ts','changeType':'ADDED'}]),
                     page('reviews',[],True,'next-review'), page('reviews',[]),
                     response({'pullRequest':subject()})])
    assert s.scope_complete and s.reviews_complete
    assert s.changed_paths == ('docs/readme.md','web/a.ts')
    assert row(s)['applicability'] == 'applicable'
    assert any('cursor=next-review' in argv for argv,_ in calls)


def test_rename_prevents_negative_skip():
    s,_=load([page('files',[{'path':'docs/readme.md','changeType':'RENAMED'}]),page('reviews',[]),response({'pullRequest':subject()})])
    assert not s.scope_complete
    assert row(s)['applicability'] == 'unknown'


def test_revision_changes_during_pagination_or_final_read():
    for tail in ([page('files',[], headRefOid='f'*40)],
                 [page('files',[]),page('reviews',[]),response({'pullRequest':subject(baseRefOid='f'*40)})]):
        s,_=load(tail)
        assert not s.scope_complete and not s.reviews_complete and not s.changed_paths


def test_page_cap_and_failed_review_page(monkeypatch):
    monkeypatch.setattr('server.sdlc.framework_observe.MAX_PAGES',1)
    s,_=load([page('files',[{'path':'web/a.ts','changeType':'MODIFIED'}],True,'next'),page('reviews',[]),response({'pullRequest':subject()})])
    assert not s.scope_complete and row(s)['applicability']=='applicable'
    s,_=load([page('files',[{'path':'docs/a.md','changeType':'MODIFIED'}]),{'errors':[{'message':'SECRET'}]}])
    assert not s.scope_complete and 'SECRET' not in repr(s)


def test_bad_cursor_page_and_path():
    for tail in ([page('files',[],True,None)], [page('files',[{'path':'../secret','changeType':'MODIFIED'}])],
                 [page('files',[None])]):
        s,_=load(tail)
        assert not s.scope_complete


def test_raw_review_body_never_in_observation():
    from server.sdlc.gates import observe_framework_evidence
    s=configured()
    r=review(s, artifacts=[{'path':'secret-token','sha256':'f'*64}])
    s,_=load([page('files',[{'path':'web/a.ts','changeType':'MODIFIED'}]),page('reviews',[r]),response({'pullRequest':subject()})])
    assert 'secret-token' not in json.dumps(observe_framework_evidence(s))
