"""All imported fields can filter synthetic demo data, indexed or otherwise."""
import csv
from dataclasses import replace
import io
import hashlib

import pytest

from conftest import dataset, import_data
from test_access_extraction import new_user, login, extract


@pytest.fixture
def demo(system):
    client, _, worker, settings = system
    worker.settings = replace(settings, batch_rows=5000)
    columns = [{'name':name, 'source':name, 'indexed':i<4, 'kind':'date' if name=='生日' else 'text'}
               for i,name in enumerate(['key', 'label', 'description', 'category', 'CtfId', '生日', '地址'])]
    dataset_id = dataset(client, columns)
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow([c['name'] for c in columns])
    for i in range(1, 206):
        writer.writerow([f'{i:06d}', f'演示对象{i}', '合成数据', 'demo', f'DEMO-{i:06d}',
                         f'200001{(i % 28)+1:02d}', f'演示园区{["A", "B", "C"][i % 3]}区'])
    _, job = import_data(system, stream.getvalue().encode('utf-8'), dataset_id)
    assert job['status']=='completed'
    return dataset_id, job['id']


def query(client, dataset_id, filters, **options):
    response = client.post(f'/api/v1/datasets/{dataset_id}/query', json={'filters':filters, **options})
    assert response.status_code==200, response.text
    return response.json()


@pytest.mark.parametrize('field,op,value,expected', [
    ('CtfId', 'eq', 'DEMO-000010', [10]),
    ('生日', 'eq', '20000111', list(range(10,206,28))),
    ('地址', 'prefix', '演示园区B', list(range(1,206,3))),
])
def test_fields_after_first_four_are_queryable(system, demo, field, op, value, expected):
    client, *_ = system
    dataset_id, _ = demo
    result = query(client, dataset_id, [{'field':field, 'op':op, 'value':value}], limit=200)
    assert [row['_row'] for row in result['rows']]==expected
    assert query(client, dataset_id, [{'field':field, 'op':op, 'value':value}], limit=200)['cache_hit']


def test_unindexed_date_range_and_indexed_combination(system, demo):
    client, *_ = system
    dataset_id, _ = demo
    filters = [{'field':'生日','op':'gte','value':'2000-01-10'},
               {'field':'生日','op':'lte','value':'20000112'},
               {'field':'key','op':'eq','value':'000010'}]
    result = query(client, dataset_id, filters, explain=True)
    assert [row['_row'] for row in result['rows']]==[10]
    assert any('SEARCH' in step and 'ix_0' in step for step in result['plan'])
    invalid = client.post(f'/api/v1/datasets/{dataset_id}/query', json={
        'filters':[{'field':'生日','op':'prefix','value':'2000'}]})
    assert invalid.status_code==422
    invalid = client.post(f'/api/v1/datasets/{dataset_id}/query', json={
        'filters':[{'field':'生日','value':'2000-02-30'}]})
    assert invalid.status_code==422


def test_unindexed_pagination_order_and_signed_cursor(system, demo):
    client, *_ = system
    dataset_id, _ = demo
    filters = [{'field':'地址','op':'prefix','value':'演示园区'}]
    expected = sorted(range(1,206), key=lambda i: (["A", "B", "C"][i%3],i))
    numbers, cursor = [], None
    for _ in range(40):
        result = query(client, dataset_id, filters, limit=7, cursor=cursor)
        numbers.extend(row['_row'] for row in result['rows'])
        cursor = result['next_cursor']
        if cursor is None:
            break
    assert cursor is None and numbers==expected
    first = query(client, dataset_id, filters, limit=7)
    wrong = client.post(f'/api/v1/datasets/{dataset_id}/query', json={
        'filters':[{'field':'CtfId','op':'prefix','value':'DEMO'}], 'cursor':first['next_cursor']})
    assert wrong.status_code==422


def test_unindexed_queries_keep_personal_annotations_and_readonly_source(system, demo):
    client, catalog, _, settings = system
    dataset_id, version = demo
    path = settings.data_dir/'versions'/f'{version}.sqlite'
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    new_user(client, 'demo-reader')
    user = login(client, 'demo-reader')
    batch = extract(client, dataset_id, user)['batch']['id']
    response = client.patch(f'/api/v1/extractions/{batch}/rows/1', headers=user,
                            json={'revision':0,'marked':True,'note':'合成演示，已核对'})
    assert response.status_code==200
    client.headers.update(user)
    result = query(client, dataset_id, [{'field':'CtfId','value':'DEMO-000001'}])
    assert result['annotations']['1']['note']=='合成演示，已核对'
    assert result['annotations']['1']['marked']
    assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
    assert not catalog.get('datasets',dataset_id)['columns'][4]['indexed']


def test_dataset_without_any_indexes_supports_filters(system):
    client, *_ = system
    dataset_id = dataset(client, [{'name':'event','source':'event','indexed':False},
                                  {'name':'quantity','source':'quantity','kind':'integer','indexed':False}])
    _, job = import_data(system,b'event,quantity\ndemo-a,2\ndemo-b,12\ndemo-c,20\n',dataset_id)
    assert job['status']=='completed'
    result = query(client,dataset_id,[{'field':'quantity','op':'gte','value':12}],limit=1)
    assert result['rows'][0]['event']=='demo-b'
    assert query(client,dataset_id,[{'field':'quantity','op':'gte','value':12}],limit=1,
                 cursor=result['next_cursor'])['rows'][0]['event']=='demo-c'


def test_unindexed_queries_keep_time_budget(system, demo):
    client, _, _, settings = system
    dataset_id, _ = demo
    client.app.state.queries.settings=replace(settings,query_timeout=1e-9)
    response = client.post(f'/api/v1/datasets/{dataset_id}/query',json={
        'filters':[{'field':'地址','op':'prefix','value':'演示园区'}]})
    assert response.status_code==503, response.text
    assert 'time budget' in response.json()['detail']
    assert response.headers['Retry-After']=='1'
