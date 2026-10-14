import csv
import io
import sqlite3

import pytest

from csvhub.parsing import csv_reader, BoundedLines, CSVRecords, CSVParseError
from conftest import import_data, upload


@pytest.mark.parametrize('content,code,record,first,last', [
    (b'key,label\n1,"SECRET"x\n', 'invalid_after_quote', 1, 2, 2),
    (b'key,label\n1,"SECRET\nnext\n', 'unexpected_eof', 1, 2, 3),
    (b'key,label\n1,"valid\nmultiline"\n2,"SECRET"x\n', 'invalid_after_quote', 2, 4, 4),
    (b'"key"x,label\n', 'invalid_after_quote', 0, 1, 1),
])
def test_csv_parse_error_reports_positions_without_values(content, code, record, first, last):
    with io.BytesIO(content) as binary:
        text, _, reader = csv_reader(binary, 'utf-8', ',', 4096)
        try:
            with pytest.raises(CSVParseError) as caught:
                list(reader)
            error = caught.value
            assert (error.code, error.record, error.first_line, error.last_line) == (code, record, first, last)
            assert 'SECRET' not in str(error)
        finally:
            text.detach()


def test_record_limit_has_position_and_remains_bounded():
    with io.BytesIO(b'key,label\n1,"line\nline\nline"\n') as binary:
        text, lines, reader = csv_reader(binary, 'utf-8', ',', 12)
        try:
            next(reader)
            with pytest.raises(CSVParseError, match='size limit') as caught:
                next(reader)
            assert (caught.value.code, caught.value.record, caught.value.first_line, caught.value.last_line) == ('record_too_large', 1, 2, 3)
            assert lines.record_bytes == 13
        finally:
            text.detach()


def test_unknown_parser_error_cannot_expose_exception_contents():
    class UnknownParser:
        def __next__(self):
            raise csv.Error('SECRET source value and arbitrary file path')

    reader = CSVRecords(UnknownParser(), BoundedLines(io.StringIO(''), 4096, 'utf-8'))
    with pytest.raises(CSVParseError, match='invalid_csv') as caught:
        next(reader)
    assert 'SECRET' not in str(caught.value) and 'file path' not in str(caught.value)


def test_preview_returns_safe_parse_diagnostic(system):
    client, *_ = system
    upload_id = upload(client, b'key,label\n1,"SECRET"x\n')
    assert client.post(f'/api/v1/uploads/{upload_id}/complete').status_code == 200
    response = client.get(f'/api/v1/uploads/{upload_id}/preview')
    assert response.status_code == 422
    assert response.json()['detail'] == 'CSV parse error: invalid_after_quote; record 1; physical lines 2-2'
    assert 'SECRET' not in response.text


def test_skip_rejects_width_error_but_stops_at_csv_structure_damage(system):
    client, _, _, settings = system
    dataset_id, old = import_data(system, b'key,label\n0,Previous\n')
    _, job = import_data(system, b'key,label\n1,Widget\n2,Widget,extra\n3,"SECRET"x\n4,Widget\n',
                         dataset_id, on_error='skip')
    assert (job['status'], job['processed'], job['accepted'], job['rejected']) == ('failed', 2, 1, 1)
    assert job['error'] == 'CSV parse error: invalid_after_quote; record 3; physical lines 4-4'
    errors = client.get(f"/api/v1/jobs/{job['id']}/errors").json()['items']
    assert errors == [{'record': 2, 'reason': 'Column count differs from header'}]
    assert client.post(f'/api/v1/datasets/{dataset_id}/query', json={}).json()['version'] == old['id']
    with sqlite3.connect(settings.data_dir / 'versions' / f"{job['id']}.sqlite") as db:
        assert db.execute('SELECT COUNT(*) FROM records').fetchone()[0] == 1
