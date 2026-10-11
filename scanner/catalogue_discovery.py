"""Public catalogue discovery and product-owned evidence capture; no scoring rules."""
import hashlib
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

from confirmed_product import confirm_product, _product_type, DENIED_PATH, DETAIL_PATH
from product_evidence import NOISE, VOID, product_text
from scan_contract import canonical_url
from text_normalizer import normalize_row
from storefront import storefront_family


class FetchError(Exception):
    def __init__(self, state, retryable=False, retry_after=None):
        super().__init__(state)
        self.state, self.retryable, self.retry_after = state, retryable, retry_after


def initial_adapter_miss(state):
    """Unavailable initial probes permit alternatives, never partial catalogues."""
    return state in {
        'http_400', 'http_404', 'http_405', 'http_410', 'http_429',
        'bad_json', 'bad_catalogue_schema', 'blocked', 'timeout',
        'connection_error', 'authority_retry_exhausted',
    } or re.fullmatch(r'http_5[0-9]{2}', state) is not None


@dataclass
class Response:
    url: str
    text: str
    headers: dict

    def json(self):
        try:
            return json.loads(self.text)
        except (ValueError, TypeError):
            raise FetchError('bad_json') from None


def catalogue_products(response, kind):
    """Validate a whole commerce page before it becomes reusable evidence."""
    data = response.json()
    if kind == 'shopify':
        products = data.get('products') if isinstance(data, dict) else None
    else:
        products = data if kind == 'woocommerce' else None
    if not isinstance(products, list):
        raise FetchError('bad_catalogue_schema')
    for obj in products:
        if not isinstance(obj, dict) or not obj.get('id'):
            raise FetchError('invalid_product_object')
        if not obj.get('handle' if kind == 'shopify' else 'permalink'):
            raise FetchError('missing_product_url')
    return products


def crawl_origin(url):
    """Separate crawl addressing from immutable configured listing identity."""
    parsed = urlsplit(url)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise FetchError('unsafe_url')
    return parsed.scheme + '://' + parsed.netloc.lower()


class PublicHTTP:
    """Bounded requests, same-site redirects, no private-address catalogue access.

    Run workers with network egress isolation as additional DNS-rebinding defence.
    """
    def __init__(self, base, timeout=30, retries=2, allow_local=False, requests_per_second=2, budget=None):
        if requests_per_second <= 0:
            raise ValueError('requests_per_second must be positive')
        self.interval = 1 / requests_per_second
        self.last_request = 0.0
        self.configured_url = base
        self.base = crawl_origin(base)
        self.resolution = None
        self.resolving = False
        self.host = urlsplit(base).hostname
        self.allowed_hosts = {self.host, 'www.' + self.host.removeprefix('www.'), self.host.removeprefix('www.')}
        self.timeout, self.retries, self.allow_local = timeout, retries, allow_local
        self.budget = budget
        self.validate(base)

    def validate(self, url):
        if self.budget is not None:
            self.budget.check()
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise FetchError('unsafe_url')
        if parsed.hostname not in self.allowed_hosts:
            raise FetchError('offsite_url')
        if not self.allow_local:
            try:
                addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
            except OSError:
                raise FetchError('connection_error', True) from None
            if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise FetchError('unsafe_address')
        return url

    def resolve(self, get=None):
        if self.resolution is not None:
            return self.resolution
        self.resolving = True
        self.redirects = []
        try:
            try:
                response = (get or self.get)(self.configured_url)
                self.base = crawl_origin(response.url)
            except FetchError as exc:
                if self.redirects or not initial_adapter_miss(exc.state):
                    raise
                # A homepage probe miss does not establish absence of a catalogue.
                self.resolution_probe_state = exc.state
            self.host = urlsplit(self.base).hostname
            self.resolution = {'configured_url': self.configured_url,
                               'crawl_origin': self.base, 'redirects': self.redirects,
                               'allowed_hosts': sorted(self.allowed_hosts),
                               'probe_state': getattr(self, 'resolution_probe_state', 'resolved')}
            return self.resolution
        finally:
            self.resolving = False

    def get(self, url):
        owner = self

        class Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                if owner.resolving:
                    target = urlsplit(crawl_origin(newurl)).hostname
                    if len(owner.redirects) >= 5:
                        raise FetchError('storefront_redirect_limit')
                    if storefront_family(target) != storefront_family(owner.host):
                        raise FetchError('offsite_url')
                    owner.allowed_hosts.add(target)
                    owner.redirects.append({'from': req.full_url, 'to': newurl,
                                            'proof': 'same_storefront_www_or_locale'})
                owner.validate(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        for attempt in range(self.retries + 1):
            try:
                self.validate(url)
                delay = self.interval - (time.monotonic() - self.last_request)
                if delay > 0:
                    if self.budget is not None:
                        self.budget.wait_until(self.budget.clock() + delay)
                    else:
                        time.sleep(delay)
                self.last_request = time.monotonic()
                req = urllib.request.Request(url, headers={'User-Agent': 'HTT-Catalogue-Audit/1.0', 'Accept': '*/*'})
                timeout = self.budget.timeout(self.timeout) if self.budget is not None else self.timeout
                with urllib.request.build_opener(Redirect).open(req, timeout=timeout) as response:
                    if self.budget is not None and callable(getattr(type(response), 'read1', None)):
                        # Check the cooperative deadline between socket/buffer
                        # blocks instead of waiting for an entire slow body.
                        blocks, size = [], 0
                        while size <= 8_000_000:
                            self.budget.check()
                            block = response.read1(min(65_536, 8_000_001 - size))
                            if not block:
                                break
                            blocks.append(block)
                            size += len(block)
                        data = b''.join(blocks)
                    else:
                        data = response.read(8_000_001)
                    if len(data) > 8_000_000:
                        raise FetchError('response_too_large')
                    return Response(response.url, data.decode('utf-8', 'replace'), dict(response.headers.items()))
            except urllib.error.HTTPError as exc:
                state = 'http_404' if exc.code == 404 else 'blocked' if exc.code in {401, 403} else 'http_' + str(exc.code)
                failure = FetchError(state, exc.code == 429 or exc.code >= 500, exc.headers.get('Retry-After') if exc.headers else None)
                exc.close()
                if exc.code == 429:
                    raise failure from None
            except (TimeoutError, socket.timeout):
                failure = FetchError('timeout', True)
            except (urllib.error.URLError, ConnectionError, OSError):
                failure = FetchError('connection_error', True)
            if self.budget is not None and failure.retryable:
                # Deadline expiry is a scheduling pause, not product evidence.
                self.budget.check()
            if attempt == self.retries or not failure.retryable:
                raise failure
            delay = min(2 ** attempt, 4)
            if self.budget is not None:
                self.budget.wait_until(self.budget.clock() + delay, 'worker_backoff')
            else:
                time.sleep(delay)


class Node:
    def __init__(self, tag='', attrs=None, parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs or []), parent
        self.children = []

    def text(self):
        return ' '.join(c.text() if isinstance(c, Node) else c for c in self.children)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def excluded(self):
        return self.tag in {'nav', 'footer', 'aside', 'style', 'noscript', 'template'} or bool(
            NOISE.search(' '.join(self.attrs.get(k, '') for k in ('id', 'class', 'aria-label')))) or bool(self.parent and self.parent.excluded())


def section_label(value):
    """One bounded label vocabulary for static panels and browser controls."""
    label = re.sub(r'\s+', ' ', value).strip(' :').lower()
    pattern = (r'(?:ingredients|inci|materials?(?:\s*(?:&|and)\s*care)?|care\s*(?:&|and)\s*materials?|description|composition|'
               r'fabric|fit\s*(?:&|and)\s*fabric|product details|details|shell|body|lining|'
               r'fill|filling|upper|sole|outsole|insole|rib|contrast|pocketing)')
    return label if re.fullmatch(pattern, label) else None


class ProductHTML(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.current = self.root
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)

    def evidence(self, url, product_id=None):
        objects, sources = [], []
        seen_panels = set()
        nodes = list(self.root.walk())
        for node in nodes:
            if node.excluded():
                continue
            if node.tag == 'script' and node.attrs.get('type') == 'application/ld+json':
                try:
                    value = json.loads(node.text())
                except ValueError:
                    continue
                def absolute_ids(node):
                    if isinstance(node, dict):
                        for key, child in node.items():
                            if key in {'url', '@id'} and isinstance(child, str):
                                node[key] = urljoin(url, child)
                            else:
                                absolute_ids(child)
                    elif isinstance(node, list):
                        for child in node:
                            absolute_ids(child)
                absolute_ids(value)
                # Require a URL match, including for singleton Product objects.
                def schema_products(node):
                    if isinstance(node, dict):
                        if _product_type(node.get('@type')):
                            yield node
                        for child in node.values():
                            yield from schema_products(child)
                    elif isinstance(node, list):
                        for child in node:
                            yield from schema_products(child)
                for obj in schema_products(value):
                    identity_url = obj.get('url') or obj.get('@id')
                    if _product_type(obj.get('@type')) and identity_url and canonical_url(urljoin(url, identity_url)) == canonical_url(url):
                        objects.append(obj)
            if node.tag in {'script', 'style'}:
                continue
            # Some storefront themes render the owned description without a
            # heading. Bind that container to the known catalogue product's
            # purchase form inside a bounded product panel before accepting it.
            description_classes = {'product-description', 'product__description',
                                   'product-single__description'}
            if (product_id and description_classes.intersection(node.attrs.get('class', '').split())
                    and node not in seen_panels):
                owner = node.parent
                while owner and owner.tag not in {'', 'main', 'body', 'html'}:
                    descendants = [n for n in owner.walk() if not n.excluded()]
                    headings = [n for n in descendants if n.tag == 'h1' and n.text().strip()]
                    forms = [n for n in descendants if n.tag == 'form']
                    if headings or forms:
                        if (len(headings) == 1 and len(forms) == 1
                                and forms[0].attrs.get('id') == 'product_form_' + str(product_id)
                                and canonical_url(urljoin(url, forms[0].attrs.get('action', '')))
                                == canonical_url(urljoin(url, '/cart/add'))):
                            value = ' '.join(safe_text(node)).strip()
                            if value and len(value) <= 30000:
                                seen_panels.add(node)
                                sources.append({'source': 'product_section:description',
                                                'raw_original': value, 'url': url,
                                                'owner_product_id': str(product_id)})
                            break
                        # A smaller incomplete wrapper may surround the form;
                        # ambiguous/foreign ownership cannot be repaired above it.
                        if len(headings) > 1 or len(forms) > 1 or (forms and forms[0].attrs.get('id') != 'product_form_' + str(product_id)):
                            break
                    owner = owner.parent
            label = section_label(node.text())
            if node.tag in {'h2', 'h3', 'h4', 'h5', 'h6', 'summary', 'button', 'dt'} and label:
                # Only the heading's panel/section, never the page or main element.
                parent = node.parent
                controlled = node.attrs.get('aria-controls')
                panel = next((n for n in nodes if controlled and n.attrs.get('id') == controlled), None)
                # Theme headings can be nested inside summary wrappers. The
                # enclosing details owns the content; a title wrapper does not.
                summary = node
                while summary.parent and summary.tag != 'summary':
                    summary = summary.parent
                if panel is None and summary.tag == 'summary' and summary.parent and summary.parent.tag == 'details':
                    panel = summary.parent
                if panel is None and parent and parent.tag in {'details', 'section', 'div', 'li', 'dl'}:
                    panel = parent
                if panel and not panel.excluded() and panel not in seen_panels:
                    value = ' '.join(n for n in safe_text(panel)).strip()
                    if len(value) <= 30000:
                        seen_panels.add(panel)
                        sources.append({'source': 'product_section:' + label, 'raw_original': value, 'url': url})
        return objects, sources


def page_identity(item, html, url):
    """Extract first-party identity; navigation and recommendations cannot attest it."""
    out = dict(item)

    def bind_platform_id(value):
        for key in ('product_id', 'platform_product_id'):
            if out.get(key) and str(out[key]) != str(value):
                raise FetchError('product_identity_mismatch')
        out['platform_product_id'] = str(value)

    parser = ProductHTML(html)
    objects, _ = parser.evidence(url)
    if len({json.dumps(obj, sort_keys=True) for obj in objects}) > 1:
        raise FetchError('ambiguous_product_identity')
    if objects:
        out.update(product_json=objects[0], title=objects[0].get('name', ''), schema_type='Product')
        for key in ('sku', 'material', 'description'):
            if isinstance(objects[0].get(key), str):
                out[key] = objects[0][key]
    # Theme product JSON is a positive platform ID only when the explicitly
    # product-owned object names this exact page. Generic hydration/navigation
    # scripts and product recommendations cannot attest a candidate.
    theme_products = []
    for node in parser.root.walk():
        if (node.tag != 'script' or node.excluded()
                or node.attrs.get('type') != 'application/json'
                or not ('data-product-json' in node.attrs
                        or re.fullmatch(r'ProductJson(?:-[A-Za-z0-9_-]+)?', node.attrs.get('id', '')))):
            continue
        try:
            obj = json.loads(node.text())
        except (ValueError, TypeError):
            raise FetchError('malformed_product_identity') from None
        if isinstance(obj, dict) and isinstance(obj.get('product'), dict):
            obj = obj['product']
        if not isinstance(obj, dict):
            raise FetchError('malformed_product_identity')
        declared_url = obj.get('url')
        if not isinstance(declared_url, str) or canonical_url(urljoin(url, declared_url)) != canonical_url(url):
            continue
        product_id, title = obj.get('id'), obj.get('title') or obj.get('name')
        if (isinstance(product_id, bool) or not isinstance(product_id, (int, str))
                or not re.fullmatch(r'[1-9][0-9]*', str(product_id))
                or not isinstance(title, str) or not title.strip()):
            raise FetchError('malformed_product_identity')
        theme_products.append(obj)
    if len({str(obj['id']) for obj in theme_products}) > 1:
        raise FetchError('ambiguous_product_identity')
    if theme_products:
        obj = theme_products[0]
        bind_platform_id(obj['id'])
        out['title'] = obj.get('title') or obj['name']
    # Schema-free commerce metadata needs an explicit product detail declaration.
    meta = {n.attrs.get('property', n.attrs.get('name', '')): n.attrs.get('content', '')
            for n in parser.root.walk() if n.tag == 'meta' and not n.excluded()}
    if meta.get('og:type') in {'product', 'og:product'} and meta.get('og:url') and canonical_url(urljoin(url, meta['og:url'])) == canonical_url(url):
        for source, target in [('product:retailer_item_id', 'sku'), ('product:price:amount', 'price'),
                               ('product:availability', 'availability'), ('og:title', 'title')]:
            if meta.get(source):
                out[target] = meta[source]
        # Some Shopify themes remove JSON-LD after hydration. A purchase form
        # can still identify this exact product page, independently of its URL
        # handle or sitemap membership. Ignore recommendation/cart-edit forms.
        product_ids = set()
        for node in parser.root.walk():
            if (node.tag != 'form' or node.excluded()
                    or 'shopify-product-form' not in node.attrs.get('class', '').split()
                    or canonical_url(urljoin(url, node.attrs.get('action', ''))) != canonical_url(urljoin(url, '/cart/add'))):
                continue
            for field in node.walk():
                if field.tag == 'input' and field.attrs.get('name') == 'product-id' and not field.excluded():
                    value = field.attrs.get('value', '')
                    if not re.fullmatch(r'[1-9][0-9]*', value):
                        raise FetchError('malformed_product_identity')
                    product_ids.add(value)
        if len(product_ids) > 1:
            raise FetchError('ambiguous_product_identity')
        if product_ids and meta.get('og:title', '').strip():
            bind_platform_id(next(iter(product_ids)))
    return out


def product_json_probe_url(item):
    """Provenance may justify a probe; it never establishes product identity."""
    if (item.get('discovery_source') != 'product_sitemap'
            or item.get('product_sitemap_identity') is not True):
        return None
    parsed = urlsplit(item.get('url') or '')
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        return None
    # Keep an optional locale path. Do not build a Shopify endpoint for arbitrary
    # navigation, category, extension-bearing or parameterized candidate URLs.
    if not re.fullmatch(r'/(?:[a-z]{2}(?:-[a-z0-9]{2,})?/)?products/[a-z0-9][a-z0-9-]*/?',
                        parsed.path, re.I):
        return None
    return parsed.scheme + '://' + parsed.netloc + parsed.path.rstrip('/') + '.js'


def product_json_identity(item, response, endpoint):
    """Bind Shopify Ajax JSON to its exact first-party requested product.

    The documented /{locale}/products/{handle}.js response includes a stable
    numeric ID, title, handle, URL and variant array. A named gift-card route,
    sitemap entry, response redirect or arbitrary JSON is never sufficient.
    Raw minor-unit prices remain in the original capture; they are not presented
    as major-unit amounts without a separately verified currency adapter.
    """
    if endpoint != product_json_probe_url(item) or canonical_url(response.url) != canonical_url(endpoint):
        raise FetchError('product_json_identity_mismatch')
    obj = response.json()
    if not isinstance(obj, dict) or not any(key in obj for key in ('id', 'handle', 'variants')):
        raise FetchError('bad_catalogue_schema')
    product_id, title, handle = obj.get('id'), obj.get('title'), obj.get('handle')
    if (isinstance(product_id, bool) or not isinstance(product_id, (int, str))
            or not re.fullmatch(r'[1-9][0-9]*', str(product_id))
            or not isinstance(title, str) or not title.strip()
            or not isinstance(handle, str) or not isinstance(obj.get('url'), str)
            or not isinstance(obj.get('variants'), list) or not obj['variants']):
        raise FetchError('malformed_product_identity')
    expected_handle = urlsplit(item['url']).path.rstrip('/').rsplit('/', 1)[-1]
    declared_url = urljoin(item['url'], obj['url'])
    declared = urlsplit(declared_url)
    if (handle != expected_handle or declared.username is not None or declared.password is not None
            or canonical_url(declared_url) != canonical_url(item['url'])):
        raise FetchError('product_json_identity_mismatch')
    variants = []
    seen = set()
    for variant in obj['variants']:
        if (not isinstance(variant, dict) or isinstance(variant.get('id'), bool)
                or not isinstance(variant.get('id'), (int, str))
                or not re.fullmatch(r'[1-9][0-9]*', str(variant['id']))
                or str(variant['id']) in seen):
            raise FetchError('malformed_product_identity')
        seen.add(str(variant['id']))
        variants.append({key: variant[key] for key in ('id', 'sku') if key in variant})
    for key in ('product_id', 'platform_product_id'):
        if item.get(key) is not None and str(item[key]) != str(product_id):
            raise FetchError('product_json_identity_mismatch')
    if item.get('handle') and item['handle'] != handle:
        raise FetchError('product_json_identity_mismatch')
    out = dict(item)
    out['platform_product_id'] = str(product_id)
    for key, value in (('title', title), ('handle', handle),
                       ('body_html', obj.get('description', '')),
                       ('product_type', obj.get('type', ''))):
        if not out.get(key) and isinstance(value, str):
            out[key] = value
    out['commerce_json'] = {'id': product_id, 'title': title, 'handle': handle,
                            'url': declared_url, 'variants': variants}
    out['product_lookup_json'] = obj
    out['product_lookup_source_endpoint'] = endpoint
    out['product_lookup_response_sha256'] = hashlib.sha256(response.text.encode()).hexdigest()
    out['confirmation_source'] = 'product_json'
    return out


def confirm_candidate(item, response, renderer=None, product_lookup=None):
    out = page_identity(item, response.text, response.url)
    accepted, reason = confirm_product(out)
    endpoint = product_json_probe_url(item) if product_lookup else None
    if not accepted and reason == 'missing_positive_product_evidence' and endpoint:
        try:
            out = product_json_identity(out, product_lookup(endpoint), endpoint)
            accepted, reason = confirm_product(out)
        except FetchError as exc:
            if not initial_adapter_miss(exc.state):
                raise
            # An unavailable per-product endpoint supplies no product evidence.
            # Keep the miss for recovery diagnostics, then try the same bounded
            # rendered identity gates used for other sitemap candidates.
            out['confirmation_probe'] = {'url': endpoint, 'state': exc.state}
    path = urlsplit(item['url']).path
    plausible = (bool(DETAIL_PATH.search(path) or re.search(r'\.(?:html?|aspx)$', path, re.I))
                 or bool(re.search(r'og:type[^>]+product|data-product-id|add[-_ ]to[-_ ]cart', response.text, re.I)))
    plausible = plausible and (not DENIED_PATH.search(path) or bool(DETAIL_PATH.search(path)))
    if not accepted and reason == 'missing_positive_product_evidence' and renderer and plausible:
        rendered = renderer(item['url'])
        if is_challenge_page(rendered, item['url']):
            raise FetchError('blocked', True)
        out = page_identity(out, rendered, item['url'])
        accepted, reason = confirm_product(out)
        if accepted:
            out['confirmation_source'] = 'rendered_html'
    return out, accepted, reason


def safe_text(node):
    if node.excluded() or node.tag in {'script', 'style'}:
        return
    for child in node.children:
        if isinstance(child, Node):
            yield from safe_text(child)
        else:
            yield child


def is_challenge_page(html, url):
    """Distinguish a challenge document from a product page loading CAPTCHA scripts.

    Shopify pages can include CAPTCHA/Turnstile code for contact/review forms.
    The mere presence of those script names is not evidence of a blocked product.
    """
    parsed = ProductHTML(html)
    title = ' '.join(node.text() for node in parsed.root.walk() if node.tag == 'title')
    message = r'just a moment|verify (?:that )?you are human|checking your browser|attention required|access denied'
    if re.search(message, title, re.I):
        return True
    objects, sections = parsed.evidence(url)
    if objects or sections:
        return False
    visible = ' '.join(safe_text(parsed.root))
    return bool(re.search(message, visible, re.I) or
                re.search(r'cf-chl-|challenge-platform', html, re.I))


def capture(item, response, renderer=None, department="auto", rendered_response=False,
            product_json_response=False):
    """Continue beyond empty JSON-LD; unknown extractor coverage is technical."""
    out = dict(item)
    original_json_response = response if product_json_response else None
    if product_json_response:
        # The same exact-response identity binding applies again when a durable
        # JSON checkpoint replaces an exhausted static HTML request. JSON is
        # product-owned commerce evidence, never a document to parse as HTML.
        endpoint = product_json_probe_url(item)
        if not endpoint:
            raise FetchError('product_json_identity_mismatch')
        out = product_json_identity(item, response, endpoint)
        response = Response(out['url'], '', response.headers)
    blocked_page = is_challenge_page(response.text, out.get('url') or response.url)
    if blocked_page:
        # A storefront may return HTTP 200 with an anti-bot challenge page.
        # That page is not product evidence, but a first-party commerce payload
        # discovered from the same shop remains valid product-owned evidence.
        # Discard the challenge HTML and continue through the normal reader;
        # if commerce evidence is insufficient, the renderer below still has to
        # obtain a real product panel or the row remains a technical failure.
        out['page_fetch_status'] = 'blocked'
        response = Response(out.get('url') or response.url, '', response.headers)
    # A previous attempt's failure must not bypass the current extraction/render gates.
    out['fetch_status'] = 'ok'
    def fill_identity(html, url):
        identity_fields = page_identity(out, html, url)
        # Capture fills missing identity. Established catalogue metadata retains
        # precedence in both static and rendered channels; composition sources
        # are retained separately and reconciled by the canonical reader below.
        for key in ('title', 'sku', 'platform_product_id', 'price', 'availability', 'schema_type'):
            if not out.get(key) and identity_fields.get(key):
                out[key] = identity_fields[key]
    fill_identity(response.text, response.url)
    objects, sections = ProductHTML(response.text).evidence(
        response.url, out.get('product_id') or out.get('platform_product_id'))
    sources = []
    for key in ('composition', 'material', 'body_html', 'description'):
        if out.get(key):
            sources.append({'source': 'commerce:' + key, 'raw_original': out[key], 'url': out['url']})
    for obj in objects:
        out.setdefault('product_json', obj)
        for source, target in [('name', 'title'), ('sku', 'sku'), ('gtin', 'gtin')]:
            if obj.get(source) and not out.get(target):
                out[target] = str(obj[source])
        out['schema_type'] = 'Product'
        for key in ('material', 'description'):
            if isinstance(obj.get(key), str):
                if not out.get(key):
                    out[key] = obj[key]
                sources.append({'source': 'jsonld:' + key, 'raw_original': obj[key], 'url': out['url']})
        for prop in obj.get('additionalProperty', []) if isinstance(obj.get('additionalProperty', []), list) else []:
            if isinstance(prop, dict) and re.search(r'material|composition|fabric', str(prop.get('name', '')), re.I):
                sources.append({'source': 'jsonld:' + str(prop['name']), 'raw_original': str(prop.get('value', '')), 'url': out['url']})
    sources.extend(sections)
    if not out.get('description'):
        descriptions = [s['raw_original'] for s in sections if s['source'] == 'product_section:description']
        if descriptions:
            out['description'] = descriptions[0]
    # Reuse the canonical reader to determine whether a deeper read is necessary.
    from scan_contract import read_products
    def project():
        def source_text(source):
            text = product_text(source['raw_original'])
            # A JSON-LD material property is an explicitly labelled declaration.
            # Preserve that context for strong-source conflict detection; plain
            # concatenation otherwise makes a contradictory material disappear.
            if source['source'] in {'jsonld:material', 'rendered:jsonld:material'} and text:
                return 'Material: ' + text
            return text
        out['page_text'] = '\n'.join(dict.fromkeys(source_text(s) for s in sources
                                                  if not s['source'].startswith('commerce:')
                                                  and not s['source'].endswith('product_section:description')
                                                  and not (s['source'] in {'jsonld:description', 'jsonld:material'} and s['raw_original'] == out.get(s['source'].split(':')[1]))))
        out['evidence_sources'] = sources
        rows, _ = read_products([normalize_row(out)], {'department': department}, True)
        if not rows:
            raise FetchError('product_identity_unresolved')
        return rows[0]
    preliminary = project()
    # Rendering may resolve either missing composition or product identity fields
    # (for example a JS-only title/schema needed to route an otherwise confirmed
    # product into the correct department).
    needs_render = preliminary.get('terminal_state') in {'undisclosed', 'category_unresolved', 'partial_composition'}
    if needs_render and renderer:
        rendered = renderer(out['url'])
        if is_challenge_page(rendered, out['url']):
            raise FetchError('blocked', True)
        fill_identity(rendered, out['url'])
        rendered_objects, rendered_sections = ProductHTML(rendered).evidence(
            out['url'], out.get('product_id') or out.get('platform_product_id'))
        for obj in rendered_objects:
            out.setdefault('product_json', obj)
            for source, target in [('name', 'title'), ('sku', 'sku'), ('gtin', 'gtin')]:
                if obj.get(source) and not out.get(target):
                    out[target] = str(obj[source])
            out['schema_type'] = 'Product'
            out['confirmation_source'] = 'rendered_html'
            for key in ('material', 'description'):
                if isinstance(obj.get(key), str):
                    if not out.get(key):
                        out[key] = obj[key]
                    sources.append({'source': 'rendered:jsonld:' + key, 'raw_original': obj[key], 'url': out['url']})
            for prop in obj.get('additionalProperty', []) if isinstance(obj.get('additionalProperty', []), list) else []:
                if isinstance(prop, dict) and re.search(r'material|composition|fabric', str(prop.get('name', '')), re.I):
                    sources.append({'source': 'rendered:jsonld:' + str(prop['name']),
                                    'raw_original': str(prop.get('value', '')), 'url': out['url']})
        for section in rendered_sections:
            section['source'] = 'rendered:' + section['source']
        sources.extend(rendered_sections)
        if not out.get('description'):
            descriptions = [s['raw_original'] for s in rendered_sections if s['source'] == 'rendered:product_section:description']
            if descriptions:
                out['description'] = descriptions[0]
        rendered_row = project()
        # The rendered document is now the evidence response for provenance.
        response = Response(out['url'], rendered, {})
        original_json_response = None
        # Identity schema alone does not establish composition extractor coverage.
        if not rendered_sections and rendered_row.get('terminal_state') == 'undisclosed':
            raise FetchError('extractor_unresolved')
    elif preliminary.get('terminal_state') == 'undisclosed' and not (rendered_response and sections):
        raise FetchError('blocked' if out.get('page_fetch_status') == 'blocked' else 'render_required', True)
    out['fetch_status'] = 'ok'
    evidence_response = original_json_response or response
    out['source_endpoint'] = evidence_response.url
    out['response_sha256'] = hashlib.sha256(evidence_response.text.encode()).hexdigest()
    return normalize_row(out)


class Discovery:
    def __init__(self, http, checkpoint=None, census_unit='product'):
        if census_unit not in {'product', 'variant'}:
            raise ValueError('census_unit must be product or variant')
        self.census_unit = census_unit
        self.http = http
        parsed = urlsplit(http.base)
        self.origin = crawl_origin(http.base)
        self.base_path = parsed.path or '/'
        self.outcomes = []
        self.checkpoint = checkpoint or (lambda url, response: None)

    def get(self, url):
        response = self.http.get(url)
        self.checkpoint(url, response)
        return response

    def probe(self, url):
        # Only use the explicit probe protocol when the HTTP implementation
        # defines it on its class. Mock/dynamic attributes must not silently
        # fabricate a probe method and bypass the real GET failure.
        probe = getattr(type(self.http), 'probe', None)
        getter = self.http.probe if callable(probe) else self.http.get
        response = getter(url)
        self.checkpoint(url, response)
        return response

    def commerce(self, kind):
        seen_pages = set()
        page = 1
        while True:
            path = '/products.json?limit=250&page=' if kind == 'shopify' else '/wp-json/wc/store/v1/products?per_page=100&page='
            url = self.origin + path + str(page)
            # A worker may recover this exact JSON request through its durable
            # browser channel. Other HTTP clients retain the static protocol.
            catalogue = getattr(type(self.http), 'catalogue', None)
            if callable(catalogue):
                response = self.http.catalogue(url, kind, probe=page == 1)
                self.checkpoint(url, response)
            else:
                response = self.probe(url) if page == 1 else self.get(url)
            products = catalogue_products(response, kind)
            if not products:
                return
            signature = hashlib.sha256(json.dumps(products, sort_keys=True).encode()).hexdigest()
            if signature in seen_pages:
                raise FetchError('pagination_repeated')
            seen_pages.add(signature)
            for obj in products:
                url = self.origin + '/products/' + str(obj.get('handle', '')) if kind == 'shopify' else obj.get('permalink')
                self.http.validate(url)
                item = {'product_id': str(obj['id']), 'url': url, 'title': obj.get('title') or obj.get('name', ''),
                        'handle': obj.get('handle', ''), 'sku': obj.get('sku', ''),
                        'product_type': obj.get('product_type', ''), 'commerce_json': obj,
                        'body_html': obj.get('body_html') or obj.get('description', ''),
                        'source_endpoint': response.url, 'discovery_source': kind}
                if self.census_unit == 'variant':
                    variants = obj.get('variants') if kind == 'shopify' else obj.get('variations')
                    if variants:
                        for variant in variants:
                            if not isinstance(variant, dict) or not variant.get('id'):
                                raise FetchError('variant_identity_unresolved')
                            # Woo Store API variation listings omit composition/SKU data;
                            # a dedicated variation adapter is required, never inherit blindly.
                            if kind != 'shopify':
                                raise FetchError('variant_evidence_adapter_required')
                            yield {**item, 'variant_id': str(variant['id']), 'sku': variant.get('sku', ''),
                                   'price': variant.get('price'), 'variant_json': variant}
                        continue
                yield item
            # Woo's explicit pagination header proves exhaustion without an out-of-range request.
            headers = {k.lower(): v for k, v in response.headers.items()}
            total_pages = headers.get('x-wp-totalpages')
            if kind == 'woocommerce' and total_pages is not None and page == int(total_pages):
                return
            page += 1

    def sitemap_roots(self):
        default = self.origin + '/sitemap.xml'
        try:
            self.get(default)
            return [default]
        except FetchError as exc:
            if exc.state not in {'http_404', 'http_410'}:
                raise
        robots_url = self.origin + '/robots.txt'
        robots = self.get(robots_url)
        roots = []
        for line in robots.text.splitlines():
            stripped = line.strip()
            if not stripped.casefold().startswith('sitemap:'):
                continue
            url = stripped.split(':', 1)[1].strip().split()[0] if stripped.split(':', 1)[1].strip() else ''
            if not url:
                continue
            self.http.validate(url)
            if url not in roots:
                roots.append(url)
        if not roots:
            raise FetchError('sitemap_unavailable')

        base_path = [part for part in self.base_path.split('/') if part]
        if base_path:
            market = base_path[0].casefold()
            matching = [url for url in roots
                        if (urlsplit(url).path.split('/')[1:2] or [''])[0].casefold() == market]
            if matching:
                roots = matching
        # A root-domain queue record has no market identity. Use one declared
        # public market sitemap rather than multiplying the same brand across
        # every regional catalogue.
        return roots if base_path else roots[:1]

    def sitemaps(self):
        # Carry whether a URL-set came from an explicitly named product sitemap.
        # This is discovery provenance, not positive product identity. Each
        # candidate still needs product-page evidence before confirmation.
        pending = [(url, False) for url in self.sitemap_roots()]
        visited, products = set(), set()
        while pending:
            url, product_branch = pending.pop(0)
            if url in visited:
                continue
            self.http.validate(url)
            visited.add(url)
            response = self.get(url)
            if '<!DOCTYPE' in response.text.upper() or '<!ENTITY' in response.text.upper():
                raise FetchError('malformed_sitemap')
            try:
                root = ElementTree.fromstring(response.text)
            except ElementTree.ParseError:
                raise FetchError('malformed_sitemap') from None
            kind = root.tag.rsplit('}', 1)[-1]
            if kind not in {'urlset', 'sitemapindex'}:
                raise FetchError('malformed_sitemap')
            entries = []
            for entry in root:
                loc = next((n.text for n in entry if n.tag.rsplit('}', 1)[-1] == 'loc'), None)
                if not loc:
                    raise FetchError('malformed_sitemap')
                self.http.validate(loc)
                entries.append((entry, loc))
            if kind == 'sitemapindex':
                # Storefront indexes commonly mix product, page, collection and
                # editorial sitemaps. When the index explicitly identifies one
                # or more product sitemaps, those are the authoritative catalogue
                # branch. Preserve that provenance into each emitted product URL.
                def product_sitemap(loc):
                    name = urlsplit(loc).path.rsplit('/', 1)[-1].casefold()
                    return re.search(r'(?:^|[-_.])products?(?:[-_.]|$)', name) is not None
                product_entries = [loc for _, loc in entries if product_sitemap(loc)]
                if product_entries:
                    pending.extend((loc, True) for loc in product_entries)
                else:
                    pending.extend((loc, product_branch) for _, loc in entries)
                continue
            for entry, loc in entries:
                if canonical_url(loc) not in products:
                    products.add(canonical_url(loc))
                    detail = bool(DETAIL_PATH.search(urlsplit(loc).path or ''))
                    yield {'url': loc,
                           'source_endpoint': response.url,
                           'catalogue_source_url': response.url,
                           'discovery_source': 'product_sitemap' if product_branch else 'sitemap',
                           'product_sitemap_identity': bool(product_branch and detail),
                           'lastmod': next((n.text for n in entry if n.tag.rsplit('}', 1)[-1] == 'lastmod'), None)}

    def enumerate(self, adapter='auto'):
        if adapter not in {'auto', 'shopify', 'woocommerce', 'sitemap'}:
            raise ValueError('Unsupported discovery adapter')
        if adapter != 'auto':
            yield from self.sitemaps() if adapter == 'sitemap' else self.commerce(adapter)
            return
        # Only an unavailable initial endpoint permits fallback. A partial failure
        # must never be mistaken for a complete smaller catalogue.
        for kind in ('shopify', 'woocommerce'):
            started = False
            try:
                for item in self.commerce(kind):
                    started = True
                    yield item
                self.outcomes.append({'adapter': kind, 'emitted': started, 'state': 'complete' if started else 'empty'})
                if started:
                    return
            except FetchError as exc:
                self.outcomes.append({'adapter': kind, 'emitted': started, 'state': exc.state})
                if started or not initial_adapter_miss(exc.state):
                    raise
        started = False
        try:
            for item in self.sitemaps():
                started = True
                yield item
            self.outcomes.append({'adapter': 'sitemap', 'emitted': started, 'state': 'complete'})
        except FetchError as exc:
            self.outcomes.append({'adapter': 'sitemap', 'emitted': started, 'state': exc.state})
            if not started and (initial_adapter_miss(exc.state) or exc.state == 'sitemap_unavailable'):
                raise FetchError('all_adapters_unavailable', exc.retryable) from None
            raise
