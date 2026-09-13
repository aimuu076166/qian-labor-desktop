"""Actual Excel bytes at the parser and processing boundaries; synthetic only."""
from io import BytesIO
import re
import struct
from zipfile import ZipFile

import pytest
from openpyxl import Workbook

from qian_labor.parsers.registry import ParserRegistry
from qian_labor.security.uploads import validate_upload
from qian_labor.jobs.processing import ProcessingPipeline
from qian_labor.services.uploads import UploadService
from qian_labor.storage.local import LocalStorage
from test_company_workspaces import api, company, record
from test_current_company import current

MIME = {'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'xls': 'application/vnd.ms-excel'}


def excel_bytes(extension, row, column, hidden_dimensions=False):
    if extension == 'xlsx':
        workbook = Workbook(); sheet = workbook.active
        sheet['A1'] = 'employee_number'; sheet.cell(row, column, 'SYN-001')
        out = BytesIO(); workbook.save(out)
        if not hidden_dimensions:
            return out.getvalue()
        modified = BytesIO()
        with ZipFile(BytesIO(out.getvalue())) as source, ZipFile(modified, 'w') as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == 'xl/worksheets/sheet1.xml':
                    data = re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1:A1"', data)
                target.writestr(item, data)
        return modified.getvalue()
    # Minimal real BIFF8 workbook in a Compound File v3 container; no writer dependency.
    def rec(code, data=b''):
        return struct.pack('<HH', code, len(data)) + data
    def bof(kind):
        return rec(0x809, struct.pack('<HHHHII', 0x600, kind, 0x0DBB, 1996, 0x41, 6))
    def label(r, c, value):
        return rec(0x204, struct.pack('<HHHHB', r, c, 0, len(value), 0) + value.encode('ascii'))
    name = b'Synthetic'
    bound = lambda offset: rec(0x85, struct.pack('<IBBBB', offset, 0, 0, len(name), 0) + name)
    global_prefix = bof(5) + rec(0x42, struct.pack('<H', 1200))
    offset = len(global_prefix + bound(0) + rec(0xA))
    stream = global_prefix + bound(offset) + rec(0xA) + bof(0x10)
    stream += rec(0x200, struct.pack('<IIHHH', 0, row, 0, column, 0))
    stream += label(0, 0, 'employee_number') + label(row - 1, column - 1, 'SYN-001') + rec(0xA)
    stream = stream.ljust(4096, b'\0')
    end, free = 0xFFFFFFFE, 0xFFFFFFFF
    header = bytearray(512); header[:8] = bytes.fromhex('d0cf11e0a1b11ae1')
    struct.pack_into('<HHHHH', header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into('<IIIIIIIII', header, 40, 0, 1, 1, 0, 4096, end, 0, end, 0)
    struct.pack_into('<109I', header, 76, 0, *([free] * 108))
    fat = struct.pack('<128I', 0xFFFFFFFD, end, *range(3, 10), end, *([free] * 118))
    def directory(name, kind, child, start, size):
        entry = bytearray(128); encoded = (name + '\0').encode('utf-16-le'); entry[:len(encoded)] = encoded
        struct.pack_into('<HBBIII', entry, 64, len(encoded), kind, 1, free, free, child)
        struct.pack_into('<IQ', entry, 116, start, size)
        return entry
    entries = directory('Root Entry', 5, 1, end, 0) + directory('Workbook', 2, free, 2, len(stream))
    return bytes(header) + fat + bytes(entries).ljust(512, b'\0') + stream


@pytest.mark.parametrize('extension', ['xlsx', 'xls'])
@pytest.mark.parametrize('row,column', [(10001, 1), (2, 201), (10020, 205)])
def test_rejects_excel_content_outside_resource_bounds(extension, row, column):
    content = excel_bytes(extension, row, column)
    validate_upload('synthetic.' + extension, MIME[extension], content)
    with pytest.raises(ValueError, match='SPREADSHEET_DIMENSION_LIMIT'):
        ParserRegistry().parse('synthetic.' + extension, content)


@pytest.mark.parametrize('extension', ['xlsx', 'xls'])
@pytest.mark.parametrize('row,column', [(2, 1), (10000, 1), (2, 200)])
def test_excel_limits_keep_last_allowed_cell_and_real_location(extension, row, column):
    parsed = ParserRegistry().parse('synthetic.' + extension, excel_bytes(extension, row, column))
    cell = next(block for block in parsed.blocks if block.text == 'SYN-001')
    assert (cell.locator['row'], cell.locator['column']) == (row, column)
    assert parsed.warnings == []


@pytest.mark.parametrize('row,column', [(2, 201), (10020, 1)])
def test_understated_xlsx_dimensions_cannot_hide_omitted_content(row, column):
    with pytest.raises(ValueError, match='SPREADSHEET_DIMENSION_LIMIT'):
        ParserRegistry().parse('synthetic.xlsx', excel_bytes('xlsx', row, column, True))


@pytest.mark.parametrize('extension', ['xlsx', 'xls'])
def test_oversized_excel_pipeline_stays_unavailable_through_material_advice_assessment_report(api, tmp_path, extension):
    client, db = api; owner = company(client); record(client, owner['id'])
    aid = current(client, owner['id'], 1)['analysis_id']
    storage = LocalStorage(tmp_path / 'storage')
    UploadService(db, storage).add(aid, 'synthetic.' + extension, MIME[extension], excel_bytes(extension, 2, 201))
    class NoProvider:
        name = 'fake'
        def extract(self, *args, **kwargs):
            pytest.fail('oversized material must be rejected before model extraction')
    ProcessingPipeline(db, storage, provider=NoProvider()).process(aid)
    workspace = client.get(f'/api/analyses/{aid}/workspace').json()
    assert workspace['files'][0]['status'] == 'failed'
    assert workspace['files'][0]['error_code'] == 'SPREADSHEET_DIMENSION_LIMIT'
    advisory = client.get(f'/api/company-workspaces/{owner["id"]}/analyses/{aid}/contract-advisories').json()
    assert advisory['observations'] == []
    assert not any(run['execution_status'] == 'completed' for run in advisory['runs'])
    report = client.get(f'/api/analyses/{aid}/report').json()
    assert report['assessment_revision']['completeness'] != 'complete'
    assert report['assessment_revision']['availability'] == 'none'
    base = f'/api/company-workspaces/{owner["id"]}/analyses/{aid}/report-versions'
    context = client.get(base).json()['current_context']
    from uuid import uuid4
    saved = client.post(base, json={'request_id': str(uuid4()), **{'expected_' + key: value for key, value in context.items()}})
    assert saved.status_code == 201
    payload = saved.json()['snapshot']['payload']
    assert payload['assessment_revision']['completeness'] != 'complete'
    assert payload['assessment_revision']['availability'] == 'none'
    assert payload['materials'][0]['error_code'] == 'SPREADSHEET_DIMENSION_LIMIT'
    assert any('足够可用材料' in line for line in payload['limitations'])
