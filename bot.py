import os
import discord
from discord import app_commands
from discord.ext import commands
import db

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    db.init_db()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Sync failed: {e}")

@bot.tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! ({latency_ms}ms)")


QUEUE_TYPES = {
    "casual": {"label": "Casual", "emoji": "🎮", "color": discord.Color.green()},
    "ranked": {"label": "Ranked", "emoji": "🏆", "color": discord.Color.red()},
    "global": {"label": "Global", "emoji": "🌐", "color": discord.Color.blue()},
}

# Tracks the currently active queue message per (guild_id, queue_type)
active_queues: dict[tuple[int, str], "LFMView"] = {}


class KickSelect(discord.ui.Select):
    def __init__(self, parent_view: "LFMView"):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(label=member.display_name, value=str(member.id))
            for member in parent_view.queue
        ]
        super().__init__(
            placeholder="Select a player to remove...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        target_id = int(self.values[0])
        self.parent_view.queue = [m for m in self.parent_view.queue if m.id != target_id]
        await interaction.response.edit_message(
            content="Removed from queue.", embed=None, view=None
        )
        await self.parent_view.refresh()


class KickView(discord.ui.View):
    def __init__(self, parent_view: "LFMView"):
        super().__init__(timeout=60)
        self.add_item(KickSelect(parent_view))


class LFMView(discord.ui.View):
    def __init__(self, requester: discord.Member, queue_type: str, guild_id: int, note: str = None):
        super().__init__(timeout=3600)  # auto-expire after 1 hour
        self.requester = requester
        self.queue_type = queue_type
        self.guild_id = guild_id
        self.note = note
        self.queue: list[discord.Member] = []
        self.message: discord.Message = None

    def build_embed(self) -> discord.Embed:
        info = QUEUE_TYPES[self.queue_type]
        embed = discord.Embed(
            title=f"{info['emoji']} {info['label']} Queue",
            color=info["color"],
        )
        embed.add_field(name="Started by", value=self.requester.mention, inline=True)
        if self.note:
            embed.add_field(name="Note", value=self.note, inline=False)

        if self.queue_type == "ranked" and self.queue:
            sorted_queue = sorted(
                self.queue, key=lambda m: db.get_rating(m.id), reverse=True
            )
            lines = []
            for i, member in enumerate(sorted_queue):
                rating = db.get_rating(member.id)
                lines.append(f"{i+1}. {member.mention} ({rating})")
            listing = "\n".join(lines)

            pairing_lines = []
            for i in range(0, len(sorted_queue), 2):
                if i + 1 < len(sorted_queue):
                    pairing_lines.append(
                        f"Match {i//2 + 1}: {sorted_queue[i].mention} vs {sorted_queue[i+1].mention}"
                    )
                else:
                    pairing_lines.append(f"Waiting for opponent: {sorted_queue[i].mention}")
            embed.add_field(name=f"Queue ({len(self.queue)}) — by Elo", value=listing, inline=False)
            embed.add_field(name="Suggested Pairings", value="\n".join(pairing_lines), inline=False)
        elif self.queue:
            listing = "\n".join(f"{i+1}. {m.mention}" for i, m in enumerate(self.queue))
            embed.add_field(name=f"Queue ({len(self.queue)})", value=listing, inline=False)
        else:
            embed.add_field(name="Queue (0)", value="*No one in queue yet.*", inline=False)

        embed.set_footer(text="Expires in 1 hour of inactivity")
        return embed

    async def refresh(self):
        if self.message:
            try:
                await self.message.edit(embed=self.build_embed(), view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Join Queue", style=discord.ButtonStyle.success, emoji="🏓")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in [m.id for m in self.queue]:
            await interaction.response.send_message("You're already in the queue.", ephemeral=True)
            return
        self.queue.append(interaction.user)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Leave Queue", style=discord.ButtonStyle.secondary, emoji="🚪")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in [m.id for m in self.queue]:
            await interaction.response.send_message("You're not in the queue.", ephemeral=True)
            return
        self.queue = [m for m in self.queue if m.id != interaction.user.id]
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Kick Queue", style=discord.ButtonStyle.danger, emoji="⛔")
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                "Only the person who started this queue can kick players.", ephemeral=True
            )
            return
        if not self.queue:
            await interaction.response.send_message("Queue is empty.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Who do you want to remove?", view=KickView(self), ephemeral=True
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        key = (self.guild_id, self.queue_type)
        if active_queues.get(key) is self:
            del active_queues[key]


@bot.tree.command(name="queue", description="Start or join a match queue and ping a role")
@app_commands.describe(
    type="Which queue to start",
    role="Role to ping (e.g. @TableTennis)",
    note="Optional note, e.g. 'casual only'",
)
@app_commands.choices(
    type=[
        app_commands.Choice(name="Casual", value="casual"),
        app_commands.Choice(name="Ranked", value="ranked"),
        app_commands.Choice(name="Global", value="global"),
    ]
)
async def queue(
    interaction: discord.Interaction,
    type: app_commands.Choice[str],
    role: discord.Role,
    note: str = None,
):
    key = (interaction.guild_id, type.value)
    existing = active_queues.get(key)
    if existing:
        link = existing.message.jump_url if existing.message else None
        msg = f"A {type.name} queue is already active."
        if link:
            msg += f" Jump to it here: {link}"
        await interaction.response.send_message(msg, ephemeral=True)
        return

    view = LFMView(
        requester=interaction.user,
        queue_type=type.value,
        guild_id=interaction.guild_id,
        note=note,
    )
    await interaction.response.send_message(content=role.mention, embed=view.build_embed(), view=view)
    view.message = await interaction.original_response()
    active_queues[key] = view


async def build_leaderboard_embed(guild: discord.Guild) -> discord.Embed:
    rows = db.get_leaderboard(limit=10)
    embed = discord.Embed(title="🏆 Ranked Leaderboard", color=discord.Color.gold())
    if not rows:
        embed.description = "No ranked matches recorded yet."
        return embed

    lines = []
    for i, (user_id, rating, games) in enumerate(rows):
        member = guild.get_member(user_id)
        name = member.mention if member else f"<@{user_id}>"
        lines.append(f"**{i+1}.** {name} — {rating} pts ({games} games)")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Updates automatically after every recorded match")
    return embed


async def update_leaderboard_message(guild: discord.Guild):
    ref = db.get_leaderboard_message(guild.id)
    if not ref:
        return
    channel_id, message_id = ref
    channel = guild.get_channel(channel_id) or bot.get_channel(channel_id)
    if not channel:
        return
    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=await build_leaderboard_embed(guild))
    except discord.NotFound:
        pass
    except discord.HTTPException:
        pass


@bot.tree.command(name="leaderboard", description="Post the ranked leaderboard (auto-updates after each match)")
async def leaderboard(interaction: discord.Interaction):
    embed = await build_leaderboard_embed(interaction.guild)
    await interaction.response.send_message(embed=embed)
    message = await interaction.original_response()
    db.set_leaderboard_message(interaction.guild_id, message.channel.id, message.id)


@bot.tree.command(name="record-match", description="[Admin] Record a ranked match result (up to 8 places)")
@app_commands.describe(
    place1="1st place",
    place2="2nd place",
    place3="3rd place",
    place4="4th place",
    place5="5th place",
    place6="6th place",
    place7="7th place",
    place8="8th place",
)
@app_commands.checks.has_permissions(administrator=True)
async def record_match(
    interaction: discord.Interaction,
    place1: discord.Member,
    place2: discord.Member = None,
    place3: discord.Member = None,
    place4: discord.Member = None,
    place5: discord.Member = None,
    place6: discord.Member = None,
    place7: discord.Member = None,
    place8: discord.Member = None,
):
    placements = [p for p in [place1, place2, place3, place4, place5, place6, place7, place8] if p]

    if len(set(m.id for m in placements)) != len(placements):
        await interaction.response.send_message(
            "The same player can't appear in multiple places.", ephemeral=True
        )
        return

    before = {m.id: db.get_rating(m.id) for m in placements}
    deltas = db.calculate_elo_changes([m.id for m in placements])
    db.apply_rating_changes({m.id: d for m, d in zip(placements, deltas)})

    lines = []
    for i, member in enumerate(placements):
        old = before[member.id]
        delta = deltas[i]
        new = old + delta
        sign = "+" if delta >= 0 else ""
        lines.append(f"**{i+1}.** {member.mention} — {old} → {new} ({sign}{delta})")

    embed = discord.Embed(
        title="🏓 Ranked Match Recorded",
        description="\n".join(lines),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed)
    await update_leaderboard_message(interaction.guild)


@record_match.error
async def record_match_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "Only admins can record ranked matches.", ephemeral=True
        )
    else:
        raise error


@bot.tree.command(name="set-elo-balance", description="[Admin] Adjust how much wins vs losses affect Elo")
@app_commands.describe(
    win_multiplier="Multiplier applied to Elo gains (e.g. 1.5 = gain 50% more than standard)",
    loss_multiplier="Multiplier applied to Elo losses (e.g. 0.5 = lose 50% less than standard)",
)
@app_commands.checks.has_permissions(administrator=True)
async def set_elo_balance(interaction: discord.Interaction, win_multiplier: float, loss_multiplier: float):
    if win_multiplier <= 0 or loss_multiplier <= 0:
        await interaction.response.send_message("Multipliers must be greater than 0.", ephemeral=True)
        return
    db.set_config("win_multiplier", win_multiplier)
    db.set_config("loss_multiplier", loss_multiplier)
    await interaction.response.send_message(
        f"Elo balance updated: wins now scale by **{win_multiplier}x**, losses by **{loss_multiplier}x**.\n"
        f"This only affects matches recorded from now on."
    )


@set_elo_balance.error
async def set_elo_balance_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "Only admins can adjust Elo balance.", ephemeral=True
        )
    else:
        raise error


bot.run(TOKEN)
