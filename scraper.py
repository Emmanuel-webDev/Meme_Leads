#!/usr/bin/env python3
"""Lead scraper for new token projects that have socials but no website.

Source: DexScreener's free public API (token profiles + pair data). A token shows up
there once its team fills in a profile, so every lead already has at least one
social link to reach them on. GMGN, Axiom and Fomo have no public API and block
scrapers, so they are not used.

Every INTERVAL seconds (default 600) it adds new leads to leads.json and serves the dashboard.
Standard library only.   Run:  python3 scraper.py   (http://localhost:8001)
"""
import email.utils, http.server, json, os, sys, threading, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", HERE)  # point at a persistent disk when deployed
os.makedirs(DATA_DIR, exist_ok=True)
OUT = os.path.join(DATA_DIR, "leads.json")
INTERVAL = int(os.environ.get("INTERVAL", "600"))  # seconds between scans (10 minutes)
PASSWORD = os.environ.get("DASH_PASSWORD", "")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "14"))
MIN_LIQ = float(os.environ.get("MIN_LIQ_USD", "0"))
MIN_MCAP = float(os.environ.get("MIN_MCAP_USD", "15000"))
MAX_MCAP = float(os.environ.get("MAX_MCAP_USD", "250000"))
BOND_MCAP = float(os.environ.get("BOND_MCAP_USD", "69000"))  # pump.fun graduates near 85 SOL; the USD value moves with SOL price
SOON_PCT = int(os.environ.get("SOON_PCT", "60"))  # "soon to bond" = this % of the way to BOND_MCAP
REFRESH_DAYS = int(os.environ.get("REFRESH_DAYS", "3"))  # keep stats fresh on leads this young
CHAINS = {c.strip() for c in os.environ.get("CHAINS", "").split(",") if c.strip()}  # empty = all
API = "https://api.dexscreener.com"
SOCIAL_HOSTS = {"telegram": ("t.me", "telegram.me"), "twitter": ("x.com", "twitter.com"), "discord": ("discord.gg", "discord.com")}

def get(url, retries=3, backoff=2.0):
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": "coinleads/1.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                ra = e.headers.get("Retry-After")
                wait = float(ra) if (ra and ra.isdigit()) else (backoff * (2 ** attempt))
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
                continue
            raise

def classify(links):
    out = {"website": "", "telegram": "", "twitter": "", "discord": ""}
    for l in links or []:
        u = (l.get("url") or "").strip()
        if not u.startswith("http"): continue
        host = urllib.parse.urlparse(u).netloc.lower().removeprefix("www.").removeprefix("mobile.")
        kind = next((k for k, hs in SOCIAL_HOSTS.items() if host in hs), None)
        if kind: out[kind] = out[kind] or u
        elif (l.get("type") or "").lower() == "website" or (l.get("label") or "").lower() == "website":
            out["website"] = out["website"] or u
    return out

def best_pairs(tokens):
    """tokens: list of (chainId, address). Returns {(chain, addr_lower): best pair by liquidity}."""
    best = {}
    uniq = list(dict.fromkeys(tokens))
    for i in range(0, len(uniq), 30):
        addrs = ",".join(a for _, a in uniq[i:i + 30])
        try: pairs = get(f"{API}/latest/dex/tokens/{addrs}").get("pairs") or []
        except Exception: continue
        for p in pairs:
            k = (p.get("chainId"), (p.get("baseToken") or {}).get("address", "").lower())
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if k not in best or liq > ((best[k].get("liquidity") or {}).get("usd") or 0): best[k] = p
        time.sleep(0.3)
    return best

def load():
    try:
        with open(OUT, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return []

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")

def stats(pr):
    """Liquidity, market cap and launch stage from the best DexScreener pair."""
    liq = (pr.get("liquidity") or {}).get("usd") or 0
    mcap = pr.get("marketCap") or pr.get("fdv") or 0
    dex = pr.get("dexId", "")
    stage, pct = ("bonding", min(99, round(mcap / BOND_MCAP * 100))) if dex == "pumpfun" else ("bonded" if dex == "pumpswap" else "trading", None)
    return {"liquidity": liq, "mcap": mcap, "stage": stage, "bond_pct": pct, "dex": dex,
            "soon": stage == "bonding" and (pct or 0) >= SOON_PCT, "updated_at": now()}

def fetch_profiles():
    seen = {}
    for path in ("/token-profiles/latest/v1", "/token-profiles/recent-updates/v1", "/token-boosts/latest/v1"):
        try:
            data = get(API + path)
            for p in data if isinstance(data, list) else []:
                if isinstance(p, dict) and "chainId" in p and "tokenAddress" in p:
                    seen.setdefault((p["chainId"], p["tokenAddress"]), p)
        except Exception:
            pass
        time.sleep(0.5)
    return list(seen.values())

def cycle():
    profiles = fetch_profiles()
    leads = load(); idx = {l["id"]: l for l in leads}; cand = []
    for p in profiles:
        if CHAINS and p.get("chainId") not in CHAINS: continue
        c = classify(p.get("links")); lid = f'{p["chainId"]}:{p["tokenAddress"]}'
        if lid in idx:
            if c["website"] and not idx[lid]["website"]: idx[lid]["website"] = c["website"]  # they built one: hide it
            continue
        if not (c["telegram"] or c["twitter"]) or c["website"]: continue
        cand.append((p, c, lid))
    fresh = (datetime.now(timezone.utc) - timedelta(days=REFRESH_DAYS)).isoformat(timespec="seconds")
    refresh = [l for l in leads if l["found_at"] >= fresh and not l["website"]]
    pairs = best_pairs([(p["chainId"], p["tokenAddress"]) for p, _, _ in cand] + [(l["chain"], l["address"]) for l in refresh])
    for l in refresh:  # update liquidity, market cap and stage on leads we already have
        pr = pairs.get((l["chain"], l["address"].lower()))
        if not pr: continue
        l.update(stats(pr))
        sites = (pr.get("info") or {}).get("websites") or []
        if sites: l["website"] = sites[0].get("url") or "listed"
    added = 0
    for p, c, lid in cand:
        pr = pairs.get((p["chainId"], p["tokenAddress"].lower()))
        if not pr: continue  # no market data, cannot apply the market cap window
        info = pr.get("info") or {}
        if info.get("websites"): continue  # DexScreener lists a site on the pair
        st = stats(pr)
        if not (MIN_MCAP <= st["mcap"] <= MAX_MCAP) or st["liquidity"] < MIN_LIQ: continue
        for sc in info.get("socials") or []:
            k = sc.get("type")
            if k in ("twitter", "telegram", "discord") and not c[k]: c[k] = sc.get("url", "")
        base = pr.get("baseToken") or {}
        leads.append({"id": lid, "name": base.get("name") or p["tokenAddress"][:8], "symbol": base.get("symbol", ""),
            "chain": p["chainId"], "address": p["tokenAddress"], "dex_url": p.get("url", ""), "pair_created": pr.get("pairCreatedAt"),
            "website": "", "telegram": c["telegram"], "twitter": c["twitter"], "discord": c["discord"],
            "found_at": now(), "source": "DexScreener", **st})
        added += 1
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    leads = [l for l in leads if l["found_at"] >= cutoff]
    with open(OUT + ".tmp", "w", encoding="utf-8") as f: json.dump(leads, f, ensure_ascii=False, indent=2)
    os.replace(OUT + ".tmp", OUT)  # file mtime = scan time, shown in the dashboard
    soon = sum(1 for l in leads if l.get("soon") and not l["website"])
    print(f"[{time.strftime('%H:%M:%S')}] profiles={len(profiles)} candidates={len(cand)} added={added} refreshed={len(refresh)} soon_to_bond={soon} total={len(leads)}", flush=True)

def serve(port):
    import base64, hmac
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def authed(self):
            if not PASSWORD: return True
            h = self.headers.get("Authorization", "")
            try: pw = base64.b64decode(h.split(" ", 1)[1]).decode().split(":", 1)[1]
            except Exception: return False
            return hmac.compare_digest(pw.encode(), PASSWORD.encode())
        def send(self, code, body, ctype="text/plain", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items(): self.send_header(k, v)
            self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/healthz": return self.send(200, b"ok")
            if not self.authed():
                return self.send(401, b"Password required", extra={"WWW-Authenticate": 'Basic realm="Coin leads"'})
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f: return self.send(200, f.read(), "text/html; charset=utf-8")
            if path == "/leads.json":
                try:
                    with open(OUT, "rb") as f: return self.send(200, f.read(), "application/json", {"Last-Modified": email.utils.formatdate(os.path.getmtime(OUT), usegmt=True)})
                except FileNotFoundError: return self.send(200, b"[]", "application/json")
            self.send(404, b"Not found")
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Dashboard on port {port}" + (" (password protected)" if PASSWORD else " (NO PASSWORD SET)"), flush=True)

if __name__ == "__main__":
    serve(int(os.environ.get("PORT", "8001")))
    while True:
        try: cycle()
        except Exception as e: print(f"[{time.strftime('%H:%M:%S')}] cycle error: {e}", flush=True)
        time.sleep(INTERVAL)
