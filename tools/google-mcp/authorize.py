#!/usr/bin/env python3
"""Authorise one Google account for Walter's read-only Google MCP server (the owner signs in himself).

Usage: ~/.walter/google-mcp/venv/bin/python ~/walter/tools/google-mcp/authorize.py EMAIL [--no-browser] [--minutes N]
       authorize.py --list            (which accounts already have a token)

Starts the MCP server (tools/google-mcp/run.sh), asks it for EMAIL's Google consent URL and
opens that page in the wall browser (skills/walter-browse show). The owner signs in and approves on
the wall himself: passwords and codes via `walter-browse typing-page`, never spoken. Google
redirects to http://localhost:8771/oauth2callback on this machine, so the consent page must
be opened HERE (the wall browser), not on a phone. This process holds the flow open until a
token appears (default 10 min), then checks it with one read-only Gmail search.
Tokens: ~/.walter/google-mcp/credentials/ (mode 700). Nothing secret is printed.
"""
import asyncio, glob, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, "run.sh")
CREDS = os.environ.get("WORKSPACE_MCP_CREDENTIALS_DIR", os.path.expanduser("~/.walter/google-mcp/credentials"))
CLIENT = os.environ.get("GOOGLE_MCP_CLIENT_FILE", os.path.expanduser("~/.walter/secrets/google-oauth-client.json"))
BROWSE = os.path.expanduser("~/.walter/home/skills/walter-browse")
WALTERCTL = os.path.expanduser("~/walter/bin/walterctl")
URL_RE = re.compile(r"https://accounts\.google\.com/o/oauth2/auth\?[^\s)\"'>]+")


def token_files():
    return {p: os.path.getmtime(p) for p in glob.glob(os.path.join(CREDS, "*.json"))}


def text_of(res) -> str:
    return "\n".join(getattr(c, "text", "") for c in res.content)


async def probe(session, email):
    """(authorised?, consent_url or None). One read-only call."""
    try:
        res = await session.call_tool("search_gmail_messages",
                                      {"query": "in:inbox", "page_size": 1, "user_google_email": email})
        txt = text_of(res)
    except Exception as e:  # some versions raise instead of returning an error result
        txt, res = str(e), None
    m = URL_RE.search(txt)
    if m:
        return False, m.group(0)
    if res is not None and not getattr(res, "isError", False):
        return True, None
    return False, None if "ACTION REQUIRED" not in txt else ""


async def main(email, browser, minutes):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    if not os.path.exists(CLIENT):
        sys.exit("No Google OAuth client yet. The owner drops it first: python3 %s/oauth-drop.py" % HERE)
    params = StdioServerParameters(command=RUN, env=dict(os.environ))
    errlog = open(os.path.expanduser("~/.walter/google-mcp/logs/authorize-stderr.log"), "a")
    async with stdio_client(params, errlog=errlog) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            ok, url = await probe(s, email)
            if ok:
                print("ALREADY AUTHORISED: %s works (read-only)." % email); return 0
            if not url:
                print("FAILED: the server didn't give a consent link (see ~/.walter/google-mcp/logs/)."); return 1
            before = token_files()
            if browser:
                subprocess.run([BROWSE, "show", url], capture_output=True)
                print("Consent page for %s is open in the wall browser. The owner: sign in as that account and "
                      "tap Allow (if Google says the app isn't verified: Advanced -> Go to ... (unsafe))." % email)
            else:
                print("Open this on THIS machine's browser:\n" + url)
            deadline = time.time() + minutes * 60
            while time.time() < deadline:
                await asyncio.sleep(3)
                now = token_files()
                if any(p not in before or now[p] > before[p] for p in now):
                    await asyncio.sleep(1)
                    ok, _ = await probe(s, email)
                    if ok:
                        print("AUTHORISED: %s (read-only Gmail, Calendar, Drive)." % email)
                        return 0
                    print("A token arrived but %s still isn't working: did the owner sign in as a different "
                          "account? Run me again." % email); return 1
            print("TIMED OUT after %d min waiting for %s; nothing saved. Run me again when the owner's ready." % (minutes, email))
            return 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        sys.exit(__doc__)
    if a[0] == "--list":
        names = sorted(os.path.basename(p)[:-5].replace("%40", "@") for p in token_files())
        print("\n".join(names) if names else "No accounts authorised yet."); sys.exit(0)
    mins = int(a[a.index("--minutes") + 1]) if "--minutes" in a else 10
    sys.exit(asyncio.run(main(a[0], "--no-browser" not in a, mins)))
