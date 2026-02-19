"""
Quick standalone test to diagnose Dhan DH-901 auth errors.
Run: python test_dhan_auth.py
"""
import os
from dotenv import load_dotenv
from dhanhq import dhanhq

load_dotenv()

client_id = os.getenv("DHAN_CLIENT_ID", "")
access_token = os.getenv("DHAN_ACCESS_TOKEN", "")

print(f"Client ID: {client_id}")
print(f"Token length: {len(access_token)}")
print(f"Token preview: {access_token[:10]}...{access_token[-6:]}")
print()

dhan = dhanhq(client_id, access_token)

# Test 1: LTP (uses custom headers WITH client-id) — this works for you
print("=" * 50)
print("TEST 1: LTP (ticker_data) — uses custom headers")
resp = dhan.ticker_data(securities={"NSE_FNO": [45585]})
print(f"  Status: {resp.get('status')}")
if resp.get("status") == "failure":
    print(f"  Error: {resp.get('remarks')}")
else:
    print(f"  Response: {resp}")
print()

# Test 2: Get fund limits (uses self.header WITHOUT client-id)
print("=" * 50)
print("TEST 2: Fund limits — uses self.header (NO client-id)")
print(f"  self.header = {dhan.header}")
resp = dhan.get_fund_limits()
print(f"  Status: {resp.get('status') if isinstance(resp, dict) else 'ok'}")
if isinstance(resp, dict) and resp.get("status") == "failure":
    print(f"  Error: {resp.get('remarks')}")
else:
    print(f"  Response keys: {list(resp.keys()) if isinstance(resp, dict) else 'N/A'}")
print()

# Test 3: Same call but WITH client-id injected
print("=" * 50)
print("TEST 3: Fund limits — with client-id INJECTED into header")
dhan.header['client-id'] = client_id
print(f"  self.header = {dhan.header}")
resp = dhan.get_fund_limits()
print(f"  Status: {resp.get('status') if isinstance(resp, dict) else 'ok'}")
if isinstance(resp, dict) and resp.get("status") == "failure":
    print(f"  Error: {resp.get('remarks')}")
else:
    print(f"  Response keys: {list(resp.keys()) if isinstance(resp, dict) else 'N/A'}")
print()

# Test 4: Order placement (will fail with margin error if auth works)
print("=" * 50)
print("TEST 4: Place order — expect margin rejection if auth is OK")
resp = dhan.place_order(
    security_id="45585",
    exchange_segment="NSE_FNO",
    transaction_type="BUY",
    quantity=1,
    order_type="LIMIT",
    product_type="MARGIN",
    price=1.0,
    trigger_price=0,
)
print(f"  Status: {resp.get('status')}")
if isinstance(resp, dict) and resp.get("status") == "failure":
    remarks = resp.get("remarks", {})
    if isinstance(remarks, dict):
        print(f"  Error code: {remarks.get('error_code')}")
        print(f"  Error msg: {remarks.get('error_message')}")
    else:
        print(f"  Remarks: {remarks}")
else:
    print(f"  Response: {resp}")

print()
print("=" * 50)
print("DONE. If Test 1 passes but Test 4 gives DH-901,")
print("your token likely lacks Trading permissions.")
print("Regenerate it at https://api.dhan.co with Trading enabled.")
