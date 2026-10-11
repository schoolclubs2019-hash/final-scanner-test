"""Optional controlled Chromium fallback. Extracted text still uses ProductHTML.

Install requirements-render.txt and Playwright Chromium on the worker image.
No persistent browser profile, cookies, or customer session is used.
"""
from email.message import Message
from urllib.parse import urljoin, urlsplit

from catalogue_discovery import FetchError, PublicHTTP, Response, section_label
from scan_policy import ScanPaused


class BrowserRenderer:
    def __init__(self, timeout_ms=30000, allow_local=False, budget=None):
        self.timeout_ms = timeout_ms
        self.allow_local = allow_local
        self.budget = budget

    def _timeout(self, milliseconds=None):
        milliseconds = self.timeout_ms if milliseconds is None else milliseconds
        if self.budget is None:
            return milliseconds
        return max(1, int(self.budget.timeout(milliseconds / 1000) * 1000))

    def fetch_json(self, url):
        return self.fetch_json_sequence([url])[0]

    def fetch_json_sequence(self, urls, request_executor=None):
        """Fetch one exact JSON endpoint; never interpret a browser DOM as JSON.

        The caller owns durable throttling and catalogue schema validation. This
        transport accepts only a successful raw JSON response from the requested
        URL, with no redirect, script, subresource or authenticated session.
        """
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
        except ImportError:
            raise FetchError('renderer_not_configured', True) from None
        if not urls or len(urls) > 3:
            raise FetchError('render_json_sequence_invalid')
        url = urls[0]
        origin = urlsplit(url)
        if any((urlsplit(v).scheme, urlsplit(v).netloc) != (origin.scheme, origin.netloc) for v in urls):
            raise FetchError('render_json_sequence_origin_mismatch')
        guard = PublicHTTP(url, allow_local=self.allow_local, budget=self.budget)
        if any(urlsplit(v).fragment for v in urls):
            raise FetchError('render_json_endpoint_mismatch')
        route_errors, captured, attempted = [], [], []
        try:
            with sync_playwright() as playwright:
                browser = (playwright.chromium.launch(headless=True, timeout=self._timeout())
                           if self.budget is not None else playwright.chromium.launch(headless=True))
                context = browser.new_context(service_workers='block', accept_downloads=False,
                                              java_script_enabled=False)
                def read_response(fetched):
                    guard.validate(fetched.url)
                    if fetched.url != url:
                        raise FetchError('render_json_endpoint_mismatch')
                    headers = dict(fetched.headers)
                    if fetched.status == 429:
                        raise FetchError('http_429', True, headers.get('retry-after'))
                    if fetched.status in {301, 302, 303, 307, 308}:
                        raise FetchError('render_json_endpoint_mismatch')
                    if fetched.status != 200:
                        raise FetchError('render_http_error', fetched.status >= 500)
                    content_type = Message()
                    content_type['content-type'] = headers.get('content-type', '')
                    media_type = content_type.get_content_type()
                    endpoint_path = urlsplit(url).path
                    ajax_json = (media_type in {'text/javascript', 'application/javascript'}
                                 and (endpoint_path.endswith('/cart.js') or
                                      '/products/' in endpoint_path and endpoint_path.endswith('.js')))
                    if not (ajax_json or media_type == 'application/json' or
                            media_type.startswith('application/') and media_type.endswith('+json')):
                        raise FetchError('render_json_content_type')
                    charset = content_type.get_content_charset()
                    if charset is not None and charset not in {'utf-8', 'utf8'}:
                        raise FetchError('render_json_charset')
                    length = headers.get('content-length')
                    if length is not None and (not length.isdigit() or int(length) > 8_000_000):
                        raise FetchError('render_response_too_large')
                    raw = fetched.body()
                    if len(raw) > 8_000_000:
                        raise FetchError('render_response_too_large')
                    try:
                        text = raw.decode('utf-8')
                    except UnicodeDecodeError:
                        raise FetchError('render_json_utf8') from None
                    if ajax_json:
                        import json
                        try:
                            value = json.loads(text)
                        except (ValueError, TypeError):
                            raise FetchError('render_ajax_not_json') from None
                        if not isinstance(value, dict):
                            raise FetchError('render_ajax_not_json')
                    return Response(fetched.url, text, headers)
                def route(request_route):
                    request = request_route.request
                    # No secondary request gets a network slot or a second try.
                    if request.resource_type != 'document' or request.method != 'GET':
                        request_route.abort()
                        return
                    try:
                        guard.validate(request.url)
                        if request.url != url or attempted:
                            raise FetchError('render_json_endpoint_mismatch')
                        attempted.append(url)
                        fetched = request_route.fetch(max_redirects=0, max_retries=0,
                                                      timeout=self._timeout())
                        try:
                            result = read_response(fetched)
                        finally:
                            # Dispose before abort/fulfill can unblock goto and
                            # let the caller close this browser context.
                            fetched.dispose()
                        captured.append(result)
                        # Chromium need not build its JSON viewer or execute the
                        # response. The original bytes above are the evidence.
                        request_route.fulfill(status=200, content_type='text/plain', body='')
                    except (FetchError, ScanPaused) as exc:
                        route_errors.append(exc)
                        request_route.abort()
                    except BrowserTimeout:
                        route_errors.append(FetchError('timeout', True))
                        request_route.abort()
                    except Exception:
                        route_errors.append(FetchError('renderer_error', True))
                        request_route.abort()
                context.route('**/*', route)
                page = context.new_page()
                results = []
                def read_one(requested_url):
                    if requested_url != url:
                        raise FetchError('render_json_endpoint_mismatch')
                    response = page.goto(url, wait_until='commit', timeout=self._timeout())
                    if route_errors:
                        raise route_errors[0]
                    if response is None or len(captured) != 1:
                        raise FetchError('render_http_error', True)
                    guard.validate(response.url)
                    guard.validate(page.url)
                    if response.url != url or page.url != url or response.request.redirected_from is not None:
                        raise FetchError('render_json_endpoint_mismatch')
                    return captured[0]
                for url in urls:
                    route_errors.clear(); captured.clear(); attempted.clear()
                    guard.validate(url)
                    results.append(request_executor(url, read_one) if request_executor else read_one(url))
                context.close()
                browser.close()
                return results
        except (FetchError, ScanPaused):
            raise
        except BrowserTimeout:
            if route_errors:
                raise route_errors[0]
            if self.budget is not None:
                self.budget.check()
            raise FetchError('timeout', True) from None
        except Exception:
            if route_errors:
                raise route_errors[0]
            raise FetchError('renderer_error', True) from None

    def __call__(self, url):
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
        except ImportError:
            raise FetchError('renderer_not_configured', True) from None
        guard = PublicHTTP(url, allow_local=self.allow_local, budget=self.budget)
        route_errors = []
        try:
            with sync_playwright() as playwright:
                browser = (playwright.chromium.launch(headless=True, timeout=self._timeout())
                           if self.budget is not None else playwright.chromium.launch(headless=True))
                context = browser.new_context(service_workers='block', accept_downloads=False)
                def route(request_route):
                    request = request_route.request
                    try:
                        guard.validate(request.url)
                    except ScanPaused as exc:
                        route_errors.append(exc)
                        request_route.abort()
                        return
                    except FetchError:
                        request_route.abort()
                        return
                    if request.resource_type in {'image', 'media', 'font'} or request.method not in {'GET', 'HEAD'}:
                        request_route.abort()
                    else:
                        # Chromium does not route subsequent redirect hops. Fetch
                        # with redirects disabled and validate each target before
                        # network I/O, then fulfill only the final safe response.
                        try:
                            fetched = request_route.fetch(max_redirects=0, timeout=self._timeout())
                            for hop in range(6):
                                guard.validate(fetched.url)
                                if fetched.status not in {301, 302, 303, 307, 308}:
                                    break
                                location = fetched.headers.get('location')
                                if not location or hop == 5:
                                    raise FetchError('render_redirect_limit')
                                target = urljoin(fetched.url, location)
                                guard.validate(target)
                                fetched.dispose()
                                fetched = context.request.fetch(target, method=request.method,
                                                                max_redirects=0, timeout=self._timeout())
                            if fetched.status == 429:
                                retry_after = fetched.headers.get('retry-after')
                                fetched.dispose()
                                raise FetchError('http_429', True, retry_after)
                            request_route.fulfill(response=fetched)
                            fetched.dispose()
                        except (FetchError, ScanPaused) as exc:
                            # Block offsite/private subresources without treating
                            # an unrelated asset redirect as product-page failure.
                            # Main documents, rate limits and budget pauses still fail.
                            if ((request.resource_type == 'document' and request.frame == page.main_frame)
                                    or not isinstance(exc, FetchError)
                                    or exc.state not in {'offsite_url', 'unsafe_address', 'unsafe_url'}):
                                route_errors.append(exc)
                            request_route.abort()
                        except Exception:
                            route_errors.append(FetchError('renderer_error', True))
                            request_route.abort()
                context.route('**/*', route)
                page = context.new_page()
                if self.budget is not None:
                    page.set_default_timeout(self._timeout())
                response = page.goto(url, wait_until='networkidle', timeout=self._timeout())
                if route_errors:
                    raise route_errors[0]
                if response is None:
                    raise FetchError('render_http_error', True)
                guard.validate(response.url)
                request = response.request
                while request is not None:
                    guard.validate(request.url)
                    request = request.redirected_from
                guard.validate(page.url)
                if response.status >= 400:
                    raise FetchError('render_http_error', True)
                # Read only labels whose product panels will be captured afterwards.
                labels = page.locator('summary, button[aria-controls], button[aria-expanded]')
                for index in range(labels.count()):
                    if self.budget is not None:
                        self.budget.check()
                    label = labels.nth(index)
                    recognised = section_label(label.inner_text())
                    if not recognised:
                        # Decorative icon text can accompany a nested theme heading.
                        headings = label.locator('h2, h3, h4, h5, h6')
                        recognised = any(section_label(headings.nth(i).inner_text()) for i in range(headings.count()))
                    if recognised and label.is_visible():
                        opened = label.evaluate("el => el.tagName === 'SUMMARY' ? Boolean(el.parentElement.open) : el.getAttribute('aria-expanded') === 'true'")
                        if not opened:
                            label.click(timeout=self._timeout(3000))
                page.wait_for_load_state('networkidle', timeout=self._timeout())
                if route_errors:
                    raise route_errors[0]
                guard.validate(page.url)
                html = page.content()
                if len(html.encode()) > 8_000_000:
                    raise FetchError('render_response_too_large')
                context.close()
                browser.close()
                return html
        except (FetchError, ScanPaused):
            raise
        except BrowserTimeout:
            if route_errors:
                raise route_errors[0]
            if self.budget is not None:
                self.budget.check()
            raise FetchError('timeout', True) from None
        except Exception:
            if route_errors:
                raise route_errors[0]
            raise FetchError('renderer_error', True) from None
