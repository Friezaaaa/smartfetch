import ipaddress
import os
import socket
from urllib.parse import urlparse

ALLOW_PRIVATE = os.getenv('ALLOW_PRIVATE_NETWORK') == '1'
ALLOWED_PORTS = {80, 443}
BLOCKED_HOST_SUFFIXES = (
    '.internal', '.local', '.localhost', '.home', '.lan',
)
BLOCKED_HOSTS = {
    'localhost',
    'metadata.google.internal',
    'metadata',
}


class DNSResolutionError(ValueError):
    """Public-URL validation failed because hostname resolution failed."""


def _check_ip(ip_text: str):
    ip = ipaddress.ip_address(ip_text)
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
    if port is not None and port not in ALLOWED_PORTS and not ALLOW_PRIVATE:
        raise ValueError('Only standard web ports 80 and 443 are allowed')

    if ALLOW_PRIVATE:
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
