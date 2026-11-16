from dataclasses import dataclass
import ipaddress
import os
import socket
from urllib.parse import urlparse

ALLOW_PRIVATE = os.getenv('ALLOW_PRIVATE_NETWORK') == '1'
ALLOWED_PORTS = {80, 443}
BLOCKED_HOST_SUFFIXES = (
    '.internal', '.local', '.localhost', '.home', '.lan', '.corp', '.private',
)
BLOCKED_HOSTS = {
    'localhost',
    'metadata.google.internal',
    'metadata',
    'instance-data',
    '169.254.169.254',
}


def is_private_allowed() -> bool:
    return ALLOW_PRIVATE or os.getenv('ALLOW_PRIVATE_NETWORK') == '1'


class DNSResolutionError(ValueError):
    """Public-URL validation failed because hostname resolution failed."""


@dataclass(frozen=True, slots=True)
class PinnedTarget:
    url: str
    hostname: str
    port: int
    scheme: str
    addresses: tuple[str, ...]


def is_ip_allowed(ip_text: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    if getattr(ip, 'ipv4_mapped', None) is not None:
        return False
    return ip.is_global


def _check_ip(ip_text: str):
    if is_private_allowed():
        return
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError as exc:
        raise ValueError(f'Invalid IP address: {ip_text}') from exc

    if getattr(ip, 'ipv4_mapped', None) is not None:
        raise ValueError('Private/local/network-metadata targets are blocked')

    if not ip.is_global:
        raise ValueError('Private/local/network-metadata targets are blocked')


def validate_public_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError('Invalid URL')
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise ValueError('Invalid URL') from exc

    if parsed.scheme not in {'http', 'https'}:
        raise ValueError('Only http:// and https:// URLs are allowed')
    if not parsed.hostname:
        raise ValueError('URL must include a hostname')
    if parsed.username or parsed.password:
        raise ValueError('URLs containing credentials are not allowed')

    port = parsed.port
    if port is not None and port not in ALLOWED_PORTS and not is_private_allowed():
        raise ValueError('Only standard web ports 80 and 443 are allowed')

    if is_private_allowed():
        return url

    host = parsed.hostname.lower().rstrip('.')
    if host in BLOCKED_HOSTS or any(host.endswith(s) for s in BLOCKED_HOST_SUFFIXES):
        raise ValueError('Private/local/network-metadata targets are blocked')

    try:
        _check_ip(host)
        return url
    except ValueError as exc:
        # If host parsed as an IP and was blocked, propagate the block.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise exc

    try:
        infos = socket.getaddrinfo(
            host,
            port or (443 if parsed.scheme == 'https' else 80),
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise DNSResolutionError(
            f'Could not resolve hostname: {host}'
        ) from exc

    addresses = {item[4][0] for item in infos}
    if not addresses:
        raise DNSResolutionError(f'Could not resolve hostname: {host}')
    for addr in addresses:
        _check_ip(addr)

    return url


def resolve_and_pin_public_url(url: str) -> PinnedTarget:
    validated_url = validate_public_url(url)
    parsed = urlparse(validated_url)
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower().rstrip('.')
    port = parsed.port or (443 if scheme == 'https' else 80)

    if is_private_allowed():
        return PinnedTarget(
            url=validated_url,
            hostname=hostname,
            port=port,
            scheme=scheme,
            addresses=(hostname,),
        )

    try:
        infos = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise DNSResolutionError(f'Could not resolve hostname: {hostname}') from exc

    addresses: list[str] = []
    for item in infos:
        addr = ipaddress.ip_address(item[4][0]).compressed
        _check_ip(addr)
        if addr not in addresses:
            addresses.append(addr)

    if not addresses:
        raise DNSResolutionError(f'Could not resolve hostname: {hostname}')

    return PinnedTarget(
        url=validated_url,
        hostname=hostname,
        port=port,
        scheme=scheme,
        addresses=tuple(addresses),
    )


from requests.adapters import HTTPAdapter


class SSRFProtectionAdapter(HTTPAdapter):
    """HTTPAdapter that verifies destination IPs to enforce SSRF and DNS rebinding policy."""

    def send(self, request, **kwargs):
        if not is_private_allowed():
            parsed = urlparse(request.url)
            host = parsed.hostname
            if host:
                try:
                    _check_ip(host)
                except ValueError as exc:
                    if 'Private' in str(exc) or 'network-metadata' in str(exc):
                        raise
                    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
                    try:
                        infos = socket.getaddrinfo(host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
                        for info in infos:
                            _check_ip(info[4][0])
                    except socket.gaierror:
                        pass
        return super().send(request, **kwargs)


def install_dns_rebinding_protection(session=None):
    """Mounts SSRF protection adapter on the given requests.Session (or no-op)."""
    if session is not None:
        adapter = SSRFProtectionAdapter()
        session.mount('http://', adapter)
        session.mount('https://', adapter)
