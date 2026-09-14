import unittest

from smartfetch.payments import BASE_SEPOLIA, X402Settings
from smartfetch.v111_contracts import V111_VARIANTS
from smartfetch.v111_payments import build_v111_http_routes


SETTINGS = X402Settings(
    enabled=True,
    pay_to="0x1111111111111111111111111111111111111111",
    price="$0.005",
    network=BASE_SEPOLIA,
)


class V111StaticPaymentTests(unittest.TestCase):
    def test_all_eight_routes_have_exact_static_prices(self):
        routes = build_v111_http_routes(SETTINGS)

        self.assertEqual(set(routes), {
            f"POST {definition.rest_path}" for definition in V111_VARIANTS
        })
        for definition in V111_VARIANTS:
            route = routes[f"POST {definition.rest_path}"]
            self.assertEqual(route.accepts.scheme, "exact")
            self.assertEqual(route.accepts.network, BASE_SEPOLIA)
            self.assertEqual(route.accepts.pay_to, SETTINGS.pay_to)
            self.assertEqual(route.accepts.price, definition.price)

    def test_no_open_ended_or_existing_fetch_route_is_returned(self):
        routes = build_v111_http_routes(SETTINGS)
        self.assertNotIn("POST /fetch", routes)
        self.assertTrue(all("{" not in key for key in routes))


if __name__ == "__main__":
    unittest.main()
