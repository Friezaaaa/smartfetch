"""The disabled prototype's broker must never accept an unsafe target."""

import unittest

from smartfetch.browser_prototype.policy import DestinationPolicy, PolicyError


class DestinationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = DestinationPolicy()

    def test_http_hostname_resolves_to_global_peer(self):
        calls = []

        def resolve(host, port):
            calls.append((host, port))
            return ["8.8.8.8"]

        target, peer = self.policy.resolve_target(
            "http://example.test/story", connect=False, resolver=resolve
        )
        self.assertEqual((target.host, target.port, target.scheme, peer),
                         ("example.test", 80, "http", "8.8.8.8"))
        self.assertEqual(calls, [("example.test", 80)])

    def test_connect_requires_dns_hostname_and_https_port(self):
        target, peer = self.policy.resolve_target(
            "example.test:443", connect=True,
            resolver=lambda host, port: ["1.1.1.1"],
        )
        self.assertEqual((target.host, target.port, target.scheme, peer),
                         ("example.test", 443, "https", "1.1.1.1"))

    def test_malformed_connect_never_resolves(self):
        for raw in (
            "https://example.test:443", "user@example.test:443",
            "127.0.0.1:443", "[::1]:443", "example.test:8443",
            "example.test:443/path", "example.test:443#fragment",
            "example.test", "example.test:0", "example.test:65536",
        ):
            with self.subTest(raw=raw):
                calls = []
                with self.assertRaises(PolicyError):
                    self.policy.resolve_target(raw, connect=True,
                                               resolver=lambda *args: calls.append(args))
                self.assertEqual(calls, [])

    def test_http_rejects_userinfo_ip_literals_and_unsafe_schemes(self):
        for raw in (
            "http://user:pw@example.test/", "http://127.0.0.1/",
            "http://[::1]/", "ftp://example.test/file",
            "http://example.test:8080/", "https://example.test:80/",
            "http://example.test:0/",
            "http://example.test/#fragment", "http://-bad.test/",
        ):
            with self.subTest(raw=raw):
                calls = []
                with self.assertRaises(PolicyError):
                    self.policy.resolve_target(raw, connect=False,
                                               resolver=lambda *args: calls.append(args))
                self.assertEqual(calls, [])

    def test_every_dns_answer_must_be_allowed(self):
        for answers in (
            [], ["127.0.0.1"], ["169.254.169.254"], ["10.0.0.1"],
            ["::1"], ["fc00::1"], ["8.8.8.8", "192.168.1.1"],
            ["not-an-ip"],
        ):
            with self.subTest(answers=answers):
                with self.assertRaises(PolicyError):
                    self.policy.resolve_target(
                        "https://example.test/", connect=False,
                        resolver=lambda host, port: answers,
                    )

    def test_peer_identity_must_match_pinned_address(self):
        self.policy.verify_peer("8.8.8.8", "8.8.8.8")
        with self.assertRaises(PolicyError) as wrong:
            self.policy.verify_peer("8.8.8.8", "8.8.4.4")
        self.assertEqual(wrong.exception.code, "peer_mismatch")

    def test_denial_code_is_finite_and_does_not_reflect_target(self):
        with self.assertRaises(PolicyError) as denied:
            self.policy.resolve_target(
                "http://secret.example.test:8080/credential", connect=False,
                resolver=lambda host, port: ["8.8.8.8"],
            )
        self.assertLessEqual(len(str(denied.exception)), 40)
        self.assertNotIn("secret", str(denied.exception))


if __name__ == "__main__":
    unittest.main()
