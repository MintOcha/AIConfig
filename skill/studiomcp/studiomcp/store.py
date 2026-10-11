"""Distribute an existing model for free in Creator Store."""
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from .upload import headers


def publish_model(asset_id):
    if not str(asset_id).isascii() or not str(asset_id).isdigit() or int(asset_id) < 1:
        raise ValueError('asset_id must be a positive model asset ID')
    data = {'modelAssetId': str(asset_id), 'published': True,
            'basePrice': {'currencyCode': 'USD', 'quantity': {'significand': 0, 'exponent': 0}}}
    request = Request('https://apis.roblox.com/cloud/v2/creator-store-products',
                      data=json.dumps(data).encode(), headers={**headers(), 'Content-Type': 'application/json'}, method='POST')
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f'Roblox HTTP {error.code}: {error.read().decode(errors="replace")}') from error
