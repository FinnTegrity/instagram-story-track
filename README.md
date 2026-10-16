# Instagram Story Track

A Python bot that periodically checks Instagram Stories through [AnonStories](https://anonstories.com/) and sends newly found Story photos and videos to recipients via Telegram. It stores content fingerprints to reduce duplicate notifications after restarts.

> **Note:** This project depends on a third-party website. The website may change, restrict access, or require verification, so uninterrupted operation is not guaranteed. Only use it with content and accounts you are authorized to access, and follow the terms of the services involved.

## Features

- Reads Instagram usernames from `accounts.txt`.
- Checks Stories on a recurring schedule.
- Sends discovered photos and videos to one or more Telegram user IDs.
- Reduces duplicate sends using multiple fingerprints, including SHA-256 content hashes, image/video fingerprints, stable IDs, and normalized URLs.
- Stores history in `data.json` and writes runtime logs to `log.txt`.
- Supports multiple recipients using comma-separated Telegram user IDs.

## Requirements

- Windows and Python 3.10+ (a currently supported Python release is recommended).
- Google Chrome installed. The current code launches Playwright with `channel="chrome"`.
- Internet access.
- A Telegram bot created via [@BotFather](https://t.me/BotFather).

## Installation

Open PowerShell in the project directory.

### 1. Install dependencies

```powershell
py -m pip install --upgrade pip
py -m pip install pyTelegramBotAPI python-dotenv playwright opencv-python numpy Pillow
```

### 2. Configure Telegram

Create or edit the `.env` file in the project root:

```env
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN_HERE
TELEGRAM_USER_ID=123456789
```

- `TELEGRAM_BOT_TOKEN`: the token provided by BotFather.
- `TELEGRAM_USER_ID`: the recipient's numeric Telegram user ID. Multiple IDs can be comma-separated, for example `123456789,987654321`.
- Each recipient must open a chat with the bot and press **Start** before the bot can send messages.

**Security:** Never share your `.env` file, publish your token on GitHub, or include the token in screenshots. If the token is exposed, revoke it and generate a new one through BotFather.

### 3. Configure Instagram accounts

Open `accounts.txt` and add one Instagram username per line, without the `@` symbol:

```text
first_username
second_username
third_username
```

Replace the sample entries with accounts you are authorized to monitor. Save the file before starting the bot.

## Run the bot

In PowerShell, from the directory containing `bot_v6.py`, run:

```powershell
py .\bot_v6.py
```

The bot runs continuously and opens a Chrome window. If the website requests a Cloudflare verification, follow the instructions shown in the browser or terminal, then press Enter when prompted. Keep the terminal open while you want the bot to run.

## Files and data

| File | Purpose |
|---|---|
| `bot_v6.py` | Main source code |
| `.env` | Telegram bot token and recipient IDs; keep it private |
| `accounts.txt` | Instagram usernames to check |
| `data.json` | Story fingerprints/history recorded for each recipient |
| `data.json.legacy-backup` | Backup created during legacy history migration, if needed |
| `log.txt` | Runtime logs and errors |

Do not delete `data.json` if you want to preserve duplicate-prevention history. Back it up before editing it or moving the project to another computer.

## Check interval

The current code uses these settings:

- `ACCOUNT_DELAY = 3`: wait 3 seconds between accounts.
- `CHECK_INTERVAL = 5600`: wait 5,600 seconds (about 93 minutes and 20 seconds) between check cycles.

You can adjust these values near the top of `bot_v6.py`. Avoid setting an excessively short interval, as the website may restrict access.

## Duplicate prevention and limitations

The bot stores fingerprints in `data.json` for each Telegram recipient. It records fingerprints before sending to prioritize avoiding duplicate notifications.

**Trade-off:** If a Story is recorded but Telegram delivery fails, or the program stops immediately afterward, that Story may **not be retried automatically** for that recipient. This is intentional in the current implementation to reduce duplicate sends.

Perceptual hashes are approximate comparisons; no fingerprinting method can guarantee perfect duplicate detection in every situation.

## Troubleshooting

- **Missing `TELEGRAM_BOT_TOKEN` or `TELEGRAM_USER_ID`:** Check the variable names in `.env` and make sure the file is in the same directory as `bot_v6.py`.
- **Chrome cannot be found or launched:** Install Google Chrome and try updating Playwright.
- **Telegram messages are not delivered:** Check the bot token and numeric user ID, and make sure the recipient has pressed Start in the bot chat.
- **AnonStories fails to load or its interface changes:** Open the website manually in Chrome. The page selectors in the code may need updating if the website changes.
- **Cloudflare verification appears:** Follow the displayed instructions. Verification may be unavailable or unsuccessful in some environments.
- **Need to inspect errors:** Open `log.txt`.

## Stop the bot

Press `Ctrl + C` in the PowerShell window to stop the program.