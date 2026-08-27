import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from x402.schemas import SupportedKind, SupportedResponse

from smartfetch import server
from smartfetch.payments import BASE_SEPOLIA, X402Settings


GLAMA_PATH = '/.well-known/glama.json'
EXPECTED_GLAMA_DOCUMENT = {
    '$schema': 'https://glama.ai/mcp/schemas/connector.json',
    'maintainers': [
        {
            'email': 'smartfetch.contact@gmail.com',
        },
    ],
}
PAID_SETTINGS = X402Settings(
    enabled=True,
    pay_to='0x1111111111111111111111111111111111111111',
    price='$0.005',
    network=BASE_SEPOLIA,
)
SUPPORTED = SupportedResponse(kinds=[SupportedKind(
    x402Version=2,
    scheme='exact',
    network=BASE_SEPOLIA,
)])


class GlamaOwnershipVerificationTests(unittest.TestCase):
    def test_route_returns_only_exact_json_and_stays_free_with_x402(self):
        with patch(
            'x402.http.HTTPFacilitatorClient.get_supported',
            return_value=SUPPORTED,
        ):
            app = server.create_app(PAID_SETTINGS)

        with TestClient(app) as client:
            response = client.get(GLAMA_PATH)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'], 'application/json')
        self.assertNotIn('payment-required', response.headers)
        self.assertEqual(response.json(), EXPECTED_GLAMA_DOCUMENT)


if __name__ == '__main__':
    unittest.main()
