#!/usr/bin/env python3
"""voice-stt-bot: Telegram voice -> Groq whisper text.

HARD RULES (owner directive):
  * audio bytes live in RAM only - never written to disk, ever (NOT a toggle)
  * logs carry METADATA only (sizes/ms/model/bools) - never audio, never transcript
  * primary model whisper-large-v3; ANY failure -> whisper-large-v3-turbo
  * chain-dispatch the next shift before exiting so coverage never gaps
  * settings.json lives IN THE REPO: toggle flips commit+push so state survives
    restarts (GHA runners are ephemeral)
  * /panel (admin) = inline toggles, GemBot-style; /toggle_* commands mirror them
"""
import json, os, subprocess, time, uuid, urllib.request, urllib.parse, urllib.error

TG_BOT = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ = os.environ["GROQ_API_KEY"]
TG = "https://api.telegram.org/bot" + TG_BOT
RUN_MIN = float(os.environ.get("RUN_MINUTES", "345"))
START = time.time()
DEADLINE = START + RUN_MIN * 60
ALL_MODELS = ("whisper-large-v3", "whisper-large-v3-turbo")
ADMINS = (6592796294, 8439794110)
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
DEFAULT_SETTINGS = {"delete_telegram_voice": True, "model_mode": "auto"}


def log(*a):
    print("[%6.1fs] " % (time.time() - START) + " ".join(str(x) for x in a), flush=True)


def tg(method, params=None, timeout=60):
    data = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(TG + "/" + method, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------- settings: load / save / persist to repo ----------------

def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return {**DEFAULT_SETTINGS, **json.load(f)}
    except Exception as e:
        log("settings_load_fallback", type(e).__name__)
        return dict(DEFAULT_SETTINGS)


def git(args):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_AUTHOR_NAME", "voice-stt-bot")
    env.setdefault("GIT_AUTHOR_EMAIL", "voice-stt-bot@local")
    env.setdefault("GIT_COMMITTER_NAME", "voice-stt-bot")
    env.setdefault("GIT_COMMITTER_EMAIL", "voice-stt-bot@local")
    p = subprocess.run(["git"] + args, capture_output=True, text=True, env=env)
    # never let a token ride into the logs (checkout persists auth in config,
    # but scrub defensively in case an error message echoes the remote)
    tok = os.environ.get("GITHUB_TOKEN", "")
    if tok and tok in p.stderr:
        p.stderr = p.stderr.replace(tok, "***")
    if tok and tok in p.stdout:
        p.stdout = p.stdout.replace(tok, "***")
    return p.returncode, (p.stdout + p.stderr).strip()


def persist_settings(settings):
    """Write settings.json AND push it back to the repo (settings must survive)."""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception as e:
        log("settings_write_FAIL", type(e).__name__)
        return False
    if not os.environ.get("GITHUB_TOKEN"):
        log("settings_persist", "no-token (local run) - file only")
        return True
    rc, out = git(["add", "settings.json"])
    if rc != 0:
        log("settings_git_add_FAIL", out[:120])
        return False
    rc, out = git(["diff", "--cached", "--quiet"])
    if rc == 0:
        log("settings_persist", "no-change")
        return True
    rc, out = git(["commit", "-q", "-m", "settings: update panel toggles"])
    if rc != 0:
        log("settings_git_commit_FAIL", out[:120])
        return False
    for attempt in (1, 2):
        git(["pull", "--rebase", "-q", "origin", os.environ.get("SETTINGS_BRANCH", "main")])
        rc, out = git(["push", "-q", "origin", "HEAD"])
        if rc == 0:
            log("settings_persist", "pushed")
            return True
        log("settings_push_retry", attempt, out[:120])
    log("settings_persist_FAIL")
    return False


SETTINGS = load_settings()


def model_chain():
    mode = SETTINGS.get("model_mode", "auto")
    if mode == "v3":
        return ("whisper-large-v3",)
    if mode == "turbo":
        return ("whisper-large-v3-turbo",)
    return ALL_MODELS


# ---------------- Groq STT ----------------

def groq_stt(audio, model):
    b = "----x" + uuid.uuid4().hex
    parts = []

    def fld(n, v):
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (b, n, v)).encode())

    fld("model", model)
    fld("response_format", "json")
    parts.append(('--%s\r\nContent-Disposition: form-data; name="file"; '
                  'filename="voice.ogg"\r\nContent-Type: audio/ogg\r\n\r\n' % b).encode())
    parts.append(audio)
    parts.append(("\r\n--%s--\r\n" % b).encode())
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        data=b"".join(parts),
        headers={"Authorization": "Bearer " + GROQ,
                 "Content-Type": "multipart/form-data; boundary=" + b,
                 # Groq sits behind Cloudflare: default Python-urllib UA gets
                 # banned with 403 "error code: 1010" (browser-signature ban).
                 # A browser UA makes urllib pass (verified live, 200).
                 "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read()).get("text", "")


def extract_audio(m):
    """Return audio bytes (RAM) for voice / audio / audio-document messages, else None."""
    node = m.get("voice") or m.get("audio")
    if not node:
        doc = m.get("document") or {}
        mime = (doc.get("mime_type") or "")
        if mime.startswith("audio/"):
            node = doc
    if not node:
        return None
    info = tg("getFile", {"file_id": node["file_id"]})
    path = info["result"]["file_path"]
    url = "https://api.telegram.org/file/bot%s/%s" % (TG_BOT, path)
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


# ---------------- control panel ----------------

def panel_text():
    dele = "🟢 ON" if SETTINGS.get("delete_telegram_voice", True) else "🔴 OFF"
    mode = (SETTINGS.get("model_mode", "auto") or "auto").upper()
    return ("🎛️ Voice STT Panel\n\n"
            "🗑️ Delete voice in Telegram: %s\n"
            "🎙️ Model: %s\n"
            "💾 Local audio files: 🛡️ ALWAYS vaporized (not a toggle)\n\n"
            "Settings survive restarts (pushed to the repo)." % (dele, mode))


def panel_kb():
    dele = "🟢 ON" if SETTINGS.get("delete_telegram_voice", True) else "🔴 OFF"
    mode = (SETTINGS.get("model_mode", "auto") or "auto").upper()
    rows = [
        [("🗑️ TG Voice Delete: " + dele, "toggle_del")],
        [("🎙️ Model: " + mode, "cycle_model")],
        [("💾 Local: ALWAYS vaporized", "locked_info"),
         ("🔄 Refresh", "refresh_panel")],
    ]
    # flat button format: [text, data] -> "text\ndata" handled by builder
    return rows


def kb_markup(rows):
    return json.dumps([
        [{"text": t, "callback_data": d} for t, d in row] for row in rows
    ])


def send_panel(chat, reply_to=None):
    params = {"chat_id": chat, "text": panel_text(),
              "reply_markup": kb_markup(panel_kb())}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    return tg("sendMessage", params)


def refresh_panel(chat, mid):
    try:
        tg("editMessageText", {"chat_id": chat, "message_id": mid,
                               "text": panel_text(),
                               "reply_markup": kb_markup(panel_kb())})
        return True
    except Exception as e:
        log("panel_edit_fail", chat, type(e).__name__)
        return False


def is_admin(uid):
    return uid in ADMINS


def handle_callback(q):
    uid = (q.get("from") or {}).get("id", 0)
    data = q.get("data") or ""
    msg = q.get("message") or {}
    chat = msg.get("chat", {}).get("id")
    mid = msg.get("message_id")
    if not is_admin(uid):
        try:
            tg("answerCallbackQuery", {"callback_query_id": q.get("id"),
                                       "text": "admin only", "show_alert": True})
        except Exception:
            pass
        log("panel_denied", uid)
        return
    try:
        tg("answerCallbackQuery", {"callback_query_id": q.get("id")})
    except Exception:
        pass
    if data == "toggle_del":
        SETTINGS["delete_telegram_voice"] = not SETTINGS.get("delete_telegram_voice", True)
        persist_settings(SETTINGS)
        log("panel", "tg_delete", SETTINGS["delete_telegram_voice"])
        refresh_panel(chat, mid)
    elif data == "cycle_model":
        order = ["auto", "v3", "turbo"]
        cur = SETTINGS.get("model_mode", "auto")
        SETTINGS["model_mode"] = order[(order.index(cur) + 1) % len(order)] if cur in order else "auto"
        persist_settings(SETTINGS)
        log("panel", "model", SETTINGS["model_mode"])
        refresh_panel(chat, mid)
    elif data == "locked_info":
        try:
            tg("answerCallbackQuery", {"callback_query_id": q.get("id"),
                                       "text": "Local files never exist - RAM only, always vaporized 😎",
                                       "show_alert": True})
        except Exception:
            pass
    elif data == "refresh_panel":
        refresh_panel(chat, mid)


# ---------------- message handling ----------------

def handle(m):
    if m.get("text", "").startswith("/panel") and not m.get("voice"):
        chat = m["chat"]["id"]
        uid = (m.get("from") or {}).get("id", 0)
        if not is_admin(uid):
            tg("sendMessage", {"chat_id": chat, "text": "admin only 😘"})
            return
        send_panel(chat, m.get("message_id"))
        log("panel", "opened", uid)
        return
    if m.get("text", "").startswith("/toggle_delete"):
        chat = m["chat"]["id"]
        uid = (m.get("from") or {}).get("id", 0)
        if not is_admin(uid):
            tg("sendMessage", {"chat_id": chat, "text": "admin only 😘"})
            return
        SETTINGS["delete_telegram_voice"] = not SETTINGS.get("delete_telegram_voice", True)
        persist_settings(SETTINGS)
        tg("sendMessage", {"chat_id": chat,
                           "text": "Telegram voice deletion: " +
                                   ("🟢 ON" if SETTINGS["delete_telegram_voice"] else "🔴 OFF")})
        log("panel", "tg_delete", SETTINGS["delete_telegram_voice"])
        return
    if m.get("text", "").startswith("/toggle_model"):
        chat = m["chat"]["id"]
        uid = (m.get("from") or {}).get("id", 0)
        if not is_admin(uid):
            tg("sendMessage", {"chat_id": chat, "text": "admin only 😘"})
            return
        order = ["auto", "v3", "turbo"]
        cur = SETTINGS.get("model_mode", "auto")
        SETTINGS["model_mode"] = order[(order.index(cur) + 1) % len(order)] if cur in order else "auto"
        persist_settings(SETTINGS)
        tg("sendMessage", {"chat_id": chat,
                           "text": "Model mode: " + SETTINGS["model_mode"].upper()})
        log("panel", "model", SETTINGS["model_mode"])
        return
    if m.get("text", "").startswith("/start"):
        chat = m["chat"]["id"]
        uid = (m.get("from") or {}).get("id", 0)
        txt = "Send me a voice message. I transcribe it and keep nothing locally."
        if is_admin(uid):
            txt += "\n\nAdmin: /panel for toggles."
        tg("sendMessage", {"chat_id": chat, "text": txt})
        return
    t0 = time.time()
    try:
        audio = extract_audio(m)
    except Exception as e:
        log("dl_fail", m.get("chat", {}).get("id"), type(e).__name__)
        return
    if not audio:
        return  # text/photos/etc: ignore silently
    chat = m["chat"]["id"]
    mid = m.get("message_id")
    nbytes = len(audio)
    used, err = None, None
    transcript = None
    for model in model_chain():
        try:
            transcript = groq_stt(audio, model)
            used = model
            break
        except Exception as e:
            err = getattr(e, "code", None) or type(e).__name__
            log("stt_fail", chat, model, err)
    del audio  # RAM only - hard rule, no toggle
    if transcript is None:
        tg("sendMessage", {"chat_id": chat, "text": "transcription failed (model chain exhausted)",
                           "reply_to_message_id": mid})
        log("done", chat, nbytes, "-", "-", "fail")
        return
    reply = transcript if transcript.strip() else "(no speech detected)"
    try:
        tg("sendMessage", {"chat_id": chat, "text": reply, "reply_to_message_id": mid})
    except Exception as e:
        log("reply_fail", chat, type(e).__name__)
    deleted = "off"
    if SETTINGS.get("delete_telegram_voice", True):
        # best-effort vaporize of the user's audio message (works in groups the
        # bot admins; Telegram may refuse in private chats - never fatal)
        try:
            d = tg("deleteMessage", {"chat_id": chat, "message_id": mid})
            deleted = str(bool(d.get("ok")))
        except Exception:
            deleted = False
    ms = int((time.time() - t0) * 1000)
    log("done", chat, nbytes, used, str(ms) + "ms", "del=" + str(deleted))


def dispatch_next():
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    wf = os.environ.get("WORKFLOW_FILE", "voice-stt.yml")
    if not (token and repo):
        log("chain", "no-token")
        return
    req = urllib.request.Request(
        "https://api.github.com/repos/%s/actions/workflows/%s/dispatches" % (repo, wf),
        data=json.dumps({"ref": "main"}).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "shift-chain"})
    try:
        urllib.request.urlopen(req, timeout=30)
        log("chain", "next-shift-dispatched")
    except Exception as e:
        log("chain", "FAIL", type(e).__name__)


def main():
    try:
        tg("deleteWebhook", {"drop_pending_updates": False})
    except Exception as e:
        log("webhook_clear_fail", type(e).__name__)
    offset = 0
    chained = False
    log("shift start", "budget", RUN_MIN, "min",
        "tg_del", SETTINGS.get("delete_telegram_voice", True),
        "model", SETTINGS.get("model_mode", "auto"))
    while time.time() < DEADLINE:
        if not chained and time.time() > DEADLINE - 45:
            dispatch_next()
            chained = True
        try:
            res = tg("getUpdates", {"offset": offset, "timeout": 45,
                                     "allowed_updates": json.dumps(
                                         ["message", "callback_query"])},
                     timeout=60)
        except Exception as e:
            log("poll_err", type(e).__name__)
            time.sleep(2)
            continue
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            try:
                if upd.get("callback_query"):
                    handle_callback(upd["callback_query"])
                else:
                    handle(upd.get("message") or {})
            except Exception as e:
                log("handle_err", type(e).__name__)
    if not chained:
        dispatch_next()
    log("shift end - no audio persisted")


if __name__ == "__main__":
    main()
