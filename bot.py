import os
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)
KICK_AT = int(os.getenv("KICK_AT", "2") or 2)
DB_PATH = os.getenv("DB_PATH", "warnings.db")
MOD_ROLE_IDS = {
    int(x.strip())
    for x in os.getenv("MOD_ROLE_IDS", "").split(",")
    if x.strip().isdigit()
}

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Put your bot token in .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("warning-bot")


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                reason TEXT NOT NULL,
                moderator_id INTEGER NOT NULL,
                moderator_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revoked_at TEXT,
                revoked_by INTEGER,
                revoke_reason TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_warnings_guild_user "
            "ON warnings(guild_id, user_id, revoked_at)"
        )


def add_warning(guild_id: int, user: discord.Member, reason: str, moderator: discord.Member) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO warnings
            (guild_id, user_id, user_name, reason, moderator_id, moderator_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, user.id, str(user), reason, moderator.id, str(moderator), now),
        )
        return int(cur.lastrowid)


def active_warning_count(guild_id: int, user_id: int) -> int:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM warnings WHERE guild_id = ? AND user_id = ? AND revoked_at IS NULL",
            (guild_id, user_id),
        ).fetchone()
        return int(row["c"])


def list_warnings(guild_id: int, user_id: int, limit: int = 20):
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT * FROM warnings
            WHERE guild_id = ? AND user_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (guild_id, user_id, limit),
        ).fetchall()


def revoke_warning(guild_id: int, warning_id: int, moderator_id: int, reason: str) -> Optional[sqlite3.Row]:
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM warnings WHERE guild_id = ? AND id = ? AND revoked_at IS NULL",
            (guild_id, warning_id),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            """
            UPDATE warnings
            SET revoked_at = ?, revoked_by = ?, revoke_reason = ?
            WHERE guild_id = ? AND id = ?
            """,
            (now, moderator_id, reason, guild_id, warning_id),
        )
        return row


def clear_warnings(guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        cur = conn.execute(
            """
            UPDATE warnings
            SET revoked_at = ?, revoked_by = ?, revoke_reason = ?
            WHERE guild_id = ? AND user_id = ? AND revoked_at IS NULL
            """,
            (now, moderator_id, reason, guild_id, user_id),
        )
        return cur.rowcount


def recent_all_warnings(guild_id: int, limit: int = 25):
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT * FROM warnings
            WHERE guild_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()


def is_moderator(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    member = interaction.user
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    if MOD_ROLE_IDS and any(role.id in MOD_ROLE_IDS for role in member.roles):
        return True
    return False


async def require_mod(interaction: discord.Interaction) -> bool:
    if is_moderator(interaction):
        return True
    await interaction.response.send_message(
        "⛔ 이 명령어는 운영진만 사용할 수 있습니다.", ephemeral=False
    )
    return False


async def send_log(guild: discord.Guild, embed: discord.Embed):
    if not LOG_CHANNEL_ID:
        return
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if channel and hasattr(channel, "send"):
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("Failed to send log message")


class WarningBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        init_db()
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced commands globally; global propagation may take time.")

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)


bot = WarningBot()


@bot.tree.command(name="warn", description="회원에게 경고를 부여합니다.")
@app_commands.describe(member="경고를 받을 회원", reason="경고 사유")
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not await require_mod(interaction):
        return
    if member.bot:
        await interaction.response.send_message("봇 계정에는 경고를 부여하지 않습니다.", ephemeral=False)
        return
    if member.id == interaction.user.id:
        await interaction.response.send_message("자기 자신에게 경고를 부여할 수 없습니다.", ephemeral=False)
        return

    warning_id = add_warning(interaction.guild.id, member, reason, interaction.user)
    count = active_warning_count(interaction.guild.id, member.id)

    embed = discord.Embed(title="⚠️ 경고 부여", color=discord.Color.red())
    embed.add_field(name="대상", value=f"{member.mention} (`{member.id}`)", inline=False)
    embed.add_field(name="경고", value=f"**{count} / {KICK_AT}**", inline=True)
    embed.add_field(name="경고 ID", value=f"`{warning_id}`", inline=True)
    embed.add_field(name="사유", value=reason[:1024], inline=False)
    embed.set_footer(text=f"처리자: {interaction.user} • {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")

    await interaction.response.send_message(embed=embed, ephemeral=False)
    await send_log(interaction.guild, embed)

    if count >= KICK_AT:
        me = interaction.guild.me
        if me and me.guild_permissions.kick_members and member.top_role < me.top_role:
            try:
                await member.kick(reason=f"경고 {count}회 누적 | 마지막 사유: {reason}")
                kick_embed = discord.Embed(title="🚪 누적 경고로 강퇴", color=discord.Color.dark_red())
                kick_embed.add_field(name="대상", value=f"{member} (`{member.id}`)")
                kick_embed.add_field(name="누적 경고", value=str(count))
                kick_embed.add_field(name="사유", value=reason[:1024], inline=False)
                kick_embed.set_footer(text=f"처리자: {interaction.user}")
                await send_log(interaction.guild, kick_embed)
            except (discord.Forbidden, discord.HTTPException):
                await interaction.followup.send(
                    "⚠️ 경고는 기록되었지만 자동 강퇴에 실패했습니다. 봇의 `Kick Members` 권한과 역할 위치를 확인해주세요.",
                    ephemeral=False,
                )
        else:
            await interaction.followup.send(
                "⚠️ 경고가 2회에 도달했지만 자동 강퇴할 수 없습니다. 봇의 `Kick Members` 권한/역할 순서를 확인해주세요.",
                ephemeral=False,
            )


@bot.tree.command(name="warnings", description="회원의 경고 이력을 조회합니다.")
@app_commands.describe(member="조회할 회원")
async def warnings(interaction: discord.Interaction, member: discord.Member):
    if not await require_mod(interaction):
        return

    rows = list_warnings(interaction.guild.id, member.id)
    count = active_warning_count(interaction.guild.id, member.id)

    embed = discord.Embed(title="📋 경고 기록", color=discord.Color.orange())
    embed.description = f"대상: {member.mention} (`{member.id}`)\n현재 유효 경고: **{count} / {KICK_AT}**"

    if not rows:
        embed.add_field(name="기록", value="경고 기록이 없습니다.", inline=False)
    else:
        for row in rows[:10]:
            status = "✅ 취소됨" if row["revoked_at"] else "⚠️ 유효"
            when = row["created_at"].replace("T", " ")[:19] + " UTC"
            text = (
                f"**#{row['id']} · {status}**\n"
                f"사유: {row['reason']}\n"
                f"처리자: <@{row['moderator_id']}> · {when}"
            )
            embed.add_field(name="\u200b", value=text[:1024], inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=False)


@bot.tree.command(name="warn_remove", description="특정 경고 1건을 취소합니다.")
@app_commands.describe(warning_id="취소할 경고 ID", reason="취소 사유")
async def warn_remove(interaction: discord.Interaction, warning_id: int, reason: str):
    if not await require_mod(interaction):
        return
    row = revoke_warning(interaction.guild.id, warning_id, interaction.user.id, reason)
    if row is None:
        await interaction.response.send_message("❌ 해당 유효 경고 ID를 찾지 못했습니다.", ephemeral=False)
        return

    embed = discord.Embed(title="↩️ 경고 취소", color=discord.Color.green())
    embed.add_field(name="대상", value=f"<@{row['user_id']}> (`{row['user_id']}`)", inline=False)
    embed.add_field(name="경고 ID", value=f"`{warning_id}`")
    embed.add_field(name="취소 사유", value=reason[:1024], inline=False)
    embed.set_footer(text=f"처리자: {interaction.user}")
    await interaction.response.send_message("✅ 경고를 취소했습니다.", ephemeral=False)
    await send_log(interaction.guild, embed)


@bot.tree.command(name="warn_clear", description="회원의 모든 유효 경고를 취소합니다.")
@app_commands.describe(member="대상 회원", reason="초기화 사유")
async def warn_clear(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not await require_mod(interaction):
        return
    changed = clear_warnings(interaction.guild.id, member.id, interaction.user.id, reason)
    embed = discord.Embed(title="🧹 경고 초기화", color=discord.Color.green())
    embed.add_field(name="대상", value=f"{member.mention} (`{member.id}`)", inline=False)
    embed.add_field(name="취소한 경고 수", value=str(changed))
    embed.add_field(name="사유", value=reason[:1024], inline=False)
    embed.set_footer(text=f"처리자: {interaction.user}")
    await interaction.response.send_message(
        f"✅ {changed}개의 유효 경고를 취소했습니다.", ephemeral=False
    )
    await send_log(interaction.guild, embed)


@bot.tree.command(name="warn_list", description="서버의 최근 경고 기록을 조회합니다.")
async def warn_list(interaction: discord.Interaction):
    if not await require_mod(interaction):
        return
    rows = recent_all_warnings(interaction.guild.id)
    embed = discord.Embed(title="🗂️ 최근 경고 기록", color=discord.Color.blurple())
    if not rows:
        embed.description = "기록이 없습니다."
    else:
        lines = []
        for row in rows:
            status = "취소" if row["revoked_at"] else "유효"
            lines.append(
                f"`#{row['id']}` **{status}** <@{row['user_id']}> — {row['reason'][:80]}"
            )
        embed.description = "\n".join(lines)[:4000]
    await interaction.response.send_message(embed=embed, ephemeral=False)


@bot.tree.command(name="warn_help", description="경고 봇 사용법을 안내합니다.")
async def warn_help(interaction: discord.Interaction):
    if not await require_mod(interaction):
        return
    text = (
        "**경고 봇 명령어**\n"
        "`/warn @회원 사유` — 경고 부여\n"
        "`/warnings @회원` — 개인 경고 이력 조회\n"
        "`/warn_remove 경고ID 사유` — 특정 경고 취소\n"
        "`/warn_clear @회원 사유` — 해당 회원의 모든 유효 경고 취소\n"
        "`/warn_list` — 최근 경고 기록 조회\n\n"
        f"현재 설정: **{KICK_AT}회 누적 시 자동 강퇴**"
    )
    await interaction.response.send_message(text, ephemeral=False)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Application command error", exc_info=error)
    message = "❌ 명령어 처리 중 오류가 발생했습니다. 운영진에게 알려주세요."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=False)
    else:
        await interaction.response.send_message(message, ephemeral=False)


bot.run(TOKEN)
