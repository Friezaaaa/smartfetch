import json
import requests
from eth_account import Account
from scripts.paid_fetch_mainnet_test import build_x402_client
from x402.http.utils import decode_payment_required_header, encode_payment_signature_header

url = "https://smartfetch-production-ea53.up.railway.app/fetch"
print("1. Sending unpaid request to:", url)
res1 = requests.post(url, json={"url": "https://example.com/"})
print("Unpaid status:", res1.status_code)
header = res1.headers.get("payment-required") or res1.headers.get("PAYMENT-REQUIRED")
print("Payment-Required header present:", bool(header))

challenge = decode_payment_required_header(header)
print("Challenge network:", challenge.accepts[0].network)
print("Challenge amount:", challenge.accepts[0].amount)
print("Challenge payTo:", challenge.accepts[0].pay_to)
print("Challenge asset:", challenge.accepts[0].asset)

account = Account.create()
print("Generated ephemeral client account:", account.address)
client = build_x402_client(account)
payload = client.create_payment_payload(challenge)
print("Signed payment payload created successfully!")
print("Payload network:", payload.accepted.network)
print("Payload scheme:", payload.accepted.scheme)

sig_header = encode_payment_signature_header(payload)
print("Sending signed payment payload to production...")
res2 = requests.post(url, json={"url": "https://example.com/"}, headers={"PAYMENT-SIGNATURE": sig_header})
print("Paid-flow test response status:", res2.status_code)
print("Paid-flow test response headers:", dict(res2.headers))
print("Paid-flow test response body:", res2.text[:300])
