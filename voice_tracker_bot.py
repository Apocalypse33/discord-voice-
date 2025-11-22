# NEW CLEAN DISCORD VOICE TRACKER BOT
# Fully rewritten from zero — clean, stable, Railway-ready, minimal, and organized.
# Features:
#  - Track join/leave/move
#  - Track total VC time
#  - Live sessions
#  - Leaderboard
#  - 24/7 stay in voice
#  - Volume-friendly (DATA_DIR)
#  - LOG_CHANNEL_ID optional
#  - Stable async file I/O

import os
import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import discord
from discord.ext import commands

# ==============================
# CONFIG / ENV
# ==============================
BOT_PREFIX = os.getenv("BOT_PREFIX", "!")
DATA_DIR = Path(os.getenv("DATA_DIR", "."))
DATA_DIR.mkdir(exist_ok=True, parents=True)

HISTORY_FILE = DATA_DIR / "voice_history.json"
TOTALS_FILE = DATA_DIR / "user_totals.json"
STAY_FILE = DATA_DIR / "stay.json"

LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
LOG_CHANNEL_ID = int(LOG_CHANNEL_ID) if LOG_CHANNEL_ID and LOG_CHANNEL_ID.isdigit() else None

INTENTS = discord.Intents.default()
INTENTS.voice_states = True
INTENTS.guilds = True
INTENTS.members = True

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=INTENTS)

# ==============================
# GLOBAL DATA
# ==============================
voice_history = []          # list[str]
user_totals = {}            # {str(user_id): seconds}
active_sessions = {}        # {user_id: timestamp}
stay_channels = {}          # {guild_id: channel_id}
_file_lock = asyncio.Lock()

# ==============================
# HELPERS
# ==============================
def now(): return datetime.now(timezone.utc)

def fmt_time(sec):
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"

async def save_json(path, data):
    async with _file_lock:
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

def load_json(path, default):
    if path.exists():
        try:
            with open(path, "r", encoding="utf8") as f:
                return json.load(f)
        except:
            return default
    return default

async def get_log_channel():
    if LOG_CHANNEL_ID is None: return None
    ch = bot.get_channel(LOG_CHANNEL_ID)
    if ch: return ch
    try: return await bot.fetch_channel(LOG_CHANNEL_ID)
    except: return None

# ==============================
# LOAD DATA ON START
# ==============================
@bot.event
async def on_ready():
    global voice_history, user_totals, stay_channels

    voice_history = load_json(HISTORY_FILE, [])
    user_totals = load_json(TOTALS_FILE, {})
    stay_channels = {int(k): int(v) for k, v in load_json(STAY_FILE, {}).items()}

    # Rebuild active sessions for people already in VC
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for m in vc.members:
                if not m.bot:
                    active_sessions[m.id] = now().timestamp()

    print(f"Logged in as {bot.user}")
    ch = await get_log_channel()
    if ch:
        await ch.send("✅ Bot restarted and loaded data.")

    bot.loop.create_task(stay_worker())

# ==============================
# VOICE EVENT HANDLER
# ==============================
@bot.event
async def on_voice_state_update(m, before, after):
    if m.bot:
        return

    t = now().timestamp()
    entry = None

    # JOIN
    if before.channel is None and after.channel is not None:
        active_sessions[m.id] = t
        entry = f"[{now()}] JOIN {m.display_name} → {after.channel.name}"

    # LEAVE
    elif before.channel is not None and after.channel is None:
        start = active_sessions.pop(m.id, None)
        dur = 0
        if start:
            dur = int(t - start)
            user_totals[str(m.id)] = user_totals.get(str(m.id), 0) + dur
        entry = f"[{now()}] LEAVE {m.display_name} ← {before.channel.name} ({fmt_time(dur)})"

    # MOVE
    elif (before.channel is not None and after.channel is not None and
          before.channel.id != after.channel.id):
        start = active_sessions.get(m.id)
        dur = 0
        if start:
            dur = int(t - start)
            user_totals[str(m.id)] = user_totals.get(str(m.id), 0) + dur
        active_sessions[m.id] = t
        entry = (f"[{now()}] MOVE {m.display_name}: {before.channel.name} → {after.channel.name}"
                 f" ({fmt_time(dur)})")

    if entry:
        voice_history.append(entry)
        voice_history[:] = voice_history[-800:]  # anti-bloat
        await save_json(HISTORY_FILE, voice_history)
        await save_json(TOTALS_FILE, user_totals)
        print(entry)

# ==============================
# COMMANDS
# ==============================
@bot.command()
async def vchistory(ctx, n: int = 10):
    n = max(1, min(50, n))
    if not voice_history:
        return await ctx.send("No history.")
    logs = "\n".join(voice_history[-n:])
    await ctx.send(f"```\n{logs}\n```")

@bot.command()
async def vcstats(ctx, member: discord.Member = None):
    member = member or ctx.author
    total = user_totals.get(str(member.id), 0)
    if member.id in active_sessions:
        total += int(now().timestamp() - active_sessions[member.id])
    await ctx.send(f"**{member.display_name}** has {fmt_time(total)} in voice.")

@bot.command()
async def vcleaderboard(ctx, n: int = 10):
    n = max(1, min(25, n))
    data = {}
    for uid, sec in user_totals.items():
        uid_int = int(uid)
        total = sec
        if uid_int in active_sessions:
            total += int(now().timestamp() - active_sessions[uid_int])
        data[uid_int] = total

    if not data:
        return await ctx.send("No data.")

    ranked = sorted(data.items(), key=lambda x: x[1], reverse=True)[:n]

    msg = []
    r = 1
    for uid, sec in ranked:
        user = ctx.guild.get_member(uid) or bot.get_user(uid)
        name = user.display_name if isinstance(user, discord.Member) else (user.name if user else uid)
        live = " 🔴" if uid in active_sessions else ""
        msg.append(f"#{r} **{name}** — {fmt_time(sec)}{live}")
        r += 1

    await ctx.send("\n".join(msg))

# ==============================
# 24/7 STAY
# ==============================
async def stay_worker():
    await bot.wait_until_ready()
    print("Stay worker started")

    while True:
        for guild_id, channel_id in list(stay_channels.items()):
            guild = bot.get_guild(guild_id)
            if not guild:
                stay_channels.pop(guild_id, None)
                await save_json(STAY_FILE, stay_channels)
                continue
            ch = guild.get_channel(channel_id)
            if not ch:
                stay_channels.pop(guild_id, None)
                await save_json(STAY_FILE, stay_channels)
                continue

            vc = discord.utils.get(bot.voice_clients, guild=guild)
            if vc and vc.channel.id == channel_id:
                continue
            try:
                if vc:
                    await vc.move_to(ch)
                else:
                    await ch.connect()
            except Exception as e:
                print("Stay connect fail:", e)

        await asyncio.sleep(30)

@bot.command()
@commands.has_permissions(manage_guild=True)
async def setstay(ctx, channel: discord.VoiceChannel):
    stay_channels[ctx.guild.id] = channel.id
    await save_json(STAY_FILE, stay_channels)
    await ctx.send(f"Bot will stay in **{channel.name}** 24/7.")

@bot.command()
@commands.has_permissions(manage_guild=True)
async def unstay(ctx):
    stay_channels.pop(ctx.guild.id, None)
    await save_json(STAY_FILE, stay_channels)
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if vc:
        await vc.disconnect()
    await ctx.send("Bot will no longer stay in voice.")

# ==============================
# RUN
# ==============================
token = os.getenv("DISCORD_TOKEN")
if not token:
    raise SystemExit("ERROR: DISCORD_TOKEN not set.")

bot.run(token)
