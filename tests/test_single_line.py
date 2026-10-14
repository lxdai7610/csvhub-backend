from contextlib import closing
from dataclasses import replace
import sqlite3

import pytest

from csvhub.worker import Worker, JobStopped
from conftest import dataset, import_data, upload


@pytest.mark.parametrize('encoding,newline', [('utf-8-sig', '\r\n'), ('utf-8', '\r'), ('gb18030', '\n')])
def test_bad_quotes_are_rejected_without_losing_the_following_record(system, encoding, newline):
    lines = ['key,label', '00001,"部件, A"', '00002,"SECRET"x', '00003,"未闭合',
             '00004,"部件 ""B"""', '', '00005,部件 C']
    dataset_id, job = import_data(system, newline.join(lines).encode(encoding),
                                 row_mode='single_line', on_error='skip', encoding=encoding)
    assert (job['status'], job['processed'], job['accepted'], job['rejected']) == ('completed', 6, 3, 3)
    result = system[0].post(f'/api/v1/datasets/{dataset_id}/query', json={}).json()
    assert [row['key'] for row in result['rows']] == ['00001', '00004', '00005']
    assert result['rows'][1]['label'] == '部件 "B"'


def test_reject_column_and_field_errors_then_build_indexes_and_cache(system):
    client, *_ = system
    columns = [{'name': 'key', 'source': 'key'}, {'name': 'quantity', 'source': 'quantity', 'kind': 'integer'},
               {'name': 'date', 'source': 'date', 'kind': 'date'}]
    dataset_id = dataset(client, columns)
    source = (b'key,quantity,date\n001,7,20261010\n002,SECRET,20261010\n'
              b'003,8,wrong-date\n004,9,20261010,extra\n005,9\n006,10,2026-10-11\n')
    _, job = import_data(system, source, dataset_id, row_mode='single_line', on_error='skip')
    assert (job['status'], job['processed'], job['accepted'], job['rejected']) == ('completed', 6, 2, 4)
    response = client.post(f'/api/v1/datasets/{dataset_id}/query', json={'filters': [{'field': 'key', 'value': '006'}]}).json()
    assert response['rows'][0]['quantity'] == 10 and response['rows'][0]['date'] == '2026-10-11'
    assert client.post(f'/api/v1/datasets/{dataset_id}/query', json={'filters': [{'field': 'key', 'value': '006'}]}).json()['cache_hit']
    assert 'SECRET' not in client.get(f"/api/v1/jobs/{job['id']}/errors").text


def test_single_line_pause_resume_preserves_rejected_counts_and_row_numbers(system):
    client, catalog, worker, settings = system
    original = worker.control

    def pause_after_checkpoint(job_id):
        if catalog.get('jobs', job_id)['processed'] >= 2:
            raise JobStopped('paused')
        original(job_id)

    worker.control = pause_after_checkpoint
    dataset_id, job = import_data(system, b'key,label\n1,A\n2,"SECRET"x\n3,B\n4,C\n',
                                 row_mode='single_line', on_error='skip')
    assert (job['status'], job['processed'], job['accepted'], job['rejected']) == ('paused', 2, 1, 1)
    assert client.post(f"/api/v1/jobs/{job['id']}/resume").status_code == 200
    Worker(settings, catalog).run_one()
    finished = catalog.get('jobs', job['id'])
    assert (finished['status'], finished['processed'], finished['accepted'], finished['rejected']) == ('completed', 4, 3, 1)
    with closing(sqlite3.connect(settings.data_dir / 'versions' / f"{job['id']}.sqlite")) as db:
        assert db.execute('SELECT row_no FROM records ORDER BY row_no').fetchall() == [(1,), (3,), (4,)]
        assert db.execute('SELECT COUNT(*) FROM errors').fetchone()[0] == 1


def test_single_line_preview_is_bounded_and_keeps_no_bad_cell_values(system):
    client, *_ = system
    upload_id = upload(client, b'key,label\n1,"SECRET"x\n2,"SECRET\n3,Valid\n')
    client.post(f'/api/v1/uploads/{upload_id}/complete')
    response = client.get(f'/api/v1/uploads/{upload_id}/preview?row_mode=single_line&limit=2')
    assert response.status_code == 200
    preview = response.json()
    assert preview['rows'] == [] and preview['preview_rejected'] == 2
    assert preview['row_mode'] == 'single_line' and len(preview['warnings']) == 2
    assert 'SECRET' not in response.text
    assert client.get(f'/api/v1/uploads/{upload_id}/preview?row_mode=single_line&limit=3').json()['rows'] == [['3', 'Valid']]


def test_bad_header_is_fatal_even_in_single_line_skip_mode(system):
    _, job = import_data(system, b'"key"x,label\n1,A\n', row_mode='single_line', on_error='skip')
    assert job['status'] == 'failed' and job['processed'] == 0
    assert job['error'] == 'CSV parse error: invalid_after_quote; record 0; physical lines 1-1'


def test_single_line_fail_mode_still_stops_at_a_bad_quote(system):
    _, job = import_data(system, b'key,label\n1,A\n2,B\n3,"SECRET"x\n4,C\n',
                         row_mode='single_line', on_error='fail')
    assert (job['status'], job['accepted'], job['rejected']) == ('failed', 2, 0)
    assert job['error'] == 'CSV parse error: invalid_after_quote; record 3; physical lines 4-4'


def test_single_line_record_size_limit_stops_instead_of_treating_partial_reads_as_rows(system):
    client, catalog, _, settings = system
    settings = replace(settings, max_record_bytes=20)
    dataset_id = dataset(client)
    upload_id = upload(client, b'key,label\n1,' + b'x' * 50 + b'\n2,Valid\n')
    client.post(f'/api/v1/uploads/{upload_id}/complete')
    job = client.post(f'/api/v1/datasets/{dataset_id}/imports', json={
        'upload_id': upload_id, 'row_mode': 'single_line', 'on_error': 'skip'}).json()
    Worker(settings, catalog).run_one()
    failed = catalog.get('jobs', job['id'])
    assert failed['status'] == 'failed' and 'record_too_large' in failed['error']


@pytest.mark.parametrize('filename', ['example.xls', 'example.xlsx'])
def test_excel_rejects_single_line_options(system, filename):
    from pathlib import Path
    client, *_ = system
    examples = Path(__file__).resolve().parents[1] / 'csvhub' / 'web'
    upload_id = upload(client, (examples / filename).read_bytes(), filename)
    client.post(f'/api/v1/uploads/{upload_id}/complete')
    assert client.get(f'/api/v1/uploads/{upload_id}/preview?row_mode=single_line').status_code == 422
    dataset_id = dataset(client)
    assert client.post(f'/api/v1/datasets/{dataset_id}/imports', json={'upload_id': upload_id,
                       'row_mode': 'single_line', 'on_error': 'skip'}).status_code == 422
