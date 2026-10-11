"""Create universe-owned passes and developer products using Open Cloud."""
import http.client
import json
import os
from pathlib import Path
import uuid

from .upload import FORMATS, LIMIT, headers


def _create(endpoint, universe_id, name, description, icon, price, for_sale, regional_pricing):
    if universe_id is None:
        from .auth import default_universe_id
        universe_id = default_universe_id()
    if not universe_id or not str(universe_id).isdigit() or int(universe_id) < 1:
        raise ValueError('universe_id is required: specify --universeId, set ROBLOX_UNIVERSE_ID, or configure in auth.json')
    if not isinstance(name, str) or not name.strip():
        raise ValueError('name must be nonempty')
    if price is not None and (type(price) is not int or price < 1):
        raise ValueError('price must be a positive integer Robux amount')
    if price is not None and not for_sale:
        for_sale = True
    if type(for_sale) is not bool or type(regional_pricing) is not bool:
        raise ValueError('for_sale and regional_pricing must be booleans')
    if for_sale and price is None:
        raise ValueError('Selling requires an explicit price')
    path = Path(icon).resolve(strict=True) if icon else None
    if path and (not path.is_file() or path.suffix.lower() not in FORMATS['Image']):
        raise ValueError('icon must be a supported local image file')
    size = path.stat().st_size if path else 0
    if path and not 0 < size <= LIMIT:
        raise ValueError('Icon must be nonempty and at most 20 MiB')
    auth = headers()
    fields = {'name': name, 'description': description, 'isForSale': str(for_sale).lower(),
              'isRegionalPricingEnabled': str(regional_pricing).lower()}
    if price is not None:
        fields['price'] = str(price)
    boundary = uuid.uuid4().hex
    prefix = b''.join((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
                      for key, value in fields.items())
    if path:
        prefix += (f'--{boundary}\r\nContent-Disposition: form-data; name="imageFile"; filename="icon{path.suffix.lower()}"\r\n'
                   f'Content-Type: {FORMATS["Image"][path.suffix.lower()]}\r\n\r\n').encode()
    ending = (('\r\n' if path else '') + f'--{boundary}--\r\n').encode()
    connection = http.client.HTTPSConnection('apis.roblox.com', timeout=60)
    stream = None
    try:
        if path:
            stream = path.open('rb')
            if os.fstat(stream.fileno()).st_size != size:
                raise ValueError('Icon changed before upload')
        connection.putrequest('POST', f'/{endpoint}/universes/{int(universe_id)}/{endpoint.split("/")[0]}')
        for key, value in {**auth, 'Content-Type': 'multipart/form-data; boundary=' + boundary,
                           'Content-Length': str(len(prefix) + size + len(ending))}.items():
            connection.putheader(key, value)
        connection.endheaders()
        connection.send(prefix)
        remaining = size
        while remaining:
            chunk = stream.read(min(remaining, 65536))
            if not chunk:
                raise ValueError('Icon changed during upload')
            connection.send(chunk)
            remaining -= len(chunk)
        if stream and stream.read(1):
            raise ValueError('Icon changed during upload')
        connection.send(ending)
        response = connection.getresponse()
        body = response.read()
        if response.status >= 300:
            raise RuntimeError(f'Roblox HTTP {response.status}: {body.decode(errors="replace")}')
        result = json.loads(body)
        identity = 'gamePassId' if endpoint.startswith('game-passes/') else 'productId'
        if not result.get(identity):
            raise RuntimeError('Roblox returned no created item ID: ' + json.dumps(result))
        return result
    finally:
        if stream:
            stream.close()
        connection.close()


def create_game_pass(universe_id, name, description='', icon=None, price=None, for_sale=False, regional_pricing=False):
    return _create('game-passes/v1', universe_id, name, description, icon, price, for_sale, regional_pricing)


def create_developer_product(universe_id, name, description='', icon=None, price=None, for_sale=False, regional_pricing=False):
    return _create('developer-products/v2', universe_id, name, description, icon, price, for_sale, regional_pricing)
