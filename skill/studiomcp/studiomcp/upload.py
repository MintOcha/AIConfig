"""Publish local files through the official Open Cloud Assets API."""
import http.client
import json
import os
from pathlib import Path
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import uuid

BASE = 'https://apis.roblox.com/assets/v1/'
FORMATS = {
    'Audio': {'.mp3': 'audio/mpeg', '.ogg': 'audio/ogg', '.wav': 'audio/wav', '.flac': 'audio/flac'},
    'Image': {'.png': 'image/png', '.jpeg': 'image/jpeg', '.jpg': 'image/jpeg', '.bmp': 'image/bmp', '.tga': 'image/tga'},
    'Decal': {'.png': 'image/png', '.jpeg': 'image/jpeg', '.jpg': 'image/jpeg', '.bmp': 'image/bmp', '.tga': 'image/tga'},
    'Model': {'.fbx': 'model/fbx', '.gltf': 'model/gltf+json', '.glb': 'model/gltf-binary', '.rbxm': 'model/x-rbxm', '.rbxmx': 'model/x-rbxm'},
    'Animation': {'.rbxm': 'model/x-rbxm', '.rbxmx': 'model/x-rbxm'},
    'Mesh': {'.mesh': 'model/x-file-mesh-data'},
}
LIMIT = 20 * 1024 * 1024


def headers():
    from .auth import api_key, AUTH_PATH
    key = api_key()
    if not key:
        raise ValueError(f'Set ROBLOX_API_KEY or api_key in {AUTH_PATH}; requires assets Read and Write permissions')
    return {'x-api-key': key, 'Accept': 'application/json'}


def operation(operation):
    identifier = operation.removeprefix('operations/')
    if not identifier or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in identifier):
        raise ValueError('Invalid Roblox operation ID')
    try:
        with urlopen(Request(BASE + 'operations/' + identifier, headers=headers()), timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f'Roblox HTTP {error.code}: {error.read().decode()}') from error
    return result


def upload(file, creator_id=None, creator_type='user', asset_type=None, name=None, description='', wait_seconds=60):
    path = Path(file).resolve(strict=True)
    if not path.is_file():
        raise ValueError('Upload requires a regular file')
    if creator_id is None:
        from .auth import default_creator_id
        creator_id = default_creator_id()
    if not creator_id or not str(creator_id).isdigit() or int(creator_id) < 1:
        raise ValueError('Specify a positive creator_id (or set ROBLOX_CREATOR_ID / auth.json)')
    if creator_type not in ('user', 'group'):
        raise ValueError('creator_type must be user or group')
    if not 0 <= wait_seconds <= 90:
        raise ValueError('wait_seconds must be between 0 and 90')
    suffix = path.suffix.lower()
    if asset_type is None:
        candidates = [kind for kind, formats in FORMATS.items() if suffix in formats and kind != 'Decal']
        if len(candidates) != 1:
            raise ValueError('Specify asset_type explicitly for ambiguous or unsupported file formats; supported types: ' + ', '.join(FORMATS))
        asset_type = candidates[0]
    if asset_type not in FORMATS or suffix not in FORMATS[asset_type]:
        raise ValueError(f'Unsupported {asset_type} file format {suffix}; supported formats: {FORMATS.get(asset_type, {})}')
    size = path.stat().st_size
    if not 0 < size <= LIMIT:
        raise ValueError('Asset file must be nonempty and at most 20 MiB')
    auth = headers()
    boundary = uuid.uuid4().hex
    request = {'assetType': asset_type, 'displayName': name or path.stem, 'description': description,
               'creationContext': {'creator': {creator_type + 'Id': str(creator_id)}}}
    prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="request"\r\nContent-Type: application/json\r\n\r\n'
              + json.dumps(request) + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="fileContent"; filename="asset{suffix}"\r\nContent-Type: {FORMATS[asset_type][suffix]}\r\n\r\n').encode()
    ending = f'\r\n--{boundary}--\r\n'.encode()
    connection = http.client.HTTPSConnection('apis.roblox.com', timeout=60)
    try:
        with path.open('rb') as stream:
            if os.fstat(stream.fileno()).st_size != size:
                raise ValueError('File changed before upload')
            connection.putrequest('POST', '/assets/v1/assets')
            for key, value in {**auth, 'Content-Type': 'multipart/form-data; boundary=' + boundary,
                               'Content-Length': str(len(prefix) + size + len(ending))}.items():
                connection.putheader(key, value)
            connection.endheaders(); connection.send(prefix)
            remaining = size
            while remaining:
                chunk = stream.read(min(remaining, 65536))
                if not chunk:
                    raise ValueError('File changed during upload')
                connection.send(chunk); remaining -= len(chunk)
            connection.send(ending)
            response = connection.getresponse(); body = response.read()
            if response.status >= 300:
                raise RuntimeError(f'Roblox HTTP {response.status}: {body.decode(errors="replace")}')
            result = json.loads(body)
    finally:
        connection.close()
    operation_path = result.get('path')
    if not operation_path:
        raise RuntimeError('Roblox did not return an operation path: ' + json.dumps(result))
    deadline = time.monotonic() + wait_seconds
    while wait_seconds and not result.get('done') and time.monotonic() < deadline:
        result = operation(operation_path)
        if not result.get('done'):
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    if result.get('error'):
        raise RuntimeError('Roblox asset processing failed: ' + json.dumps(result['error']))
    return result
