import os
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
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

        if self.queue:
            listing = "\n".join(f"{i+1}. {m.mention}" for i, m in enumerate(self.queue))
        else:
            listing = "*No one in queue yet.*"
        embed.add_field(name=f"Queue ({len(self.queue)})", value=listing, inline=False)
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


bot.run(TOKEN)
