import os

SERVICE_NAME = os.getenv('SERVICE_NAME', 'SmartFetch')
SERVICE_VERSION = '1.10.6'
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', '8787'))

# Public API safety / resource controls
MAX_REQUEST_BODY_BYTES = int(os.getenv('MAX_REQUEST_BODY_BYTES', '20000'))
DEFAULT_MAX_OUTPUT_CHARS = int(os.getenv('DEFAULT_MAX_OUTPUT_CHARS', '20000'))
MAX_OUTPUT_CHARS = int(os.getenv('MAX_OUTPUT_CHARS', '50000'))
MAX_LINKS = int(os.getenv('MAX_LINKS', '50'))
TOTAL_REQUEST_TIMEOUT_SECONDS = float(os.getenv('TOTAL_REQUEST_TIMEOUT_SECONDS', '25'))
MAX_CONCURRENT_FETCHES = int(os.getenv('MAX_CONCURRENT_FETCHES', '4'))
MAX_CONCURRENT_BROWSERS = int(os.getenv('MAX_CONCURRENT_BROWSERS', '1'))

# Simple unauthenticated pre-payment protection. This is deliberately conservative
# until x402 sits in front of /fetch.
RATE_LIMIT_PER_MINUTE = int(os.getenv('RATE_LIMIT_PER_MINUTE', '30'))
RATE_LIMIT_BURST = int(os.getenv('RATE_LIMIT_BURST', '10'))
