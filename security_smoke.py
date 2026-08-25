import os, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ.pop('ALLOW_PRIVATE_NETWORK',None)
from smartfetch.security import validate_public_url

blocked = [
    'http://127.0.0.1/',
    'http://localhost/',
    'http://169.254.169.254/latest/meta-data/',
    'http://10.0.0.1/',
    'ftp://example.com/file',
    'http://user:pass@example.com/',
    'https://example.com:8443/',
]
for u in blocked:
    try:
        validate_public_url(u)
    except ValueError:
        print('PASS blocked',u)
    else:
        raise AssertionError('Should have blocked '+u)
print(f'PASS security: {len(blocked)}/{len(blocked)} blocked')
