import os
import asyncio
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
    "casual": {"label": "Casual", "emoji": "🎮", "color": discord.Color.green(), "size": None},
    "ranked": {"label": "Ranked", "emoji": "🏆", "color": discord.Color.red(), "size": None},
    "global": {"label": "Global", "emoji": "🌐", "color": discord.Color.blue(), "size": None},
    "bracket4": {"label": "4-Player Bracket", "emoji": "🥊", "color": discord.Color.purple(), "size": 4},
    "bracket8": {"label": "8-Player Bracket", "emoji": "🥊", "color": discord.Color.dark_purple(), "size": 8},
}

# Tracks the currently active queue message per (guild_id, queue_type)
active_queues: dict[tuple[int, str], object] = {}


def build_bracket_rounds(seeded_players: list[discord.Member]) -> list[list[dict]]:
    """seeded_players must already be sorted by Elo descending."""
    size = len(seeded_players)
    s = seeded_players
    if size == 4:
        round1 = [
            {"player1": s[0], "player2": s[3], "winner": None, "label": "Semifinal 1"},
            {"player1": s[1], "player2": s[2], "winner": None, "label": "Semifinal 2"},
        ]
        round2 = [
            {"player1": None, "player2": None, "winner": None, "label": "Final"},
            {"player1": None, "player2": None, "winner": None, "label": "3rd Place Match"},
        ]
        return [round1, round2]
    elif size == 8:
        round1 = [
            {"player1": s[0], "player2": s[7], "winner": None, "label": "Quarterfinal 1"},
            {"player1": s[3], "player2": s[4], "winner": None, "label": "Quarterfinal 2"},
            {"player1": s[1], "player2": s[6], "winner": None, "label": "Quarterfinal 3"},
            {"player1": s[2], "player2": s[5], "winner": None, "label": "Quarterfinal 4"},
        ]
        round2 = [
            {"player1": None, "player2": None, "winner": None, "label": "Semifinal 1"},
            {"player1": None, "player2": None, "winner": None, "label": "Semifinal 2"},
        ]
        round3 = [
            {"player1": None, "player2": None, "winner": None, "label": "Final"},
            {"player1": None, "player2": None, "winner": None, "label": "3rd Place Match"},
        ]
        return [round1, round2, round3]
    raise ValueError("Only bracket sizes 4 and 8 are supported")


def record_bracket_result(rounds: list[list[dict]], round_idx: int, match_idx: int, winner: discord.Member):
    """Records a winner and propagates winner/loser to the next round. Returns the loser."""
    match = rounds[round_idx][match_idx]
    match["winner"] = winner
    loser = match["player2"] if winner.id == match["player1"].id else match["player1"]
    last_idx = len(rounds) - 1

    if round_idx == last_idx:
        pass  # Final or 3rd place match — nothing further to propagate
    elif round_idx == last_idx - 1:
        # Semifinal round: winner -> Final, loser -> 3rd Place Match
        final_match = rounds[last_idx][0]
        third_match = rounds[last_idx][1]
        slot = "player1" if match_idx == 0 else "player2"
        final_match[slot] = winner
        third_match[slot] = loser
    else:
        # Earlier round (e.g. quarterfinals): winner advances, loser is eliminated
        nxt = rounds[round_idx + 1][match_idx // 2]
        slot = "player1" if match_idx % 2 == 0 else "player2"
        nxt[slot] = winner

    return loser


def get_pending_bracket_matches(rounds: list[list[dict]]):
    """Returns list of (round_idx, match_idx, match) for matches ready to be played."""
    pending = []
    for r_idx, round_matches in enumerate(rounds):
        for m_idx, match in enumerate(round_matches):
            if match["player1"] and match["player2"] and match["winner"] is None:
                pending.append((r_idx, m_idx, match))
    return pending


def bracket_is_finished(rounds: list[list[dict]]) -> bool:
    final_match = rounds[-1][0]
    third_match = rounds[-1][1]
    return final_match["winner"] is not None and third_match["winner"] is not None


class AddPlayerSelect(discord.ui.UserSelect):
    def __init__(self, parent_view):
        super().__init__(placeholder="Select a player to add...", min_values=1, max_values=1)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        member = self.values[0]
        if not isinstance(member, discord.Member):
            # Resolve to a full Member if a bare User came back
            member = interaction.guild.get_member(member.id) or member

        if member.bot:
            await interaction.response.edit_message(content="You can't add a bot to the queue.", view=None)
            return
        if member.id in [m.id for m in self.parent_view.queue]:
            await interaction.response.edit_message(content=f"{member.display_name} is already in the queue.", view=None)
            return

        size = getattr(self.parent_view, "size", None)
        started = getattr(self.parent_view, "started", False)
        if started:
            await interaction.response.edit_message(content="This bracket has already started.", view=None)
            return
        if size and len(self.parent_view.queue) >= size:
            await interaction.response.edit_message(content="The queue is already full.", view=None)
            return

        self.parent_view.queue.append(member)
        if size and len(self.parent_view.queue) == size:
            self.parent_view.start_bracket()

        await interaction.response.edit_message(content=f"Added {member.display_name} to the queue.", view=None)
        await self.parent_view.refresh()


class AddPlayerView(discord.ui.View):
    def __init__(self, parent_view):
        super().__init__(timeout=60)
        self.add_item(AddPlayerSelect(parent_view))


class CloseChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Closing this channel...")
        try:
            await interaction.channel.delete()
        except discord.HTTPException:
            pass


def get_sorted_pairs(members: list[discord.Member]):
    """Sorts by Elo desc and pairs adjacently (1v2, 3v4, ...). Returns (pairs, leftover_or_None)."""
    sorted_m = sorted(members, key=lambda m: db.get_rating(m.id), reverse=True)
    pairs = []
    leftover = None
    for i in range(0, len(sorted_m), 2):
        if i + 1 < len(sorted_m):
            pairs.append((sorted_m[i], sorted_m[i + 1]))
        else:
            leftover = sorted_m[i]
    return pairs, leftover


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

    @discord.ui.button(label="Add Player", style=discord.ButtonStyle.secondary, emoji="➕")
    async def add_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Who do you want to add to the queue?", view=AddPlayerView(self), ephemeral=True
        )

    @discord.ui.button(label="Start Matches", style=discord.ButtonStyle.primary, emoji="▶️")
    async def start_matches(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.queue) < 2:
            await interaction.response.send_message(
                "Need at least 2 players in the queue to start matches.", ephemeral=True
            )
            return

        if self.queue_type == "global":
            await interaction.response.send_message("🏓 Starting matches!")
            channel = interaction.channel
            for n in (5, 4, 3, 2, 1):
                await asyncio.sleep(1)
                await channel.send(str(n))
            await asyncio.sleep(1)
            await channel.send("**GO**")
            return

        # casual / ranked -> create a private match channel per pairing
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        category_id = db.get_match_category(guild.id)
        category = guild.get_channel(category_id) if category_id else None

        pairs, leftover = get_sorted_pairs(self.queue)
        created = []
        for p1, p2 in pairs:
            number = db.get_next_match_number(guild.id)
            name = f"Match {number}: {p1.display_name} vs {p2.display_name}"
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                p1: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                p2: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            }
            try:
                channel = await guild.create_text_channel(name=name, category=category, overwrites=overwrites)
            except discord.HTTPException:
                safe_name = f"match-{number}-{p1.display_name}-vs-{p2.display_name}"
                channel = await guild.create_text_channel(name=safe_name, category=category, overwrites=overwrites)
            await channel.send(
                content=f"{p1.mention} vs {p2.mention} — when the match is over, press Close.",
                view=CloseChannelView(),
            )
            created.append(channel)

        summary = "\n".join(c.mention for c in created) if created else "*No full pairs found.*"
        if leftover:
            summary += f"\n\n{leftover.mention} is waiting for an opponent (odd number of players)."
        await interaction.followup.send(f"Created {len(created)} match channel(s):\n{summary}", ephemeral=True)

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


@bot.tree.command(name="queue", description="Start or join a match queue")
@app_commands.describe(
    type="Which queue to start",
    role="Role to ping (optional — if set, sent as a separate message so it always notifies)",
    note="Optional note, e.g. 'casual only'",
)
@app_commands.choices(
    type=[
        app_commands.Choice(name="Casual", value="casual"),
        app_commands.Choice(name="Ranked", value="ranked"),
        app_commands.Choice(name="Global", value="global"),
        app_commands.Choice(name="4-Player Bracket", value="bracket4"),
        app_commands.Choice(name="8-Player Bracket", value="bracket8"),
    ]
)
async def queue(
    interaction: discord.Interaction,
    type: app_commands.Choice[str],
    role: discord.Role = None,
    note: str = None,
):
    is_bracket = QUEUE_TYPES[type.value]["size"] is not None
    key = (interaction.guild_id, type.value)

    if not is_bracket:
        existing = active_queues.get(key)
        if existing:
            link = existing.message.jump_url if existing.message else None
            msg = f"A {type.name} is already active."
            if link:
                msg += f" Jump to it here: {link}"
            await interaction.response.send_message(msg, ephemeral=True)
            return

    size = QUEUE_TYPES[type.value]["size"]
    if size:
        view = BracketView(
            requester=interaction.user,
            queue_type=type.value,
            guild_id=interaction.guild_id,
            size=size,
            note=note,
        )
    else:
        view = LFMView(
            requester=interaction.user,
            queue_type=type.value,
            guild_id=interaction.guild_id,
            note=note,
        )

    if role:
        # Ping goes out as its own plain message so it always notifies,
        # separate from the queue embed itself.
        await interaction.response.send_message(role.mention)
        message = await interaction.followup.send(embed=view.build_embed(), view=view)
    else:
        await interaction.response.send_message(embed=view.build_embed(), view=view)
        message = await interaction.original_response()

    view.message = message
    if not is_bracket:
        active_queues[key] = view


async def post_to_results_channel(guild: discord.Guild, content: str = None, embed: discord.Embed = None):
    channel_id = db.get_results_channel(guild.id)
    if not channel_id:
        return
    channel = guild.get_channel(channel_id) or bot.get_channel(channel_id)
    if not channel:
        return
    try:
        await channel.send(content=content, embed=embed)
    except discord.HTTPException:
        pass


def build_match_result_embed(winner: discord.Member, loser: discord.Member, before: dict, after: dict, label: str) -> discord.Embed:
    """Same style as /record-match: shows old -> new rating with the delta for both players."""
    def line(member, tag):
        old = before[member.id]
        new = after[member.id]
        delta = new - old
        sign = "+" if delta >= 0 else ""
        return f"**{tag}** {member.mention} — {old} → {new} ({sign}{delta})"

    embed = discord.Embed(
        title="🏓 Match Result",
        description=f"{label}\n\n{line(winner, 'W')}\n{line(loser, 'L')}",
        color=discord.Color.orange(),
    )
    return embed


class ConfirmResultView(discord.ui.View):
    """
    Generic result-confirmation view.
    - required_confirmer_id set -> only that specific user may confirm (used for 1v1 reports).
    - required_confirmer_id None -> anyone except the reporter may confirm (used for bracket matches).
    - on_confirm(interaction) is called after Elo is applied, for any extra bookkeeping
      (e.g. advancing a bracket). If omitted, the confirmed result is posted to the results channel.
    """

    def __init__(
        self,
        winner: discord.Member,
        loser: discord.Member,
        reporter_id: int,
        label: str,
        required_confirmer_id: int = None,
        on_confirm=None,
    ):
        super().__init__(timeout=600)  # 10 minutes to confirm
        self.winner = winner
        self.loser = loser
        self.reporter_id = reporter_id
        self.label = label
        self.required_confirmer_id = required_confirmer_id
        self.on_confirm = on_confirm
        self.resolved = False
        self.message: discord.Message = None

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.required_confirmer_id is not None:
            if interaction.user.id != self.required_confirmer_id:
                await interaction.response.send_message(
                    "Only the opponent in this match can confirm this result.", ephemeral=True
                )
                return
        elif interaction.user.id == self.reporter_id:
            await interaction.response.send_message(
                "You reported this result — someone else needs to confirm it.", ephemeral=True
            )
            return

        if self.resolved:
            await interaction.response.send_message("This result was already resolved.", ephemeral=True)
            return
        self.resolved = True

        before = {self.winner.id: db.get_rating(self.winner.id), self.loser.id: db.get_rating(self.loser.id)}
        deltas = db.calculate_elo_changes([self.winner.id, self.loser.id])
        db.apply_rating_changes({self.winner.id: deltas[0], self.loser.id: deltas[1]})
        after = {self.winner.id: before[self.winner.id] + deltas[0], self.loser.id: before[self.loser.id] + deltas[1]}

        result_embed = build_match_result_embed(self.winner, self.loser, before, after, self.label)

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"✅ Confirmed by {interaction.user.mention}",
            embed=result_embed,
            view=self,
        )

        if self.on_confirm:
            await self.on_confirm(interaction)
        else:
            guild = interaction.guild
            if guild:
                await update_leaderboard_message(guild)
                await post_to_results_channel(guild, embed=result_embed)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, emoji="❌")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved:
            await interaction.response.send_message("This result was already resolved.", ephemeral=True)
            return
        self.resolved = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"❌ Result rejected by {interaction.user.mention}. Please report the match again if needed.",
            view=self,
        )

    async def on_timeout(self):
        if not self.resolved:
            for item in self.children:
                item.disabled = True
            try:
                await self.message.edit(
                    content="⏱️ This result report expired without confirmation.", view=self
                )
            except (discord.HTTPException, AttributeError):
                pass


class BracketWinnerSelect(discord.ui.Select):
    def __init__(self, bracket_view: "BracketView", round_idx: int, match_idx: int, match: dict):
        self.bracket_view = bracket_view
        self.round_idx = round_idx
        self.match_idx = match_idx
        self.match = match
        options = [
            discord.SelectOption(label=f"{match['player1'].display_name} wins", value=str(match["player1"].id)),
            discord.SelectOption(label=f"{match['player2'].display_name} wins", value=str(match["player2"].id)),
        ]
        super().__init__(placeholder="Who won?", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        winner_id = int(self.values[0])
        winner = self.match["player1"] if self.match["player1"].id == winner_id else self.match["player2"]
        loser = self.match["player2"] if winner.id == self.match["player1"].id else self.match["player1"]

        await interaction.response.edit_message(
            content="Report submitted — waiting for confirmation.", view=None
        )

        bracket_view = self.bracket_view
        round_idx = self.round_idx
        match_idx = self.match_idx

        async def on_confirm(confirm_interaction: discord.Interaction):
            record_bracket_result(bracket_view.rounds, round_idx, match_idx, winner)
            if bracket_is_finished(bracket_view.rounds):
                bracket_view.finished = True
                bracket_view.clear_items()
            await bracket_view.refresh()
            guild = confirm_interaction.guild
            if guild:
                await update_leaderboard_message(guild)
                if bracket_view.finished:
                    await post_to_results_channel(guild, embed=bracket_view.build_final_ranking_embed())

        confirm_view = ConfirmResultView(
            winner=winner,
            loser=loser,
            reporter_id=interaction.user.id,
            label=self.match["label"],
            required_confirmer_id=None,  # anyone but the reporter
            on_confirm=on_confirm,
        )
        if self.bracket_view.message:
            confirm_view.message = await self.bracket_view.message.channel.send(
                content=(
                    f"📋 {interaction.user.mention} reports **{winner.mention}** defeated {loser.mention} "
                    f"in **{self.match['label']}**.\nSomeone other than the reporter must confirm."
                ),
                view=confirm_view,
            )


class BracketWinnerView(discord.ui.View):
    def __init__(self, bracket_view: "BracketView", round_idx: int, match_idx: int, match: dict):
        super().__init__(timeout=120)
        self.add_item(BracketWinnerSelect(bracket_view, round_idx, match_idx, match))


class BracketMatchSelect(discord.ui.Select):
    def __init__(self, bracket_view: "BracketView", pending: list):
        self.bracket_view = bracket_view
        self.pending = pending
        options = [
            discord.SelectOption(
                label=f"{match['label']}: {match['player1'].display_name} vs {match['player2'].display_name}",
                value=f"{r_idx}:{m_idx}",
            )
            for r_idx, m_idx, match in pending
        ]
        super().__init__(placeholder="Select the match to record...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        r_idx_str, m_idx_str = self.values[0].split(":")
        r_idx, m_idx = int(r_idx_str), int(m_idx_str)
        match = self.bracket_view.rounds[r_idx][m_idx]
        await interaction.response.edit_message(
            content=f"**{match['label']}**: {match['player1'].mention} vs {match['player2'].mention}\nWho won?",
            view=BracketWinnerView(self.bracket_view, r_idx, m_idx, match),
        )


class BracketMatchPickView(discord.ui.View):
    def __init__(self, bracket_view: "BracketView", pending: list):
        super().__init__(timeout=120)
        self.add_item(BracketMatchSelect(bracket_view, pending))


class RecordMatchButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Record Match", style=discord.ButtonStyle.primary, emoji="📝")

    async def callback(self, interaction: discord.Interaction):
        bracket_view: BracketView = self.view
        if bracket_view.finished:
            await interaction.response.send_message("This bracket is already finished.", ephemeral=True)
            return
        pending = get_pending_bracket_matches(bracket_view.rounds)
        if not pending:
            await interaction.response.send_message("No matches are ready to record yet.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Which match do you want to record?", view=BracketMatchPickView(bracket_view, pending), ephemeral=True
        )


class BracketView(discord.ui.View):
    def __init__(self, requester: discord.Member, queue_type: str, guild_id: int, size: int, note: str = None):
        super().__init__(timeout=3600)
        self.requester = requester
        self.queue_type = queue_type
        self.guild_id = guild_id
        self.size = size
        self.note = note
        self.queue: list[discord.Member] = []
        self.message: discord.Message = None
        self.rounds: list[list[dict]] = None
        self.started = False
        self.finished = False

    def build_embed(self) -> discord.Embed:
        info = QUEUE_TYPES[self.queue_type]
        embed = discord.Embed(title=f"{info['emoji']} {info['label']}", color=info["color"])
        embed.add_field(name="Started by", value=self.requester.mention, inline=True)
        if self.note:
            embed.add_field(name="Note", value=self.note, inline=False)

        if not self.started:
            if self.queue:
                listing = "\n".join(f"{i+1}. {m.mention}" for i, m in enumerate(self.queue))
            else:
                listing = "*No one in queue yet.*"
            embed.add_field(name=f"Players ({len(self.queue)}/{self.size})", value=listing, inline=False)
            embed.set_footer(text="Bracket starts automatically once full")
            return embed

        # Bracket in progress or finished — show full bracket state
        for round_matches in self.rounds:
            lines = []
            for match in round_matches:
                p1 = match["player1"]
                p2 = match["player2"]
                if p1 is None or p2 is None:
                    lines.append(f"**{match['label']}**: TBD")
                elif match["winner"] is not None:
                    winner = match["winner"]
                    loser = p2 if winner.id == p1.id else p1
                    lines.append(f"**{match['label']}**: ✅ {winner.mention} def. {loser.mention}")
                else:
                    lines.append(f"**{match['label']}**: ⏳ {p1.mention} vs {p2.mention}")
            round_name = round_matches[0]["label"].split()[0] if len(round_matches) > 1 else "Round"
            embed.add_field(name=f"Round: {round_name}s", value="\n".join(lines), inline=False)

        pending = get_pending_bracket_matches(self.rounds)
        if pending and not self.finished:
            next_lines = [f"{m['label']}: {m['player1'].mention} vs {m['player2'].mention}" for _, _, m in pending]
            embed.add_field(name="▶️ Next Matches", value="\n".join(next_lines), inline=False)

        if self.finished:
            final_match = self.rounds[-1][0]
            third_match = self.rounds[-1][1]
            champion = final_match["winner"]
            runner_up = final_match["player2"] if champion.id == final_match["player1"].id else final_match["player1"]
            third = third_match["winner"]
            fourth = third_match["player2"] if third.id == third_match["player1"].id else third_match["player1"]
            standings = (
                f"🥇 {champion.mention}\n"
                f"🥈 {runner_up.mention}\n"
                f"🥉 {third.mention}\n"
                f"4️⃣ {fourth.mention}"
            )
            embed.add_field(name="🏁 Final Ranking", value=standings, inline=False)
            embed.set_footer(text="Bracket complete")
        else:
            embed.set_footer(text="Admins: use Record Match after each game")

        return embed

    async def refresh(self):
        if self.message:
            try:
                await self.message.edit(embed=self.build_embed(), view=self)
            except discord.HTTPException:
                pass

    def build_final_ranking_embed(self) -> discord.Embed:
        info = QUEUE_TYPES[self.queue_type]
        final_match = self.rounds[-1][0]
        third_match = self.rounds[-1][1]
        champion = final_match["winner"]
        runner_up = final_match["player2"] if champion.id == final_match["player1"].id else final_match["player1"]
        third = third_match["winner"]
        fourth = third_match["player2"] if third.id == third_match["player1"].id else third_match["player1"]

        embed = discord.Embed(title=f"🏁 {info['label']} Complete!", color=info["color"])
        embed.description = (
            f"🥇 {champion.mention}\n"
            f"🥈 {runner_up.mention}\n"
            f"🥉 {third.mention}\n"
            f"4️⃣ {fourth.mention}"
        )
        if self.message:
            embed.description += f"\n\n[Jump to bracket]({self.message.jump_url})"
        return embed

    def start_bracket(self):
        seeded = sorted(self.queue, key=lambda m: db.get_rating(m.id), reverse=True)
        self.rounds = build_bracket_rounds(seeded)
        self.started = True
        self.clear_items()
        self.add_item(RecordMatchButton())

    @discord.ui.button(label="Join Queue", style=discord.ButtonStyle.success, emoji="🏓")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in [m.id for m in self.queue]:
            await interaction.response.send_message("You're already in the queue.", ephemeral=True)
            return
        if len(self.queue) >= self.size:
            await interaction.response.send_message("This bracket is already full.", ephemeral=True)
            return
        self.queue.append(interaction.user)
        if len(self.queue) == self.size:
            self.start_bracket()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Leave Queue", style=discord.ButtonStyle.secondary, emoji="🚪")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.started:
            await interaction.response.send_message("The bracket has already started.", ephemeral=True)
            return
        if interaction.user.id not in [m.id for m in self.queue]:
            await interaction.response.send_message("You're not in the queue.", ephemeral=True)
            return
        self.queue = [m for m in self.queue if m.id != interaction.user.id]
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Kick Queue", style=discord.ButtonStyle.danger, emoji="⛔")
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.started:
            await interaction.response.send_message("The bracket has already started.", ephemeral=True)
            return
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                "Only the person who started this bracket can kick players.", ephemeral=True
            )
            return
        if not self.queue:
            await interaction.response.send_message("Queue is empty.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Who do you want to remove?", view=KickView(self), ephemeral=True
        )

    @discord.ui.button(label="Add Player", style=discord.ButtonStyle.secondary, emoji="➕")
    async def add_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.started:
            await interaction.response.send_message("The bracket has already started.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Who do you want to add to the bracket?", view=AddPlayerView(self), ephemeral=True
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


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


@bot.tree.command(name="set-results-channel", description="[Admin] Set the channel where final results get posted")
@app_commands.describe(channel="Channel for bracket final rankings and confirmed 1v1 results")
@app_commands.checks.has_permissions(administrator=True)
async def set_results_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    db.set_results_channel(interaction.guild_id, channel.id)
    await interaction.response.send_message(f"Results will now be posted in {channel.mention}.")


@set_results_channel.error
async def set_results_channel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "Only admins can set the results channel.", ephemeral=True
        )
    else:
        raise error


@bot.tree.command(name="report-match", description="Report a ranked 1v1 result — your opponent must confirm it")
@app_commands.describe(opponent="Who you played against", i_won="Did you win?")
async def report_match(interaction: discord.Interaction, opponent: discord.Member, i_won: bool):
    if opponent.id == interaction.user.id:
        await interaction.response.send_message("You can't report a match against yourself.", ephemeral=True)
        return
    if opponent.bot:
        await interaction.response.send_message("You can't report a match against a bot.", ephemeral=True)
        return

    winner = interaction.user if i_won else opponent
    loser = opponent if i_won else interaction.user

    confirm_view = ConfirmResultView(
        winner=winner,
        loser=loser,
        reporter_id=interaction.user.id,
        label="Ranked 1v1",
        required_confirmer_id=opponent.id,
    )
    await interaction.response.send_message(
        content=(
            f"📋 {interaction.user.mention} reports **{winner.mention}** defeated {loser.mention}.\n"
            f"{opponent.mention}, please confirm or reject this result."
        ),
        view=confirm_view,
    )
    confirm_view.message = await interaction.original_response()


@bot.tree.command(name="set-match-category", description="[Admin] Set the category where private match channels are created")
@app_commands.describe(category="Category for Start Matches channels")
@app_commands.checks.has_permissions(administrator=True)
async def set_match_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    db.set_match_category(interaction.guild_id, category.id)
    await interaction.response.send_message(f"Match channels will now be created under **{category.name}**.")


@set_match_category.error
async def set_match_category_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "Only admins can set the match category.", ephemeral=True
        )
    else:
        raise error


bot.run(TOKEN)
