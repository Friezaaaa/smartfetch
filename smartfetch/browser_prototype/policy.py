"""Destination validation for the disabled browser proxy prototype."""

from dataclasses import dataclass
import ipaddress
import re
from urllib.parse import urlsplit


_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_CONNECT = re.compile(r"^([A-Za-z0-9.-]+):([0-9]{1,5})$")


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    scheme: str


class PolicyError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class DestinationPolicy:
    """Strict public-DNS policy; test-only fixture exceptions use a subclass."""

    def port_allowed(self, scheme: str, port: int) -> bool:
        return (scheme, port) in {("http", 80), ("https", 443)}

    def address_allowed(self, ip: str) -> bool:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return address.is_global and not getattr(address, "ipv4_mapped", None)

    @staticmethod
    def _hostname(raw: str) -> str:
        if not isinstance(raw, str) or not raw.isascii():
            raise PolicyError("invalid_host")
        host = raw.lower()
        labels = host.split(".")
        if (len(host) > 253 or len(labels) < 2 or
                any(not _LABEL.fullmatch(label) for label in labels)):
            raise PolicyError("invalid_host")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return host
        raise PolicyError("ip_literal_denied")

    def resolve_target(self, raw_target: str, *, connect: bool, resolver):
        if (not isinstance(raw_target, str) or len(raw_target) > 4096 or
                any(ord(char) <= 32 or ord(char) == 127 or char == "\\"
                    for char in raw_target)):
            raise PolicyError("invalid_target")

        if connect:
            match = _CONNECT.fullmatch(raw_target)
            if match is None:
                raise PolicyError("invalid_connect")
            host = self._hostname(match.group(1))
            port = int(match.group(2))
            scheme = "https"
        else:
            try:
                parsed = urlsplit(raw_target)
                scheme = parsed.scheme.lower()
                if (scheme not in {"http", "https"} or not parsed.netloc or
                        parsed.fragment or parsed.username is not None or
                        parsed.password is not None or "@" in parsed.netloc):
                    raise PolicyError("invalid_target")
                host = self._hostname(parsed.hostname)
                port = (parsed.port if parsed.port is not None else
                        (443 if scheme == "https" else 80))
            except ValueError as error:
                raise PolicyError("invalid_target") from error

        if not 1 <= port <= 65535 or not self.port_allowed(scheme, port):
            raise PolicyError("port_denied")

        try:
            answers = resolver(host, port)
        except Exception as error:
            raise PolicyError("dns_failed") from error
        if not isinstance(answers, (list, tuple)) or not answers:
            raise PolicyError("dns_failed")
        if not all(isinstance(answer, str) and self.address_allowed(answer)
                   for answer in answers):
            raise PolicyError("address_denied")
        return Target(host, port, scheme), answers[0]

    @staticmethod
    def verify_peer(chosen_ip: str, actual_ip: str) -> None:
        try:
            if ipaddress.ip_address(chosen_ip) == ipaddress.ip_address(actual_ip):
                return
        except ValueError:
            pass
        raise PolicyError("peer_mismatch")
