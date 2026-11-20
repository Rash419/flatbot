"""Entrypoint: `python -m app.main`.

Background daemon threads: Telegram polling, the SMS inbox, and the
order-email poller when AI is on. The SMS webhook runs on the main thread.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import traceback

from . import mail
from .ai import CodexAI
from .bot import Bot
from .config import ConfigError, load_config
from .db import DB
from .extract import Extractor, money_candidate
from .server import make_server
from .telegram import Telegram, TelegramError

log = logging.getLogger("app")

GETME_DELAYS = (2, 4, 8, 15, 30, 30)  # seconds between attempts while the network comes up
BACKOFF_START = 5
BACKOFF_CAP = 60


def _describe(e: BaseException) -> str:
    """Exception type and stack, without the message (which could quote a user's text)."""
    return f"{type(e).__name__}\n" + "".join(traceback.format_tb(e.__traceback__)).rstrip()


def connect(tg: Telegram, stop: threading.Event) -> dict | None:
    """getMe, retrying only while Telegram is unreachable. A rejected token fails at once."""
    for delay in (*GETME_DELAYS, None):
        try:
            return tg.call("getMe")
        except TelegramError as e:
            if "could not reach" not in str(e) or delay is None:
                raise
            log.warning("Telegram is not reachable yet, retrying in %ss", delay)
            if stop.wait(delay):
                return None
    return None


def poll(bot: Bot, tg: Telegram, db: DB, stop: threading.Event) -> None:
    """Long-poll Telegram forever. Never lets an exception end the thread."""
    raw = db.get("tg_offset")
    offset = int(raw) if raw and raw.lstrip("-").isdigit() else None
    backoff = BACKOFF_START
    while not stop.is_set():
        try:
            updates = tg.get_updates(offset)
            for update in updates:
                try:
                    bot.handle_update(update)
                except Exception as e:  # Bot promises not to raise; be safe anyway
                    log.error("Unhandled error in an update: %s", _describe(e))
                offset = update["update_id"] + 1
                db.set("tg_offset", str(offset))
            backoff = BACKOFF_START
        except TelegramError as e:
            log.warning("Telegram polling failed (%s); retrying in %ss", e, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_CAP)
        except Exception as e:
            log.error("Polling loop error, retrying in %ss: %s", backoff, _describe(e))
            stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_CAP)


def setup_ai(cfg):
    """(extractor, mailbox): both None when Codex is missing or not signed in."""
    ai = CodexAI(model=cfg.codex_model, effort=cfg.codex_effort, timeout=cfg.ai_timeout)
    how = "sign in with: docker compose run --rm bot codex login --device-auth"
    if not ai.installed():
        log.warning("AI is off: the codex program is not installed, so bank SMS will not be read "
                    "(payers get the SMS text to type in), and bill photos and order emails are off.")
        return None, None
    if not ai.signed_in():
        log.warning("AI is off: Codex is not signed in, so bank SMS will not be read until someone "
                    "signs in (payers get the SMS text to type in), and bill photos and order emails "
                    "are off. To turn it on, %s, then restart.", how)
        return None, None
    log.info("AI is on: Codex (model %s, effort %s) reads SMS, order emails and bill photos",
             cfg.codex_model or "default", cfg.codex_effort)
    return Extractor(ai), mail


def supervise(name: str, loop, stop: threading.Event) -> None:
    """Run one of the bot's blocking loops; restart it if it ever raises."""
    backoff = BACKOFF_START
    while not stop.is_set():
        try:
            loop(stop)
            return
        except Exception as e:
            log.error("The %s loop failed, restarting in %ss: %s", name, backoff, _describe(e))
            stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_CAP)


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        cfg = load_config()
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 1

    stop = threading.Event()
    server = None

    def on_signal(signum, _frame):
        log.info("Received %s, shutting down", signal.Signals(signum).name)
        stop.set()
        if server is not None:
            # shutdown() blocks until serve_forever returns, so it cannot run on this thread.
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    parent = os.path.dirname(os.path.abspath(cfg.db_path))
    os.makedirs(parent, exist_ok=True)
    db = DB(cfg.db_path)
    tg = Telegram(cfg.telegram_token, cfg.telegram_api_base)

    try:
        me = connect(tg, stop)
    except TelegramError as e:
        log.error("Telegram rejected the bot token or is unavailable: %s", e)
        return 1
    if me is None:
        return 0  # stopped while waiting for the network
    username = me.get("username", "?")
    log.info("Connected to Telegram as @%s", username)

    extractor, mailbox = setup_ai(cfg)
    bot = Bot(db, tg, currency=cfg.currency, public_url=cfg.public_url, tz=cfg.timezone,
              extractor=extractor, mailbox=mailbox, mail_senders=cfg.mail_senders,
              bank_senders=cfg.bank_senders,
              money_candidate=money_candidate)

    try:
        tg.call("setMyCommands", commands=[
            {"command": name, "description": desc} for name, desc in Bot.COMMANDS])
    except TelegramError as e:
        log.warning("Could not register the command menu: %s", e)

    if not cfg.public_url:
        log.warning("PUBLIC_URL is not set, so /phone cannot show the full webhook URL")
    log.info("To join, each flatmate sends this to @%s in a private chat:  /start %s",
             username, bot.join_code)

    threading.Thread(target=poll, args=(bot, tg, db, stop), name="telegram-poll", daemon=True).start()
    threading.Thread(target=supervise, args=("inbox", bot.run_inbox, stop),
                     name="sms-inbox", daemon=True).start()
    if mailbox is not None:
        threading.Thread(target=supervise, args=("mail", lambda s: bot.run_mail(s, cfg.mail_poll_seconds), stop),
                         name="mail-poll", daemon=True).start()

    server = make_server(bot, cfg.port)
    if stop.is_set():  # a signal arrived during startup
        server.server_close()
        return 0
    log.info("SMS webhook listening on port %s", server.server_address[1])
    try:
        server.serve_forever()
    finally:
        server.server_close()
    log.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
