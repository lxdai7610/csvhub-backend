from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from conftest import import_data
from csvhub.access import ADMIN_ID, Accounts, password_matches
from csvhub.api import create_app


PASSWORD = "Synthetic-pass-123"


def new_user(client, name="worker-one"):
    response = client.post('/api/v1/users', json={'username': name, 'password': PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


def login(client, name="worker-one", password=PASSWORD):
    response = client.post('/api/v1/auth/login', json={'username': name, 'password': password})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['token']}


def inventory(system, count=1105, dataset_id=None, rejected=False):
    system[2].settings = replace(system[3], batch_rows=5000)
    content = 'key,label\n' + ''.join(f'{i:06d},widget-{i}\n' if not rejected or i != 50 else 'invalid\n' for i in range(1, count + 1))
    return import_data(system, content.encode(), dataset_id, on_error='skip')


def extract(client, dataset_id, headers=None, size=100, request_id=None):
    response = client.post(f'/api/v1/datasets/{dataset_id}/extractions', headers=headers or {},
                           json={'size': size, 'request_id': request_id or str(uuid4())})
    assert response.status_code == 201, response.text
    return response.json()


def test_password_sessions_and_restart(system):
    client, catalog, _, settings = system
    user = new_user(client, '测试用户')
    assert user['role'] == 'user' and user['password_set'] == 1
    assert PASSWORD not in json.dumps(user) and 'password_hash' not in user
    with closing(catalog.connect()) as db:
        stored = db.execute('SELECT password_hash FROM users WHERE id=?', (user['id'],)).fetchone()[0]
    assert stored != PASSWORD and password_matches(PASSWORD, stored)
    assert not password_matches('wrong-password', stored)
    headers = login(client, '测试用户')
    assert client.get('/api/v1/auth/me', headers=headers).json()['id'] == user['id']
    other = Accounts(catalog, settings.api_token)
    assert other.authenticate(headers['Authorization']).id == user['id']
    with closing(catalog.connect()) as db:
        assert headers['Authorization'][7:] not in str(db.execute('SELECT * FROM sessions').fetchall())
    assert client.post('/api/v1/auth/logout', headers=headers).status_code == 200
    assert client.get('/api/v1/auth/me', headers=headers).status_code == 401


def test_user_validation_and_no_credential_echo(system):
    client, *_ = system
    user = new_user(client)
    assert client.post('/api/v1/users', json={'username': 'WORKER-ONE', 'password': PASSWORD}).status_code == 409
    for payload in ({'username':'new', 'password':PASSWORD, 'role':'admin'},
                    {'username':'\ud800', 'password':PASSWORD}, {'username':'new','password':'short'}):
        response = client.post('/api/v1/users', content=json.dumps(payload), headers={'Content-Type':'application/json'})
        assert response.status_code == 422
        assert PASSWORD not in response.text and '"input"' not in response.text
    for name in ('worker-one', 'unknown-user'):
        response = client.post('/api/v1/auth/login', json={'username':name, 'password':'wrong-pass'})
        assert response.status_code == 401 and response.json()['detail'] == 'Incorrect username or password'
    assert client.patch('/api/v1/users/'+user['id'], json={'role':'admin'}).status_code == 422


@pytest.mark.parametrize('method,path,body', [
    ('GET','/users',None),('POST','/users',{'username':'intruder','password':PASSWORD}),
    ('PATCH','/users/unknown',{'enabled':True}),('GET','/stats',None),('GET','/jobs',None),
    ('GET','/jobs/unknown',None),('GET','/jobs/unknown/errors',None),('POST','/jobs/unknown/cancel',None),
    ('POST','/datasets',{'name':'unauthorized','columns':[{'name':'key','source':'key'}]}),
    ('GET','/datasets/unknown/versions',None),('POST','/datasets/unknown/imports',{'upload_id':'unknown'}),
    ('POST','/uploads',{'filename':'test.csv','size':20,'sha256':'0'*64}),
    ('GET','/uploads/unknown',None),('PUT','/uploads/unknown/chunks?offset=0',None),
    ('POST','/uploads/unknown/complete',None),('GET','/uploads/unknown/preview',None),
])
def test_server_admin_permissions(system, method, path, body):
    client, *_ = system
    new_user(client)
    headers = login(client)
    response = client.request(method, '/api/v1'+path, headers=headers, **({'json':body} if body is not None else {}))
    assert response.status_code == 403, response.text


def test_disable_reset_expiry_and_admin_setup(system):
    client, catalog, *_ = system
    user = new_user(client)
    headers = login(client)
    assert client.patch('/api/v1/users/'+user['id'], json={'password':'New-pass-123'}).status_code == 200
    assert client.get('/api/v1/auth/me', headers=headers).status_code == 401
    headers = login(client, password='New-pass-123')
    assert client.patch('/api/v1/users/'+user['id'], json={'enabled':False}).status_code == 200
    assert client.get('/api/v1/auth/me', headers=headers).status_code == 401
    assert client.post('/api/v1/auth/login', json={'username':user['username'],'password':'New-pass-123'}).status_code == 401
    client.patch('/api/v1/users/'+user['id'], json={'enabled':True})
    headers = login(client, password='New-pass-123')
    with catalog.transaction() as db:
        db.execute('UPDATE sessions SET expires_at=0')
    assert client.get('/api/v1/auth/me', headers=headers).status_code == 401
    assert client.patch('/api/v1/users/'+ADMIN_ID, json={'enabled':False}).status_code == 409
    assert client.patch('/api/v1/users/'+ADMIN_ID, json={'username':'manager','password':PASSWORD}).status_code == 200
    admin = login(client, 'manager')
    assert client.get('/api/v1/users', headers=admin).status_code == 200
    assert client.get('/api/v1/auth/me').json()['username'] == 'manager'


def test_sequential_batches_rejected_gaps_replay_and_tail(system):
    client, _, _, settings = system
    dataset_id, _ = inventory(system, rejected=True)
    request_id = str(uuid4())
    first = extract(client, dataset_id, size=1000, request_id=request_id)
    assert len(first['rows']) == 1000 and first['batch']['has_more']
    assert first['rows'][49]['row_no'] == 51
    replay = extract(client, dataset_id, size=1000, request_id=request_id)
    assert replay['batch']['id'] == first['batch']['id']
    assert client.post(f'/api/v1/datasets/{dataset_id}/extractions', json={'size':100,'request_id':request_id}).status_code == 409
    second = extract(client, dataset_id, size=200)
    assert len(second['rows']) == 104 and second['rows'][0]['row_no'] == 1002
    assert not second['batch']['has_more']
    assert client.post(f'/api/v1/datasets/{dataset_id}/extractions', json={'size':100,'request_id':str(uuid4())}).status_code == 409
    status = client.get(f'/api/v1/datasets/{dataset_id}/extractions').json()
    assert status['extracted'] == 1104 and status['remaining'] == 0
    restarted = create_app(settings).state.extractions.get(ADMIN_ID, first['batch']['id'])
    assert restarted['rows'][0]['values']['key'] == '000001'


@pytest.mark.parametrize('size', [0, 99, 150, 1001, 100.0, '100', True])
def test_extraction_size_validation(system, size):
    client, *_ = system
    assert client.post('/api/v1/datasets/unknown/extractions', json={'size':size,'request_id':str(uuid4())}).status_code == 422


def test_user_ownership_annotations_and_query(system):
    client, *_ = system
    dataset_id, _ = inventory(system, count=305)
    user = new_user(client)
    a = login(client)
    new_user(client, 'worker-two')
    b = login(client, 'worker-two')
    one, two = extract(client, dataset_id, a), extract(client, dataset_id, b)
    assert one['rows'][0]['row_no'] == two['rows'][0]['row_no'] == 1
    path = '/api/v1/extractions/'+one['batch']['id']
    assert client.get(path, headers=b).status_code == 404
    assert client.get(path).status_code == 404  # Administrator's own work also stays independent.
    response = client.patch(path+'/rows/1', headers=a, json={'revision':0,'marked':True,'note':'<script>alert(1)</script>\n已检查'})
    assert response.status_code == 200
    assert response.json()['revision'] == 1
    assert client.patch(path+'/rows/1', headers=a, json={'revision':0,'note':'stale'}).status_code == 409
    assert client.patch(path+'/rows/1', headers=b, json={'revision':1,'marked':False}).status_code == 404
    assert client.patch(path+'/rows/200', headers=a, json={'revision':0,'marked':True}).status_code == 404
    assert client.patch(path+'/rows/1', headers=a, json={'revision':1,'note':'x'*501}).status_code == 422
    assert client.patch(path+'/rows/1', headers={**a,'Content-Type':'application/json'},
                        content=json.dumps({'revision':1,'note':'\ud800'})).status_code == 422
    restored = client.get(path, headers=a).json()['rows'][0]
    assert restored['marked'] and '已检查' in restored['note']
    assert not client.get('/api/v1/extractions/'+two['batch']['id'], headers=b).json()['rows'][0]['marked']
    client.patch(path+'/rows/1', headers=a, json={'revision':1,'marked':False})
    assert '已检查' in client.get(path, headers=a).json()['rows'][0]['note']
    assert client.get('/api/v1/overview', headers=a).json()['unmarked'] == 100
    assert client.get('/api/v1/datasets', headers=a).status_code == 200
    assert client.get('/api/v1/datasets/'+dataset_id, headers=a).status_code == 200
    result = client.post(f'/api/v1/datasets/{dataset_id}/query', headers=a, json={'filters':[]}).json()
    assert len(result['rows']) == 50 and 'note' not in result['rows'][0]


def test_version_independence(system):
    client, *_ = system
    dataset_id, old = inventory(system, count=205)
    first = extract(client, dataset_id)
    path = '/api/v1/extractions/'+first['batch']['id']
    client.patch(path+'/rows/1', json={'revision':0,'marked':True,'note':'old version'})
    _, new = inventory(system, count=105, dataset_id=dataset_id)
    second = extract(client, dataset_id)
    assert second['batch']['version_id'] == new['id'] != old['id']
    assert second['rows'][0]['row_no'] == 1 and not second['rows'][0]['marked']
    assert client.get(path).json()['rows'][0]['note'] == 'old version'


def test_concurrent_allocations_no_overlap(system):
    client, catalog, _, settings = system
    dataset_id, _ = inventory(system, count=305)
    service = create_app(settings).state.extractions
    from csvhub.extraction import ExtractionCreate
    with ThreadPoolExecutor(max_workers=2) as pool:
        batches = list(pool.map(lambda _: service.create(ADMIN_ID, dataset_id, ExtractionCreate(size=100,request_id=uuid4())), range(2)))
    sets = [{r['row_no'] for r in batch['rows']} for batch in batches]
    assert not sets[0] & sets[1] and len(sets[0] | sets[1]) == 200


def test_response_budget_does_not_skip_records(system):
    client, catalog, _, settings = system
    dataset_id, _ = inventory(system, count=105)
    service = create_app(replace(settings, max_response_bytes=18000)).state.extractions
    from csvhub.extraction import ExtractionCreate
    first = service.create(ADMIN_ID, dataset_id, ExtractionCreate(size=100,request_id=uuid4()))
    second = service.create(ADMIN_ID, dataset_id, ExtractionCreate(size=100,request_id=uuid4()))
    assert 0 < len(first['rows']) < 100 and first['batch']['has_more']
    assert second['rows'][0]['row_no'] == first['rows'][-1]['row_no'] + 1
    assert len(json.dumps(first).encode()) <= 18000


def test_anonymous_and_login_rate_limit(system):
    client, *_ = system
    for path in ('/users','/datasets','/overview','/extractions/missing'):
        assert client.get('/api/v1'+path, headers={'Authorization':''}).status_code == 401
    accounts = client.app.state.accounts
    for _ in range(30):
        accounts.throttle('synthetic-client')
    with pytest.raises(Exception) as error:
        accounts.throttle('synthetic-client')
    assert error.value.status_code == 429
