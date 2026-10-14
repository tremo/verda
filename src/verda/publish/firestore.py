"""Cloud Firestore over REST, signed in as a service account. Standard library plus PyJWT; the key file is read only to sign, never logged."""
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import jwt

DEFAULT_KEY = Path.home() / 'Library/Application Support/Verda/secrets/firebase-publisher.json'
SCOPE = 'https://www.googleapis.com/auth/datastore'
SSL_CONTEXT = ssl.create_default_context(cafile='/etc/ssl/cert.pem' if Path('/etc/ssl/cert.pem').exists() else None)


class Firestore:
    def __init__(self, key_path=None, *, database='(default)', opener=None):
        self.key_path = Path(os.environ.get('VERDA_FIREBASE_KEY') or key_path or DEFAULT_KEY)
        self.database = database
        self._opener = opener or self._open
        self._token = None
        self._expires = 0
        key = json.loads(self.key_path.read_text(encoding='utf-8'))
        self.project = key['project_id']
        self._email, self._private, self._uri = key['client_email'], key['private_key'], key.get('token_uri', 'https://oauth2.googleapis.com/token')

    @staticmethod
    def _open(request, timeout=30):
        return urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT)

    def token(self, now=None):
        now = time.time() if now is None else now
        if self._token and now < self._expires - 60:
            return self._token
        assertion = jwt.encode({'iss': self._email, 'scope': SCOPE, 'aud': self._uri, 'iat': int(now), 'exp': int(now) + 3600}, self._private, algorithm='RS256')
        form = urllib.parse.urlencode({'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': assertion}).encode()
        with self._opener(urllib.request.Request(self._uri, data=form, method='POST')) as response:
            answer = json.loads(response.read())
        self._token, self._expires = answer['access_token'], now + int(answer.get('expires_in', 3600))
        return self._token

    def _base(self):
        return f'https://firestore.googleapis.com/v1/projects/{self.project}/databases/{urllib.parse.quote(self.database, safe="()")}/documents'

    def name(self, collection, doc_id):
        return f'projects/{self.project}/databases/{self.database}/documents/{collection}/{urllib.parse.quote(doc_id, safe="")}'

    def request(self, method, url, body=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(url, data=data, method=method, headers={'Authorization': f'Bearer {self.token()}', 'Content-Type': 'application/json'})
        try:
            with self._opener(request) as response:
                return json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'firestore_http_{error.code}') from None

    def get(self, collection, doc_id):
        return self.request('GET', f'{self._base()}/{collection}/{urllib.parse.quote(doc_id, safe="")}')

    def list_ids(self, collection, page_size=300):
        """Document ids only (a field mask keeps the answer small)."""
        ids, token = [], None
        while True:
            url = f'{self._base()}/{collection}?pageSize={page_size}&mask.fieldPaths=__name__' + (f'&pageToken={token}' if token else '')
            answer = self.request('GET', url)
            ids += [d['name'].rsplit('/', 1)[-1] for d in answer.get('documents', [])]
            token = answer.get('nextPageToken')
            if not token:
                return ids


    def list_docs(self, collection, page_size=300):
        """[(id, plain dict)] for every document of a collection."""
        out, token = [], None
        while True:
            answer = self.request('GET', f'{self._base()}/{collection}?pageSize={page_size}' + (f'&pageToken={token}' if token else ''))
            out += [(d['name'].rsplit('/', 1)[-1], plain({'mapValue': {'fields': d.get('fields', {})}})) for d in answer.get('documents', [])]
            token = answer.get('nextPageToken')
            if not token:
                return out


def plain(v):
    """Firestore wire value -> Python."""
    if 'mapValue' in v:
        return {k: plain(x) for k, x in v['mapValue'].get('fields', {}).items()}
    if 'arrayValue' in v:
        return [plain(x) for x in v['arrayValue'].get('values', [])]
    for key, cast in (('stringValue', str), ('integerValue', int), ('doubleValue', float), ('booleanValue', bool), ('timestampValue', str)):
        if key in v:
            return cast(v[key])
    return None
