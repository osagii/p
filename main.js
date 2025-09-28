// main_termux.js — Termux-only helper (tanpa Puppeteer)
// Flow:
// • Poll /api/jobs/open per detik, pilih job BELUM REPOST paling baru → tampil 1 URL (auto copy)
// • Kamu repost manual, lalu TEKAN ENTER
// • Script: POST /verify-retweet → GET /verify-status (sekali), log hasil
// • Jika tak ada job lain, langsung log: "✅ Semua job selesai. Menunggu job baru…"

import fs from "fs";
import os from "os";
import { exec } from "child_process";
import readline from "readline";
import axios from "axios";
import * as dotenv from "dotenv";

dotenv.config();

// ====== CONFIG ======
const BASE = "https://wurk.fun";
const API_OPEN = "/api/jobs/open?sort=newest&limit=24&offset=0";
const API_JOB = (sid) => `/api/jobs/${sid}`;
const API_VERIFY_STATUS = (sid) => `/api/jobs/${sid}/verify-status`;
const API_VERIFY_RETWEET = (sid) => `/api/jobs/${sid}/verify-retweet`;
const POLL_MS = Number(process.env.POLL_MS || 1000);
const COOKIE_FILE = "cookies_wurk.json";

const LOG = (...a) => console.log(new Date().toISOString(), ...a);

// ====== (ADD) TELEGRAM NOTIFY ======
const TG_ENABLED = (process.env.TG_ENABLED || "false").toLowerCase() === "true";
const TG_BOT_TOKEN = process.env.TG_BOT_TOKEN || process.env.TELEGRAM_BOT_TOKEN || "";
const TG_CHAT_ID = process.env.TG_CHAT_ID || process.env.TELEGRAM_CHAT_ID || "";
function tgNotify(text) {
  if (!TG_ENABLED || !TG_BOT_TOKEN || !TG_CHAT_ID) return;
  axios
    .post(`https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage`, {
      chat_id: TG_CHAT_ID,
      text,
      disable_web_page_preview: true,
    }, { timeout: 4000 })
    .catch(() => {});
}

// ====== READLINE ======
const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
function waitEnterOnce(promptText = "") {
  if (promptText) process.stdout.write(promptText);
  return new Promise((resolve) => {
    const onLine = () => { rl.removeListener("line", onLine); resolve(); };
    rl.on("line", onLine);
  });
}

// ====== CLIPBOARD (Termux + fallback minimal) ======
async function copyToClipboard(text) {
  const hasTermux =
    process.env.TERMUX_VERSION ||
    fs.existsSync("/data/data/com.termux/files/usr/bin/termux-clipboard-set");
  if (hasTermux) {
    return new Promise((resolve) => {
      const safe = (text || "").replace(/"/g, '\\"');
      exec(`printf %s "${safe}" | termux-clipboard-set`, (err) => resolve(!err));
    });
  }
  // Fallback (opsional jika bukan di Termux)
  const runSh = (cmd) => new Promise((resolve) => exec(cmd, () => resolve(true)));
  if (os.platform() === "darwin") return runSh(`printf %s "${text.replace(/"/g,'\\"')}" | pbcopy`);
  if (fs.existsSync("/usr/bin/wl-copy") || fs.existsSync("/bin/wl-copy"))
    return runSh(`printf %s "${text.replace(/"/g,'\\"')}" | wl-copy`);
  if (fs.existsSync("/usr/bin/xclip") || fs.existsSync("/bin/xclip"))
    return runSh(`printf %s "${text.replace(/"/g,'\\"')}" | xclip -selection clipboard`);
  if (fs.existsSync("/usr/bin/xsel") || fs.existsSync("/bin/xsel"))
    return runSh(`printf %s "${text.replace(/"/g,'\\"')}" | xsel --clipboard --input`);
  if (os.platform() === "win32")
    return runSh(`powershell -Command "Set-Clipboard -Value \\"${text.replace(/"/g,'\\"')}\\""`);
  return false;
}

// ====== COOKIE SOURCES ======
function cookieFromFile(file = COOKIE_FILE) {
  try {
    const arr = JSON.parse(fs.readFileSync(file, "utf-8"));
    const map = new Map(arr.map((c) => [c.name, c.value]));
    const xsrf = map.get("XSRF-TOKEN");
    const sid = map.get("wurk.sid");
    if (!xsrf || !sid) return null;
    return { cookie: `XSRF-TOKEN=${xsrf}; wurk.sid=${sid}`, xsrf };
  } catch {
    return null;
  }
}
function xsrfFromCookieStr(cookieStr) {
  const m = cookieStr.match(/(?:^|;\s*)XSRF-TOKEN=([^;]+)/i);
  if (!m) return null;
  try { return decodeURIComponent(m[1]); } catch { return m[1]; }
}
function makeClient() {
  const fromFile = cookieFromFile();
  const cookieStr = fromFile?.cookie || (process.env.WURK_COOKIE || "").trim();
  if (!cookieStr) {
    LOG("❌ Butuh cookies. Isi .env WURK_COOKIE atau sediakan cookies_wurk.json");
    process.exit(1);
  }
  const xsrf = fromFile?.xsrf || xsrfFromCookieStr(cookieStr);
  const headers = {
    Accept: "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    Pragma: "no-cache",
    Cookie: cookieStr,
  };
  if (xsrf) headers["X-XSRF-TOKEN"] = xsrf;
  return axios.create({ baseURL: BASE, headers, validateStatus: () => true, timeout: 20000 });
}

// ====== FETCHERS ======
async function getOpenJobs(ax) {
  const r = await ax.get(API_OPEN);
  if (r.status === 401 || r.status === 403) {
    LOG(`❌ Unauthorized (${r.status}). Cookie kadaluarsa/salah. Update WURK_COOKIE / cookies_wurk.json`);
    return [];
  }
  if (r.status >= 400) {
    LOG(`⚠ open jobs error: ${r.status}`);
    return [];
  }
  const arr = r.data?.jobs ?? r.data ?? [];
  return Array.isArray(arr) ? arr : [];
}
async function getJobDetail(ax, sid) {
  const r = await ax.get(API_JOB(sid));
  return r.status >= 200 && r.status < 300 ? r.data : null;
}
async function verifyStatus(ax, sid) {
  const r = await ax.get(API_VERIFY_STATUS(sid));
  if (r.status === 401 || r.status === 403) {
    LOG(`❌ Verify unauthorized (${r.status}). Perbarui cookie.`);
    return null;
  }
  return r.data ?? null;
}
async function verifyRetweet(ax, sid) {
  const r = await ax.post(API_VERIFY_RETWEET(sid), {});
  if (r.status === 401 || r.status === 403) {
    LOG(`❌ Verify-retweet unauthorized (${r.status}). Perbarui cookie.`);
    return null;
  }
  return r.data ?? null;
}

// ====== HELPERS ======
function extractTweetUrl(j, detail) {
  return detail?.work_url ?? j?.tweet_url ?? j?.tweet_snapshot?.url ?? j?.work_url ?? null;
}
function parseReward(j, detail) {
  const raw =
    detail?.reward_per_retweet_sol ??
    detail?.reward_per_retweet ??
    detail?.reward ??
    detail?.data?.reward_per_retweet_sol ??
    j?.reward_per_retweet_sol ??
    "0";
  return parseFloat(String(raw).replace(/[^0-9.]/g, "")) || 0;
}
// Listed by: cek di detail DAN list (j)
function getListedBy(detail, j) {
  return (
    // detail
    detail?.listed_by ??
    detail?.listedBy ??
    detail?.poster ??
    detail?.posted_by ??
    detail?.creator ??
    detail?.owner ??
    detail?.user?.username ??
    detail?.user?.name ??
    detail?.account?.username ??
    detail?.creator_username ??
    detail?.creator_handle ??
    // list
    j?.listed_by ??
    j?.listedBy ??
    j?.poster ??
    j?.posted_by ??
    j?.creator ??
    j?.owner ??
    j?.user?.username ??
    j?.user?.name ??
    j?.account?.username ??
    j?.creator_username ??
    j?.creator_handle ??
    null
  );
}
async function buildCandidates(ax, jobs, doneSet) {
  const out = [];
  for (const j of jobs) {
    const sid = j?.short_id ?? j?.shortId ?? j?.id;
    if (!sid || doneSet.has(sid)) continue;

    const detail = (await getJobDetail(ax, sid)) || {};
    const hasReposted = !!(detail?._user_has_reposted ?? detail?.user_has_reposted ?? j?._user_has_reposted);
    if (hasReposted) { doneSet.add(sid); continue; }

    const url = extractTweetUrl(j, detail);
    if (!url) continue;

    const reward = parseReward(j, detail);
    const name =
      j?.tweet_snapshot?.id ??
      j?.title ??
      j?.tweet_snapshot?.tweet_id ??
      j?.name ??
      j?.description ??
      "(no-title)";
    const listedBy = getListedBy(detail, j);
    out.push({ sid, name, reward, url, listedBy });
  }
  return out; // newest-first sesuai API
}

// ====== STATE ======
const done = new Set();
let lastShownSid = null;
let activeJob = null;

// ====== MAIN ======
async function main() {
  LOG("Start (Termux, manual latest-first)… POLL:", POLL_MS, "ms");
  const ax = makeClient();

  LOG("✅ Client siap. Jika 401/403, update cookie.");
  LOG("-".repeat(60));
  LOG("Alur:");
  LOG("• Script menampilkan job BELUM REPOST (paling baru), auto copy URL ke clipboard");
  LOG("• Kamu repost manual, lalu TEKAN ENTER → script verify-retweet → verify-status (sekali)");
  LOG("• Jika job habis → langsung log: '✅ Semua job selesai. Menunggu job baru…'");
  LOG("-".repeat(60));

  let lastHeartbeat = Date.now();

  while (true) {
    try {
      const jobs = await getOpenJobs(ax);
      const candidates = await buildCandidates(ax, jobs, done);
      const newest = candidates[0] || null;

      // Tampilkan hanya jika berbeda & tidak ada aktif yang menunggu
      if (newest && newest.sid !== lastShownSid && !activeJob) {
        activeJob = newest;
        lastShownSid = newest.sid;

        console.log("-".repeat(60));
        LOG("🆕 ACTIVE JOB (LATEST)");
        LOG(`📋 ID      : ${activeJob.sid}`);
        LOG(`📝 Name    : ${activeJob.name}`);
        if (activeJob.listedBy) LOG(`🙋 Listed by : ${activeJob.listedBy}`);
        LOG(`💰 Reward  : ${activeJob.reward} SOL`);
        LOG(`🔗 URL     : ${activeJob.url}`);

        // (ADD) Notifikasi Telegram saat ketemu job baru
        tgNotify(
          ["🆕 Job baru terdeteksi", `ID: ${activeJob.sid}`, `Reward: ${activeJob.reward} SOL`, `URL: ${activeJob.url}`].join("\n")
        );

        const copied = await copyToClipboard(activeJob.url);
        LOG(
          copied
            ? "📋 URL disalin ke clipboard (Termux)."
            : "⚠ Gagal copy ke clipboard. Install termux-api & app Termux:API, lalu coba lagi."
        );

        LOG("👉 Aksi: REPOST MANUAL sekarang, lalu tekan ENTER untuk verifikasi sekali.");
        (async () => {
          await waitEnterOnce("");
          try {
            LOG("➡️ Enter diterima. ⌛ Verify retweet…");
            const vr = await verifyRetweet(ax, activeJob.sid);
            const retweetOk = !!(vr?.ok || vr?.verified || vr?.success || vr?.retweet_verified);
            LOG(`🔁 Verify-retweet ${activeJob.sid}: ${retweetOk ? "✅ OK" : "⚠ Tidak pasti/ditolak"}`);

            LOG("🔎 Cek verify-status…");
            const vs = await verifyStatus(ax, activeJob.sid);
            const verified = !!(
              vs?.verified ||
              vs?.is_verified ||
              vs?.user_has_reposted ||
              vs?._user_has_reposted ||
              vs?.ok ||
              vs?.retweet_verified
            );
            LOG(`📊 Verify-status ${activeJob.sid}: ${verified ? "✅ TRUE" : "❌ FALSE"}`);
            if (verified && activeJob.reward > 0) {
              LOG(`💎 Reward +${activeJob.reward} SOL (sesuai sistem wurk.fun)`);
            }
            done.add(activeJob.sid); // tandai selesai
          } catch (e) {
            LOG(`❌ Verify error ${activeJob.sid}:`, e.message || e);
          } finally {
            activeJob = null;

            // SCAN CEPAT: kalau memang belum ada job lain → umumkan idle sekarang
            try {
              const jobs2 = await getOpenJobs(ax);
              const c2 = await buildCandidates(ax, jobs2, done);
              if (!c2.length) {
                console.log("-".repeat(60));
                LOG("✅ Semua job selesai. Menunggu job baru…");
              }
            } catch {}
          }
        })();
      }

      // Heartbeat tiap ~30 detik
      if (Date.now() - lastHeartbeat > 30000) {
        lastHeartbeat = Date.now();
        LOG(`⚡ Monitoring… Active: ${activeJob?.sid ?? "-"} | Done: ${done.size}`);
      }
    } catch (e) {
      LOG("Loop error:", e.message || e);
    }

    await new Promise((r) => setTimeout(r, POLL_MS));
  }
}

main().catch((e) => {
  LOG("Fatal:", e);
  process.exit(1);
});
