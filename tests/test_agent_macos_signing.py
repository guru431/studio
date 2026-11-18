import datetime
import hashlib
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('sign_release', Path(__file__).resolve().parents[1] / 'agent/macos/sign_release.py')
signing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(signing)
NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
TEAM = 'EXAMPLE123'
CERT = hashlib.sha1(b'fixture').hexdigest()


def test_only_requested_developer_id_application_identity_is_accepted():
    listing = f'  1) {CERT} "Developer ID Application: Example ({TEAM})"'
    assert signing.signing_team(CERT.upper(), listing) == TEAM
    for invalid in ('Apple Development', 'Developer ID Installer'):
        with pytest.raises(ValueError):
            signing.signing_team(CERT, listing.replace('Developer ID Application', invalid))
    with pytest.raises(ValueError):
        signing.signing_team('A' * 40, listing)


@pytest.mark.parametrize('change', ['valid', 'expired', 'foreign-team', 'foreign-cert', 'wrong-id', 'no-grant'])
def test_capture_entitlement_requires_matching_approved_profile(change):
    grants = {'com.apple.application-identifier': TEAM + '.com.studio.agent',
              'com.apple.developer.team-identifier': TEAM, signing.CAPTURE: True}
    profile = {'TeamIdentifier': [TEAM], 'DeveloperCertificates': [b'fixture'],
               'Entitlements': grants, 'ExpirationDate': datetime.datetime(2027, 1, 1)}
    if change == 'valid':
        grants['unrelated'] = True
        assert set(signing.capture_entitlements(profile, TEAM, CERT, NOW)) == set(grants) - {'unrelated'}
        return
    if change == 'expired':
        profile['ExpirationDate'] = datetime.datetime(2025, 1, 1)
    elif change == 'foreign-team':
        profile['TeamIdentifier'] = ['OTHER12345']
    elif change == 'foreign-cert':
        profile['DeveloperCertificates'] = [b'other']
    elif change == 'wrong-id':
        grants['com.apple.application-identifier'] = TEAM + '.*'
    else:
        grants[signing.CAPTURE] = False
    with pytest.raises(ValueError):
        signing.capture_entitlements(profile, TEAM, CERT, NOW)
