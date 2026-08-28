import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient
from fastapi.openapi.models import OpenAPI
from starlette.requests import Request
from x402.extensions.bazaar import validate_discovery_extension_spec
from x402.http.utils import decode_payment_required_header
from x402.schemas import SupportedKind, SupportedResponse

from smartfetch import bazaar, discovery, server
from smartfetch.payments import BASE_MAINNET, BASE_SEPOLIA, X402Settings


VALID_ADDRESS = '0x1111111111111111111111111111111111111111'
MCP_HEADERS = {
    'Accept': 'application/json, text/event-stream',
    'Content-Type': 'application/json',
}
TOOL_NAMES = [
    'fetch_webpage',
    'webpage_to_markdown',
    'extract_webpage_text',
    'render_webpage',
]
TOOL_DESCRIPTIONS = {
    'fetch_webpage': bazaar.FETCH_DESCRIPTION,
    'webpage_to_markdown': (
        'Convert a public webpage or URL into clean Markdown for AI agents, '
        'with core retrieval metadata.'
    ),
    'extract_webpage_text': (
        'Extract clean readable text from a public webpage or URL for AI '
        'agents, with core retrieval metadata.'
    ),
    'render_webpage': (
        'Browser-render a public JavaScript-heavy webpage or URL, then return '
        'clean text, Markdown, links, and metadata.'
    ),
}
SUPPORTED = SupportedResponse(kinds=[SupportedKind(
    x402Version=2,
    scheme='exact',
    network=BASE_SEPOLIA,
)])
PAID_SETTINGS = X402Settings(
    enabled=True,
    pay_to=VALID_ADDRESS,
    price='$0.005',
    network=BASE_SEPOLIA,
)
FREE_SETTINGS = X402Settings(
    enabled=False,
    pay_to=None,
    price='$0.005',
    network=BASE_SEPOLIA,
)
FULL_RESULT = {
    'success': True,
    'requested_url': 'https://example.com/',
    'final_url': 'https://example.com/',
    'status_code': 200,
    'render_method': 'http',
    'elapsed_ms': 12,
    'fallback_reason': 'test fallback',
    'title': 'Example Domain',
    'content': 'clean text',
    'markdown': '# clean markdown',
    'links': [],
    'word_count': 2,
    'content_hash': '0' * 64,
    'low_quality': False,
    'truncated': False,
    'original_content_chars': 10,
    'original_markdown_chars': 16,
    'returned_content_chars': 10,
    'returned_markdown_chars': 16,
    'links_returned': 0,
    'max_chars': 20000,
}
REPO_ROOT = Path(__file__).resolve().parents[1]


def rpc(client, method, params=None, request_id=1):
    payload = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
    if params is not None:
        payload['params'] = params
    response = client.post('/mcp', headers=MCP_HEADERS, json=payload)
    if response.status_code != 200:
        raise AssertionError(
            f'MCP {method} returned {response.status_code}: {response.text}'
        )
    return response.json()


def initialize(client):
    return rpc(client, 'initialize', {
        'protocolVersion': '2025-06-18',
        'capabilities': {},
        'clientInfo': {'name': 'smartfetch-v19-test', 'version': '1.0'},
    })


def list_tools(client):
    return rpc(client, 'tools/list', {}, request_id=2)['result']['tools']


def call_tool(client, name, arguments=None, request_id=3):
    if arguments is None:
        arguments = {
            'url': 'https://example.com/',
            'max_chars': 20000,
        }
        if name != 'render_webpage':
            arguments['force_browser'] = False
    return rpc(client, 'tools/call', {
        'name': name,
        'arguments': arguments,
    }, request_id=request_id)


class V19MCPToolContractTests(unittest.TestCase):
    def test_tools_list_exposes_exactly_four_distinct_contracts(self):
        with TestClient(server.create_app(FREE_SETTINGS)) as client:
            initialized = initialize(client)
            tools = list_tools(client)

        self.assertEqual(
            initialized['result']['serverInfo']['name'],
            'SmartFetch',
        )
        self.assertEqual([tool['name'] for tool in tools], TOOL_NAMES)
        self.assertEqual(
            {tool['name']: tool['description'] for tool in tools},
            TOOL_DESCRIPTIONS,
        )
        by_name = {tool['name']: tool for tool in tools}
        for name in TOOL_NAMES:
            schema = by_name[name]['inputSchema']
            self.assertEqual(schema['required'], ['url'])
            self.assertEqual(schema['properties']['url']['type'], 'string')
            max_chars = schema['properties']['max_chars']
            self.assertEqual(max_chars['default'], 20000)
            self.assertEqual(max_chars['minimum'], 1000)
            self.assertEqual(max_chars['maximum'], 50000)
            if name == 'render_webpage':
                self.assertNotIn('force_browser', schema['properties'])
            else:
                force_browser = schema['properties']['force_browser']
                self.assertEqual(force_browser['default'], False)
                self.assertEqual(force_browser['type'], 'boolean')

    def test_each_tool_reuses_one_internal_fetch_with_expected_projection(self):
        with (
            patch(
                'smartfetch.server._run_fetch',
                new_callable=AsyncMock,
                return_value=FULL_RESULT,
            ) as fetch,
            TestClient(server.create_app(FREE_SETTINGS)) as client,
        ):
            initialize(client)
            results = {
                name: json.loads(
                    call_tool(client, name, request_id=index)['result'][
                        'content'
                    ][0]['text']
                )
                for index, name in enumerate(TOOL_NAMES, start=3)
            }

        self.assertEqual(fetch.await_count, 4)
        self.assertEqual(fetch.await_args_list[0].args, (
            'https://example.com/', False, 20000,
        ))
        self.assertEqual(fetch.await_args_list[1].args, (
            'https://example.com/', False, 20000,
        ))
        self.assertEqual(fetch.await_args_list[2].args, (
            'https://example.com/', False, 20000,
        ))
        self.assertEqual(fetch.await_args_list[3].args, (
            'https://example.com/', True, 20000,
        ))

        self.assertEqual(results['fetch_webpage']['content'], 'clean text')
        self.assertEqual(
            results['fetch_webpage']['markdown'],
            '# clean markdown',
        )
        self.assertEqual(
            results['webpage_to_markdown']['markdown'],
            '# clean markdown',
        )
        self.assertNotIn('content', results['webpage_to_markdown'])
        self.assertEqual(
            results['extract_webpage_text']['content'],
            'clean text',
        )
        self.assertNotIn('markdown', results['extract_webpage_text'])
        self.assertEqual(results['render_webpage']['content'], 'clean text')
        self.assertEqual(
            results['render_webpage']['markdown'],
            '# clean markdown',
        )


class V19BazaarBuilderTests(unittest.TestCase):
    def test_per_tool_bazaar_schemas_and_examples_match_outputs(self):
        configs = {
            'fetch_webpage': (
                bazaar.FETCH_DESCRIPTION,
                bazaar.FETCH_INPUT_SCHEMA,
                bazaar.FETCH_INPUT_EXAMPLE,
                bazaar.FETCH_OUTPUT_SCHEMA,
                bazaar.FETCH_OUTPUT_EXAMPLE,
            ),
            'webpage_to_markdown': (
                bazaar.MARKDOWN_DESCRIPTION,
                bazaar.FETCH_INPUT_SCHEMA,
                bazaar.FETCH_INPUT_EXAMPLE,
                bazaar.MARKDOWN_OUTPUT_SCHEMA,
                bazaar.MARKDOWN_OUTPUT_EXAMPLE,
            ),
            'extract_webpage_text': (
                bazaar.TEXT_DESCRIPTION,
                bazaar.FETCH_INPUT_SCHEMA,
                bazaar.FETCH_INPUT_EXAMPLE,
                bazaar.TEXT_OUTPUT_SCHEMA,
                bazaar.TEXT_OUTPUT_EXAMPLE,
            ),
            'render_webpage': (
                bazaar.RENDER_DESCRIPTION,
                bazaar.RENDER_INPUT_SCHEMA,
                bazaar.RENDER_INPUT_EXAMPLE,
                bazaar.FETCH_OUTPUT_SCHEMA,
                bazaar.RENDER_OUTPUT_EXAMPLE,
            ),
        }
        for name, config in configs.items():
            with self.subTest(name=name):
                description, input_schema, input_example, output_schema, output_example = config
                extension = bazaar.mcp_discovery_extension(
                    tool_name=name,
                    description=description,
                    input_schema=input_schema,
                    input_example=input_example,
                    output_schema=output_schema,
                    output_example=output_example,
                )
                declaration = extension['bazaar']
                self.assertTrue(
                    validate_discovery_extension_spec(declaration).valid
                )
                info = declaration['info']
                self.assertEqual(info['input']['type'], 'mcp')
                self.assertEqual(info['input']['toolName'], name)
                self.assertEqual(info['input']['transport'], 'streamable-http')
                self.assertEqual(info['input']['inputSchema'], input_schema)
                self.assertEqual(info['input']['example'], input_example)
                self.assertEqual(info['output']['example'], output_example)

        self.assertNotIn(
            'content',
            bazaar.MARKDOWN_OUTPUT_SCHEMA['properties'],
        )
        self.assertNotIn('content', bazaar.MARKDOWN_OUTPUT_EXAMPLE)
        self.assertNotIn(
            'markdown',
            bazaar.TEXT_OUTPUT_SCHEMA['properties'],
        )
        self.assertNotIn('markdown', bazaar.TEXT_OUTPUT_EXAMPLE)
        self.assertNotIn(
            'force_browser',
            bazaar.RENDER_INPUT_SCHEMA['properties'],
        )
        self.assertNotIn('force_browser', bazaar.RENDER_INPUT_EXAMPLE)
        self.assertEqual(
            bazaar.RENDER_OUTPUT_EXAMPLE['render_method'],
            'browser',
        )


class V19MCPPaymentDiscoveryTests(unittest.TestCase):
    def setUp(self):
        patcher = patch(
            'x402.http.HTTPFacilitatorClient.get_supported',
            return_value=SUPPORTED,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_all_four_wrappers_use_shared_payment_and_unique_bazaar_resources(
        self,
    ):
        from x402.mcp import create_payment_wrapper as real_wrapper

        with patch(
            'x402.mcp.create_payment_wrapper',
            wraps=real_wrapper,
        ) as wrapper:
            app = server.create_app(PAID_SETTINGS)

        self.assertEqual(wrapper.call_count, 4)
        calls = wrapper.call_args_list
        self.assertTrue(all(
            call.args[0] is app.state.smartfetch_mcp.resource_server
            for call in calls
        ))
        accepts = calls[0].kwargs['accepts']
        self.assertTrue(all(call.kwargs['accepts'] is accepts for call in calls))
        self.assertEqual(
            [str(call.kwargs['resource'].url) for call in calls],
            [f'mcp://tool/{name}' for name in TOOL_NAMES],
        )
        for name, call in zip(TOOL_NAMES, calls):
            declaration = call.kwargs['extensions']['bazaar']
            self.assertTrue(validate_discovery_extension_spec(
                declaration,
            ).valid)
            info = declaration['info']['input']
            self.assertEqual(info['type'], 'mcp')
            self.assertEqual(info['toolName'], name)
            self.assertEqual(info['description'], TOOL_DESCRIPTIONS[name])
            self.assertEqual(info['transport'], 'streamable-http')

    def test_every_unpaid_tool_challenges_before_retrieval(self):
        app = server.create_app(PAID_SETTINGS)

        with (
            patch('smartfetch.server._run_fetch', new_callable=AsyncMock) as fetch,
            TestClient(app) as client,
        ):
            initialize(client)
            challenges = {
                name: call_tool(client, name, request_id=index)['result']
                for index, name in enumerate(TOOL_NAMES, start=3)
            }

        fetch.assert_not_awaited()
        for name, result in challenges.items():
            self.assertTrue(result['isError'])
            challenge = result['structuredContent']
            self.assertEqual(challenge['error'], 'Payment Required')
            self.assertEqual(
                challenge['resource']['url'],
                f'mcp://tool/{name}',
            )
            accepted = challenge['accepts'][0]
            self.assertEqual(accepted['scheme'], 'exact')
            self.assertEqual(accepted['amount'], '5000')
            self.assertEqual(accepted['network'], BASE_SEPOLIA)
            self.assertEqual(accepted['payTo'], VALID_ADDRESS)
            info = challenge['extensions']['bazaar']['info']['input']
            self.assertEqual(info['type'], 'mcp')
            self.assertEqual(info['toolName'], name)

    def test_existing_http_bazaar_resource_remains_distinct(self):
        app = server.create_app(PAID_SETTINGS)

        with TestClient(app, base_url='https://agent.example') as client:
            response = client.post('/fetch', json={
                'url': 'https://example.com/',
            })

        self.assertEqual(response.status_code, 402)
        required = decode_payment_required_header(
            response.headers['payment-required']
        )
        info = (required.extensions or {})['bazaar']['info']['input']
        self.assertEqual(info['type'], 'http')
        self.assertEqual(info['method'], 'POST')
        self.assertEqual(str(required.resource.url), 'https://agent.example/fetch')
        self.assertNotIn(
            str(required.resource.url),
            [f'mcp://tool/{name}' for name in TOOL_NAMES],
        )
        accepted = required.accepts[0]
        self.assertEqual(accepted.scheme, 'exact')
        self.assertEqual(accepted.amount, '5000')
        self.assertEqual(accepted.network, BASE_SEPOLIA)
        self.assertEqual(accepted.pay_to, VALID_ADDRESS)


class V19DiscoveryDocumentTests(unittest.TestCase):
    def setUp(self):
        scope = {
            'type': 'http',
            'http_version': '1.1',
            'method': 'GET',
            'scheme': 'https',
            'path': '/meta',
            'raw_path': b'/meta',
            'query_string': b'',
            'headers': [(b'host', b'agent.example:9443')],
            'client': ('127.0.0.1', 1234),
            'server': ('agent.example', 9443),
        }
        self.request = Request(scope)
        self.urls = discovery.public_urls(self.request)

    def test_public_urls_use_only_proxy_resolved_request_scheme_and_host(self):
        self.assertEqual(
            discovery.public_base_url(self.request),
            'https://agent.example:9443',
        )
        self.assertEqual(self.urls, {
            'base': 'https://agent.example:9443',
            'x402': 'https://agent.example:9443/.well-known/x402',
            'docs': 'https://agent.example:9443/docs',
            'openapi': 'https://agent.example:9443/openapi.json',
            'llms': 'https://agent.example:9443/llms.txt',
            'robots': 'https://agent.example:9443/robots.txt',
            'sitemap': 'https://agent.example:9443/sitemap.xml',
            'meta': 'https://agent.example:9443/meta',
            'fetch': 'https://agent.example:9443/fetch',
            'mcp': 'https://agent.example:9443/mcp',
        })

    def test_openapi_is_valid_v31_and_describes_only_post_fetch(self):
        document = discovery.openapi_document(self.urls, PAID_SETTINGS)

        OpenAPI.model_validate(document)
        self.assertEqual(document['openapi'], '3.1.0')
        self.assertEqual(document['info']['version'], '1.10.1')
        self.assertEqual(document['servers'], [{
            'url': 'https://agent.example:9443',
        }])
        self.assertEqual(list(document['paths']), ['/fetch'])
        operation = document['paths']['/fetch']['post']
        self.assertEqual(
            operation['requestBody']['content']['application/json']['schema'],
            bazaar.FETCH_INPUT_SCHEMA,
        )
        self.assertEqual(
            operation['responses']['200']['content'][
                'application/json'
            ]['example'],
            bazaar.FETCH_OUTPUT_EXAMPLE,
        )
        self.assertEqual(
            set(operation['responses']),
            {'200', '400', '402', '429', '502', '503', '504'},
        )
        self.assertEqual(
            document['externalDocs']['url'],
            'https://agent.example:9443/docs',
        )

    def test_html_and_llms_text_describe_all_public_capabilities(self):
        html = discovery.docs_html(self.urls)
        llms = discovery.llms_text(self.urls)

        for output in (html, llms):
            for tool in TOOL_NAMES:
                self.assertIn(tool, output)
            self.assertIn('$0.005', output)
            self.assertIn(
                'https://agent.example:9443/.well-known/x402',
                output,
            )
            self.assertIn('https://agent.example:9443/mcp', output)
            self.assertIn('https://github.com/Friezaaaa/smartfetch', output)
        for phrase in (
            'webpage reader',
            'fetch',
            'scrape',
            'extract',
            'Markdown',
            'browser rendering',
            'MCP',
            'x402',
        ):
            self.assertIn(phrase, html)
        for example_url in (
            'https://github.com/Friezaaaa/smartfetch/blob/main/examples/'
            'python/paid_mcp_client.py',
            'https://github.com/Friezaaaa/smartfetch/blob/main/examples/'
            'typescript/paid-mcp-client.ts',
        ):
            self.assertIn(example_url, html)
            self.assertIn(example_url, llms)

    def test_robots_and_sitemap_are_valid_and_exclude_paid_execution(self):
        robots = discovery.robots_text(self.urls)
        self.assertEqual(robots, (
            'User-agent: *\n'
            'Allow: /\n'
            'Sitemap: https://agent.example:9443/sitemap.xml\n'
        ))

        root = ET.fromstring(discovery.sitemap_xml(self.urls))
        namespace = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        locations = {
            element.text for element in root.findall('s:url/s:loc', namespace)
        }
        self.assertEqual(locations, {
            'https://agent.example:9443',
            'https://agent.example:9443/meta',
            'https://agent.example:9443/docs',
            'https://agent.example:9443/openapi.json',
            'https://agent.example:9443/llms.txt',
        })
        serialized = ET.tostring(root, encoding='unicode')
        for paid_or_noncontent in ('/fetch', '/mcp', '/robots.txt', '/sitemap.xml'):
            self.assertNotIn(paid_or_noncontent, serialized)

    def test_runtime_discovery_documents_contain_no_fixed_host_or_secrets(self):
        settings = X402Settings(
            True,
            VALID_ADDRESS,
            '$0.005',
            BASE_MAINNET,
            'organizations/test/apiKeys/test',
            'private credential material',
        )
        output = json.dumps(discovery.openapi_document(self.urls, settings))
        output += json.dumps(discovery.x402_manifest(self.urls, settings))
        output += discovery.docs_html(self.urls)
        output += discovery.llms_text(self.urls)
        output += discovery.robots_text(self.urls)
        output += discovery.sitemap_xml(self.urls)
        lowered = output.lower()
        for forbidden in (
            'smartfetch-production-ea53.up.railway.app',
            'x402_pay_to',
            'cdp_api_key',
            'private_key',
            'wallet_secret',
            VALID_ADDRESS.lower(),
            'private credential material',
        ):
            self.assertNotIn(forbidden, lowered)

    def test_x402_manifest_is_proxy_aware_and_describes_paid_resources(self):
        settings = X402Settings(
            True,
            VALID_ADDRESS,
            '$0.005',
            BASE_MAINNET,
            'organizations/test/apiKeys/test',
            'private credential material',
        )

        manifest = discovery.x402_manifest(self.urls, settings)

        self.assertEqual(manifest, {
            'spec': 'agent402-service-manifest/1',
            'version': 1,
            'name': 'SmartFetch',
            'summary': (
                'Reliable public-web retrieval for AI agents: URL in, clean '
                'text, Markdown, links, and metadata out.'
            ),
            'homepage': 'https://agent.example:9443',
            'repository': 'https://github.com/Friezaaaa/smartfetch',
            'resources': ['https://agent.example:9443/fetch'],
            'payment': {
                'protocol': 'x402',
                'x402Version': 2,
                'enabled': True,
                'scheme': 'exact',
                'price': '$0.005',
                'network': 'eip155:8453',
                'asset': 'USDC',
            },
            'endpoints': {
                'mcp': {
                    'url': 'https://agent.example:9443/mcp',
                    'transport': 'streamable-http',
                },
                'openapi': 'https://agent.example:9443/openapi.json',
                'llms': 'https://agent.example:9443/llms.txt',
                'docs': 'https://agent.example:9443/docs',
                'metadata': 'https://agent.example:9443/meta',
            },
        })
        serialized = json.dumps(manifest).lower()
        for forbidden in (
            VALID_ADDRESS.lower(),
            'organizations/test/apikeys/test',
            'private credential material',
            'payment-signature',
            'authorization',
            'smartfetch-production-ea53.up.railway.app',
            'fd12:7ebb',
        ):
            self.assertNotIn(forbidden, serialized)


class V19DiscoveryRouteTests(unittest.TestCase):
    def setUp(self):
        patcher = patch(
            'x402.http.HTTPFacilitatorClient.get_supported',
            return_value=SUPPORTED,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_all_discovery_routes_and_mcp_discovery_are_free(self):
        app = server.create_app(PAID_SETTINGS)
        expected_types = {
            '/.well-known/x402': 'application/json',
            '/docs': 'text/html',
            '/openapi.json': 'application/json',
            '/llms.txt': 'text/plain',
            '/robots.txt': 'text/plain',
            '/sitemap.xml': 'application/xml',
        }

        with TestClient(app, base_url='https://agent.example') as client:
            responses = {
                path: client.get(path) for path in (
                    '/',
                    '/health',
                    '/meta',
                    *expected_types,
                )
            }
            initialize_response = initialize(client)
            tools = list_tools(client)
            paid_http = client.post('/fetch', json={
                'url': 'https://example.com/',
            })

        for path, response in responses.items():
            with self.subTest(path=path):
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('payment-required', response.headers)
        for path, media_type in expected_types.items():
            self.assertTrue(
                responses[path].headers['content-type'].startswith(media_type)
            )
        self.assertIn('result', initialize_response)
        self.assertEqual([tool['name'] for tool in tools], TOOL_NAMES)
        self.assertEqual(paid_http.status_code, 402)

        meta = responses['/meta'].json()
        self.assertEqual(meta['mcp']['tool'], 'fetch_webpage')
        self.assertEqual(meta['mcp']['tools'], TOOL_NAMES)
        self.assertEqual(meta['mcp']['url'], 'https://agent.example/mcp')
        self.assertEqual(meta['discovery'], {
            'x402': 'https://agent.example/.well-known/x402',
            'docs': 'https://agent.example/docs',
            'openapi': 'https://agent.example/openapi.json',
            'llms': 'https://agent.example/llms.txt',
            'robots': 'https://agent.example/robots.txt',
            'sitemap': 'https://agent.example/sitemap.xml',
        })

    def test_railway_uvicorn_proxy_config_drives_all_public_https_urls(self):
        test_environ = dict(os.environ)
        test_environ.pop('FORWARDED_ALLOW_IPS', None)
        test_environ['RAILWAY_ENVIRONMENT_ID'] = 'test-environment-id'
        with patch.dict(os.environ, test_environ, clear=True):
            config = server.create_uvicorn_config(
                server.create_app(FREE_SETTINGS)
            )
            config.load()
            with TestClient(
                config.loaded_app,
                base_url='http://test-host.example',
            ) as client:
                headers = {
                    'Host': 'test-host.example',
                    'X-Forwarded-For': '203.0.113.10',
                    'X-Forwarded-Proto': 'https',
                }
                meta = client.get('/meta', headers=headers).json()
                docs = client.get('/docs', headers=headers).text
                openapi = client.get('/openapi.json', headers=headers).json()
                llms = client.get('/llms.txt', headers=headers).text
                robots = client.get('/robots.txt', headers=headers).text
                sitemap = client.get('/sitemap.xml', headers=headers).text
                x402 = client.get(
                    '/.well-known/x402',
                    headers=headers,
                ).json()

        base = 'https://test-host.example'
        self.assertEqual(meta['mcp']['url'], f'{base}/mcp')
        self.assertEqual(x402['homepage'], base)
        self.assertEqual(x402['resources'], [f'{base}/fetch'])
        self.assertEqual(x402['endpoints']['mcp']['url'], f'{base}/mcp')
        self.assertTrue(all(
            url.startswith(f'{base}/')
            for url in meta['discovery'].values()
        ))
        self.assertIn(f'{base}/mcp', docs)
        self.assertEqual(openapi['servers'], [{'url': base}])
        self.assertIn(f'{base}/mcp', llms)
        self.assertIn(f'Sitemap: {base}/sitemap.xml', robots)
        self.assertIn(f'<loc>{base}</loc>', sitemap)


class V19RegistryManifestTests(unittest.TestCase):
    def test_manifest_is_exact_v110_remote_only_metadata(self):
        manifest = json.loads((REPO_ROOT / 'server.json').read_text(
            encoding='utf-8'
        ))

        self.assertEqual(manifest, {
            '$schema': (
                'https://static.modelcontextprotocol.io/schemas/'
                '2025-12-11/server.schema.json'
            ),
            'name': 'io.github.Friezaaaa/smartfetch',
            'title': 'SmartFetch',
            'description': (
                'Read, fetch, scrape, and render public webpages into clean '
                'text, Markdown, links, and metadata.'
            ),
            'version': '1.10.1',
            'repository': {
                'url': 'https://github.com/Friezaaaa/smartfetch',
                'source': 'github',
            },
            'remotes': [{
                'type': 'streamable-http',
                'url': (
                    'https://smartfetch-production-ea53.up.railway.app/mcp'
                ),
            }],
        })
        serialized = json.dumps(manifest).lower()
        for forbidden in (
            'packages',
            'headers',
            'api_key',
            'private_key',
            'wallet',
            'x402_pay_to',
            'cdp_api',
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == '__main__':
    unittest.main()
