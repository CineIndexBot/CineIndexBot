# CineIndexBot

A Telegram movie/series search bot that **never needs a SESSION string**.

Instead of using a Pyrogram user session to call `search_messages()` live, this bot:
1. Indexes every post from your content channels into MongoDB automatically
2. Searches MongoDB when users ask for a movie — fast, no Telegram API calls

## How it works

```
Content channel post → Bot receives it (as admin) → Saved to MongoDB
User types "Movie name" → MongoDB search → Results forwarded
```

## Setup on Railway

### 1. Environment variables

| Variable | Required | Description |
|---|---|---|
| `API_ID` | ✅ | From my.telegram.org |
| `API_HASH` | ✅ | From my.telegram.org |
| `BOT_TOKEN` | ✅ | From @BotFather |
| `OWNER_ID` | ✅ | Your Telegram user ID |
| `LOG_CHANNEL` | ✅ | Channel ID for logs |
| `RESULTS_CHANNEL` | ✅ | Channel where results are forwarded |
| `MONGO_URI` or `MONGODB_PASSWORD` | ✅ | MongoDB connection |
| `PORT` | ❌ | Defaults to 5000 |

**No `SESSION` variable needed!**

### 2. Add bot to content channels

Add the bot as **Admin** (with "Post Messages" read permission) in every channel that has your movie files.

### 3. Connect channels to your group

In your Telegram group, use:
```
/addsource add -100xxxxxxxxxx
```

### 4. Backfill old messages (optional)

To index messages posted before the bot was added:
```bash
SESSION="your_pyrogram_session" python scripts/backfill.py -100xxxxxxxxxx
```
The main bot works without this — it only indexes new posts going forward.

## Commands

| Command | Who | Description |
|---|---|---|
| `/addsource list` | Owner | Show connected channels |
| `/addsource add <id>` | Owner | Add a source channel |
| `/addsource remove <id>` | Owner | Remove a channel |
| `/addsource wipe <id>` | Owner | Remove channel + delete its index |
| `/stats` | Bot owner | Total groups/users/indexed messages |
| `/ping` | Anyone | Check if bot is alive |

## vs CineRequestBot

| | CineRequestBot | CineIndexBot |
|---|---|---|
| Needs SESSION | ✅ Yes (user account) | ❌ No |
| Searches old posts | ✅ All history | ⚠️ Only after setup (+ backfill) |
| AUTH_KEY_DUPLICATED | Possible | Never |
| Search speed | Depends on Telegram API | Fast (MongoDB) |
