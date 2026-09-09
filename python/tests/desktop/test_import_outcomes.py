"""Synthetic ordinary selection boundaries and safe partial response contract."""
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from test_company_workspaces import api, snapshot
from test_historical_import import setup_history
from test_finding_review_api import review_case, HEADERS
from qian_labor.models.core import SourceLocator
from qian_labor.models.core import UploadedFile
from qian_labor.security.uploads import UploadPolicy
from qian_labor.security.masking import mask_sensitive


def post(client, aid, paths):
    return client.post(f'/api/analyses/{aid}/import-paths', json={'paths': [str(p) for p in paths]})


def test_good_bad_good_and_duplicate_at_capacity(api, tmp_path):
    client, db = api
    aid, _ = snapshot(db)
    paths = [tmp_path / name for name in ['first.csv', 'broken.pdf', 'last.csv']]
    for p, content in zip(paths, [b'a,b\n1,2', b'%PDF broken', b'a,b\n3,4']):
        p.write_bytes(content)
    service = client.app.state.import_service
    service.uploads.policy = UploadPolicy(max_files=2)
    response = post(client, aid, paths)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [r['status'] for r in body['results']] == ['imported', 'error', 'imported']
    assert [r['index'] for r in body['results']] == [0, 1, 2]
    assert body['results'][1]['error_code'] == 'DESKTOP_IMPORT_CONTENT_INVALID'
    assert body['results'][1]['file_id'] is None
    assert len(body['files']) == 2
    retry = post(client, aid, paths).json()
    assert [r['status'] for r in retry['results']] == ['duplicate', 'error', 'duplicate']
    with db.session() as s:
        assert len(list(s.scalars(select(UploadedFile)))) == 2


@pytest.mark.parametrize('kind,code', [('missing', 'DESKTOP_IMPORT_FILE_NOT_FOUND'),
    ('empty', 'DESKTOP_IMPORT_EMPTY'), ('oversize', 'DESKTOP_IMPORT_TOO_LARGE'),
    ('fifo', 'DESKTOP_IMPORT_NOT_REGULAR'), ('directory', 'DESKTOP_IMPORT_NOT_REGULAR'),
    ('unsupported', 'DESKTOP_IMPORT_FORMAT_UNSUPPORTED'), ('denied', 'DESKTOP_IMPORT_PERMISSION_DENIED')])
def test_safe_individual_failures(api, tmp_path, monkeypatch, kind, code):
    client, db = api
    aid, _ = snapshot(db)
    path = tmp_path / ('synthetic-138' + '00138000' + ('.exe' if kind == 'unsupported' else '.csv'))
    if kind == 'fifo': os.mkfifo(path)
    elif kind == 'directory': path.mkdir()
    elif kind != 'missing': path.write_bytes(b'' if kind == 'empty' else b'x' * 17)
    client.app.state.import_service.uploads.policy = UploadPolicy(max_bytes=16)
    if kind == 'denied':
        original = os.open
        def denied(selected, *args, **kwargs):
            if Path(selected) == path: raise PermissionError(f'private path {path}')
            return original(selected, *args, **kwargs)
        monkeypatch.setattr(os, 'open', denied)
    response = post(client, aid, [path])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['files'] == []
    assert body['results'][0]['error_code'] == code
    assert str(tmp_path) not in response.text and '138' + '00138000' not in response.text


def test_opened_descriptor_controls_type_and_read_bound(api, tmp_path, monkeypatch):
    client, db = api
    aid, _ = snapshot(db)
    selected = tmp_path / 'selected.csv'; selected.write_bytes(b'ok')
    replacement = tmp_path / 'replacement.csv'; replacement.write_bytes(b'x' * 100)
    client.app.state.import_service.uploads.policy = UploadPolicy(max_bytes=16)
    original = os.open
    def replaced(path, flags, *args, **kwargs):
        return original(replacement if Path(path) == selected else path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', replaced)
    body = post(client, aid, [selected]).json()
    assert body['results'][0]['error_code'] == 'DESKTOP_IMPORT_TOO_LARGE'


def test_growth_after_fstat_is_read_only_to_limit_plus_one(api, tmp_path, monkeypatch):
    client, db = api
    aid, _ = snapshot(db)
    path = tmp_path / 'growing.csv'; path.write_bytes(b'x' * 100)
    client.app.state.import_service.uploads.policy = UploadPolicy(max_bytes=16)
    original_stat, original_open = os.fstat, os.fdopen
    read_sizes = []
    def stale_size(fd):
        values = list(original_stat(fd)); values[6] = 1
        return os.stat_result(values)
    class BoundedSpy:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def read(self, size):
            read_sizes.append(size)
            return self.stream.read(size)
    monkeypatch.setattr(os, 'fstat', stale_size)
    monkeypatch.setattr(os, 'fdopen', lambda *args, **kwargs: BoundedSpy(original_open(*args, **kwargs)))
    response = post(client, aid, [path])
    assert response.json()['results'][0]['error_code'] == 'DESKTOP_IMPORT_TOO_LARGE'
    assert read_sizes == [17]


def test_ordinary_explicit_symlink_and_hardlink_are_supported(api, tmp_path):
    client, db = api
    aid, _ = snapshot(db)
    original = tmp_path / 'original.csv'; original.write_bytes(b'synthetic,selected\n')
    symlink = tmp_path / 'selected-link.csv'; symlink.symlink_to(original)
    hardlink = tmp_path / 'selected-hardlink.csv'; os.link(original, hardlink)
    body = post(client, aid, [symlink, hardlink]).json()
    assert [r['status'] for r in body['results']] == ['imported', 'duplicate']


def test_unexpected_per_file_error_is_safe_and_later_selections_continue(api, tmp_path, monkeypatch):
    client, db = api
    aid, _ = snapshot(db)
    paths = [tmp_path / f'synthetic-{i}.csv' for i in range(3)]
    for i, path in enumerate(paths): path.write_bytes(f'synthetic,{i}\n'.encode())
    uploads = client.app.state.import_service.uploads
    add = uploads.add
    def fail_middle(analysis_id, name, mime, content):
        if name == paths[1].name: raise RuntimeError('/private/synthetic-secret 138' + '00138000')
        return add(analysis_id, name, mime, content)
    monkeypatch.setattr(uploads, 'add', fail_middle)
    response = post(client, aid, paths)
    assert [r['status'] for r in response.json()['results']] == ['imported', 'error', 'imported']
    assert response.json()['results'][1]['error_code'] == 'DESKTOP_IMPORT_FAILED'
    assert '/private/' not in response.text and '138' + '00138000' not in response.text


def test_historical_and_missing_analysis_refused_before_open(api, tmp_path, monkeypatch):
    client, db, aid, _, _, _ = setup_history(api)
    def forbidden(*args, **kwargs): raise AssertionError('must refuse before file access')
    monkeypatch.setattr(os, 'open', forbidden)
    assert post(client, aid, [tmp_path / 'missing.csv']).status_code == 409
    assert post(client, 'missing-analysis', [tmp_path / 'missing.csv']).status_code == 404


def test_filename_masked_on_new_storage_duplicate_and_legacy_projection(api, tmp_path):
    client, db = api
    aid, _ = snapshot(db)
    raw = 'synthetic-138' + '00138000' + '-110' + '101199001010037' + '.csv'
    path = tmp_path / raw; path.write_bytes(b'first,synthetic\n')
    body = post(client, aid, [path]).json()
    assert body['results'][0]['filename'].endswith('.csv')
    assert raw not in json.dumps(body)
    file_id = body['files'][0]['id']
    with db.session() as s:
        item = s.get(UploadedFile, file_id)
        assert item.original_filename != raw
        item.original_filename = raw; s.commit()
    assert raw not in client.get(f'/api/analyses/{aid}/workspace').text
    assert raw not in post(client, aid, [path]).text
    with db.session() as s:
        assert s.get(UploadedFile, file_id).original_filename == raw


def test_unsupported_outcome_masks_identifier_across_suffix_boundary(api, tmp_path):
    client, db = api
    aid, _ = snapshot(db)
    raw = 'synthetic-138' + '00138.000'
    path = tmp_path / raw; path.write_bytes(b'synthetic only')
    response = post(client, aid, [path])
    assert response.status_code == 200
    outcome = response.json()['results'][0]
    assert outcome['status'] == 'error'
    assert outcome['error_code'] == 'DESKTOP_IMPORT_FORMAT_UNSUPPORTED'
    assert outcome['filename'] == mask_sensitive(raw)
    assert raw not in response.text


@pytest.mark.parametrize('raw', ['synthetic-138' + '00138000' + '-110' + '101199001010037' + '.csv',
                                 'synthetic-138' + '00138.000'])
def test_legacy_source_report_and_processing_projection_mask_without_rewrite(review_case, raw):
    client, db, aid, _, finding_id = review_case
    with db.session() as s:
        item = s.scalar(select(UploadedFile)); item.original_filename = raw
        source = s.scalar(select(SourceLocator)); source.location = {'file_name': raw, 'row': 1}
        s.commit()
        file_id, source_id = item.id, source.id
    for url in [f'/api/findings/{finding_id}', f'/api/analyses/{aid}/report',
                f'/api/analyses/{aid}/processing', f'/api/analyses/{aid}/workspace']:
        response = client.get(url, headers=HEADERS)
        assert response.status_code == 200
        assert raw not in response.text, url
        assert mask_sensitive(raw) in response.text, url
    detail = client.get(f'/api/findings/{finding_id}', headers=HEADERS).json()
    assert any(row['id'] == source_id and row['file_id'] == file_id for row in detail['sources'])
    with db.session() as s:
        assert s.get(UploadedFile, file_id).original_filename == raw
        assert s.get(SourceLocator, source_id).location['file_name'] == raw
