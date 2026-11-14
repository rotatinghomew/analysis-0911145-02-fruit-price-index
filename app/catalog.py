def product_at(catalog, sku, week):
    matches = [product for product in catalog if product['sku'] == sku and
               product['valid_from'] <= week and
               (product['valid_to'] is None or week < product['valid_to'])]
    if len(matches) != 1:
        raise ValueError('missing or overlapping product version')
    return dict(matches[0])
