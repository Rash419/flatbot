# Flat expenses bot

A small Telegram bot that keeps track of what two flatmates owe each other, without anyone typing expenses in by hand. Each person's Android phone forwards bank SMS to the bot. When an SMS is a completed spend, the bot messages the person who paid with a few buttons; one tap records it and tells the other flatmate their share and the running balance. Cash spends are typed in as a short message or sent as a photo of the bill, and order emails (Swiggy, Blinkit, Amazon and so on) let you split a mixed order item by item. The SMS, emails and photos are read by an AI model through the Codex CLI, using a ChatGPT subscription. The bot keeps its own ledger in a SQLite file rather than using Splitwise.

It runs as a single Docker container on your homeserver. It talks to Telegram by polling, so the only thing that needs to reach the server from outside is the phones' SMS webhook.

## Setup

1. In Telegram, talk to [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token it gives you.
2. Copy the example settings and fill them in. At minimum set `TELEGRAM_BOT_TOKEN`; set `PUBLIC_URL` to the address the phones will use to reach the server (see the next section).

   ```sh
   cp .env.example .env
   ```

3. Build the image and sign Codex in to your ChatGPT account (see [AI](#ai) below; this is required for bank SMS to be read automatically):

   ```sh
   docker compose build
   docker compose run --rm bot codex login --device-auth
   ```

4. Start it:

   ```sh
   docker compose up -d
   ```

5. Find the join code in the logs. Look for the line that says `To join, each flatmate sends this to @yourbot`:

   ```sh
   docker compose logs bot | grep "To join"
   ```

6. Each flatmate opens a private chat with the bot and sends `/start <code>` using that code.

The database and the Codex sign-in are kept in `./data` next to `docker-compose.yml`. The container starts as root only to make that folder writable, then runs the bot as an unprivileged user, so you do not need to create or chown the folder yourself.

## AI

The bot uses an AI model for three things: reading every bank SMS that mentions an amount of money, reading order emails from the shops in `MAIL_SENDERS`, and reading the total off bill photos. It runs on a ChatGPT subscription through OpenAI's Codex CLI, which is built into the image, so there is no API key to buy.

Sign in once, before the first start or whenever the sign-in expires:

```sh
docker compose run --rm bot codex login --device-auth
docker compose up -d
```

The first command prints a link and a code; open the link on any device, sign in to ChatGPT and enter the code. If device sign-in is not available for your account, run `codex login` on a computer with a browser instead and copy its `~/.codex/auth.json` to `data/codex/auth.json` on the server. If the bot was already running, restart it with `docker compose restart` so it notices the sign-in. The startup log says whether AI is on.

Sign-in is required for automatic SMS reading. Without it, typed entries, the buttons and balances still work, but each bank SMS that mentions an amount only produces a message to the payer saying it could not be read because the server is not signed in, with the SMS text so they can type the expense in. Bill photos and order emails are off too. If an AI call fails or times out, the bot tries that SMS again later, and if it still cannot read it, sends the payer the SMS text.

Each AI call takes several seconds, so the prompt for an SMS arrives a little after the SMS. Calls count against your ChatGPT plan's Codex usage; `CODEX_EFFORT=low` keeps them light.

## Phone setup

The phones need to reach the server from outside your home network, because SMS arrive when you are out. Any of these works: Tailscale on the phones and the server, a tunnel such as Cloudflare Tunnel, or a reverse proxy with HTTPS in front of port 8080. Put the resulting base address in `PUBLIC_URL` and restart with `docker compose up -d`.

Each person then sends `/phone` to the bot. It replies with that person's own webhook URL, which looks like `https://your.server/sms/<token>`. Treat it like a password: anyone who has it can send fake SMS on your behalf.

Install an Android SMS forwarder that can POST incoming SMS to a URL. One example is "Incoming SMS to URL forwarder" from F-Droid, but any app that POSTs the SMS text works. Point it at your personal URL and send the message as JSON with the SMS in a `text` field, for example `{"text": "%text%"}` (the placeholder syntax depends on the app). The server also accepts `message`, `body`, `msg`, `content` or `sms` as the field name, a form-encoded body, or the bare SMS as plain text. If the app can filter by sender, forward only your banks' sender IDs (for example ones ending in `HDFCBK` or `ICICIB`), so personal messages never leave the phone.

To test the webhook by hand from any machine:

```sh
curl -X POST "https://your.server/sms/<token>" \
  -H "Content-Type: application/json" \
  -d '{"text": "Sent Rs.450.00 From HDFC Bank A/C *1234 To BLINKIT On 20/11/26 Ref 123456789012"}'
```

The reply is `{"status": "queued"}` when the SMS was accepted (the bot then reads it and, if it is a spend, messages you), `"ignored"` for messages with no amount and for OTPs, and `"duplicate"` when the same SMS was already received. A 404 with `"unknown"` means the token in the URL is wrong.

## Order emails

So a mixed order (your shampoo and shared groceries in one Blinkit order) can be split by item, the bot can read order emails. Each person connects their own mailbox by sending the bot:

```
/email you@gmail.com <app password>
```

For Gmail, turn on 2-step verification and create an app password at https://myaccount.google.com/apppasswords; your normal password will not work. For other providers, add the IMAP server as a third argument, for example `/email you@outlook.com <app password> outlook.office365.com`. The bot checks the login, deletes your message containing the password from the chat, and from then on looks only at mail whose sender matches the `MAIL_SENDERS` list. It opens the mailbox read-only: nothing is marked read, moved or deleted. `/email off` disconnects.

When an order email matches a payment by exact amount, the prompt for that payment gains a **Pick shared items** button with a tick list of the items. Delivery fees, taxes and discounts are spread over the ticked items in proportion to their prices. Email only adds detail to a payment an SMS already brought in; it never creates an entry by itself.

## Daily use

When a spend comes in, the payer gets a private message with these buttons:

- **Split equally**: the other flatmate owes half.
- **Flatmate owes all**: the whole amount was for the other flatmate.
- **Paid flatmate back**: this was a repayment to the other flatmate, not a shared expense.
- **Personal**: nothing is recorded against the other flatmate.

After a tap, the other flatmate gets a message with their share and the new balance.

When only part of a payment was shared and there is no order email, tap **Only part is shared** and reply with the shared amount.

For cash, just send the amount and a description, like `450 groceries`. If AI is on, you can send a photo of the bill instead.

To fix something, reply to the bot's message about that expense: `dinner` renames it, `520` corrects the amount, and `520 dinner` does both. Every recorded entry also has an **Undo** button, which puts it back to the original buttons. Only the person who paid can change an entry.

For merchants that always go the same way, save a rule: after you tap **Split equally** or **Personal** on an SMS spend, an **Always split Blinkit** or **Always skip Netflix** button appears under it. From then on, SMS from that merchant are split (with a message and an Undo) or skipped silently. Rules are per person; `/rules` lists yours and lets you delete them.

`/balance` shows who owes whom, `/history` lists the last 15 shared entries, and `/export` sends the whole ledger as a CSV file. `/invite` shows the join code again and `/name Rahul` changes how you appear to the other person.

To bring over a balance you already had, record it as one expense paid by the person who is owed: that person sends `1240 carried over` and taps **Flatmate owes all**.

## Privacy

- Every SMS that mentions an amount of money, every order email from a sender in `MAIL_SENDERS`, and every bill photo you send are sent to OpenAI through Codex to be read. Nothing else from your mailbox is downloaded.
- OTPs, verification codes and messages without an amount never leave the server and are not stored.
- SMS text waits in a queue in the database until it has been read, and is then cleared from the queue. The text of an SMS that turned out to be a spend is kept with that entry in the database; everything else is discarded.
- The mailbox app password is stored in `data/bot.db` in plain text, so protect that file (and its backups). Use an app password, never your main password, so it can be revoked on its own.
- The logs never contain SMS text, email content, webhook tokens or passwords.
- Prompts go only to the payer's private chat with the bot. The other flatmate sees an expense only once it is shared with them.
- Your personal webhook URL is a secret. If it leaks, anyone can send fake spends to your chat.

## Backup

Everything the bot knows is in `data/bot.db`; `data/codex` holds the Codex sign-in. Copying the `data` folder is a full backup; stop the container first (`docker compose stop`) for a clean copy, then start it again. The backup contains the mailbox app passwords and the ChatGPT sign-in, so keep it somewhere private.

## Running the tests

From the project root, with Python 3.12 or newer:

```sh
python3 -m unittest discover -s tests -t .
```

To check the AI path against the real model once the container is signed in (this sends the sample SMS, order email and bill photo in `tests/live` to OpenAI):

```sh
docker compose run --rm -v ./tests/live:/test:ro bot python /test/ai_check.py
```
