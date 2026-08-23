"""The web app shell: `lol.py serve` (local dashboard) and `lol.py export`
(static site for GitHub Pages).

Both expose the same API shape — serve computes JSON per request, export
pre-bakes the identical paths as files — so web/index.html runs unchanged in
either mode. New analysis domains plug in by adding their endpoints here in
both places.
"""

import json
import os
import re
import sys

import scaling
from common import WEB_DIR, db_connect


def cmd_export(args):
    """Write the web app + pre-generated API JSON as a static site (for GitHub Pages)."""
    import shutil
    con = db_connect()
    meta = scaling.api_meta(con)
    if not meta["tiers"]:
        sys.exit("Database is empty — run `lol.py scaling sync` or `lol.py import-json` first.")
    out = args.out
    os.makedirs(os.path.join(out, "api"), exist_ok=True)
    shutil.copy(os.path.join(WEB_DIR, "index.html"), os.path.join(out, "index.html"))

    def dump(rel, obj):
        path = os.path.join(out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(obj, f, separators=(",", ":"))

    dump("api/meta.json", meta)
    files = 1
    for tier in meta["tiers"]:
        patches = scaling.db_patches(con, tier)
        dump(f"api/rows/{tier}.json", scaling.build_rows(con, tier, patches))
        names = [r[0] for r in con.execute(
            "SELECT DISTINCT champion FROM stats WHERE tier=? ORDER BY champion", (tier,))]
        dump(f"api/champions/{tier}.json", names)
        for name in names:
            dump(f"api/champion/{tier}/{name}.json", scaling.api_champion(con, tier, name))
        files += 2 + len(names)
    print(f"Exported static site to {out}/ ({files} API files, tiers: {', '.join(meta['tiers'])})")


def cmd_serve(args):
    import threading
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                con = db_connect()
                if u.path == "/api/meta.json":
                    self._json(scaling.api_meta(con))
                elif m := re.fullmatch(r"/api/rows/([a-z0-9_]+)\.json", u.path):
                    tier = m.group(1)
                    patches = scaling.db_patches(con, tier)
                    if not patches:
                        self._json({"error": f"no data for tier {tier}"}, 404)
                        return
                    self._json(scaling.build_rows(con, tier, patches))
                elif m := re.fullmatch(r"/api/champions/([a-z0-9_]+)\.json", u.path):
                    names = [r[0] for r in con.execute(
                        "SELECT DISTINCT champion FROM stats WHERE tier=? ORDER BY champion",
                        (m.group(1),))]
                    self._json(names)
                elif m := re.fullmatch(r"/api/champion/([a-z0-9_]+)/([a-z0-9]+)\.json", u.path):
                    self._json(scaling.api_champion(con, m.group(1), m.group(2)))
                else:
                    self._json({"error": "not found"}, 404)
            except BrokenPipeError:
                pass
            except Exception as e:
                self._json({"error": str(e)}, 500)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Serving dashboard at {url}  (Ctrl-C to stop)")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
