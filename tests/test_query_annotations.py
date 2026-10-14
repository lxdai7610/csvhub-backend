"""Query joins use durable personal annotations, never another reader's cached notes."""
from dataclasses import replace
import json

import pytest
from fastapi.testclient import TestClient

from csvhub.api import create_app
from test_access_extraction import inventory, new_user, login, extract


def query(client, dataset_id, headers=None, **body):
    response = client.post(f'/api/v1/datasets/{dataset_id}/query', headers=headers or {}, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def patch(client, batch, row, headers=None, **body):
    response = client.patch(f'/api/v1/extractions/{batch}/rows/{row}', headers=headers or {}, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_query_annotations_are_durable_fresh_and_user_scoped(system):
    client, _, _, settings = system
    dataset_id, _ = inventory(system, count=205)
    new_user(client, 'annotator-a')
    a = login(client, 'annotator-a')
    new_user(client, 'annotator-b')
    b = login(client, 'annotator-b')
    first = query(client, dataset_id, a)
    assert first['annotations']['1'] == {'marked': False, 'note': '', 'revision': 0, 'updated_at': None}
    batch = extract(client, dataset_id, a)['batch']['id']
    note = '<img src=x onerror="alert(1)"> 已检查\n第二行'
    patch(client, batch, 1, a, revision=0, marked=True, note=note)
    updated = query(client, dataset_id, a)
    assert updated['cache_hit'] and updated['annotations']['1']['marked']
    assert updated['annotations']['1']['note'] == note and updated['annotations']['1']['updated_at']
    assert updated['rows'] == first['rows']  # Source business fields are not changed.
    other = query(client, dataset_id, b)
    admin = query(client, dataset_id)
    assert other['cache_hit'] and admin['cache_hit']
    assert other['annotations']['1']['note'] == admin['annotations']['1']['note'] == ''
    assert not other['annotations']['1']['marked']
    # Every cache entry remains shared source data without a private note payload.
    assert all('annotations' not in json.loads(entry[1]) for entry in client.app.state.queries.cache.items.values())
    with TestClient(create_app(settings)) as restarted:
        restored = query(restarted, dataset_id, a)
        assert restored['annotations']['1']['note'] == note and restored['annotations']['1']['marked']
    patch(client, batch, 1, a, revision=1, marked=False, note='')
    cleared = query(client, dataset_id, a)
    assert cleared['cache_hit'] and cleared['annotations']['1']['note'] == ''
    assert not cleared['annotations']['1']['marked'] and cleared['annotations']['1']['revision'] == 2


def test_query_annotations_on_indexed_pages_empty_results_and_explain(system):
    client, *_ = system
    dataset_id, _ = inventory(system, count=205, rejected=True)
    batch = extract(client, dataset_id)['batch']['id']
    patch(client, batch, 51, revision=0, marked=True, note='来源 51，拒绝记录之后')
    filters = [{'field':'key','op':'prefix','value':'000'}]
    first = query(client, dataset_id, filters=filters, limit=49)
    assert first['rows'][-1]['_row'] == 49 and '51' not in first['annotations']
    second = query(client, dataset_id, filters=filters, limit=49, cursor=first['next_cursor'])
    assert second['rows'][0]['_row'] == 51 and second['annotations']['51']['marked']
    assert set(second['annotations']) == {str(row['_row']) for row in second['rows']}
    explained = query(client, dataset_id, filters=[{'field':'key','op':'eq','value':'000051'}], explain=True)
    assert explained['plan'] and explained['annotations']['51']['note'].startswith('来源 51')
    empty = query(client, dataset_id, filters=[{'field':'key','op':'eq','value':'missing'}])
    assert empty['rows'] == [] and empty['annotations'] == {}


def test_new_version_does_not_inherit_old_annotation(system):
    client, *_ = system
    dataset_id, _ = inventory(system, count=105)
    batch = extract(client, dataset_id)['batch']['id']
    patch(client, batch, 1, revision=0, marked=True, note='old version note')
    old = query(client, dataset_id)
    assert old['annotations']['1']['marked']
    _, new_job = inventory(system, count=105, dataset_id=dataset_id)
    current = query(client, dataset_id)
    assert current['version'] == new_job['id'] != old['version']
    assert not current['annotations']['1']['marked'] and current['annotations']['1']['note'] == ''
    assert client.get('/api/v1/extractions/'+batch).json()['rows'][0]['note'] == 'old version note'


def test_business_column_names_cannot_collide_with_annotation_metadata(system):
    client, catalog, worker, settings = system
    from conftest import dataset, import_data
    worker.settings = replace(settings, batch_rows=5000)
    columns = [{'name':name,'source':name} for name in ('marked','note','annotations')]
    dataset_id = dataset(client, columns)
    import_data(system, b'marked,note,annotations\nbusiness status,business note,business value\n', dataset_id)
    batch = extract(client, dataset_id)['batch']['id']
    patch(client, batch, 1, revision=0, marked=True, note='personal note')
    result = query(client, dataset_id)
    assert result['rows'][0]['note'] == 'business note'
    assert result['rows'][0]['marked'] == 'business status'
    assert result['rows'][0]['annotations'] == 'business value'
    assert result['annotations']['1']['note'] == 'personal note'


@pytest.mark.parametrize('mode', ['warmup', 'indexed', 'explain'])
def test_notes_response_budget_preserves_cursor_sequence(system, mode):
    client, _, _, settings = system
    dataset_id, _ = inventory(system, count=105)
    batch = extract(client, dataset_id)['batch']['id']
    for row in range(1, 5):
        patch(client, batch, row, revision=0, marked=True, note='中'*500)
    small = create_app(replace(settings, max_response_bytes=2400))
    with TestClient(small) as limited:
        limited.headers['Authorization'] = 'Bearer '+settings.api_token
        filters = [] if mode == 'warmup' else [{'field':'key','op':'prefix','value':'000'}]
        first = query(limited, dataset_id, filters=filters, explain=mode == 'explain')
        assert len(first['rows']) == 1 and first['next_cursor'] and first['annotations']['1']['marked']
        assert len(json.dumps(first, ensure_ascii=False, separators=(',',':')).encode()) <= 2400
        numbers, cursor = [row['_row'] for row in first['rows']], first['next_cursor']
        for _ in range(20):
            if cursor is None:
                break
            page = query(limited, dataset_id, filters=filters, explain=mode == 'explain', cursor=cursor)
            assert len(json.dumps(page, ensure_ascii=False, separators=(',',':')).encode()) <= 2400
            numbers += [row['_row'] for row in page['rows']]
            cursor = page['next_cursor']
        assert cursor is None and numbers == list(range(1, 106))


def test_oversize_note_response_rejects_without_losing_batch_or_records(system):
    client, _, _, settings = system
    dataset_id, _ = inventory(system, count=105)
    batch = extract(client, dataset_id)['batch']['id']
    patch(client, batch, 1, revision=0, marked=True, note='中'*500)
    with TestClient(create_app(replace(settings, max_response_bytes=200))) as limited:
        response = limited.post(f'/api/v1/datasets/{dataset_id}/query', json={},
                                headers={'Authorization':'Bearer '+settings.api_token})
        assert response.status_code == 422
    full = query(client, dataset_id)
    assert full['rows'][0]['_row'] == 1 and full['annotations']['1']['note'] == '中'*500
