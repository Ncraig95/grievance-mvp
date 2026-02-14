from __future__ import annotations

import argparse
import hashlib
import hmac
import time
import requests

def sign(secret: str, ts: str, body: bytes) -> str:
    msg = ts.encode("utf-8") + b"." + body
    mac = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={mac}"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--json", required=True, help="Path to intake JSON file")
    args = ap.parse_args()

    body = open(args.json, "rb").read()
    ts = str(int(time.time()))
    sig = sign(args.secret, ts, body)

    r = requests.post(
        args.url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": ts,
            "X-Signature": sig,
        },
        timeout=30,
    )
    print(r.status_code, r.text)

if __name__ == "__main__":
    main()
