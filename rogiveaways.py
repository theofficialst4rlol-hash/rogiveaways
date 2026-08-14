#!/usr/bin/env python3
"""
RoGiveaways Bot — a simple, self-hosted Discord giveaway bot.

Commands (prefix "!" by default, all also available as slash commands):
    gstart  <duration> <winners> <prize>   Start a giveaway
    gend    [message_id]                   End a giveaway early
    greroll [message_id]                   Pick new winners for an ended giveaway
    gdelete [message_id]                   Remove a giveaway entirely
    glist                                  List active giveaways in this server
    ghelp                                  Show this help

Setup:
    1.  pip install -r requirements.txt
    2.  Put your bot token in config.json (or set the DISCORD_TOKEN env var)
    3.  Enable the "Message Content Intent" in the Discord Developer Portal
    4.  python rogiveaways.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import sqlite3
from pathlib import Path

import discord
from discord.ext import commands, tasks

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_FILE = Path(__file__).with_name("config.json")

DEFAULTS = {
    "token": "",
    "bot_name": "RoGiveaways Bot",
    "prefix": "!",
    "react_emoji": "🎉",
    "embed_color": 0x5865F2,       # Discord blurple
    "db_file": "giveaways.db",
    "allowed_roles": [],           # role IDs that may run giveaway commands (empty = Manage Server+)
    "check_interval": 30,          # seconds between auto-end checks
    "announcement_channel": None,  # optional channel ID for winner announcements
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError) as exc:
            raise SystemExit(f"Could not read {CONFIG_FILE}: {exc}") from exc
    cfg["token"] = os.environ.get("DISCORD_TOKEN", "").strip() or str(cfg.get("token", "")).strip()
    color = cfg["embed_color"]
    if isinstance(color, str):
        cfg["embed_color"] = int(color, 16)
    return cfg


config = load_config()

if not config["token"]:
    raise SystemExit("No bot token found. Put one in config.json or set the DISCORD_TOKEN env var.")

TOKEN = config["token"]
PREFIX = config["prefix"]
BOT_NAME = config.get("bot_name")
REACT_EMOJI = str(config["react_emoji"])
EMBED_COLOR = int(config["embed_color"])
DB_PATH = Path(__file__).with_name(config["db_file"])
CHECK_INTERVAL = max(5, int(config["check_interval"]))
ALLOWED_ROLES = {int(r) for r in config.get("allowed_roles", [])}
ANNOUNCE_CHANNEL = int(config["announcement_channel"]) if config.get("announcement_channel") else None

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS giveaways (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                guild_id   INTEGER NOT NULL,
                prize      TEXT    NOT NULL,
                winners    INTEGER NOT NULL,
                ends_at    TEXT    NOT NULL,
                hosted_by  INTEGER NOT NULL,
                ended      INTEGER NOT NULL DEFAULT 0,
                winner_ids TEXT    NOT NULL DEFAULT '[]'
            )
            """
        )


def insert_giveaway(message_id, channel_id, guild_id, prize, winners, ends_at, hosted_by) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO giveaways (message_id, channel_id, guild_id, prize, winners, ends_at, hosted_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, channel_id, guild_id, prize, winners, ends_at.isoformat(), hosted_by),
        )


def get_giveaway(message_id: int):
    with get_db() as conn:
        return conn.execute("SELECT * FROM giveaways WHERE message_id = ?", (message_id,)).fetchone()


def set_ended(message_id: int, winner_ids: list) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE giveaways SET ended = 1, winner_ids = ? WHERE message_id = ?",
            (json.dumps(winner_ids), message_id),
        )


def active_giveaways(guild_id: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM giveaways WHERE guild_id = ? AND ended = 0 ORDER BY ends_at",
            (guild_id,),
        ).fetchall()


def pending_giveaways():
    with get_db() as conn:
        return conn.execute("SELECT * FROM giveaways WHERE ended = 0").fetchall()


def delete_giveaway(message_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM giveaways WHERE message_id = ?", (message_id,))


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
DURATION_RE = re.compile(r"(\d+)\s*(s|m|h|d|w)", re.IGNORECASE)


def parse_duration(text: str) -> int | None:
    """Parse durations like '2d', '1h30m', '45m', '90s' into seconds."""
    seconds = sum(
        int(value) * DURATION_UNITS[unit.lower()] for value, unit in DURATION_RE.findall(text)
    )
    leftover = DURATION_RE.sub("", text).strip()
    return seconds if seconds > 0 and not leftover else None


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None,
    activity=discord.Game(name=f"{PREFIX}ghelp | Giveaways"),
)


def can_manage_giveaways():
    """Allow users with an allowed role, or with Manage Server/Administrator."""
    async def predicate(ctx: commands.Context) -> bool:
        if ALLOWED_ROLES:
            return any(role.id in ALLOWED_ROLES for role in getattr(ctx.author, "roles", []))
        perms = getattr(ctx.author, "guild_permissions", None)
        return bool(perms and (perms.manage_guild or perms.administrator))
    return commands.check(predicate)


async def get_entrants(message: discord.Message) -> list[discord.User]:
    """All non-bot users who reacted with the giveaway emoji."""
    reaction = next((r for r in message.reactions if str(r.emoji) == REACT_EMOJI), None)
    if reaction is None:
        return []
    return [user async for user in reaction.users() if not user.bot]


async def resolve_message_id(ctx: commands.Context, message_id: int | None) -> int | None:
    """Use the provided ID, or the ID of the message the command is replying to."""
    if message_id is not None:
        return message_id
    reference = getattr(getattr(ctx, "message", None), "reference", None)
    if reference is not None and reference.message_id is not None:
        return reference.message_id
    return None


async def end_giveaway(message_id: int) -> None:
    row = get_giveaway(message_id)
    if row is None or row["ended"]:
        return

    channel = bot.get_channel(row["channel_id"])
    message = None
    if channel is not None:
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

    entrants: list[discord.User] = await get_entrants(message) if message is not None else []
    count = min(row["winners"], len(entrants))
    winners = random.sample(entrants, count) if count > 0 else []
    set_ended(message_id, [w.id for w in winners])

    if message is not None:
        winner_text = ", ".join(w.mention for w in winners) if winners else "No one — not enough entries."
        embed = discord.Embed(
            title=f"🎉 {row['prize']}",
            description=(
                f"**Ended**\n\n"
                f"**Winner(s):** {winner_text}\n"
                f"👥 Entries: **{len(entrants)}**\n"
                f"👑 Hosted by: <@{row['hosted_by']}>"
            ),
            color=EMBED_COLOR,
        )
        embed.set_footer(text=f"Giveaway ID: {message_id}")
        try:
            await message.edit(content=None, embed=embed)
        except discord.HTTPException:
            pass

        announce = bot.get_channel(ANNOUNCE_CHANNEL) if ANNOUNCE_CHANNEL else None
        if announce is None:
            announce = channel
        try:
            if winners:
                await announce.send(
                    f"🎉 **{row['prize']}** — congratulations {', '.join(w.mention for w in winners)}!\n"
                    f"They won from **{len(entrants)}** entrant(s). (ID: `{message_id}`)"
                )
            else:
                await announce.send(
                    f"Giveaway **{row['prize']}** ended with no winner — not enough entries. (ID: `{message_id}`)"
                )
        except discord.HTTPException:
            pass


async def reroll_giveaway(row) -> list[discord.User]:
    channel = bot.get_channel(row["channel_id"])
    if channel is None:
        raise LookupError("channel")
    try:
        message = await channel.fetch_message(row["message_id"])
    except discord.NotFound:
        raise LookupError("message")

    entrants = await get_entrants(message)
    previous_ids = set(json.loads(row["winner_ids"]))
    eligible = [u for u in entrants if u.id not in previous_ids]
    count = min(row["winners"], len(eligible))
    new_winners = random.sample(eligible, count) if count > 0 else []
    set_ended(row["message_id"], sorted(previous_ids | {u.id for u in new_winners}))
    return new_winners


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.hybrid_command(
    name="gstart",
    aliases=["giveaway"],
    description="Start a giveaway: gstart <duration> <winners> <prize>",
)
@can_manage_giveaways()
async def gstart(ctx: commands.Context, duration: str, winners: int, *, prize: str):
    await ctx.defer()
    if ctx.guild is None:
        await ctx.send("Giveaways must be started inside a server channel.")
        return

    seconds = parse_duration(duration)
    if seconds is None:
        await ctx.send("Invalid duration. Use formats like `2d`, `1h30m`, `45m`, `90s`.")
        return
    if seconds < 10:
        await ctx.send("Giveaways must last at least 10 seconds.")
        return
    if not 1 <= winners <= 100:
        await ctx.send("Winner count must be between 1 and 100.")
        return
    if len(prize) > 256:
        await ctx.send("The prize description is too long (max 256 characters).")
        return

    ends_at = discord.utils.utcnow() + dt.timedelta(seconds=seconds)

    embed = discord.Embed(
        title=f"🎉 {prize}",
        description=(
            f"**React with {REACT_EMOJI} to enter!**\n\n"
            f"👥 Winners: **{winners}**\n"
            f"⏰ Ends: {discord.utils.format_dt(ends_at, style='R')} ({discord.utils.format_dt(ends_at, style='f')})\n"
            f"👑 Hosted by: {ctx.author.mention}"
        ),
        color=EMBED_COLOR,
    )

    message = await ctx.send(embed=embed)
    try:
        await message.add_reaction(REACT_EMOJI)
    except discord.HTTPException:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        await ctx.send("Couldn't add the entry reaction — check the emoji is usable in this server. Giveaway cancelled.")
        return

    embed.set_footer(text=f"Giveaway ID: {message.id}")
    await message.edit(embed=embed)
    insert_giveaway(message.id, ctx.channel.id, ctx.guild.id, prize, winners, ends_at, ctx.author.id)
    await ctx.send(
        f"✅ Giveaway started! **{prize}** with **{winners}** winner(s) — "
        f"ends {discord.utils.format_dt(ends_at, style='R')}."
    )


@bot.hybrid_command(
    name="gend",
    description="End a giveaway early: gend [message_id] (or reply to the giveaway)",
)
@can_manage_giveaways()
async def gend(ctx: commands.Context, message_id: int | None = None):
    await ctx.defer()
    target = await resolve_message_id(ctx, message_id)
    if target is None:
        await ctx.send("Reply to the giveaway message or pass its message ID.")
        return
    row = get_giveaway(target)
    if row is None:
        await ctx.send("That message is not a tracked giveaway.")
        return
    if row["ended"]:
        await ctx.send("That giveaway has already ended.")
        return
    await end_giveaway(target)
    await ctx.send("✅ Giveaway ended early.")


@bot.hybrid_command(
    name="greroll",
    description="Reroll winners of an ended giveaway: greroll [message_id]",
)
@can_manage_giveaways()
async def greroll(ctx: commands.Context, message_id: int | None = None):
    await ctx.defer()
    target = await resolve_message_id(ctx, message_id)
    if target is None:
        await ctx.send("Reply to the giveaway message or pass its message ID.")
        return
    row = get_giveaway(target)
    if row is None:
        await ctx.send("That message is not a tracked giveaway.")
        return
    if not row["ended"]:
        await ctx.send("That giveaway hasn't ended yet — use `gend` or wait for it to finish.")
        return
    try:
        new_winners = await reroll_giveaway(row)
    except LookupError as exc:
        await ctx.send("Giveaway channel is gone." if str(exc) == "channel" else "The giveaway message no longer exists.")
        return
    if new_winners:
        line = ", ".join(w.mention for w in new_winners)
        await ctx.send(f"🎉 **Reroll complete!** New winner(s) for **{row['prize']}**: {line}")
        channel = bot.get_channel(row["channel_id"])
        if channel is not None:
            try:
                await channel.send(f"🎉 **Reroll!** {line} — you won **{row['prize']}**!")
            except discord.HTTPException:
                pass
    else:
        await ctx.send("No eligible entrants left to reroll.")


@bot.hybrid_command(
    name="gdelete",
    description="Delete a giveaway entirely: gdelete [message_id]",
)
@can_manage_giveaways()
async def gdelete(ctx: commands.Context, message_id: int | None = None):
    await ctx.defer()
    target = await resolve_message_id(ctx, message_id)
    if target is None:
        await ctx.send("Reply to the giveaway message or pass its message ID.")
        return
    row = get_giveaway(target)
    if row is None:
        await ctx.send("That message is not a tracked giveaway.")
        return
    delete_giveaway(target)
    channel = bot.get_channel(row["channel_id"])
    if channel is not None:
        try:
            msg = await channel.fetch_message(target)
            await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    await ctx.send("🗑️ Giveaway deleted.")


@bot.hybrid_command(name="glist", description="List active giveaways in this server")
@can_manage_giveaways()
async def glist(ctx: commands.Context):
    await ctx.defer()
    if ctx.guild is None:
        await ctx.send("Run this in a server.")
        return
    rows = active_giveaways(ctx.guild.id)
    if not rows:
        await ctx.send("No active giveaways in this server.")
        return
    lines = [
        f"`{row['message_id']}` • **{row['prize']}** • {row['winners']} winner(s) • "
        f"ends <t:{int(dt.datetime.fromisoformat(row['ends_at']).timestamp())}:R>"
        for row in rows
    ]
    await ctx.send("**Active giveaways:**\n" + "\n".join(lines))


@bot.hybrid_command(name="ghelp", aliases=["help"], description="Show RoGiveaways command help")
async def ghelp(ctx: commands.Context):
    embed = discord.Embed(title="🎉 RoGiveaways Bot — Help", color=EMBED_COLOR)
    embed.add_field(
        name=f"{PREFIX}gstart <duration> <winners> <prize>",
        value="Start a giveaway. Example: `!gstart 2h 1 Nitro Classic`.\n"
              "Durations: `s`, `m`, `h`, `d`, `w` — combinable (`1d12h`).",
        inline=False,
    )
    embed.add_field(
        name=f"{PREFIX}gend [message_id]",
        value="End a giveaway early. Reply to the giveaway message or pass its ID.",
        inline=False,
    )
    embed.add_field(
        name=f"{PREFIX}greroll [message_id]",
        value="Reroll an ended giveaway, excluding previous winners.",
        inline=False,
    )
    embed.add_field(
        name=f"{PREFIX}gdelete [message_id]",
        value="Delete a giveaway and remove it from tracking.",
        inline=False,
    )
    embed.add_field(
        name=f"{PREFIX}glist",
        value="List all active giveaways in this server.",
        inline=False,
    )
    embed.set_footer(text="Every command also works as a slash command.")
    await ctx.send(embed=embed)


@bot.command(name="sync")
@commands.is_owner()
async def sync(ctx: commands.Context):
    synced = await bot.tree.sync()
    await ctx.send(f"Synced {len(synced)} slash command(s).")


# ---------------------------------------------------------------------------
# Events & background task
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Serving {len(bot.guilds)} guild(s). Prefix: {PREFIX}")
    if BOT_NAME and bot.user is not None and bot.user.name != BOT_NAME:
        try:
            await bot.user.edit(username=BOT_NAME)
            print(f"Renamed bot to {BOT_NAME}")
        except discord.HTTPException as exc:
            print(f"Could not rename bot to {BOT_NAME}: {exc} (Discord limits renames to twice per hour)")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except discord.HTTPException as exc:
        print(f"Slash command sync failed: {exc}")
    check_giveaways.start()


@tasks.loop(seconds=CHECK_INTERVAL)
async def check_giveaways():
    now = discord.utils.utcnow()
    for row in pending_giveaways():
        if dt.datetime.fromisoformat(row["ends_at"]) <= now:
            await end_giveaway(row["message_id"])


@check_giveaways.before_loop
async def before_check_giveaways():
    await bot.wait_until_ready()


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        # Prefix invocations get a plain message; slash invocations can use ephemeral replies.
        if ctx.interaction is not None:
            await ctx.send("You don't have permission to use this command.", ephemeral=True)
        else:
            await ctx.send("You don't have permission to use this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing required argument. See `{PREFIX}ghelp`.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"Invalid arguments. See `{PREFIX}ghelp`.")
    else:
        print(f"Unhandled error in {ctx.command}: {type(error).__name__}: {error}")


if __name__ == "__main__":
    bot.run(TOKEN)
