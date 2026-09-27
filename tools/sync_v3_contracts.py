"""Canonical UTF-8/LF bytes -> manifest -> Harmony receipt, without version changes.

Run with --phone <checkout>. CRLF input is explicitly rewritten to LF before
hashing; bare CR and BOM are rejected. --check never normalizes distributed bytes.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(data):
    text = data.decode('utf-8')
    if text.startswith('\ufeff') or '\r' in text.replace('\r\n', ''):
        raise ValueError('contract must be UTF-8 without BOM or bare CR')
    return text.replace('\r\n', '\n').encode('utf-8')


def entries(manifest):
    return [manifest['release_lock'], *manifest['schemas'].values(),
            *manifest['apis'].values(), *manifest['fixtures'].values()]


def verify(read_desktop, read_phone):
    """Readers supply exact filesystem/Git/archive bytes. No normalization here."""
    raw = read_desktop('contracts/v3/manifest.json')
    manifest = json.loads(raw)
    for name, data in [('manifest.json', raw)] + [
        (e['path'], read_desktop('contracts/v3/' + e['path'])) for e in entries(manifest)
    ]:
        if canonical(data) != data:
            raise ValueError(f'noncanonical contract bytes: {name}')
    for entry in entries(manifest):
        if digest(read_desktop('contracts/v3/' + entry['path'])) != entry['sha256']:
            raise ValueError('contract digest mismatch: ' + entry['path'])
    phone_raw = read_phone('contracts/v3/source.json')
    if canonical(phone_raw) != phone_raw:
        raise ValueError('noncanonical Harmony receipt')
    receipt = json.loads(phone_raw)
    if receipt['canonical_manifest_sha256'] != digest(raw):
        raise ValueError('Harmony manifest digest mismatch')
    fixture_test = read_phone('phone/src/test/V3ContractFixture.test.ets').decode('utf-8')
    for name, expected in receipt['canonical_fixtures'].items():
        if digest(read_desktop('contracts/v3/fixtures/' + name)) != expected or expected not in fixture_test:
            raise ValueError('Harmony fixture digest mismatch: ' + name)


def generate(desktop, phone):
    root = desktop / 'contracts/v3'
    for path in root.rglob('*.json'):
        data = canonical(path.read_bytes())
        json.loads(data)
        path.write_bytes(data)
    manifest_path = root / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    for entry in entries(manifest):
        entry['sha256'] = digest((root / entry['path']).read_bytes())
    manifest_path.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    path = phone / 'contracts/v3/source.json'
    receipt = json.loads(path.read_bytes())
    receipt['canonical_manifest_sha256'] = digest(manifest_path.read_bytes())
    fixture_path = phone / 'phone/src/test/V3ContractFixture.test.ets'
    fixture = canonical(fixture_path.read_bytes()).decode('utf-8')
    for name, constant in [('core-resources.json', 'CORE_FIXTURE_SHA256'), ('forward-enums.json', 'FORWARD_FIXTURE_SHA256')]:
        value = digest((root / 'fixtures' / name).read_bytes())
        fixture, count = re.subn(r"(const " + constant + r": string =\s*')[a-f0-9]{64}(')",
                                lambda match, value=value: match[1] + value + match[2], fixture)
        if count != 1:
            raise ValueError('expected one fixture receipt: ' + constant)
        receipt['canonical_fixtures'][name] = value
    fixture_path.write_bytes(fixture.encode('utf-8'))
    path.write_bytes((json.dumps(receipt, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    verify(lambda name: (desktop / name).read_bytes(), lambda name: (phone / name).read_bytes())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phone', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    desktop = Path(__file__).resolve().parents[1]
    if args.check:
        verify(lambda name: (desktop / name).read_bytes(), lambda name: (args.phone / name).read_bytes())
    else:
        generate(desktop, args.phone)
    print('PASS canonical contract bytes and Harmony receipts')
