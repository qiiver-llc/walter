#!/usr/bin/env python3
"""One-time (idempotent) Cloudflare setup for the release channel.

  cf_pages_setup.py ACCOUNT_ID ZONE PROJECT HOSTNAME

Token comes from $CLOUDFLARE_API_TOKEN only (never printed or put on a command line).
Creates the Pages project (direct upload, production branch main) if missing, attaches
HOSTNAME as a Pages custom domain, and creates ONE proxied CNAME HOSTNAME ->
PROJECT.pages.dev if no record with that exact name exists. It never edits or deletes
any other DNS record (the apex, walter.*, hh-* are left alone).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"


def call(method: str, path: str, body: dict | None = None) -> dict:
    tok = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not tok:
        raise SystemExit("CLOUDFLARE_API_TOKEN not set")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"success": False, "errors": [{"message": f"HTTP {e.code}"}]}


def main(acct: str, zone_name: str, project: str, host: str) -> int:
    if not host.endswith("." + zone_name) or host.split(".")[0] in ("walter",) or host.startswith("hh-"):
        raise SystemExit(f"refusing to touch {host}")
    r = call("GET", f"/accounts/{acct}/pages/projects/{project}")
    if not r.get("success"):
        r = call("POST", f"/accounts/{acct}/pages/projects",
                 {"name": project, "production_branch": "main"})
        if not r.get("success"):
            raise SystemExit(f"couldn't create Pages project: {r.get('errors')}")
        print(f"created Pages project {project}")
    else:
        print(f"Pages project {project} exists")
    sub = r["result"].get("subdomain") or f"{project}.pages.dev"

    doms = call("GET", f"/accounts/{acct}/pages/projects/{project}/domains").get("result") or []
    if not any(d.get("name") == host for d in doms):
        r = call("POST", f"/accounts/{acct}/pages/projects/{project}/domains", {"name": host})
        if not r.get("success"):
            raise SystemExit(f"couldn't add custom domain: {r.get('errors')}")
        print(f"attached custom domain {host}")
    else:
        print(f"custom domain {host} already attached")

    z = call("GET", f"/zones?name={zone_name}")
    if not z.get("result"):
        raise SystemExit(f"zone {zone_name} not visible to this token")
    zid = z["result"][0]["id"]
    recs = call("GET", f"/zones/{zid}/dns_records?name={host}").get("result") or []
    if recs:
        print(f"DNS record for {host} exists ({recs[0]['type']} -> {recs[0]['content']}); left as is")
    else:
        r = call("POST", f"/zones/{zid}/dns_records", {"type": "CNAME", "name": host, "content": sub,
                 "proxied": True, "ttl": 1, "comment": "Walter release channel (Pages)"})
        if not r.get("success"):
            raise SystemExit(f"couldn't create CNAME: {r.get('errors')}")
        print(f"created CNAME {host} -> {sub}")
    st = [d for d in (call("GET", f"/accounts/{acct}/pages/projects/{project}/domains").get("result") or [])
          if d.get("name") == host]
    if st:
        print(f"domain status: {st[0].get('status')}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(*sys.argv[1:]))
