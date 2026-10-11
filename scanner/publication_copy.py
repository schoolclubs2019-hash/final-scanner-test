"""Uniform publication copy using the owner's Kent example.

This returns fields for the external publisher; it never writes to WordPress.
Only a publishable verified textile result can use the supplied textile template.
"""
import re


def publication_copy(brand_name, report):
    name = str(brand_name or '').strip()
    if not name:
        return {'status': 'brand_name_required', 'fields': None}
    if len(name) > 200 or re.search(r'[<>\x00-\x1f]', name):
        return {'status': 'invalid_brand_name', 'fields': None}
    if not report.get('publish_eligible'):
        return {'status': 'verified_report_required', 'fields': None}
    category = report.get('category')
    if category not in {'clothing', 'bedding', 'swimwear'}:
        # The approved fibre wording does not describe INCI or food evidence.
        return {'status': 'category_template_required', 'fields': None}
    product = {'clothing': 'clothing', 'bedding': 'bedding', 'swimwear': 'swimwear'}[category]
    fully_natural = (report.get('status') == 'verified' and report.get('score') == 5
                     and report.get('plantPct') == 100 and category != 'swimwear')
    qualifier = 'fully natural ' if fully_natural else ''
    excerpt = (f'{name} sells {qualifier}{product} that we checked fibre by fibre for natural, non toxic materials. '
               f'See its verified sustainability rating and review to help you buy natural, non toxic {product}.')
    lead = f'{name} {product} rated fully natural.' if fully_natural else f'{name} {product} reviewed fibre by fibre.'
    description = f'{lead} See its verified sustainability rating and review to buy natural, non toxic {product}.'
    return {'status': 'ready', 'template': 'kent-textile-v1',
            'fields': {'title': name, 'excerpt': excerpt, 'rank_math_description': description}}
