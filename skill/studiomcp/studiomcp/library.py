"""Official Creator Store search, live filter metadata, and thumbnail galleries."""
import base64
from functools import lru_cache
from io import BytesIO
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from PIL import Image
from .gallery import gallery

SCHEMA_URL = 'https://raw.githubusercontent.com/Roblox/creator-docs/main/content/en-us/reference/cloud/toolbox-service/v1.json'
BASE_URL = 'https://apis.roblox.com/toolbox-service/v2'


def _json(url):
    headers = {'Accept': 'application/json'}
    if url.startswith(BASE_URL):
        from .auth import api_key
        key = api_key()
        if key:
            headers['x-api-key'] = key
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f'Roblox HTTP {error.code}: {error.read().decode()}') from error


@lru_cache(maxsize=1)
def _schema():
    return _json(SCHEMA_URL)


def _resolved(schema):
    if '$ref' in schema:
        return _schema()['components']['schemas'][schema['$ref'].rsplit('/', 1)[1]]
    if 'allOf' in schema:
        return {**_resolved(schema['allOf'][0]), **{k: v for k, v in schema.items() if k != 'allOf'}}
    return schema


def options():
    parameters = _schema()['paths']['/toolbox-service/v2/assets:search']['get']['parameters']
    return {'source': SCHEMA_URL, 'endpoint': BASE_URL + '/assets:search',
            'filters': {p['name']: {'description': p.get('description', ''), 'required': p.get('required', False),
                                  'schema': _resolved(p['schema'])} for p in parameters},
            'schemas': _schema()['components']['schemas'],
            'notes': ['Images use the official Decal category.', 'Plugin results are discovery only; never installed or executed.',
                      'Use nextPageToken as pageToken with unchanged query filters.',
                      'ROBLOX_API_KEY is optional where anonymous access is allowed; permission failures are returned.']}


def _validate(value, schema, name):
    schema = _resolved(schema)
    kind = schema.get('type')
    valid = {'string': isinstance(value, str), 'integer': isinstance(value, int) and not isinstance(value, bool),
             'boolean': isinstance(value, bool), 'array': isinstance(value, list)}
    if kind in valid and not valid[kind]:
        raise ValueError(f'{name} must be {kind}')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{name} must be one of {schema["enum"]}')
    if kind == 'integer' and (value < schema.get('minimum', value) or value > schema.get('maximum', value)):
        raise ValueError(f'{name} outside official allowed range')
    if kind == 'array':
        for item in value:
            _validate(item, schema['items'], name)


def details(asset_id):
    if not str(asset_id).isdigit() or int(asset_id) <= 0:
        raise ValueError('assetId must be a positive integer')
    return _json(BASE_URL + '/assets/' + str(asset_id))


def search(query='', category='Model', filters=None, output=None):
    metadata = options()
    arguments = dict(filters or {})
    arguments.setdefault('maxPageSize', 9)
    if 'query' in arguments or 'searchCategoryType' in arguments:
        raise ValueError('Use query and category arguments, not duplicate filters')
    arguments.update(query=query, searchCategoryType=category)
    for name, value in arguments.items():
        if name not in metadata['filters']:
            raise ValueError(f'Unsupported official filter: {name}; call libraryOptions')
        _validate(value, metadata['filters'][name]['schema'], name)
    for a, b in [('userId', 'groupId'), ('pageToken', 'pageNumber')]:
        if a in arguments and b in arguments:
            raise ValueError(f'{a} and {b} are mutually exclusive')
    encoded = {k: str(v).lower() if isinstance(v, bool) else v for k, v in arguments.items()}
    result = _json(BASE_URL + '/assets:search?' + urlencode(encoded, doseq=True))
    entries = result.get('creatorStoreAssets', [])
    ids = [str(item['asset']['id']) for item in entries]
    thumbnails = {}
    if ids:
        data = _json('https://thumbnails.roblox.com/v1/assets?' + urlencode({'assetIds': ','.join(ids), 'returnPolicy': 'PlaceHolder', 'size': '420x420', 'format': 'Png'}))
        thumbnails = {str(t['targetId']): t for t in data.get('data', [])}
    images = []
    for index, item in enumerate(entries, 1):
        asset = item['asset']
        asset_id = str(asset['id'])
        thumbnail = thumbnails.get(asset_id, {})
        item['creatorStoreUrl'] = 'https://create.roblox.com/store/asset/' + asset_id
        item['thumbnail'] = thumbnail
        item['selection'] = index
        if asset.get('textureId'):
            item['imageUri'] = 'rbxassetid://' + str(asset['textureId'])
        if thumbnail.get('state') == 'Completed' and thumbnail.get('imageUrl'):
            try:
                with urlopen(thumbnail['imageUrl'], timeout=30) as response:
                    image = Image.open(BytesIO(response.read())).convert('RGB')
                label = asset.get('name') or f'Asset {asset_id}'
                images.append((f'#{index} | ID {asset_id}\n{label}', image))
            except (OSError, ValueError) as error:
                item['thumbnailError'] = str(error)
    result['request'] = arguments
    result['availableOptions'] = {'categories': metadata['filters']['searchCategoryType']['schema']['enum'],
                                  'facets': result.get('queryFacets'), 'filtersTool': 'libraryOptions'}
    if images:
        png = gallery(images, caption_overlay=True)
        if output:
            target = Path(output).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(png)
            result['galleryPath'] = str(target)
        else:
            result['png'] = base64.b64encode(png).decode()
    return result
