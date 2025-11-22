# voice_tracker_bot.py
"""
Clean, complete Discord voice tracker bot.

Environment variables:
  DISCORD_TOKEN   (required) - your bot token
  DATA_DIR        (optional) - path for JSON files (default: current directory)
  LOG_CHANNEL_ID  (optional) - channel ID for embed logs
  BOT_PREFIX      (optional) - default: "!"
  STAY_CHECK_INTERVAL (optional) - seconds (default: 30)

IMPORTANT:
- Enable privileged intents in Discord Developer Portal:
  * SERVER MEMBERS INTENT
  * MESSAGE CONTENT INTENT
- Restart the bot after toggling intents.
"""

import os
import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import traceback
import discord
from discord.ext import commands
from typing import Optional, Dict

# -------------------- Configuration --------------------
BOT_PREFIX = os.getenv("BOT_PREFIX", "!")
DATA_DIR = Path(os.getenv("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)

HISTORY_FILE = DATA_DIR / "voice_history.json"
TOTALS_FILE = DATA_DIR / "user_totals.json"
STAY_FILE = DATA_DIR / "persistent_stays.json"

LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
LOG_CHANNEL_ID = int(LOG_CHANNEL_ID) if LOG_CHANNEL_ID and LOG_CHANNEL_ID.isdigit() else None

STAY_CHECK_INTERVAL = int(os.getenv("STAY_CHECK_INTERVAL", "30"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "2000"))

# -------------------- Intents --------------------
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.members = True            # privileged: enable in Developer Portal
intents.message_content = True    # privileged: enable in Developer Portal

# -------------------- Bot --------------------
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

# -------------------- Data structures --------------------
_file_lock = asyncio.Lock()
voice_history: list = []                       # human-readable lines
user_totals: Dict[str, int] = {}               # {str(user_id): total_seconds}
user_sessions: Dict[int, float] = {}           # {user_id: start_timestamp}
persistent_stays: Dict[int, int] = {}          # {guild_id: channel_id}

# -------------------- Helpers --------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def ts() -> str:
    return now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")

def fmt_duration(sec: int) -> str:
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

async def safe_write_json(path: Path, data) -> None:
    async with _file_lock:
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(path))

def safe_read_json(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        print(f"[WARN] Failed to read {path}; using default.")
    return default

async def persist_all():
    await asyncio.gather(
        safe_write_json(HISTORY_FILE, voice_history),
        safe_write_json(TOTALS_FILE, user_totals),
        safe_write_json(STAY_FILE, {str(k): v for k, v in persistent_stays.items()}),
    )

async def get_log_channel() -> Optional[discord.TextChannel]:
    if not LOG_CHANNEL_ID:
        return None
    ch = bot.get_channel(LOG_CHANNEL_ID)
    if ch:
        return ch
    try:
        return await bot.fetch_channel(LOG_CHANNEL_ID)
    except Exception:
        return None

async def send_embed_log(member: discord.Member, action: str, color: discord.Color, description: str):
    ch = await get_log_channel()
    if not ch:
        return
    try:
        embed = discord.Embed(title=f"🎧 Voice Update: {action}", description=description, color=color, timestamp=now_utc())
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url if member.display_avatar else None)
        embed.set_thumbnail(url=member.display_avatar.url if member.display_avatar else None)
        await ch.send(embed=embed)
    except Exception as e:
        print("[WARN] failed to send embed log:", e)

def record_session_end(user_id: int, start_ts: float, end_ts: float) -> int:
    dur = int(end_ts - start_ts)
    key = str(user_id)
    user_totals[key] = user_totals.get(key, 0) + dur
    return dur

# -------------------- Startup --------------------
@bot.event
async def on_ready():
    global voice_history, user_totals, persistent_stays

    # load persisted files
    voice_history = safe_read_json(HISTORY_FILE, [])
    user_totals = safe_read_json(TOTALS_FILE, {})
    raw = safe_read_json(STAY_FILE, {})
    if isinstance(raw, dict):
        persistent_stays = {int(k): int(v) for k, v in raw.items()}
    else:
        persistent_stays = {}

    print(f"✅ Logged in as {bot.user} ({bot.user.id}) - {ts()}")
    print(f"Loaded: {len(voice_history)} history lines, {len(user_totals)} totals, {len(persistent_stays)} stays")

    # Rebuild active sessions from current voice states if we have members intent
    if intents.members:
        for g in bot.guilds:
            for vc in g.voice_channels:
                for m in vc.members:
                    if not m.bot and m.id not in user_sessions:
                        user_sessions[m.id] = datetime.now(timezone.utc).timestamp()

    # Start the stay background worker
    bot.loop.create_task(stay_worker())

    # announce startup to log channel
    ch = await get_log_channel()
    if ch:
        try:
            await ch.send(f"✅ Bot started ({bot.user}) — active sessions: {len(user_sessions)}")
        except Exception:
            pass

# -------------------- Voice state handling --------------------
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # ignore bots
    if member.bot:
        return

    now_ts = datetime.now(timezone.utc).timestamp()
    action = None
    desc = ""
    color = discord.Color.blue()
    log_line = None

    # joined
    if before.channel is None and after.channel is not None:
        user_sessions[member.id] = now_ts
        action = "Joined"
        desc = f"🔊 **{member.mention}** joined **{after.channel.name}**"
        color = discord.Color.green()
        log_line = f"[{ts()}] JOIN {member.display_name} -> {after.channel.name}"

    # left
    elif before.channel is not None and after.channel is None:
        start = user_sessions.pop(member.id, None)
        dur_text = ""
        if start:
            dur = record_session_end(member.id, start, now_ts)
            dur_text = f" (Stayed: {fmt_duration(dur)})"
        action = "Left"
        desc = f"❌ **{member.mention}** left **{before.channel.name}**{dur_text}"
        color = discord.Color.red()
        log_line = f"[{ts()}] LEAVE {member.display_name} <- {before.channel.name}{dur_text}"

    # moved channel
    elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
        start = user_sessions.get(member.id)
        dur_text = ""
        if start:
            dur = record_session_end(member.id, start, now_ts)
            dur_text = f" (Stayed in {before.channel.name}: {fmt_duration(dur)})"
        user_sessions[member.id] = now_ts
        action = "Moved"
        desc = f"➡️ **{member.mention}** moved from **{before.channel.name}** → **{after.channel.name}**{dur_text}"
        color = discord.Color.orange()
        log_line = f"[{ts()}] MOVE {member.display_name}: {before.channel.name} -> {after.channel.name}{dur_text}"

    # if we made a log event, persist + send to log channel
    if log_line:
        voice_history.append(log_line)
        # trim
        if len(voice_history) > MAX_HISTORY:
            voice_history[:] = voice_history[-MAX_HISTORY:]
        try:
            await persist_all()
        except Exception as e:
            print("[WARN] persist failed:", e)
        print(log_line)
        await send_embed_log(member, action or "Voice", color, desc)

# -------------------- Commands --------------------
@bot.event
async def on_message(message: discord.Message):
    # ignore DMs & bots
    if message.author.bot:
        return
    await bot.process_commands(message)

@bot.command(name="ping")
async def ping_cmd(ctx: commands.Context):
    await ctx.send("pong")

@bot.command(name="vchistory")
async def vchistory_cmd(ctx: commands.Context, limit: int = 10):
    limit = max(1, min(limit, 50))
    if not voice_history:
        return await ctx.send("No voice history yet.")
    logs = "\n".join(voice_history[-limit:])
    # send as code block if long
    if len(logs) > 1800:
        await ctx.send(f"```{logs[:1900]}```")
    else:
        await ctx.send(f"```{logs}```")

@bot.command(name="vcstats")
async def vcstats_cmd(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    total = int(user_totals.get(str(member.id), 0))
    if member.id in user_sessions:
        total += int(datetime.now(timezone.utc).timestamp() - user_sessions[member.id])
    await ctx.send(f"**{member.display_name}** total VC time: **{fmt_duration(total)}**")

@bot.command(name="vcleaderboard", aliases=["vcleaders", "vctop"])
async def vcleaderboard_cmd(ctx: commands.Context, top: int = 10):
    top = max(1, min(top, 25))
    combined: Dict[int, int] = {}
    # start with stored totals
    for uid_str, secs in user_totals.items():
        try:
            uid = int(uid_str)
        except:
            continue
        combined[uid] = int(secs)
    # add live sessions
    for uid, start_ts in user_sessions.items():
        combined[uid] = combined.get(uid, 0) + int(datetime.now(timezone.utc).timestamp() - start_ts)
    if not combined:
        return await ctx.send("No voice time recorded yet.")
    items = sorted(combined.items(), key=lambda x: x[1], reverse=True)[:top]
    lines = []
    rank = 1
    for uid, secs in items:
        user = None
        display = str(uid)
        try:
            if ctx.guild:
                user = ctx.guild.get_member(uid)
            if not user:
                user = bot.get_user(uid)
            if user:
                display = user.display_name if isinstance(user, discord.Member) else f"{user.name}#{user.discriminator}"
        except Exception:
            pass
        live_marker = " 🔴" if uid in user_sessions else ""
        lines.append(f"#{rank} • **{display}** — {fmt_duration(secs)}{live_marker}")
        rank += 1
    await ctx.send("\n".join(lines))

@bot.command(name="forcejoin")
@commands.has_permissions(manage_guild=True)
async def forcejoin_cmd(ctx: commands.Context, channel_id: int):
    guild = ctx.guild
    ch = guild.get_channel(channel_id) if guild else None
    if ch is None:
        return await ctx.send(f"Channel ID {channel_id} not found in this guild.")
    try:
        vc = discord.utils.get(bot.voice_clients, guild=guild)
        if vc and vc.is_connected():
            await vc.move_to(ch)
        else:
            await ch.connect()
        await ctx.send(f"✅ Joined {ch.name}")
    except discord.Forbidden:
        await ctx.send("❌ Forbidden: check Connect/Speak permissions for me.")
    except Exception as e:
        await ctx.send(f"❌ Failed to join: {e}")
        print("forcejoin error:", traceback.format_exc())

# 24/7 stay commands
@bot.command(name="stayvc")
@commands.has_permissions(connect=True)
async def stayvc_cmd(ctx: commands.Context):
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send("You must be in a voice channel. Join a channel and run this command.")
    channel = ctx.author.voice.channel
    persistent_stays[ctx.guild.id] = channel.id
    try:
        await safe_write_json(STAY_FILE, {str(k): v for k, v in persistent_stays.items()})
    except Exception:
        pass
    # try immediate join
    try:
        vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
        if vc and vc.is_connected():
            await vc.move_to(channel)
        else:
            await channel.connect()
        await ctx.send(f"✅ I will stay in **{channel.name}** 24/7 for this server.")
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to join that voice channel (Connect/Speak).")
    except Exception as e:
        await ctx.send(f"⚠️ I failed to join right now: {e}")

@bot.command(name="setstayvc")
@commands.has_permissions(manage_guild=True)
async def setstayvc_cmd(ctx: commands.Context, channel: discord.VoiceChannel):
    persistent_stays[ctx.guild.id] = channel.id
    try:
        await safe_write_json(STAY_FILE, {str(k): v for k, v in persistent_stays.items()})
    except Exception:
        pass
    # try immediate join
    try:
        vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
        if vc and vc.is_connected():
            await vc.move_to(channel)
        else:
            await channel.connect()
        await ctx.send(f"✅ Persistent stay set to **{channel.name}**")
    except discord.Forbidden:
        await ctx.send("❌ I lack permissions to join (Connect/Speak).")
    except Exception as e:
        await ctx.send(f"⚠️ Failed to join: {e}")

@bot.command(name="unstayvc")
@commands.has_permissions(manage_guild=True)
async def unstayvc_cmd(ctx: commands.Context):
    persistent_stays.pop(ctx.guild.id, None)
    try:
        await safe_write_json(STAY_FILE, {str(k): v for k, v in persistent_stays.items()})
    except Exception:
        pass
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if vc and vc.is_connected():
        try:
            await vc.disconnect()
        except Exception:
            pass
    await ctx.send("✅ Persistent stay removed for this server.")

@bot.command(name="staystatus")
async def staystatus_cmd(ctx: commands.Context):
    guild_id = ctx.guild.id
    if guild_id not in persistent_stays:
        return await ctx.send("No persistent stay set. Use `!stayvc` or `!setstayvc`.")
    ch = ctx.guild.get_channel(persistent_stays[guild_id])
    name = ch.name if ch else f"<deleted:{persistent_stays[guild_id]}>"
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    connected = bool(vc and vc.is_connected() and vc.channel.id == persistent_stays[guild_id])
    await ctx.send(f"📌 Staying in **{name}** — connected: {connected}")

# -------------------- Background stay worker --------------------
async def stay_worker():
    await bot.wait_until_ready()
    print("Stay worker running.")
    while True:
        try:
            for guild_id, channel_id in list(persistent_stays.items()):
                guild = bot.get_guild(guild_id)
                if not guild:
                    persistent_stays.pop(guild_id, None)
                    await safe_write_json(STAY_FILE, {str(k): v for k, v in persistent_stays.items()})
                    continue
                channel = guild.get_channel(channel_id)
                if not channel:
                    persistent_stays.pop(guild_id, None)
                    await safe_write_json(STAY_FILE, {str(k): v for k, v in persistent_stays.items()})
                    continue
                vc = discord.utils.get(bot.voice_clients, guild=guild)
                if vc and vc.is_connected() and vc.channel.id == channel_id:
                    continue
                try:
                    if vc and vc.is_connected():
                        await vc.move_to(channel)
                    else:
                        await channel.connect(reconnect=True)
                except discord.Forbidden:
                    print(f"[WARN] Forbidden to connect to channel {channel.name} ({channel.id}) in guild {guild.name}")
                except Exception as e:
                    print("[WARN] stay connect failed:", e)
            await asyncio.sleep(STAY_CHECK_INTERVAL)
        except Exception as e:
            print("[ERROR] stay_worker crashed:", e)
            traceback.print_exc()
            await asyncio.sleep(10)

# -------------------- Run --------------------
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("ERROR: DISCORD_TOKEN not set. Set this environment variable and restart the bot.")
        raise SystemExit(1)
    try:
        bot.run(token)
    except discord.errors.PrivilegedIntentsRequired:
        print("ERROR: Privileged intents required. Enable SERVER MEMBERS and MESSAGE CONTENT intents in Developer Portal.")
        raise
    except Exception as e:
        print("Bot crashed:", e)
        traceback.print_exc()
        raise
