#!/usr/bin/env python3
"""voice-stt-bot: Telegram voice -> Groq whisper text. Nothing persists.

HARD RULES (owner directive):
  * audio bytes live in RAM only - never written to disk, ever
  * logs carry METADATA only (sizes/ms/model/bools) - never audio, never transcript
  * user's voice message is best-effort deleted right after the reply
  * primary model whisper-large-v3; ANY failure -> whisper-large-v3-turbo
  * chain-dispatch the next shift before exiting so coverage never gaps
"""
import json, os, time, uuid, urllib.request, urllib.parse, urllib.error

TG_BOT = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ = os.environ["GROQ_API_KEY"]
TG = "https://api.telegram.org/bot" + TG_BOT
RUN_MIN = float(os.environ.get("RUN_MINUTES", "345"))
START = time.time()
DEADLINE = START + RUN_MIN * 60
MODELS = ("whisper-large-v3", "whisper-large-v3-turbo")


def log(*a):
    print("[%6.1fs] " % (time.time() - START) + " ".join(str(x) for x in a), flush=True)


def tg(method, params=None, timeout=60):
    data = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(TG + "/" + method, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


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
                 "Content-Type": "multipart/form-data; boundary=" + b})
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


def handle(m):
    chat = m["chat"]["id"]
    mid = m.get("message_id")
    text = m.get("text") or ""
    if text.startswith("/start"):
        tg("sendMessage", {"chat_id": chat,
                           "text": "Send me a voice message. I transcribe it and keep nothing."})
        return
    t0 = time.time()
    try:
        audio = extract_audio(m)
    except Exception as e:
        log("dl_fail", chat, type(e).__name__)
        return
    if not audio:
        return  # text/photos/etc: ignore silently
    nbytes = len(audio)
    used, err = None, None
    transcript = None
    for model in MODELS:
        try:
            transcript = groq_stt(audio, model)
            used = model
            break
        except Exception as e:
            err = getattr(e, "code", None) or type(e).__name__
            log("stt_fail", chat, model, err)
    del audio
    if transcript is None:
        tg("sendMessage", {"chat_id": chat, "text": "transcription failed (both models)",
                           "reply_to_message_id": mid})
        log("done", chat, nbytes, "-", "-", "fail")
        return
    reply = transcript if transcript.strip() else "(no speech detected)"
    try:
        tg("sendMessage", {"chat_id": chat, "text": reply, "reply_to_message_id": mid})
    except Exception as e:
        log("reply_fail", chat, type(e).__name__)
    # best-effort vaporize of the user's audio message (works in groups the bot admins;
    # Telegram may refuse in private chats - result is logged, never fatal)
    try:
        d = tg("deleteMessage", {"chat_id": chat, "message_id": mid})
        deleted = bool(d.get("ok"))
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
    log("shift start", "budget", RUN_MIN, "min")
    while time.time() < DEADLINE:
        if not chained and time.time() > DEADLINE - 45:
            dispatch_next()
            chained = True
        try:
            res = tg("getUpdates", {"offset": offset, "timeout": 45,
                                     "allowed_updates": json.dumps(["message"])},
                     timeout=60)
        except Exception as e:
            log("poll_err", type(e).__name__)
            time.sleep(2)
            continue
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            try:
                handle(upd.get("message") or {})
            except Exception as e:
                log("handle_err", type(e).__name__)
    if not chained:
        dispatch_next()
    log("shift end - nothing persisted")


if __name__ == "__main__":
    main()
