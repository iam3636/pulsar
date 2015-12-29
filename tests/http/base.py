import os
from base64 import b64decode
from functools import wraps
import socket
import unittest

import examples

from pulsar import send, SERVER_SOFTWARE, get_event_loop
from pulsar.utils.path import Path
from pulsar.utils.httpurl import iri_to_uri
from pulsar.apps.http import (HttpClient, TooManyRedirects, HttpResponse,
                              HTTPError)

__test__ = False


def dodgyhook(response, exc=None):
    raise ValueError('Dodgy header hook')


def no_tls(f):
    # Don't do the test when tunneling, it causes timeout at times

    @wraps(f)
    def _(self):
        if not self.with_tls:
            return f(self)

    return _


class TestHttpClientBase:
    app = None
    with_httpbin = True
    with_proxy = False
    with_tls = False
    proxy_app = None
    # concurrency is set by the config object unless you set it here
    concurrency = None
    timeout = 10

    @classmethod
    def setUpClass(cls):
        # Create the HttpBin server by sending this request to the arbiter
        from examples.proxyserver.manage import server as pserver
        from examples.httpbin import manage
        concurrency = cls.concurrency or cls.cfg.concurrency
        if cls.with_httpbin:
            server = manage.server
            if cls.with_tls:
                base_path = os.path.abspath(os.path.dirname(manage.__file__))
                key_file = os.path.join(base_path, 'server.key')
                cert_file = os.path.join(base_path, 'server.crt')
            else:
                key_file, cert_file = None, None
            s = server(bind='127.0.0.1:0', concurrency=concurrency,
                       name='httpbin-%s' % cls.__name__.lower(),
                       keep_alive=30, key_file=key_file, cert_file=cert_file,
                       workers=1)
            cfg = yield from send('arbiter', 'run', s)
            cls.app = cfg.app()
            bits = ('https' if cls.with_tls else 'http',) + cfg.addresses[0]
            cls.uri = '%s://%s:%s/' % bits
        if cls.with_proxy:
            s = pserver(bind='127.0.0.1:0', concurrency=concurrency,
                        name='proxyserver-%s' % cls.__name__.lower())
            cfg = yield from send('arbiter', 'run', s)
            cls.proxy_app = cfg.app()
            cls.proxy_uri = 'http://{0}:{1}'.format(*cfg.addresses[0])
        cls._client = cls.client()

    @classmethod
    def tearDownClass(cls):
        if cls.app is not None:
            yield from send('arbiter', 'kill_actor', cls.app.name)
        if cls.proxy_app is not None:
            yield from send('arbiter', 'kill_actor', cls.proxy_app.name)

    @classmethod
    def client(cls, loop=None, parser=None, pool_size=2, verify=False,
               **kwargs):
        parser = cls.parser()
        if cls.with_proxy:
            kwargs['proxies'] = {'http': cls.proxy_uri,
                                 'https': cls.proxy_uri,
                                 'ws': cls.proxy_uri,
                                 'wss': cls.proxy_uri}
        return HttpClient(loop=loop, parser=parser, pool_size=pool_size,
                          verify=verify, **kwargs)

    @classmethod
    def parser(cls):
        return None

    @property
    def tunneling(self):
        '''When tunneling, the client needs to perform an extra request.'''
        return int(self.with_proxy and self.with_tls)

    def _check_pool(self, http, response, available=1, processed=1,
                    sessions=1, pools=1):
        # Test the connection pool
        self.assertEqual(len(http.connection_pools), pools)
        if pools:
            pool = http.connection_pools[response.request.key]
            self.assertEqual(http.sessions, sessions)
            self.assertEqual(pool.available, available)
            self.assertEqual(http.requests_processed, processed)

    def _after(self, method, response):
        '''Check for a after_%s % method to test the response.'''
        method = getattr(self, 'after_%s' % method, None)
        if method:
            method(response)

    def httpbin(self, *suffix):
        if suffix:
            return self.uri + '/'.join(suffix)
        else:
            return self.uri

    def after_test_home_page(self, response, processed=1):
        request = response.request
        self.assertEqual(request.scheme, 'https' if self.with_tls else 'http')
        # Only one connection pool,
        # even though the proxy and the connection are for different addresses
        http = response.producer
        self.assertEqual(len(http.connection_pools), 1)
        pool = http.connection_pools[response.request.key]
        self.assertEqual(pool.available, 1)
        self.assertEqual(pool.in_use, 0)
        self.assertEqual(http.sessions, 1)
        self.assertEqual(http.requests_processed, processed)
        self.assertEqual(response._connection._processed, processed)

    def _check_server(self, response):
        self.assertEqual(response.headers['server'], SERVER_SOFTWARE)

    def _test_stream_response(self, siz=3000, rep=10):
        http = self._client
        response = yield from http.get(
            self.httpbin('stream/%d/%d' % (siz, rep)))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.parser.is_chunked())
        body = response.content
        self.assertEqual(len(body), siz*rep)


class TestHttpClient(TestHttpClientBase, unittest.TestCase):

    def test_home_page(self):
        http = self.client()
        response = yield from http.get(self.httpbin())
        self.assertEqual(str(response), '200')
        self.assertTrue('content-length' in response.headers)
        content = response.content
        size = response.headers['content-length']
        self.assertEqual(len(content), int(size))
        self.assertEqual(response.headers['connection'], 'Keep-Alive')
        self._check_server(response)
        self.after_test_home_page(response)
        # Try again
        response = yield from http.get(self.httpbin())
        self.assertEqual(str(response), '200')
        self._check_server(response)
        self.after_test_home_page(response, 2)

class d:
    def test_200_get(self):
        http = self.client()
        response = yield from http.get(self.httpbin())
        self.assertEqual(str(response), '200')
        self.assertEqual(repr(response), 'HttpResponse(200)')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_status(), '200 OK')
        self.assertTrue(response.content)
        self.assertEqual(response.url, self.httpbin())
        self._check_pool(http, response)
        response = yield from http.get(self.httpbin('get'))
        self.assertEqual(response.status_code, 200)
        self._check_pool(http, response, processed=2)

    def test_200_get_data(self):
        http = self.client()
        response = yield from http.get(self.httpbin('get'),
                                       data={'bla': 'foo'})
        result = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'],
                         'application/json; charset=utf-8')
        self.assertEqual(result['args'], {'bla': 'foo'})
        self.assertEqual(response.url,
                         self.httpbin(iri_to_uri('get', {'bla': 'foo'})))
        self._check_pool(http, response)

    def test_200_gzip(self):
        http = self._client
        response = yield from http.get(self.httpbin('gzip'))
        self.assertEqual(response.status_code, 200)
        content = response.json()
        self.assertTrue(content['gzipped'])
        if 'content-encoding' in response.headers:
            self.assertTrue(response.headers['content-encoding'], 'gzip')

    def test_json_post(self):
        http = self._client
        data = {'bla': 'foo',
                'unz': 'whatz',
                'numero': [1, 2]}
        ct = 'application/json'
        response = yield from http.post(self.httpbin('post'),
                                        headers=[('content-type', ct)],
                                        data=data)
        self.assertEqual(response.request.headers['content-type'], ct)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result['args'])
        self.assertEqual(result['args']['numero'], [1, 2])

    def test_upload_images(self):
        http = self._client
        path = Path(examples.__file__).parent.parent
        path = path.join('docs', 'source', '_static')
        files = []
        sent = []
        for name in ('pulsar.png', 'favicon.ico'):
            with open(path.join(name), 'rb') as file:
                image = file.read()
            sent.append(image)
            files.append(('images', (name, image)))
        response = yield from http.put(self.httpbin('upload'), files=files)
        self.assertEqual(response.status_code, 200)
        ct = response.request.headers['content-type']
        self.assertTrue(ct.startswith('multipart/form-data; boundary='))
        data = response.json()
        images = data['files']['images']
        self.assertEqual(len(images), 2)
        for image, s in zip(images, sent):
            image = b64decode(image.encode('utf-8'))
            self.assertEqual(image, s)

    def test_upload_files(self):
        http = self._client
        files = {'test': 'simple file'}
        data = (('bla', 'foo'), ('unz', 'whatz'),
                ('numero', '1'), ('numero', '2'))
        response = yield from http.put(self.httpbin('upload'), data=data,
                                       files=files)
        self.assertEqual(response.status_code, 200)
        ct = response.request.headers['content-type']
        self.assertTrue(ct.startswith('multipart/form-data; boundary='))
        data = response.json()
        self.assertEqual(data['files'], {'test': ['simple file']})
        self.assertEqual(data['args']['numero'], ['1', '2'])

    def test_HttpResponse(self):
        r = HttpResponse(loop=get_event_loop())
        self.assertEqual(r.request, None)
        self.assertEqual(str(r), '<None>')
        self.assertEqual(r.headers, None)

    def test_client(self):
        http = self.client(max_redirects=5, timeout=33)
        self.assertTrue('accept-encoding' in http.headers)
        self.assertEqual(http.timeout, 33)
        self.assertEqual(http.version, 'HTTP/1.1')
        self.assertEqual(http.max_redirects, 5)
        if self.with_proxy:
            self.assertEqual(http.proxies, {'http': self.proxy_uri,
                                            'https': self.proxy_uri,
                                            'ws': self.proxy_uri,
                                            'wss': self.proxy_uri})

    def test_request_object(self):
        http = self._client
        response = yield from http.get(self.httpbin())
        request = response.request
        self.assertTrue(request.headers)
        self.assertTrue(request.has_header('Connection'))
        self.assertTrue(request.has_header('Accept-Encoding'))
        self.assertTrue(request.has_header('User-Agent'))
        self.assertFalse(request.has_header('foo'))
        self.assertEqual(request.headers.kind, 'client')
        self.assertEqual(request.unredirected_headers.kind, 'client')

    def test_http10(self):
        '''By default HTTP/1.0 close the connection if no keep-alive header
        was passed by the client.
        '''
        http = self.client(version='HTTP/1.0')
        http.headers.clear()
        self.assertEqual(http.version, 'HTTP/1.0')
        response = yield from http.get(self.httpbin())
        self.assertEqual(response.headers['connection'], 'close')
        self.assertEqual(str(response), '200')
        self._check_pool(http, response, available=0)

    def test_http11(self):
        '''By default HTTP/1.1 keep alive the connection if no keep-alive
        header was passed by the client.
        '''
        http = self.client()
        http.headers.clear()
        self.assertEqual(http.version, 'HTTP/1.1')
        response = yield from http.get(self.httpbin())
        self.assertEqual(response.headers['connection'], 'keep-alive')
        self._check_pool(http, response)

    def test_http11_close(self):
        http = self.client()
        self.assertEqual(http.version, 'HTTP/1.1')
        response = yield from http.get(
            self.httpbin(), headers=[('connection', 'close')])
        self.assertEqual(response.headers['connection'], 'close')
        self._check_pool(http, response, available=0)

    def test_post(self):
        data = (('bla', 'foo'), ('unz', 'whatz'),
                ('numero', '1'), ('numero', '2'))
        http = self._client
        response = yield from http.post(self.httpbin('post'),
                                        encode_multipart=False,
                                        data=data)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result['args'])
        self.assertEqual(result['args']['numero'], ['1', '2'])

    def test_400_and_get(self):
        http = self.client()
        response = yield from http.get(self.httpbin('status', '400'))
        self._check_pool(http, response, available=0)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_status(), '400 Bad Request')
        self.assertTrue(response.content)
        self.assertRaises(HTTPError, response.raise_for_status)
        # Make sure we only have one connection after a valid request
        response = yield from http.get(self.httpbin('get'))
        self.assertEqual(response.status_code, 200)
        # for tunneling this fails sometimes
        self._check_pool(http, response, sessions=2, processed=2)

    def test_404_get(self):
        http = self._client
        response = yield from http.get(self.httpbin('status', '404'))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(response.headers.has('connection', 'close'))
        self.assertTrue('content-type' in response.headers)
        self.assertTrue(response.content)
        self.assertRaises(HTTPError, response.raise_for_status)

    def test_dodgy_on_header_event(self):
        client = self._client
        response = yield from client.get(self.httpbin(), on_headers=dodgyhook)
        self.assertTrue(response.headers)
        self.assertEqual(response.status_code, 200)

    def test_redirect_1(self):
        http = self.client()
        response = yield from http.get(self.httpbin('redirect', '1'))
        self.assertEqual(response.status_code, 200)
        history = response.history
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0].url.endswith('/redirect/1'))
        self._after('test_redirect_1', response)

    def after_test_redirect_1(self, response):
        redirect = response.history[0]
        self.assertEqual(redirect.connection, response.connection)
        self.assertEqual(response.connection._processed, 2)

    def test_redirect_6(self):
        http = self.client()
        response = yield from http.get(self.httpbin('redirect', '6'))
        self.assertEqual(response.status_code, 200)
        history = response.history
        self.assertEqual(len(history), 6)
        self.assertTrue(history[0].url.endswith('/redirect/6'))
        self._after('test_redirect_6', response)

    def after_test_redirect_6(self, response):
        redirect = response.history[-1]
        self.assertEqual(redirect.connection, response.connection)
        self.assertEqual(response.connection._processed, 7)

    def test_large_response(self):
        http = self._client
        response = yield from http.get(self.httpbin('getsize/600000'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['size'], 600000)
        self.assertEqual(len(data['data']), 600000)
        self.assertFalse(response.parser.is_chunked())

    def test_too_many_redirects(self):
        http = self._client
        try:
            response = yield from http.get(self.httpbin('redirect', '5'),
                                           max_redirects=2)
        except TooManyRedirects as e:
            response = e.response
        else:
            assert False, 'TooManyRedirects not raised'
        history = response.history
        self.assertEqual(len(history), 2)
        self.assertTrue(history[0].url.endswith('/redirect/5'))
        self.assertTrue(history[1].url.endswith('/redirect/4'))

    def test_post_multipart(self):
        data = (('bla', 'foo'), ('unz', 'whatz'),
                ('numero', '1'), ('numero', '2'))
        http = self._client
        response = yield from http.post(self.httpbin('post'), data=data)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result['args'])
        self.assertEqual(result['args']['numero'], ['1', '2'])

    def test_put(self):
        data = (('bla', 'foo'), ('unz', 'whatz'),
                ('numero', '1'), ('numero', '2'))
        http = self._client
        response = yield from http.put(self.httpbin('put'), data=data)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result['args'])
        self.assertEqual(result['args']['numero'], ['1', '2'])

    def test_patch(self):
        data = (('bla', 'foo'), ('unz', 'whatz'),
                ('numero', '1'), ('numero', '2'))
        http = self._client
        response = yield from http.patch(self.httpbin('patch'),
                                         data=data)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result['args'])
        self.assertEqual(result['args']['numero'], ['1', '2'])

    def test_delete(self):
        data = (('bla', 'foo'), ('unz', 'whatz'),
                ('numero', '1'), ('numero', '2'))
        http = self._client
        response = yield from http.delete(self.httpbin('delete'), data=data)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result['args'])
        self.assertEqual(result['args']['numero'], ['1', '2'])

    def test_response_headers(self):
        http = self._client
        response = yield from http.get(self.httpbin('response_headers'))
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result['Transfer-Encoding'], 'chunked')
        parser = response.parser
        self.assertTrue(parser.is_chunked())

    def test_stream_response(self):
        return self._test_stream_response()

    @no_tls
    def test_stream_response_large_chunk(self):
        return self._test_stream_response(100000, 3)

    def test_expect(self):
        http = self._client
        data = (('bla', 'foo'), ('unz', 'whatz'),
                ('numero', '1'), ('numero', '2'))
        response = yield from http.post(self.httpbin('post'), data=data,
                                        wait_continue=True)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result['args'])
        self.assertEqual(result['args']['numero'], ['1', '2'])

    def test_send_cookie(self):
        http = self._client
        cookies = {'sessionid': 't1', 'cookies_are': 'working'}
        response = yield from http.get(self.httpbin('cookies'),
                                       cookies=cookies)
        self.assertEqual(response.status_code, 200)
        data = response.decode_content()
        self.assertEqual(data['cookies']['sessionid'], 't1')
        self.assertEqual(data['cookies']['cookies_are'], 'working')

    def test_cookie(self):
        http = self._client
        # First set the cookies
        r = yield from http.get(self.httpbin('cookies', 'set', 'bla', 'foo'))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.history)
        self.assertTrue(r.history[0].headers['set-cookie'])
        self.assertTrue(http.cookies)
        # Now check if I get them
        r = yield from http.get(self.httpbin('cookies'))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.request.unredirected_headers)
        result = r.json()
        self.assertTrue(result['cookies'])
        self.assertEqual(result['cookies']['bla'], 'foo')
        # Try without saving cookies
        http = self.client(store_cookies=False)
        r = yield from http.get(self.httpbin('cookies', 'set', 'bla', 'foo'))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.history)
        self.assertTrue(r.history[0].headers['set-cookie'])
        r = yield from http.get(self.httpbin('cookies'))
        self.assertEqual(r.status_code, 200)
        result = r.json()
        self.assertFalse(result['cookies'])

    def test_basic_authentication(self):
        http = self._client
        r = yield from http.get(self.httpbin('basic-auth/bla/foo'))
        # The response MUST include a WWW-Authenticate header field
        self.assertEqual(r.status_code, 401)
        http.add_basic_authentication('bla', 'foo')
        r = yield from http.get(self.httpbin('basic-auth/bla/foo'))
        self.assertEqual(r.status_code, 200)

    def test_digest_authentication(self):
        # TODO fix this test. Issue #94
        if not self.tunneling:
            http = self.client()
            r = yield from http.get(self.httpbin(
                'digest-auth/luca/bla/auth'))
            self.assertEqual(r.status_code, 401)
            http.add_digest_authentication('luca', 'bla')
            r = yield from http.get(self.httpbin(
                'digest-auth/luca/bla/auth'))
            self.assertEqual(r.status_code, 200)

    def test_missing_host_400(self):
        http = self._client

        def remove_host(response, exc=None):
            r = response.request
            self.assertTrue(r.has_header('host'))
            response.request.remove_header('host')
            self.assertFalse(r.has_header('host'))

        response = yield from http.get(self.httpbin(),
                                       pre_request=remove_host)
        self.assertEqual(response.status_code, 400)

    def test_missing_host_10(self):
        http = self.client(version='HTTP/1.0')

        def remove_host(response, exc=None):
            r = response.request
            self.assertTrue(r.has_header('host'))
            r.remove_header('host')
            self.assertFalse(r.has_header('host'))
            return response

        response = yield from http.get(self.httpbin(),
                                       pre_request=remove_host)
        self.assertEqual(response.status_code, 200)

    def test_expect_fail(self):
        '''This is an important test for the proxy server example.
        The expect-continue must be handled by the upstream server which in
        this case refuses the continue.'''
        http = self._client
        data = (('bla', 'foo'), ('unz', 'whatz'),
                ('numero', '1'), ('numero', '2'))
        response = yield from http.post(self.httpbin('expect'), data=data,
                                        wait_continue=True)
        self.assertEqual(response.status_code, 417)

    def test_media_root(self):
        http = self._client
        response = yield from http.get(self.httpbin('media/'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'],
                         'text/html; charset=utf-8')

    def test_media_file(self):
        http = self._client
        response = yield from http.get(self.httpbin('media/httpbin.js'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['content-type'] in
                        ('application/javascript',
                         'application/x-javascript'))
        self.assertTrue(int(response.headers['content-length']) > 0)
        modified = response.headers.get('Last-modified')
        self.assertTrue(modified)
        #
        # Test if modified since
        response = yield from http.get(
            self.httpbin('media/httpbin.js'),
            headers=[('If-modified-since', modified)])
        self.assertEqual(response.status_code, 304)
        self.assertFalse('Content-length' in response.headers)

    def test_http_get_timeit(self):
        N = 10
        client = self._client
        bench = yield from client.timeit('get', N, self.httpbin('get'),
                                         data={'bla': 'foo'})
        self.assertTrue(bench.taken)
        self.assertEqual(len(bench.result), N)
        for r in bench.result:
            self.assertEqual(r.status_code, 200)

    def test_send_files(self):
        client = self._client
        files = {'test': 'simple file'}
        data = (('bla', 'foo'), ('unz', 'whatz'),
                ('numero', '1'), ('numero', '2'))
        response = yield from client.post(self.httpbin('post'), data=data,
                                          files=files)
        self.assertEqual(response.status_code, 200)
        ct = response.request.headers['content-type']
        self.assertTrue(ct.startswith('multipart/form-data; boundary='))
        data = response.json()
        self.assertEqual(data['files'], {'test': ['simple file']})
        self.assertEqual(data['args']['numero'], ['1', '2'])

    def test_send_images(self):
        path = Path(examples.__file__).parent.parent
        path = path.join('docs', 'source', '_static')
        files = []
        sent = []
        for name in ('pulsar.png', 'favicon.ico'):
            with open(path.join(name), 'rb') as file:
                image = file.read()
            sent.append(image)
            files.append(('images', (name, image)))
        client = self._client
        response = yield from client.post(self.httpbin('post'), files=files)
        self.assertEqual(response.status_code, 200)
        ct = response.request.headers['content-type']
        self.assertTrue(ct.startswith('multipart/form-data; boundary='))
        data = response.json()
        images = data['files']['images']
        self.assertEqual(len(images), 2)
        for image, s in zip(images, sent):
            image = b64decode(image.encode('utf-8'))
            self.assertEqual(image, s)

    def test_bench_json(self):
        http = self._client
        response = yield from http.get(self.httpbin('json'))
        self.assertEqual(response.headers['content-type'],
                         'application/json; charset=utf-8')
        result = response.decode_content()
        self.assertEqual(result, {'message': 'Hello, World!'})

    def test_bench_text(self):
        http = self._client
        response = yield from http.get(self.httpbin('plaintext'))
        self.assertEqual(response.headers['content-type'],
                         'text/plain; charset=utf-8')
        result = response.decode_content()
        self.assertEqual(result, 'Hello, World!')

    def test_pool_200(self):
        N = 6
        http = self.client(pool_size=2)
        bench = yield from http.timeit('get', N, self.httpbin())
        self.assertEqual(len(bench.result), N)
        for response in bench.result:
            self.assertEqual(str(response), '200')
            self.assertTrue('content-length' in response.headers)
        self.assertEqual(len(http.connection_pools), 1)
        pool = tuple(http.connection_pools.values())[0]
        self.assertEqual(pool.pool_size, 2)
        self.assertEqual(pool.in_use, 0)
        self.assertEqual(pool.available, 2)

    def test_pool_400(self):
        N = 6
        http = self.client(pool_size=2)
        bench = yield from http.timeit('get', N, self.httpbin('status', '400'))
        self.assertEqual(len(bench.result), N)
        for response in bench.result:
            self.assertEqual(str(response), '400')
            self.assertTrue('content-length' in response.headers)
        self.assertEqual(len(http.connection_pools), 1)
        pool = tuple(http.connection_pools.values())[0]
        self.assertEqual(pool.pool_size, 2)
        self.assertEqual(pool.in_use, 0)
        self.assertEqual(pool.available, 0)

    def test_415(self):
        http = self._client
        response = yield from http.get(
            self.httpbin(''), headers=[('accept', 'application/json')])
        self.assertEqual(response.status_code, 415)

    def test_servername(self):
        http = self.client()
        http.headers.remove_header('host')
        self.assertNotIn('host', http.headers)
        http.headers['host'] = 'fakehost'
        response = yield from http.get(self.httpbin('servername'))
        self.assertEqual(response.status_code, 200)
        result = response.decode_content().strip()
        # result should resolve to loopback address
        ips = {sockaddr[0] for _, _, _, _, sockaddr in
               socket.getaddrinfo(result, None)}
        self.assertTrue({'127.0.0.1', '::1'} & ips)

    def test_raw_property(self):
        http = self._client
        response = yield from http.get(self.httpbin('plaintext'))
        raw = response.raw
        self.assertEqual(raw._response, response)
        yield from self.async.assertEqual(raw.read(), b'')

    def test_stream_dont_stream(self):
        http = self._client
        response = yield from http.get(self.httpbin('plaintext'), stream=True)
        yield from response.on_finished
        self.async.assertEqual(response.text(), 'Hello, World!')

    def test_raw_stream(self):
        http = self._client
        response = yield from http.get(self.httpbin('plaintext'), stream=True)
        raw = response.raw
        self.assertEqual(raw._response, response)
        yield from self.async.assertEqual(raw.read(), b'Hello, World!')
        self.assertTrue(raw.done)
        yield from self.async.assertEqual(raw.read(), b'')

    @no_tls
    def test_raw_stream_large(self):
        http = self._client
        url = self.httpbin('stream/100000/3')
        response = yield from http.get(url, stream=True)
        raw = response.raw
        self.assertEqual(raw._response, response)
        data = yield from raw.read()
        self.assertTrue(len(data), 300000)
