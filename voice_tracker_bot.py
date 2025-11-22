# voice_tracker_bot.py
# Discord bot: tracks voice channel joins/leaves/moves, leaderboards, and 24/7 stay
# Deployment-ready: reads DATA_DIR and LOG_CHANNEL_ID from environment (Railway friendly)

import os
import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import discord
from discord.ext import commands
from typing import Optional

# -------------------- Configuration (env-aware) --------------------
BOT_PREFIX = os.getenv("BOT_PREFIX", "!")
DATA_DIR = Path(os.getenv("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = str(DATA_DIR / "voice_history.json")
TOTALS_FILE = str(DATA_DIR / "user_totals.json")
PERSIST_STAYS_FILE = str(DATA_DIR / "persistent_stays.json")

# LOG_CHANNEL_ID optional; if not set, the bot will skip logging to channel
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
LOG_CHANNEL_ID = int(LOG_CHANNEL_ID) if LOG_CHANNEL_ID and LOG_CHANNEL_ID.isdigit() else None

INTENTS = discord.Intents.default()
INTENTS.voice_states = True
INTENTS.guilds = True
INTENTS.members = True

# -------------------- Globals --------------------
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=INTENTS)
voice_history: list[str] = []
user_sessions: dict[int, dict] = {}
user_totals: dict[str, int] = {}
persistent_stays: dict[int, int] = {}
_file_lock = asyncio.Lock()
_stay_task: Optional[asyncio.Task] = None
_STAY_CHECK_INTERVAL = int(os.getenv("STAY_CHECK_INTERVAL", "30"))  # seconds
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "1000"))

# -------------------- Time helpers --------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ts_str(dt: Optional[datetime] = None) -> str:
    dt = dt or now_utc()
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}h {mins}m {secs}s"
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"

# -------------------- Safe file I/O --------------------
async def safe_write_json(filename: str, data) -> None:
    async with _file_lock:
        tmp = f"{filename}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, filename)


def safe_read_json(filename: str, default):
    try:
        if Path(filename).exists():
            with open(filename, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"Failed to read {filename}:", e)
    return default

# -------------------- Persistence loaders --------------------

def load_data() -> None:
    global voice_history, user_totals
    voice_history = safe_read_json(HISTORY_FILE, [])
    user_totals = safe_read_json(TOTALS_FILE, {})


def load_persistent_stays() -> None:
    global persistent_stays
    raw = safe_read_json(PERSIST_STAYS_FILE, {})
    if isinstance(raw, dict):
        persistent_stays = {int(k): int(v) for k, v in raw.items()}
    else:
        persistent_stays = {}


async def save_all() -> None:
    await asyncio.gather(
        safe_write_json(HISTORY_FILE, voice_history),
        safe_write_json(TOTALS_FILE, user_totals),
        safe_write_json(PERSIST_STAYS_FILE, {str(k): v for k, v in persistent_stays.items()}),
    )

# -------------------- Logging helper --------------------
async def get_log_channel() -> Optional[discord.TextChannel]:
    if not LOG_CHANNEL_ID:
        return None
    ch = bot.get_channel(LOG_CHANNEL_ID)
    if ch is None:
        try:
            ch = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception:
            ch = None
    return ch


async def send_log(channel: discord.abc.Messageable, member: discord.Member, action: str, color: discord.Color, description: str) -> None:
    try:
        embed = discord.Embed(
            title=f"🎧 Voice Update: {action}",
            description=description,
            color=color,
            timestamp=now_utc(),
        )
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)
        await channel.send(embed=embed)
    except Exception as e:
        print("Failed to send embed log:", e)

# -------------------- Session recording --------------------

def _record_session_completed(member_id: int, start_dt: datetime, end_dt: datetime) -> int:
    dur = int((end_dt - start_dt).total_seconds())
    key = str(member_id)
    user_totals[key] = user_totals.get(key, 0) + dur
    return dur

# -------------------- Bot events --------------------
@bot.event
async def on_ready() -> None:
    global _stay_task
    load_data()
    load_persistent_stays()
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")

    # Resume active sessions for members already in voice channels
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for member in vc.members:
                if member.bot:
                    continue
                if member.id not in user_sessions:
                    user_sessions[member.id] = {"channel_id": vc.id, "start": now_utc()}
                    print(f"[{ts_str()}] resumed session for {member} in {vc.name}")

    # start stay worker
    if _stay_task is None or _stay_task.done():
        _stay_task = bot.loop.create_task(_stay_worker())

    # optional startup log
    ch = await get_log_channel()
    if ch:
        await ch.send(f"✅ Bot started. Active sessions: {len(user_sessions)}. Persistent stays: {len(persistent_stays)}")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    if member.bot:
        return

    action = None
    description = ""
    color = discord.Color.blue()
    log_entry = None
    now = now_utc()

    # joined
    if before.channel is None and after.channel is not None:
        action = "Joined"
        user_sessions[member.id] = {"channel_id": after.channel.id, "start": now}
        description = f"🔊 **{member.mention}** joined **{after.channel.name}**"
        color = discord.Color.green()
        log_entry = f"[{ts_str(now)}] 🔊 {member.display_name} joined {after.channel.name}"

    # left
    elif before.channel is not None and after.channel is None:
        action = "Left"
        session = user_sessions.pop(member.id, None)
        duration_text = ""
        if session and session.get("start"):
            dur = _record_session_completed(member.id, session["start"], now)
            duration_text = f" (Stayed: {format_duration(dur)})"
        description = f"❌ **{member.mention}** left **{before.channel.name}**{duration_text}"
        color = discord.Color.red()
        log_entry = f"[{ts_str(now)}] ❌ {member.display_name} left {before.channel.name}{duration_text}"

    # moved
    elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
        action = "Moved"
        session = user_sessions.get(member.id)
        duration_text = ""
        if session and session.get("start") and session.get("channel_id") == before.channel.id:
            dur = _record_session_completed(member.id, session["start"], now)
            duration_text = f" (Stayed in {before.channel.name}: {format_duration(dur)})"
        user_sessions[member.id] = {"channel_id": after.channel.id, "start": now}
        description = f"➡️ **{member.mention}** moved from **{before.channel.name}** → **{after.channel.name}**{duration_text}"
        color = discord.Color.orange()
        log_entry = f"[{ts_str(now)}] ➡️ {member.display_name} moved from {before.channel.name} to {after.channel.name}{duration_text}"

    if log_entry:
        voice_history.append(log_entry)
        if len(voice_history) > MAX_HISTORY:
            voice_history[:] = voice_history[-MAX_HISTORY:]
        try:
            await save_all()
        except Exception as e:
            print("Failed saving data:", e)
        print(log_entry)
        ch = await get_log_channel()
        if ch:
            await send_log(ch, member, action or "Update", color, description)

# -------------------- Stay worker (keep bot in channel) --------------------
async def _ensure_bot_in_channel(guild: discord.Guild, channel: discord.VoiceChannel) -> bool:
    try:
        vc: Optional[discord.VoiceClient] = discord.utils.get(bot.voice_clients, guild=guild)
        if vc and vc.is_connected():
            if vc.channel.id == channel.id:
                return True
            try:
                await vc.move_to(channel)
                return True
            except Exception as e:
                print(f"Failed to move VC in guild {guild.id}:", e)
        try:
            await channel.connect(reconnect=True, timeout=30)
            return True
        except Exception as e:
            print(f"Failed to connect to channel {channel.id} in guild {guild.id}:", e)
            return False
    except Exception as e:
        print("Unexpected error ensuring bot in channel:", e)
        return False


async def _stay_worker() -> None:
    await bot.wait_until_ready()
    print("Stay worker started.")
    backoff: dict[int, tuple[float, int]] = {}
    loop = asyncio.get_event_loop()
    while not bot.is_closed():
        try:
            now = loop.time()
            for guild_id, channel_id in list(persistent_stays.items()):
                guild = bot.get_guild(guild_id)
                if guild is None:
                    persistent_stays.pop(guild_id, None)
                    continue
                channel = guild.get_channel(channel_id)
                if channel is None:
                    persistent_stays.pop(guild_id, None)
                    ch = await get_log_channel()
                    if ch:
                        await ch.send(f"⛔ Persistent stay channel {channel_id} for guild {guild.name} no longer exists; staying cancelled.")
                    continue
                nb = backoff.get(guild_id)
                if nb and now < nb[0]:
                    continue
                ok = await _ensure_bot_in_channel(guild, channel)
                if ok:
                    backoff.pop(guild_id, None)
                else:
                    prev = backoff.get(guild_id, (0, 10))[1]
                    nxt = min(prev * 2, 600)
                    backoff[guild_id] = (now + nxt, nxt)
            await asyncio.sleep(_STAY_CHECK_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print("Error in stay worker loop:", e)
            await asyncio.sleep(5)
    print("Stay worker exiting.")

# -------------------- Commands --------------------
@bot.command(name="vchistory")
async def vchistory(ctx: commands.Context, limit: int = 10) -> None:
    limit = max(1, min(limit, 50))
    if not voice_history:
        await ctx.send("No voice history yet.")
        return
    logs = voice_history[-limit:]
    desc = "\n".join(logs)
    embed = discord.Embed(title="📜 Voice Channel History", description=desc, color=discord.Color.purple(), timestamp=now_utc())
    await ctx.send(embed=embed)


@bot.command(name="vcstats")
async def vcstats(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    member = member or ctx.author
    total_seconds = int(user_totals.get(str(member.id), 0))
    session = user_sessions.get(member.id)
    if session and session.get("start"):
        total_seconds += int((now_utc() - session["start"]).total_seconds())
    embed = discord.Embed(
        title=f"📊 Voice Channel Stats for {member.display_name}",
        description=f"🕒 Total VC Time: **{format_duration(total_seconds)}**",
        color=discord.Color.gold(),
        timestamp=now_utc(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="vcleaderboard", aliases=["vcleaders", "vctop"])
async def vcleaderboard(ctx: commands.Context, top: int = 10) -> None:
    top = max(1, min(top, 25))
    combined: dict[int, int] = {}
    for mid_str, secs in user_totals.items():
        try:
            mid = int(mid_str)
        except Exception:
            continue
        combined[mid] = int(secs)
    for mid, session in user_sessions.items():
        if session and session.get("start"):
            live = int((now_utc() - session["start"]).total_seconds())
            combined[mid] = combined.get(mid, 0) + live
    if not combined:
        await ctx.send("No voice time recorded yet.")
        return
    items = sorted(combined.items(), key=lambda x: x[1], reverse=True)[:top]
    lines: list[str] = []
    rank = 1
    for mid, secs in items:
        member_obj = None
        if ctx.guild:
            member_obj = ctx.guild.get_member(mid)
        if not member_obj:
            member_obj = bot.get_user(mid)
        if member_obj:
            if isinstance(member_obj, discord.Member):
                mention = member_obj.mention
            else:
                mention = f"`{member_obj.name}#{member_obj.discriminator}`"
        else:
            mention = f"`{mid}`"
        marker = " 🔴" if mid in user_sessions else ""
        lines.append(f"**#{rank}** • {mention} — **{format_duration(int(secs))}**{marker}")
        rank += 1
    desc = "\n".join(lines)
    embed = discord.Embed(title=f"🏆 VC Leaderboard — Top {len(lines)}", description=desc, color=discord.Color.blurple(), timestamp=now_utc())
    embed.set_footer(text="Totals include stored totals plus live session time.")
    await ctx.send(embed=embed)


# ---------- 24/7 stay commands (user & admin) ----------
@bot.command(name="stayvc")
@commands.has_permissions(connect=True)
async def stayvc(ctx: commands.Context) -> None:
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.send("You need to be in a voice channel for me to stay in it. Join one and try again.")
        return
    channel = ctx.author.voice.channel
    guild_id = ctx.guild.id
    persistent_stays[guild_id] = channel.id
    try:
        await safe_write_json(PERSIST_STAYS_FILE, {str(k): v for k, v in persistent_stays.items()})
    except Exception as e:
        print("Failed to save persistent stays:", e)
    ok = await _ensure_bot_in_channel(ctx.guild, channel)
    if ok:
        await ctx.send(f"✅ I will stay in **{channel.name}** 24/7 for this server. Use `!unstayvc` to cancel.")
    else:
        await ctx.send(f"⚠️ I couldn't join **{channel.name}** right now but I'll keep trying. Check my permissions (Connect / Speak).")


@bot.command(name="setstayvc")
@commands.has_permissions(manage_guild=True)
async def setstayvc(ctx: commands.Context, channel: discord.VoiceChannel) -> None:
    if not isinstance(channel, discord.VoiceChannel):
        await ctx.send("Please provide a valid voice channel (mention or ID).")
        return
    guild_id = ctx.guild.id
    persistent_stays[guild_id] = channel.id
    try:
        await safe_write_json(PERSIST_STAYS_FILE, {str(k): v for k, v in persistent_stays.items()})
    except Exception as e:
        print("Failed to save persistent stays:", e)
    ok = await _ensure_bot_in_channel(ctx.guild, channel)
    if ok:
        await ctx.send(f"✅ I will stay in **{channel.name}** 24/7 for this server. Use `!unstayvc` to cancel.")
        ch = await get_log_channel()
        if ch:
            await ch.send(f"📌 Persistent stay set for **{ctx.guild.name}** -> **{channel.name}** by {ctx.author.mention}.")
    else:
        await ctx.send(f"⚠️ I couldn't join **{channel.name}** right now. I saved the channel and will keep trying (check my Connect/Speak permissions).")


@bot.command(name="unstayvc")
@commands.has_permissions(manage_guild=True)
async def unstayvc(ctx: commands.Context) -> None:
    guild_id = ctx.guild.id
    if guild_id not in persistent_stays:
        await ctx.send("I'm not set to stay in a voice channel for this server.")
        return
    persistent_stays.pop(guild_id, None)
    try:
        await safe_write_json(PERSIST_STAYS_FILE, {str(k): v for k, v in persistent_stays.items()})
    except Exception as e:
        print("Failed to save persistent stays:", e)
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if vc and vc.is_connected():
        try:
            await vc.disconnect()
            await ctx.send("✅ I've left the voice channel and will no longer stay here 24/7.")
        except Exception as e:
            await ctx.send("⚠️ Removed stay but failed to disconnect: " + str(e))
    else:
        await ctx.send("✅ Removed stay setting. I wasn't connected anyway.")


@bot.command(name="staystatus")
async def staystatus(ctx: commands.Context) -> None:
    guild_id = ctx.guild.id
    if guild_id in persistent_stays:
        ch = ctx.guild.get_channel(persistent_stays[guild_id])
        name = ch.name if ch else f"<deleted:{persistent_stays[guild_id]}>"
        vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
        connected = bool(vc and vc.is_connected() and vc.channel.id == persistent_stays[guild_id])
        await ctx.send(f"📌 Staying in **{name}** — connected: {connected}")
    else:
        await ctx.send("No persistent stay set for this server. Use `!stayvc` while in a voice channel to enable it.")

# -------------------- Graceful shutdown saving --------------------
async def _shutdown_save() -> None:
    try:
        await save_all()
    except Exception as e:
        print("Failed saving on shutdown:", e)

# -------------------- Run --------------------
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable not set.")
    try:
        bot.run(token)
    finally:
        try:
            asyncio.get_event_loop().run_until_complete(_shutdown_save())
        except Exception:
            pass
