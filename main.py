# auto_wurk_retweet.py
# Flow:
# • Poll /api/jobs/open per detik (newest), pilih job BELUM REPOST paling baru
# • Ambil tweet_id dari URL, auto-retweet via GraphQL (headers dari headersx.py)
# • Lalu POST /verify-retweet sekali → GET /verify-status sekali
# • Ulang terus. Support notif Telegram (opsional, via .env)
import os, re, time, json
from datetime import datetime
import requests

# (opsional) .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ====== CONFIG WURK ======
BASE = "https://wurk.fun"
API_OPEN = "/api/jobs/open?sort=newest&limit=24&offset=0"
API_JOB = lambda sid: f"/api/jobs/{sid}"
API_VERIFY_STATUS = lambda sid: f"/api/jobs/{sid}/verify-status"
API_VERIFY_RETWEET = lambda sid: f"/api/jobs/{sid}/verify-retweet"
POLL_MS = int(os.environ.get("POLL_MS", "1000"))
COOKIE_FILE = "cookies_wurk.json"

# ====== TELEGRAM (opsional) ======
TG_ENABLED = (os.environ.get("TG_ENABLED", "false").lower() == "true")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID")   or os.environ.get("TELEGRAM_CHAT_ID", "")

def LOG(*a): print(datetime.utcnow().isoformat() + "Z", *a, flush=True)

def tgNotify(text: str):
    if not (TG_ENABLED and TG_BOT_TOKEN and TG_CHAT_ID): return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=4,
        )
    except Exception: pass

# ====== COOKIES WURK ======
def cookieFromFile(file=COOKIE_FILE):
    try:
        arr = json.loads(open(file, "r", encoding="utf-8").read())
        mp = {c.get("name"): c.get("value") for c in arr if isinstance(c, dict)}
        xsrf = mp.get("XSRF-TOKEN"); sid = mp.get("wurk.sid")
        if not xsrf or not sid: return None
        return {"cookie": f"XSRF-TOKEN={xsrf}; wurk.sid={sid}", "xsrf": xsrf}    except Exception:
        return None

def xsrfFromCookieStr(cookieStr: str):
    m = re.search(r'(?:^|;\s*)XSRF-TOKEN=([^;]+)', cookieStr, flags=re.I)
    return m.group(1) if m else None

def make_wurk_client() -> requests.Session:
    from_file = cookieFromFile()
    cookieStr = (from_file["cookie"] if from_file else (os.environ.get("WURK_COOKIE", "").strip()))
    if not cookieStr:
        LOG("❌ Butuh cookies. Isi .env WURK_COOKIE atau sediakan cookies_wurk.json")
        raise SystemExit(1)
    xsrf = from_file["xsrf"] if from_file else xsrfFromCookieStr(cookieStr)
    sess = requests.Session()
    sess.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Cookie": cookieStr,
    })
    if xsrf: sess.headers["X-XSRF-TOKEN"] = xsrf
    sess.base_url = BASE
    return sess

def get_open_jobs(ax: requests.Session):
    try:
        r = ax.get(ax.base_url + API_OPEN, timeout=20)
        if r.status_code in (401,403):
            LOG(f"❌ Unauthorized ({r.status_code}). Cookie kadaluarsa/salah.")
            return []
        if r.status_code >= 400:
            LOG(f"⚠ open jobs error: {r.status_code}")
            return []
        data = r.json()
        arr = data.get("jobs", data)
        return arr if isinstance(arr, list) else []
    except Exception as e:
        LOG("open jobs exception:", str(e)); return []

def get_job_detail(ax: requests.Session, sid: str):
    try:
        r = ax.get(ax.base_url + API_JOB(sid), timeout=20)
        return r.json() if 200 <= r.status_code < 300 else None
    except Exception:
        return None

def verify_status(ax: requests.Session, sid: str):
    try:
        r = ax.get(ax.base_url + API_VERIFY_STATUS(sid), timeout=20)
        return r.json() if r.ok else None
    except Exception:
        return None

def verify_retweet(ax: requests.Session, sid: str):
    try:
        r = ax.post(ax.base_url + API_VERIFY_RETWEET(sid), json={}, timeout=20)
        return r.json() if r.ok else None
    except Exception:
        return None

def extract_tweet_url(j: dict, detail: dict):
    return (
        (detail or {}).get("work_url")
        or j.get("tweet_url")
        or (j.get("tweet_snapshot") or {}).get("url")
        or j.get("work_url")
        or None
    )

def parse_reward(j: dict, detail: dict):
    raw = (
        (detail or {}).get("reward_per_retweet_sol")
        or (detail or {}).get("reward_per_retweet")
        or (detail or {}).get("reward")
        or ((detail or {}).get("data") or {}).get("reward_per_retweet_sol")
        or j.get("reward_per_retweet_sol")
        or "0"
    )
    try: return float(re.sub(r"[^0-9.]", "", str(raw)) or "0")
    except: return 0.0

def get_listed_by(detail: dict, j: dict):
    cands = [
        "listed_by","listedBy","poster","posted_by","creator","owner",
        ("user","username"),("user","name"),("account","username"),
        "creator_username","creator_handle",
        # list fallbacks
        ("listed_by",),("listedBy",),("poster",),("posted_by",),
        ("creator",),("owner",),("user","username"),("user","name"),
        ("account","username"),("creator_username",),("creator_handle",)
    ]
    for k in cands:
        if isinstance(k, tuple):
            v = detail or {}
            ok = True
            for kk in k:
                if isinstance(v, dict) and kk in v: v = v[kk]
                else: ok=False; break
            if ok and v: return v
            v = j or {}
            ok=True
            for kk in k:
                if isinstance(v, dict) and kk in v: v = v[kk]
                else: ok=False; break
            if ok and v: return v
        else:
            if detail and detail.get(k): return detail[k]
            if j and j.get(k): return j[k]
    return None

def build_candidates(ax, jobs, done_set):
    out=[]
    for j in jobs:
        sid = j.get("short_id") or j.get("shortId") or j.get("id")
        if not sid or sid in done_set: continue
        detail = get_job_detail(ax, sid) or {}
        hasReposted = bool(detail.get("_user_has_reposted") or detail.get("user_has_reposted") or j.get("_user_has_reposted"))
        if hasReposted:
            done_set.add(sid); continue
        url = extract_tweet_url(j, detail)
        if not url: continue
        reward = parse_reward(j, detail)
        name = (
            ((j.get("tweet_snapshot") or {}).get("id")) or j.get("title") or
            ((j.get("tweet_snapshot") or {}).get("tweet_id")) or j.get("name") or
            j.get("description") or "(no-title)"
        )
        listedBy = get_listed_by(detail, j)
        out.append({"sid":sid,"name":name,"reward":reward,"url":url,"listedBy":listedBy})
    return out

# ====== X GRAPHQL RETWEET ======
# butuh headersx.py (file terpisah) berisi HEADERS_<ALIAS> = { 'authorization': 'Bearer ...', 'x-csrf-token': '...', 'cookie': 'ct0=...; auth_token=...; ...', ... }
CREATE_QID = "ojPdsZsimiJrUGLR1sjUtA"

import base64, random, string

def generate_transaction_id():
    """Generate random transaction ID untuk setiap request"""
    random_bytes = ''.join(random.choices(string.ascii_letters + string.digits, k=64))
    return base64.b64encode(random_bytes.encode()).decode()[:88]

def load_headers_accounts():
    try:
        import importlib
        m = importlib.import_module("headersx")
    except Exception:
        LOG("❌ headersx.py tidak ditemukan / error import.")
        return {}
    accs={}
    for attr in dir(m):
        if attr.startswith("HEADERS_"):
            key = attr[len("HEADERS_"):].lower()
            accs[key]=getattr(m, attr)
    return accs

def post_json(url, headers, payload):
    # retry kecil jaringan
    for _ in range(2):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=30)            break
        except Exception as e:
            LOG(f"Network error: {e}")
            time.sleep(0.3)
            continue
    else:
        return "NET_FAIL"

    LOG(f"HTTP {r.status_code}: {r.text[:200]}")

    if r.status_code == 401:
        return "EXPIRED 401"
    try: return r.json()
    except: return r.text

def retweet_once(headers, tweet_id: str):
    url = f"https://x.com/i/api/graphql/{CREATE_QID}/CreateRetweet"
    payload = {"variables":{"tweet_id":str(tweet_id),"dark_request":False},"queryId":CREATE_QID}

    # Generate transaction ID baru setiap request
    headers = headers.copy()  # Copy untuk tidak mengubah original
    headers["x-client-transaction-id"] = generate_transaction_id()

    LOG(f"Debug: URL={url}")
    LOG(f"Debug: CSRF header={headers.get('x-csrf-token', 'NONE')[:20]}...")
    LOG(f"Debug: New transaction ID={headers['x-client-transaction-id'][:30]}...")

    res = post_json(url, headers, payload)
    if isinstance(res, str):
        return res
    if isinstance(res, dict) and res.get("errors"):
        err = res["errors"][0]
        code = str(err.get("code") or err.get("extensions",{}).get("code") or "")
        msg  = (err.get("message") or "").lower()
        LOG(f"Twitter API error: code={code}, msg={msg}")
        if code in ("465","144","327") or "outdated" in msg or "no status found" in msg or "already retweeted" in msg:
            return f"SKIP {code or 'ERR'}"
    data = (res.get("data",{}) if isinstance(res,dict) else {})
    rid  = (((data.get("create_retweet") or {}).get("retweet_results") or {}).get("result") or {}).get("rest_id")
    return f"OK rest_id={rid}" if rid else ("OK" if isinstance(res,dict) else "UNKNOWN")

def get_tweet_id(url: str):
    m = re.search(r"/status/(\d{10,25})", url)
    return m.group(1) if m else None

# ====== MAIN LOOP ======
def main():
    LOG(f"Start… Auto-retweet + verify (no Puppeteer). POLL={POLL_MS}ms")
    ax = make_wurk_client()
    accounts = load_headers_accounts()
    if not accounts:
        LOG("❌ Tidak ada HEADERS_* di headersx.py. Keluar.")
        raise SystemExit(1)

    done=set()
    lastShown=None
    lastHeartbeat=time.time()

    while True:
        try:
            jobs = get_open_jobs(ax)
            cand = build_candidates(ax, jobs, done)
            newest = cand[0] if cand else None

            if newest and newest["sid"] != lastShown:
                lastShown = newest["sid"]

                print("-"*60, flush=True)
                LOG("🆕 ACTIVE JOB (LATEST)")
                LOG(f"📋 ID      : {newest['sid']}")
                LOG(f"📝 Name    : {newest['name']}")
                if newest.get("listedBy"): LOG(f"🙋 Listed by : {newest['listedBy']}")
                LOG(f"💰 Reward  : {newest['reward']} SOL")
                LOG(f"🔗 URL     : {newest['url']}")
                tgNotify("\n".join([
                    "🆕 Job baru terdeteksi",
                    f"ID: {newest['sid']}",
                    f"Reward: {newest['reward']} SOL",
                    f"URL: {newest['url']}",
                ]))

                # === AUTO RETWEET ===
                tid = get_tweet_id(newest["url"])
                if not tid:
                    LOG("❌ Gagal ambil tweet_id dari URL. Skip.")
                    done.add(newest["sid"])
                else:
                    # pakai account pertama (atau loop semua kalau mau multi-akun)
                    alias, headers = next(iter(accounts.items()))
                    LOG(f"🔁 Retweet via @{alias} → tweet_id={tid}")

                    # Delay untuk avoid rate limit
                    time.sleep(2)

                    out = retweet_once(headers, tid)
                    LOG(f"📨 Retweet result:")
                    LOG(f"   {out}")

                    # Jika 404, coba reload headers
                    if "NET_FAIL" in str(out) or out == "":
                        LOG("⚠️ Retweet gagal, coba reload accounts...")
                        accounts = load_headers_accounts()
                        if accounts:
                            alias, headers = next(iter(accounts.items()))
                            time.sleep(3)
                            out2 = retweet_once(headers, tid)
                            LOG(f"📨 Retry result: {out2}")
                            if out2 and "OK" in str(out2):
                                out = out2

                    # lanjut verify di WURK (sekali)
                    LOG("➡️ Verify-retweet (sekali)…")
                    vr = verify_retweet(ax, newest["sid"])
                    retweetOk = bool((vr or {}).get("ok") or (vr or {}).get("verified") or (vr or {}).get("success") or (vr or {}).get("retweet_verified"))
                    LOG(f"🔁 Verify-retweet {newest['sid']}: {'✅ OK' if retweetOk else '⚠ Tidak pasti/ditolak'}")

                    LOG("🔎 Verify-status (sekali)…")
                    vs = verify_status(ax, newest["sid"])
                    verified = bool(
                        (vs or {}).get("verified") or (vs or {}).get("is_verified") or
                        (vs or {}).get("user_has_reposted") or (vs or {}).get("_user_has_reposted") or
                        (vs or {}).get("ok") or (vs or {}).get("retweet_verified")
                    )
                    LOG(f"📊 Verify-status {newest['sid']}: {'✅ TRUE' if verified else '❌ FALSE'}")
                    if verified and newest["reward"] > 0:
                        LOG(f"💎 Reward +{newest['reward']} SOL")

                    done.add(newest["sid"])

                    # jika habis, umumkan idle sekarang
                    jobs2 = get_open_jobs(ax)
                    if not build_candidates(ax, jobs2, done):
                        print("-"*60, flush=True)
                        LOG("✅ Semua job selesai. Menunggu job baru…")

            # heartbeat ~30s
            if time.time() - lastHeartbeat > 30:
                lastHeartbeat = time.time()
                LOG(f"⚡ Monitoring… Done: {len(done)}")

        except Exception as e:
            LOG("Loop error:", str(e))

        time.sleep(POLL_MS/1000)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        LOG("Exit by user.")
