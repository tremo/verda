"""Synthetic tools with a real provider-driven loop. No fixed task graph or web access."""
from verda.agency.registry import Registry


def synthetic_registry(config):
    fixtures = {
        'sahibinden.search': lambda a: {'synthetic': True, 'listings': [{'id': 'DEMO-101', 'new': True}]},
        'sahibinden.read_listing': lambda a: {'synthetic': True, 'listing_id': a['listing_id'], 'price_tl': 8_000_000, 'area_m2': 2000, 'parcel_key': 'DEMO:1/1'},
        'sahibinden.read_thread': lambda a: {'synthetic': True, 'listing_id': a['listing_id'], 'messages': [], 'unanswered': ['Yol erişimi var mı?']},
        'tkgm.lookup': lambda a: {'synthetic': True, 'parcel_key': a['parcel_key'], 'identity_confirmed': True},
        'policy.screen': lambda a: {'synthetic': True, 'within_price': a['price_tl'] <= 15_000_000, 'enough_area': a['area_m2'] >= 1000},
    }
    # Synthetic transports consume no website resource and should not block real ones.
    data = config.model_dump(mode='json')
    for tool in data['tools']:
        tool['resource'] = 'synthetic:' + tool['resource'] if tool['resource'] else None
        tool['min_interval_seconds'] = 0
        if tool['key'] in fixtures:
            tool['description'] += ' Bu oturumdaki uygulama yalnız sentetik test verisi döndürür.'
            tool['transport'] = 'python'
    from verda.agency.registry import AgencyConfig
    return Registry(AgencyConfig.model_validate(data), fixtures)
