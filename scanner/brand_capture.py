"""Bounded enrichment from pages already fetched by the crawler; no extra network fan-out."""
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from product_evidence import product_text

SOCIAL = {'instagram.com', 'facebook.com', 'linkedin.com', 'youtube.com', 'tiktok.com', 'pinterest.com', 'x.com', 'twitter.com'}
REPORT = re.compile(r'sustainab|ethic|modern.slavery|supply.chain|impact.report|annual.report|responsib|certificat', re.I)
OWNERSHIP = re.compile(r'\b(?:owned by|subsidiary of|legal (?:entity|name)|parent company|company registration)\b', re.I)


def safe_url(base, value):
    url = urljoin(base, str(value or ''))
    parsed = urlsplit(url)
    return url if parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username else ''


class BrandPage(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.images = []
        self.socials = []
        self.reports = []
        self.link = None
        self.link_text = []
        self.script = False
        self.json_text = []
        self.organizations = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and (attrs.get('property') or attrs.get('name')) in {'og:image', 'twitter:image'}:
            url = safe_url(self.url, attrs.get('content'))
            if url:
                self.images.append({'url': url, 'kind': 'landing_social_image'})
        if tag == 'img' and re.search(r'hero|banner', ' '.join(attrs.get(k, '') for k in ('class', 'id', 'alt')), re.I):
            url = safe_url(self.url, attrs.get('src') or attrs.get('data-src'))
            if url:
                self.images.insert(0, {'url': url, 'kind': 'landing_hero_image'})
        if tag == 'a':
            self.link = safe_url(self.url, attrs.get('href')) if attrs.get('href') else ''
            self.link_text = []
        if tag == 'script' and attrs.get('type') == 'application/ld+json':
            self.script = True
            self.json_text = []

    def handle_data(self, value):
        if self.link:
            self.link_text.append(value)
        if self.script:
            self.json_text.append(value)

    def handle_endtag(self, tag):
        if tag == 'a' and self.link:
            host = (urlsplit(self.link).hostname or '').lower().removeprefix('www.')
            label = ' '.join(self.link_text).strip()
            if host in SOCIAL and not re.search(r'/shar(?:e|er)|/intent/', self.link):
                self.socials.append(self.link)
            if REPORT.search(label + ' ' + self.link):
                self.reports.append({'url': self.link, 'label': label[:300]})
            self.link = None
        if tag == 'script' and self.script:
            self.script = False
            try:
                value = json.loads(''.join(self.json_text))
                objects = value.get('@graph', [value]) if isinstance(value, dict) else value
                if isinstance(objects, list):
                    self.organizations.extend(x for x in objects if isinstance(x, dict) and x.get('@type') in ('Organization', 'Corporation'))
            except (ValueError, TypeError):
                pass


def capture_brand(pages):
    if pages is None:
        return {'status': 'not_supplied', 'featured_image_candidate': None, 'social_links': [], 'report_links': [], 'ownership_evidence': []}
    if not isinstance(pages, list) or len(pages) > 8:
        raise ValueError('brand_pages must contain at most eight already-fetched pages.')
    result = {'status': 'captured', 'featured_image_candidate': None, 'social_links': [], 'report_links': [], 'ownership_evidence': []}
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get('html'), str) or len(page['html'].encode()) > 300_000:
            raise ValueError('Each brand page requires HTML of at most 300 KB.')
        url = safe_url('', page.get('url'))
        if not url:
            raise ValueError('Each brand page requires an HTTP(S) source URL.')
        parser = BrandPage(url)
        parser.feed(page['html'])
        source = {'source_url': url, 'captured_at': str(page.get('fetchedAt') or '')[:80], 'verification': 'brand_claim'}
        if urlsplit(url).path in ('', '/') and parser.images and not result['featured_image_candidate']:
            result['featured_image_candidate'] = {**parser.images[0], **source, 'apply_policy': 'fill_empty_only'}
        for link in dict.fromkeys(parser.socials):
            if not any(x['url'] == link for x in result['social_links']):
                result['social_links'].append({'url': link, **source})
        for report in parser.reports[:30]:
            result['report_links'].append({**report, **source})
        for org in parser.organizations:
            for key in ('legalName', 'parentOrganization', 'taxID'):
                if org.get(key):
                    result['ownership_evidence'].append({'field': key, 'value': org[key], **source})
        for sentence in re.split(r'[\n.!?]', product_text(page['html'])):
            if OWNERSHIP.search(sentence):
                result['ownership_evidence'].append({'field': 'ownership_statement', 'value': sentence.strip()[:1000], **source})
    result['ownership_evidence'] = result['ownership_evidence'][:40]
    return result
