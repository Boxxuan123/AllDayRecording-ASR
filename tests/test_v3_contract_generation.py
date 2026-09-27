from pathlib import Path
import shutil
import pytest
from tools.sync_v3_contracts import canonical, generate, verify


def test_crlf_repair_is_idempotent_and_checks_exact_bytes(tmp_path):
    desktop = tmp_path / 'desktop'
    phone = tmp_path / 'phone'
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source / 'contracts', desktop / 'contracts')
    fixture = phone / 'phone/src/test/V3ContractFixture.test.ets'
    fixture.parent.mkdir(parents=True)
    fixture.write_text("const CORE_FIXTURE_SHA256: string = '" + '0'*64 + "';\nconst FORWARD_FIXTURE_SHA256: string = '" + '0'*64 + "';\n")
    receipt = phone / 'contracts/v3/source.json'
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"canonical_fixtures":{}}')
    contract = desktop / 'contracts/v3/release-lock.json'
    contract.write_bytes(contract.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
    generate(desktop, phone)
    before = {p: p.read_bytes() for root in (desktop, phone) for p in root.rglob('*') if p.is_file()}
    generate(desktop, phone)
    assert all(p.read_bytes() == data for p, data in before.items())
    def read_pc(name):
        return (desktop/name).read_bytes()
    def read_phone(name):
        return (phone/name).read_bytes()
    verify(read_pc, read_phone)
    contract.write_bytes(contract.read_bytes().replace(b'\n', b'\r\n'))
    with pytest.raises(ValueError, match='noncanonical'):
        verify(read_pc, read_phone)
    generate(desktop, phone)
    assert all(p.read_bytes() == data for p, data in before.items())
    with pytest.raises(ValueError):
        canonical(b'bare\rnewline')
    with pytest.raises(ValueError):
        canonical(b'\xef\xbb\xbf{}')
