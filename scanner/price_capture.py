"""Capture actual offers in their stated currency; never guess an exchange rate."""
from decimal import Decimal, InvalidOperation
import re
from confirmed_product import product_objects


def amount(value):
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    # Unambiguous machine values only: "$1,299" and "1.299,00" need source-specific parsing.
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    try:
        number = Decimal(text)
        return float(number) if number.is_finite() and 0 <= number <= Decimal('1e12') else None
    except InvalidOperation:
        return None


def currency(value):
    text = str(value or '').strip().upper()
    return text if re.fullmatch(r'[A-Z]{3}', text) else ''


def capture_price(item, default_currency=''):
    candidates = []
    preferred = currency(default_currency or item.get('default_currency'))

    def add(obj, source, fallback=''):
        code = currency(obj.get('priceCurrency') or obj.get('currency') or fallback)
        price = amount(obj.get('current_price', obj.get('price')))
        low = amount(obj.get('lowPrice', obj.get('price_min')))
        high = amount(obj.get('highPrice', obj.get('price_max')))
        if price is None and low is None:
            return
        if low is None:
            low = price
        if high is None:
            high = price if price is not None else low
        if low is not None and high is not None and low > high:
            return
        candidates.append({'price': price, 'price_min': low, 'price_max': high,
                           'currency': code, 'price_source': source,
                           'regular_price': amount(obj.get('regular_price')),
                           'compare_at_price': amount(obj.get('compare_at_price')),
                           'sale_price': amount(obj.get('sale_price')),
                           'availability': obj.get('availability') or item.get('availability') or ''})

    for obj in product_objects(item):
        add(obj, 'commerce_product_json', preferred)
        offers = obj.get('offers') or []
        if isinstance(offers, dict):
            offers = [offers]
        if isinstance(offers, list):
            for offer in offers:
                if isinstance(offer, dict):
                    add(offer, 'jsonld_offer', obj.get('priceCurrency') or obj.get('currency') or preferred)
    add(item, 'submitted_product_price', preferred)
    if not candidates:
        return {'price': None, 'price_min': None, 'price_max': None, 'currency': '',
                'price_status': 'not_disclosed', 'price_source': None}
    # Choose the brand's default currency first, then a real USD offer, then another stated offer.
    candidates.sort(key=lambda c: (0 if preferred and c['currency'] == preferred else
                                   1 if c['currency'] == 'USD' else 2 if c['currency'] else 3))
    chosen = dict(candidates[0])
    peers = [c for c in candidates if c['currency'] == chosen['currency'] and c['price_source'] == chosen['price_source']]
    if len(peers) > 1:
        lows = [c['price_min'] for c in peers if c['price_min'] is not None]
        highs = [c['price_max'] for c in peers if c['price_max'] is not None]
        chosen['price_min'] = min(lows) if lows else None
        chosen['price_max'] = max(highs) if highs else None
        if chosen['price_min'] != chosen['price_max']:
            chosen['price'] = None
            chosen['regular_price'] = chosen['sale_price'] = chosen['compare_at_price'] = None
    chosen['price_status'] = 'available' if chosen['currency'] else 'currency_unresolved'
    chosen['price_checked_at'] = item.get('fetchedAt') or ''
    return chosen
