import io
import logging
import math
from datetime import datetime
import datetime as dt
import random
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import openpotd
import shared


# Change this if you want a different algorithm
def weighted_score(attempts: int):
    return 0.9 ** (attempts - 1)


weighted_score_dict = [1, 0.9, 0.65, 0.45, 0.25, 0.1]


def weighted_score_new(attempts: int):
    return weighted_score_dict[min(attempts, len(weighted_score_dict)) - 1]


class ManualReviewActionButton(discord.ui.Button):
    LABELS = {
        'claim': 'Claim',
        'correct': 'Correct',
        'incorrect': 'Incorrect',
    }
    STYLES = {
        'claim': discord.ButtonStyle.primary,
        'correct': discord.ButtonStyle.success,
        'incorrect': discord.ButtonStyle.danger,
    }

    def __init__(self, interface: "Interface", submission_id: int, action: str, disabled: bool = False):
        super().__init__(
            label=self.LABELS[action],
            style=self.STYLES[action],
            custom_id=f'manual_review:{submission_id}:{action}',
            disabled=disabled,
        )
        self.interface = interface
        self.submission_id = submission_id
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        await self.interface.handle_manual_review_action(interaction, self.submission_id, self.action)


class ManualReviewView(discord.ui.View):
    def __init__(self, interface: "Interface", submission_id: int, status: str):
        super().__init__(timeout=None)
        claim_disabled = status in ('claimed', 'reviewed')
        decision_disabled = status == 'reviewed'

        self.add_item(ManualReviewActionButton(interface, submission_id, 'claim', disabled=claim_disabled))
        self.add_item(ManualReviewActionButton(interface, submission_id, 'correct', disabled=decision_disabled))
        self.add_item(ManualReviewActionButton(interface, submission_id, 'incorrect', disabled=decision_disabled))


class ManualReviewDecisionModal(discord.ui.Modal):
    reviewer_note = discord.ui.TextInput(
        label='Message to submitter (optional)',
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1200,
    )

    def __init__(self, interface: "Interface", submission_id: int, is_correct: bool):
        super().__init__(title='Mark Correct' if is_correct else 'Mark Incorrect')
        self.interface = interface
        self.submission_id = submission_id
        self.is_correct = is_correct

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user is None or not self.interface._is_authorised_marker(interaction.user, interaction.guild):
            await interaction.response.send_message('You are not authorised to use this action.', ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        note = str(self.reviewer_note).strip()
        if not note:
            note = None

        _, details = await self.interface.review_manual_submission(
            self.submission_id,
            self.is_correct,
            interaction.user.id,
            require_claim=True,
            reviewer_note=note,
        )
        await interaction.followup.send(details, ephemeral=True)


class SubproblemSubmitModal(discord.ui.Modal):
    answer = discord.ui.TextInput(
        label='Your Submission',
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000,
    )

    def __init__(self, interface: "Interface", potd_id: int, season_id: int, subproblem_id: int):
        super().__init__(title='Submit Answer')
        self.interface = interface
        self.potd_id = potd_id
        self.season_id = season_id
        self.subproblem_id = subproblem_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user is None:
            await interaction.response.send_message('Unable to identify user.', ephemeral=True)
            return

        if self.interface.bot.config.get('cooldown'):
            if interaction.user.id in self.interface.cooldowns and \
                    self.interface.cooldowns[interaction.user.id] > datetime.utcnow():
                await interaction.response.send_message(
                    "You're on cooldown. Please wait before submitting again.",
                    ephemeral=True,
                )
                return

        cursor = self.interface.bot.db.cursor()
        cursor.execute(
            'INSERT OR IGNORE INTO users (discord_id, nickname, anonymous) VALUES (?, ?, ?)',
            (interaction.user.id, interaction.user.display_name, True),
        )
        self.interface.bot.db.commit()
        response = await self.interface.process_subproblem_submission(
            user=interaction.user,
            potd_id=self.potd_id,
            season_id=self.season_id,
            subproblem_id=self.subproblem_id,
            content=str(self.answer),
            attachments=[],
            dm_channel_id=interaction.channel_id if interaction.channel_id is not None else 0,
            dm_message_id=None,
        )
        await interaction.response.send_message(response, ephemeral=True)


class SubproblemSubmitSelect(discord.ui.Select):
    def __init__(
            self,
            interface: "Interface",
            owner_user_id: int,
            potd_id: int,
            season_id: int,
            subproblems: list,
    ):
        options = [
            discord.SelectOption(
                label=f'{row[1]} ({row[3]} marks)',
                description=f'Subproblem ID {row[0]}',
                value=str(row[0]),
            )
            for row in subproblems[:25]
        ]
        super().__init__(
            placeholder='Select a subproblem to submit for',
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f'subproblem_submit:{potd_id}:{owner_user_id}',
        )
        self.interface = interface
        self.owner_user_id = owner_user_id
        self.potd_id = potd_id
        self.season_id = season_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user is None or interaction.user.id != self.owner_user_id:
            await interaction.response.send_message('This menu is not for you.', ephemeral=True)
            return

        subproblem_id = int(self.values[0])
        modal = SubproblemSubmitModal(self.interface, self.potd_id, self.season_id, subproblem_id)
        await interaction.response.send_modal(modal)


class SubproblemSubmitView(discord.ui.View):
    def __init__(
            self,
            interface: "Interface",
            owner_user_id: int,
            potd_id: int,
            season_id: int,
            subproblems: list,
    ):
        super().__init__(timeout=300)
        self.add_item(SubproblemSubmitSelect(interface, owner_user_id, potd_id, season_id, subproblems))


class PendingSubproblemSelect(discord.ui.Select):
    def __init__(
            self,
            interface: "Interface",
            owner_user_id: int,
            potd_id: int,
            subproblems: list,
    ):
        options = [
            discord.SelectOption(
                label=f'{row[1]} ({row[3]} marks)',
                description=f'ID {row[0]} | {"manual" if bool(row[5]) else "auto"}',
                value=str(row[0]),
            )
            for row in subproblems[:25]
        ]
        super().__init__(
            placeholder='Choose the subproblem for this submission',
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f'pending_subproblem:{potd_id}:{owner_user_id}',
        )
        self.interface = interface
        self.owner_user_id = owner_user_id
        self.potd_id = potd_id
        self.subproblem_by_id = {row[0]: row for row in subproblems}

    async def callback(self, interaction: discord.Interaction):
        if interaction.user is None or interaction.user.id != self.owner_user_id:
            await interaction.response.send_message('This selector is not for you.')
            return

        pending = self.interface.pending_subproblem_prompts.get(self.owner_user_id)
        if pending is None:
            await interaction.response.send_message(
                'This submission selector has expired. Send your answer again.',
            )
            return

        if pending['expires_at'] < datetime.utcnow() or pending['potd_id'] != self.potd_id:
            self.interface.pending_subproblem_prompts.pop(self.owner_user_id, None)
            await interaction.response.send_message(
                'This submission selector has expired. Send your answer again.',
            )
            return

        selected_id = int(self.values[0])
        selected = self.subproblem_by_id.get(selected_id)
        if selected is None:
            await interaction.response.send_message('Invalid subproblem selection.')
            return

        await interaction.response.defer(thinking=False)

        response_text = await self.interface._submit_pending_subproblem_answer(interaction.user, pending, selected)
        self.interface.pending_subproblem_prompts.pop(self.owner_user_id, None)

        if self.view is not None:
            for item in self.view.children:
                item.disabled = True
            try:
                if interaction.message is not None:
                    await interaction.message.edit(view=self.view)
            except (discord.Forbidden, discord.HTTPException):
                pass

        await interaction.followup.send(response_text)


class PendingSubproblemView(discord.ui.View):
    def __init__(
            self,
            interface: "Interface",
            owner_user_id: int,
            potd_id: int,
            subproblems: list,
    ):
        super().__init__(timeout=600)
        self.add_item(PendingSubproblemSelect(interface, owner_user_id, potd_id, subproblems))


class Interface(commands.Cog):
    def __init__(self, bot: openpotd.OpenPOTD):
        self.bot = bot
        self.logger = logging.getLogger('interface')
        self.cooldowns = {}
        self._registered_manual_review_submission_ids = set()
        self.pending_subproblem_prompts = {}

    def _filter_allowed_servers(self, servers):
        return [server for server in servers if self.bot.is_allowed_guild_id(server[0])]

    def _is_authorised_marker(self, user: discord.abc.User, guild: discord.Guild | None) -> bool:
        authorised = self.bot.config.get('authorised') or []
        if isinstance(authorised, int):
            if user.id == authorised:
                return True
        elif user.id in set(authorised):
            return True

        if guild is None or not isinstance(user, discord.Member):
            return False

        cursor = self.bot.db.cursor()
        cursor.execute('SELECT submission_ping_role_id FROM config WHERE server_id = ?', (guild.id,))
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return False

        reviewer_role_id = int(row[0])
        return any(role.id == reviewer_role_id for role in user.roles)

    @staticmethod
    def _build_review_thread_name(submission_id: int, potd_id: int, status: str, decision: Optional[bool]) -> str:
        if status == 'reviewed':
            outcome = 'correct' if decision else 'incorrect'
            name = f'✅ {outcome} - potw-{potd_id} - sub-{submission_id}'
        elif status == 'claimed':
            name = f'🟡 claimed - potw-{potd_id} - sub-{submission_id}'
        else:
            name = f'📝 pending - potw-{potd_id} - sub-{submission_id}'
        return name[:100]

    def _build_manual_review_embed(
            self,
            submission_id: int,
            user_id: int,
            potd_id: int,
            season_id: int,
            subproblem_label: Optional[str],
            subproblem_marks: Optional[int],
            status: str,
            claimed_by: Optional[int],
            reviewer_id: Optional[int],
            decision: Optional[bool],
            otd_prefix: Optional[str],
    ) -> discord.Embed:
        label = shared.format_otd_label(otd_prefix or self.bot.config['otd_prefix'])

        if status == 'reviewed':
            status_text = 'Reviewed'
            colour = discord.Color.green() if decision else discord.Color.red()
        elif status == 'claimed':
            status_text = 'Claimed'
            colour = discord.Color.orange()
        else:
            status_text = 'Pending'
            colour = discord.Color.gold()

        embed = discord.Embed(
            title=f'{label} Manual Review',
            colour=colour,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name='Submission ID', value=f'`{submission_id}`', inline=True)
        embed.add_field(name='Status', value=f'`{status_text}`', inline=True)
        embed.add_field(name='User', value=f'<@{user_id}> (`{user_id}`)', inline=False)
        embed.add_field(name='Problem', value=f'{label} `{potd_id}` (Season `{season_id}`)', inline=False)
        if subproblem_label is not None:
            marks_display = f' (`{subproblem_marks}` marks)' if subproblem_marks is not None else ''
            embed.add_field(name='Subproblem', value=f'`{subproblem_label}`{marks_display}', inline=True)
        embed.add_field(name='Claimed By', value=f'<@{claimed_by}>' if claimed_by else '`Unclaimed`', inline=True)

        if status == 'reviewed':
            result_text = 'Correct' if decision else 'Incorrect'
            embed.add_field(name='Reviewed By', value=f'<@{reviewer_id}>' if reviewer_id else '`Unknown`', inline=True)
            embed.add_field(name='Result', value=f'`{result_text}`', inline=True)
            embed.set_footer(text='Review complete.')
        else:
            embed.set_footer(text='Use buttons below to claim and mark this submission.')

        return embed

    def _build_manual_review_view(self, submission_id: int, status: str) -> ManualReviewView:
        return ManualReviewView(self, submission_id, status)

    def _register_manual_review_view(self, submission_id: int, control_message_id: Optional[int], status: str):
        if status == 'reviewed':
            return
        if submission_id in self._registered_manual_review_submission_ids:
            return

        self.bot.add_view(self._build_manual_review_view(submission_id, status))
        self._registered_manual_review_submission_ids.add(submission_id)

    async def _get_channel_by_id(self, channel_id: Optional[int]):
        if channel_id is None:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            channel = await self.bot.fetch_channel(channel_id)
            return channel
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None

    async def _sync_manual_submission_review_messages(self, submission_id: int):
        cursor = self.bot.db.cursor()
        cursor.execute(
            'SELECT id, user_id, potd_id, subproblem_id, season_id, status, claimed_by, reviewer_id, decision '
            'FROM manual_submissions WHERE id = ?',
            (submission_id,),
        )
        submission = cursor.fetchone()
        if submission is None:
            return

        _, user_id, potd_id, subproblem_id, season_id, status, claimed_by, reviewer_id, decision = submission
        subproblem_label = None
        subproblem_marks = None
        if subproblem_id is not None:
            cursor.execute('SELECT label, marks FROM subproblems WHERE id = ?', (subproblem_id,))
            sub = cursor.fetchone()
            if sub is not None:
                subproblem_label = sub[0]
                subproblem_marks = sub[1]

        cursor.execute(
            'SELECT msm.server_id, msm.channel_id, msm.message_id, msm.thread_id, msm.control_message_id, config.otd_prefix '
            'FROM manual_submission_messages msm '
            'LEFT JOIN config ON config.server_id = msm.server_id '
            'WHERE msm.submission_id = ?',
            (submission_id,),
        )
        mirrored = cursor.fetchall()

        for server_id, channel_id, message_id, thread_id, control_message_id, otd_prefix in mirrored:
            thread_channel = await self._get_channel_by_id(thread_id) if thread_id else None
            parent_channel = await self._get_channel_by_id(channel_id)
            target_channel = thread_channel if thread_channel is not None else parent_channel

            if target_channel is None:
                continue

            embed = self._build_manual_review_embed(
                submission_id=submission_id,
                user_id=user_id,
                potd_id=potd_id,
                season_id=season_id,
                subproblem_label=subproblem_label,
                subproblem_marks=subproblem_marks,
                status=status,
                claimed_by=claimed_by,
                reviewer_id=reviewer_id,
                decision=bool(decision) if decision is not None else None,
                otd_prefix=otd_prefix,
            )
            view = self._build_manual_review_view(submission_id, status)
            keep_original_embed = (control_message_id is not None and control_message_id == message_id)

            control_message = None
            if control_message_id is not None:
                fetch_channels = []
                if thread_channel is not None:
                    fetch_channels.append(thread_channel)
                if parent_channel is not None and parent_channel not in fetch_channels:
                    fetch_channels.append(parent_channel)

                for fetch_channel in fetch_channels:
                    try:
                        control_message = await fetch_channel.fetch_message(control_message_id)
                        break
                    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                        continue

            if control_message is None:
                try:
                    control_message = await shared.send_with_auto_publish(
                        target_channel,
                        embed=embed,
                        view=view,
                        logger=self.logger,
                        log_prefix='MANUAL REVIEW',
                    )
                    control_message_id = control_message.id
                    cursor.execute(
                        'UPDATE manual_submission_messages SET control_message_id = ? WHERE submission_id = ? AND server_id = ?',
                        (control_message_id, submission_id, server_id),
                    )
                    self.bot.db.commit()
                except (discord.Forbidden, discord.HTTPException):
                    control_message = None
            else:
                try:
                    if keep_original_embed:
                        await control_message.edit(view=view)
                    else:
                        await control_message.edit(embed=embed, view=view)
                except (discord.Forbidden, discord.HTTPException):
                    pass

            self._register_manual_review_view(submission_id, control_message_id, status)

            if thread_id is not None:
                thread = await self._get_channel_by_id(thread_id)
                if isinstance(thread, discord.Thread):
                    new_name = self._build_review_thread_name(
                        submission_id=submission_id,
                        potd_id=potd_id,
                        status=status,
                        decision=bool(decision) if decision is not None else None,
                    )
                    try:
                        if status == 'reviewed':
                            await thread.edit(name=new_name, archived=True, locked=True)
                        elif thread.name != new_name:
                            await thread.edit(name=new_name)
                    except (discord.Forbidden, discord.HTTPException):
                        pass

        self.bot.db.commit()

    async def _post_action_note_to_submission_threads(self, submission_id: int, content: str):
        cursor = self.bot.db.cursor()
        cursor.execute(
            'SELECT DISTINCT thread_id FROM manual_submission_messages WHERE submission_id = ? AND thread_id IS NOT NULL',
            (submission_id,),
        )
        for (thread_id,) in cursor.fetchall():
            thread = await self._get_channel_by_id(thread_id)
            if isinstance(thread, discord.Thread):
                try:
                    await thread.send(content)
                except (discord.Forbidden, discord.HTTPException):
                    continue

    async def _notify_manual_submission_claimed(self, submission_id: int, claimer_id: int):
        cursor = self.bot.db.cursor()
        cursor.execute(
            'SELECT user_id, potd_id FROM manual_submissions WHERE id = ?',
            (submission_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return

        user_id, potd_id = row
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except (discord.NotFound, discord.HTTPException):
                return

        marker = self.bot.get_user(claimer_id)
        marker_text = f'<@{claimer_id}>' if marker is None else marker.mention
        try:
            await user.send(
                f'Your submission `{submission_id}` for {shared.config_otd_label(self.bot.config)} `{potd_id}` '
                f'is now being reviewed by {marker_text}.'
            )
        except (discord.Forbidden, discord.HTTPException):
            return

    async def _notify_manual_submission_result(
            self,
            user_id: int,
            submission_id: int,
            potd_id: int,
            is_correct: bool,
            num_attempts: int,
            solved_now: bool,
            subproblem_id: int | None = None,
            reviewer_note: str | None = None,
    ):
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except (discord.NotFound, discord.HTTPException):
                return

        label = shared.config_otd_label(self.bot.config)
        subproblem_text = ''
        if subproblem_id is not None:
            cursor = self.bot.db.cursor()
            cursor.execute('SELECT label, marks FROM subproblems WHERE id = ?', (subproblem_id,))
            sub = cursor.fetchone()
            if sub is not None:
                subproblem_text = f' (Subproblem `{sub[0]}`, `{sub[1]}` marks)'

        if is_correct and subproblem_id is not None:
            body = (
                f'Your submission `{submission_id}` for {label} `{potd_id}`{subproblem_text} '
                f'was marked **correct**.'
            )
        elif is_correct:
            if solved_now:
                body = (
                    f'Your submission `{submission_id}` for {label} `{potd_id}`{subproblem_text} '
                    f'was marked **correct**. '
                    f'You solved it after `{num_attempts}` attempt(s).'
                )
            else:
                body = (
                    f'Your submission `{submission_id}` for {label} `{potd_id}`{subproblem_text} '
                    f'was marked **correct**, '
                    f'but you had already solved this problem.'
                )
        else:
            body = (
                f'Your submission `{submission_id}` for {label} `{potd_id}`{subproblem_text} '
                f'was marked **incorrect**. '
                f'You currently have `{num_attempts}` official attempt(s).'
            )

        clean_note = (reviewer_note or '').strip()
        if clean_note:
            note_prefix = '\n\nMessage from reviewer:\n'
            available = 2000 - len(body) - len(note_prefix)
            if available > 0:
                clipped = clean_note[:available]
                if len(clean_note) > len(clipped) and len(clipped) > 15:
                    clipped = clipped[:-15] + '...[truncated]'
                body += note_prefix + clipped

        try:
            await user.send(body)
        except (discord.Forbidden, discord.HTTPException):
            return

    async def claim_manual_submission(self, submission_id: int, claimer_id: int):
        cursor = self.bot.db.cursor()
        cursor.execute(
            'SELECT status, claimed_by FROM manual_submissions WHERE id = ?',
            (submission_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return False, f'No manual submission with ID `{submission_id}`.'

        status, claimed_by = row
        if status == 'reviewed':
            return False, f'Submission `{submission_id}` has already been reviewed.'

        if claimed_by is not None and claimed_by != claimer_id:
            return False, f'Submission `{submission_id}` is already claimed by <@{claimed_by}>.'

        if status == 'claimed' and claimed_by == claimer_id:
            return True, f'Submission `{submission_id}` is already claimed by you.'

        cursor.execute(
            'UPDATE manual_submissions SET status = ?, claimed_by = ?, claimed_at = ? WHERE id = ?',
            ('claimed', claimer_id, datetime.utcnow(), submission_id),
        )
        self.bot.db.commit()

        await self._sync_manual_submission_review_messages(submission_id)
        await self._post_action_note_to_submission_threads(submission_id, f'Claimed by <@{claimer_id}>.')
        await self._notify_manual_submission_claimed(submission_id, claimer_id)
        return True, f'Claimed submission `{submission_id}`.'

    async def handle_manual_review_action(self, interaction: discord.Interaction, submission_id: int, action: str):
        if interaction.user is None or not self._is_authorised_marker(interaction.user, interaction.guild):
            if interaction.response.is_done():
                await interaction.followup.send('You are not authorised to use this action.', ephemeral=True)
            else:
                await interaction.response.send_message('You are not authorised to use this action.', ephemeral=True)
            return

        if action == 'claim':
            await interaction.response.defer(ephemeral=True, thinking=True)
            success, details = await self.claim_manual_submission(submission_id, interaction.user.id)
            await interaction.followup.send(details, ephemeral=True)
            return
        elif action == 'correct':
            await interaction.response.send_modal(ManualReviewDecisionModal(self, submission_id, True))
            return
        elif action == 'incorrect':
            await interaction.response.send_modal(ManualReviewDecisionModal(self, submission_id, False))
            return
        else:
            await interaction.response.send_message('Unknown action.', ephemeral=True)
            return

    async def cog_load(self):
        cursor = self.bot.db.cursor()
        cursor.execute(
            'SELECT msm.submission_id, msm.control_message_id, ms.status '
            'FROM manual_submission_messages msm '
            'INNER JOIN manual_submissions ms ON ms.id = msm.submission_id '
            'WHERE msm.control_message_id IS NOT NULL AND ms.status != ?',
            ('reviewed',),
        )
        rows = cursor.fetchall()

        for submission_id, control_message_id, status in rows:
            self._register_manual_review_view(submission_id, control_message_id, status)

        if rows:
            self.logger.info(f'Restored {len(rows)} manual-review control view(s).')

    @commands.command()
    @commands.check(lambda ctx: False)  # This command is disabled since it only applies for multi-server config
    async def register(self, ctx, *, season):
        cursor = self.bot.db.cursor()
        cursor.execute('''SELECT id from seasons where name = ? and server_id = ?''', (season, ctx.guild.id))
        ids = cursor.fetchall()
        if len(ids) == 0:
            await ctx.send('No such season!')
            return
        else:
            season_id = ids[0][0]
        cursor.execute('''INSERT OR IGNORE INTO users (discord_id, nickname, anonymous) VALUES (?, ?, ?)''',
                       (ctx.author.id, ctx.author.display_name, True))

        cursor.execute('''SELECT EXISTS (SELECT 1 from registrations WHERE registrations.user_id = ? 
                            AND registrations.season_id = ?)''', (ctx.author.id, season_id))
        existence = cursor.fetchall()[0][0]
        if existence:
            await ctx.send("You've already signed up for this season!")
            return
        else:
            cursor.execute('''INSERT into registrations (user_id, season_id) VALUES (?, ?)''',
                           (ctx.author.id, season_id))
            await ctx.send(f"Registered you for {season}. ")
        self.bot.db.commit()

    def update_rankings(self, season: int, potd_id: int = -1):
        cursor = self.bot.db.cursor()

        # NOTE: THE OPENPOTD TEAM IS USING A NEW SCORING SYSTEM FOR SEASON 11. IF THIS DOES NOT APPLY TO YOU,
        # REMOVE THIS.
        if season == 11:
            # Get all solves this season
            cursor.execute('select solves.user, solves.problem_id, solves.num_attempts from problems left join solves '
                           'where problems.season = ? and problems.id = solves.problem_id and official = ?',
                           (season, True))
            solves = cursor.fetchall()

            # Get all ranked people
            cursor.execute('select user_id from rankings where season_id = ?', (season,))
            ranked_users = cursor.fetchall()

            # Calculate scores of each person
            total_score = {user[0]: 0 for user in ranked_users}
            for solve in solves:
                total_score[solve[0]] += 8 - solve[2]

        else:
            # Get all solves this season
            cursor.execute('select solves.user, solves.problem_id, solves.num_attempts from problems left join solves '
                           'where problems.season = ? and problems.id = solves.problem_id and official = ?',
                           (season, True))
            solves = cursor.fetchall()

            # Get weighted attempts for each problem
            weighted_attempts = {}
            if season > 11:
                for solve in solves:
                    if solve[1] in weighted_attempts:
                        weighted_attempts[solve[1]] += weighted_score_new(solve[2])
                    else:
                        weighted_attempts[solve[1]] = weighted_score_new(solve[2])
            else:
                for solve in solves:
                    if solve[1] in weighted_attempts:
                        weighted_attempts[solve[1]] += weighted_score(solve[2])
                    else:
                        weighted_attempts[solve[1]] = weighted_score(solve[2])

            # Calculate how many points each problem should be worth on the 1st attempt
            if season > 11:
                problem_points = {i: self.bot.config['base_points'] / (weighted_attempts[i] + 3) for i in
                                  weighted_attempts}
            else:
                problem_points = {i: self.bot.config['base_points'] / weighted_attempts[i] for i in weighted_attempts}

            # Get all ranked people
            cursor.execute('select user_id from rankings where season_id = ?', (season,))
            ranked_users = cursor.fetchall()

            # Calculate scores of each person
            total_score = {user[0]: 0 for user in ranked_users}
            if season > 11: 
                for solve in solves:
                    total_score[solve[0]] += problem_points[solve[1]] * weighted_score_new(solve[2])
            else: 
                for solve in solves:
                    total_score[solve[0]] += problem_points[solve[1]] * weighted_score(solve[2])

            if potd_id == -1:
                # Then we shall update all the potds
                cursor.executemany('UPDATE problems SET weighted_solves = ?, base_points = ? WHERE problems.id = ?',
                                   [(weighted_attempts[i], problem_points[i], i) for i in weighted_attempts])
            else:
                # Only update the specified potd
                if potd_id in weighted_attempts:
                    cursor.execute('UPDATE problems SET weighted_solves = ?, base_points = ? WHERE problems.id = ?',
                                   (weighted_attempts[potd_id], problem_points[potd_id], potd_id))
                else:
                    self.logger.error(f'No potd with id {potd_id} present. Cannot refresh stats [update_rankings]')

        # Log stuff
        self.logger.info('Updating rankings')

        # Prepare data to be put into the db
        total_score_list = [(i, total_score[i]) for i in total_score]
        total_score_list.sort(key=lambda x: -x[1])
        cursor.executemany('update rankings SET rank = ?, score = ? WHERE user_id = ? and season_id = ?',
                           [(i + 1, total_score_list[i][1], total_score_list[i][0], season) for i in
                            range(len(total_score_list))])

        # Commit
        self.bot.db.commit()

    async def update_embed(self, potd_id: int):
        cursor = self.bot.db.cursor()
        cursor.execute('SELECT config.server_id, potd_channel, otd_prefix, message_id from config left join '
                       'stats_messages ON config.server_id = stats_messages.server_id WHERE stats_messages.id '
                       'is NOT NULL and stats_messages.potd_id = ?;', (potd_id,))
        servers = self._filter_allowed_servers(cursor.fetchall())

        problem = shared.POTD(potd_id, self.bot.db)

        for server_data in servers:
            potd_channel: discord.TextChannel = self.bot.get_channel(server_data[1])
            if potd_channel is not None:
                try:
                    stats_message = await potd_channel.fetch_message(server_data[3])
                    embed = problem.build_embed(self.bot.db, False, server_data[2])
                    await stats_message.edit(embed=embed)
                except discord.errors.NotFound as e:
                    self.logger.warning(f'[UPDATE_EMBED] Server {server_data[0]} no message id {server_data[3]}')

    def refresh(self, season: int, potd_id: int):
        # Update the rankings in the db
        self.update_rankings(season, potd_id)

        # Update the embed showing stats
        self.bot.loop.create_task(self.update_embed(potd_id))

    async def forward_auto_submission(
            self,
            user: discord.User,
            potd_id: int,
            season_id: int,
            answer: int,
            attempt_number: int,
            is_correct: bool,
    ):
        cursor = self.bot.db.cursor()
        cursor.execute(
            'SELECT server_id, submission_channel_id, otd_prefix '
            'FROM config WHERE submission_channel_id IS NOT NULL'
        )
        servers = self._filter_allowed_servers(cursor.fetchall())

        status_text = 'Correct' if is_correct else 'Incorrect'
        status_colour = discord.Color.green() if is_correct else discord.Color.red()

        for server_id, submission_channel_id, otd_prefix in servers:
            guild = self.bot.get_guild(server_id)
            if guild is None:
                continue
            if guild.get_member(user.id) is None:
                continue

            channel: discord.TextChannel = self.bot.get_channel(submission_channel_id)
            if channel is None:
                self.logger.warning(f'[SUBMISSION MIRROR] No channel {submission_channel_id} in guild {server_id}')
                continue

            label = shared.format_otd_label(otd_prefix or self.bot.config["otd_prefix"])
            embed = discord.Embed(
                title=f'{label} Submission',
                colour=status_colour,
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name='User', value=f'<@{user.id}> (`{user.id}`)', inline=False)
            embed.add_field(name='Problem', value=f'{label} `{potd_id}` (Season `{season_id}`)', inline=False)
            embed.add_field(name='Answer', value=f'`{answer}`', inline=True)
            embed.add_field(name='Attempt', value=f'`{attempt_number}`', inline=True)
            embed.add_field(name='Auto Check', value=f'`{status_text}`', inline=True)

            try:
                await shared.send_with_auto_publish(
                    channel,
                    embed=embed,
                    logger=self.logger,
                    log_prefix='SUBMISSION MIRROR',
                )
            except Exception as e:
                self.logger.warning(f'[SUBMISSION MIRROR] Failed in guild {server_id}: {e}')

    @staticmethod
    def _try_parse_submission_int(content: str):
        if not content:
            return None
        s = content.strip()
        if not s:
            return None
        if not (s[1:].isdecimal() if s[0] in ('-', '+') else s.isdecimal()):
            return None
        value = int(s)
        if not -9223372036854775808 <= value <= 9223372036854775807:
            return None
        return value

    def _get_subproblems_for_problem(self, potd_id: int):
        cursor = self.bot.db.cursor()
        cursor.execute(
            'SELECT id, label, statement, marks, answer, manual_marking FROM subproblems WHERE potd_id = ? '
            'ORDER BY order_index ASC, id ASC',
            (potd_id,),
        )
        return cursor.fetchall()

    def _get_subproblem_by_id(self, subproblem_id: int):
        cursor = self.bot.db.cursor()
        cursor.execute(
            'SELECT id, potd_id, label, statement, marks, answer, manual_marking FROM subproblems WHERE id = ?',
            (subproblem_id,),
        )
        return cursor.fetchone()

    def _resolve_subproblem_fetch(self, potd_id: int, reference: str):
        cursor = self.bot.db.cursor()
        cursor.execute(
            'SELECT id, label, statement, marks, manual_marking FROM subproblems '
            'WHERE potd_id = ? ORDER BY order_index ASC, id ASC',
            (potd_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            return None

        clean = (reference or '').strip().lower()
        if not clean:
            return None

        if clean.isdecimal():
            raw_number = int(clean)
            for row in rows:
                if row[0] == raw_number:
                    return row
            if 1 <= raw_number <= len(rows):
                return rows[raw_number - 1]

        for idx, row in enumerate(rows, start=1):
            label = str(row[1]).strip().lower()
            if clean == label:
                return row
            if clean in {f'q{label}', f'subproblem {label}', f'question {label}'}:
                return row
            if clean in {f'q{idx}', f'subproblem {idx}', f'question {idx}'}:
                return row

        return None

    def _parse_subproblem_choice(self, choice: str, subproblems: list):
        clean = (choice or '').strip().lower()
        if not clean:
            return None

        by_id = {str(row[0]): row for row in subproblems}
        if clean in by_id:
            return by_id[clean]

        for idx, row in enumerate(subproblems, start=1):
            label = str(row[1]).strip().lower()
            if clean == label:
                return row
            if clean in {f'q{label}', f'question {label}', f'subproblem {label}'}:
                return row
            if clean == str(idx):
                return row
            if clean in {f'q{idx}', f'question {idx}', f'subproblem {idx}'}:
                return row
        return None

    @staticmethod
    async def _attachments_to_files(attachments):
        files = []
        for attachment in attachments or []:
            try:
                files.append(await attachment.to_file())
            except Exception:
                continue
        return files

    async def forward_subproblem_auto_submission(
            self,
            user: discord.User,
            potd_id: int,
            season_id: int,
            subproblem_label: str,
            marks: int,
            answer: int | None,
            attempt_number: int,
            is_correct: bool,
    ):
        cursor = self.bot.db.cursor()
        cursor.execute(
            'SELECT server_id, submission_channel_id, otd_prefix '
            'FROM config WHERE submission_channel_id IS NOT NULL'
        )
        servers = self._filter_allowed_servers(cursor.fetchall())

        status_text = 'Correct' if is_correct else 'Incorrect'
        status_colour = discord.Color.green() if is_correct else discord.Color.red()

        for server_id, submission_channel_id, otd_prefix in servers:
            guild = self.bot.get_guild(server_id)
            if guild is None:
                continue
            if guild.get_member(user.id) is None:
                continue

            channel: discord.TextChannel = self.bot.get_channel(submission_channel_id)
            if channel is None:
                continue

            label = shared.format_otd_label(otd_prefix or self.bot.config["otd_prefix"])
            embed = discord.Embed(
                title=f'{label} Subproblem Submission',
                colour=status_colour,
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name='User', value=f'<@{user.id}> (`{user.id}`)', inline=False)
            embed.add_field(name='Problem', value=f'{label} `{potd_id}` (Season `{season_id}`)', inline=False)
            embed.add_field(name='Subproblem', value=f'`{subproblem_label}` (`{marks}` marks)', inline=True)
            if answer is not None:
                embed.add_field(name='Answer', value=f'`{answer}`', inline=True)
            embed.add_field(name='Attempt', value=f'`{attempt_number}`', inline=True)
            embed.add_field(name='Auto Check', value=f'`{status_text}`', inline=True)

            try:
                await shared.send_with_auto_publish(
                    channel,
                    embed=embed,
                    logger=self.logger,
                    log_prefix='SUBPROBLEM SUBMISSION',
                )
            except Exception:
                continue

    async def process_subproblem_submission(
            self,
            user: discord.User,
            potd_id: int,
            season_id: int,
            subproblem_id: int,
            content: str,
            attachments: list,
            dm_channel_id: int,
            dm_message_id: int | None,
    ) -> str:
        subproblem = self._get_subproblem_by_id(subproblem_id)
        if subproblem is None or subproblem[1] != potd_id:
            return 'Invalid subproblem selection.'

        _, _, sub_label, _, marks, sub_answer, sub_manual_marking = subproblem
        sub_manual_marking = bool(sub_manual_marking)

        if sub_manual_marking:
            submission_id = await self.create_manual_submission_record(
                user_id=user.id,
                potd_id=potd_id,
                season_id=season_id,
                content=content,
                dm_channel_id=dm_channel_id,
                dm_message_id=dm_message_id,
                subproblem_id=subproblem_id,
            )
            files = await self._attachments_to_files(attachments)
            await self.mirror_manual_submission_payload(
                user=user,
                submission_id=submission_id,
                potd_id=potd_id,
                season_id=season_id,
                content=content,
                attachments=files,
                subproblem_id=subproblem_id,
                dm_channel_id=dm_channel_id,
                dm_message_id=dm_message_id,
            )
            if self.bot.config.get('cooldown'):
                cursor = self.bot.db.cursor()
                cursor.execute(
                    'SELECT count() from manual_submissions where user_id = ? and potd_id = ? and subproblem_id = ?',
                    (user.id, potd_id, subproblem_id),
                )
                manual_attempts = cursor.fetchone()[0]
                cool_down = 10 if manual_attempts < 5 else 1800 if manual_attempts == 5 else 1000000
                self.cooldowns[user.id] = datetime.utcnow() + dt.timedelta(seconds=cool_down)

            return (
                f'Thank you! Submitted for subproblem `{sub_label}` (`{marks}` marks). '
                f'Review ID: `{submission_id}`.'
            )

        if sub_answer is None:
            return (
                f'Subproblem `{sub_label}` is set to auto-check but has no answer configured. '
                f'Please ask staff to fix this.'
            )

        parsed = self._try_parse_submission_int(content)
        if parsed is None:
            return f'Subproblem `{sub_label}` expects an integer answer.'

        cursor = self.bot.db.cursor()
        is_correct = (sub_answer is not None and parsed == int(sub_answer))
        cursor.execute(
            'INSERT INTO subproblem_attempts (user_id, potd_id, subproblem_id, submission, submit_time, is_correct) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (user.id, potd_id, subproblem_id, parsed, datetime.utcnow(), is_correct),
        )
        cursor.execute(
            'SELECT COUNT(1) FROM subproblem_attempts WHERE user_id = ? AND subproblem_id = ?',
            (user.id, subproblem_id),
        )
        num_attempts = cursor.fetchone()[0]
        cursor.execute(
            'SELECT EXISTS (SELECT 1 FROM subproblem_solves WHERE user_id = ? AND subproblem_id = ? AND official = ?)',
            (user.id, subproblem_id, True),
        )
        solved_before = bool(cursor.fetchone()[0])
        solved_now = False
        if is_correct and not solved_before:
            cursor.execute(
                'INSERT INTO subproblem_solves (user_id, potd_id, subproblem_id, num_attempts, official) '
                'VALUES (?, ?, ?, ?, ?)',
                (user.id, potd_id, subproblem_id, num_attempts, True),
            )
            solved_now = True
        self.bot.db.commit()

        await self.forward_subproblem_auto_submission(
            user=user,
            potd_id=potd_id,
            season_id=season_id,
            subproblem_label=sub_label,
            marks=marks,
            answer=parsed,
            attempt_number=num_attempts,
            is_correct=is_correct,
        )

        if self.bot.config.get('cooldown'):
            cool_down = 10 if num_attempts < 5 else 1800 if num_attempts == 5 else 1000000
            self.cooldowns[user.id] = datetime.utcnow() + dt.timedelta(seconds=cool_down)

        if is_correct:
            if solved_now:
                return (
                    f'Correct for subproblem `{sub_label}`. '
                    f'You solved it after `{num_attempts}` attempt(s).'
                )
            return f'Correct for subproblem `{sub_label}`, but you already solved it.'
        return f'Incorrect for subproblem `{sub_label}`. Attempts: `{num_attempts}`.'

    async def create_manual_submission_record(
            self,
            user_id: int,
            potd_id: int,
            season_id: int,
            content: str,
            dm_channel_id: int,
            dm_message_id: int | None,
            subproblem_id: int | None = None,
    ) -> int:
        cursor = self.bot.db.cursor()
        cursor.execute(
            'INSERT INTO manual_submissions '
            '(user_id, potd_id, subproblem_id, season_id, content, submitted_at, dm_channel_id, dm_message_id) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (
                user_id,
                potd_id,
                subproblem_id,
                season_id,
                content,
                datetime.utcnow(),
                dm_channel_id,
                dm_message_id,
            ),
        )
        self.bot.db.commit()
        return cursor.lastrowid

    async def create_manual_submission(
            self,
            message: discord.Message,
            potd_id: int,
            season_id: int,
            subproblem_id: int | None = None,
    ) -> int:
        return await self.create_manual_submission_record(
            user_id=message.author.id,
            potd_id=potd_id,
            season_id=season_id,
            content=message.content,
            dm_channel_id=message.channel.id,
            dm_message_id=message.id,
            subproblem_id=subproblem_id,
        )

    async def mirror_manual_submission(
            self,
            message: discord.Message,
            submission_id: int,
            potd_id: int,
            season_id: int,
            subproblem_id: int | None = None,
    ):
        files = await self._attachments_to_files(message.attachments)
        await self.mirror_manual_submission_payload(
            user=message.author,
            submission_id=submission_id,
            potd_id=potd_id,
            season_id=season_id,
            content=message.content,
            attachments=files,
            subproblem_id=subproblem_id,
            dm_channel_id=message.channel.id,
            dm_message_id=message.id,
        )

    async def _get_manual_submission_source_message(
            self,
            dm_channel_id: int | None,
            dm_message_id: int | None,
    ) -> discord.Message | None:
        if dm_channel_id is None or dm_message_id is None:
            return None

        channel = await self._get_channel_by_id(dm_channel_id)
        if not isinstance(channel, discord.DMChannel):
            return None

        try:
            return await channel.fetch_message(dm_message_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None

    async def mirror_manual_submission_payload(
            self,
            user: discord.User,
            submission_id: int,
            potd_id: int,
            season_id: int,
            content: str,
            attachments: list,
            subproblem_id: int | None = None,
            dm_channel_id: int | None = None,
            dm_message_id: int | None = None,
    ):
        cursor = self.bot.db.cursor()
        cursor.execute(
            'SELECT server_id, submission_channel_id, submission_ping_role_id, otd_prefix '
            'FROM config WHERE submission_channel_id IS NOT NULL'
        )
        servers = self._filter_allowed_servers(cursor.fetchall())
        raw_text = content or ''
        preview = raw_text if len(raw_text) <= 1000 else raw_text[:1000] + '\n...[truncated]'
        if not preview:
            preview = '[No text content]'

        subproblem = None
        if subproblem_id is not None:
            cursor.execute(
                'SELECT id, label, marks FROM subproblems WHERE id = ? AND potd_id = ?',
                (subproblem_id, potd_id),
            )
            subproblem = cursor.fetchone()
        source_message = await self._get_manual_submission_source_message(dm_channel_id, dm_message_id)

        for server_id, submission_channel_id, ping_role_id, otd_prefix in servers:
            guild = self.bot.get_guild(server_id)
            if guild is None or guild.get_member(user.id) is None:
                continue

            channel: discord.TextChannel = self.bot.get_channel(submission_channel_id)
            if channel is None:
                self.logger.warning(f'[MANUAL SUBMISSION] No channel {submission_channel_id} in guild {server_id}')
                continue

            files = list(attachments)
            if raw_text and len(raw_text) > 1000:
                files.append(
                    discord.File(
                        io.BytesIO(raw_text.encode('utf-8')),
                        filename=f'manual-submission-{submission_id}.txt',
                    )
                )

            label = shared.format_otd_label(otd_prefix or self.bot.config["otd_prefix"])
            embed = discord.Embed(
                title=f'{label} Manual Submission',
                colour=discord.Color.gold(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name='Submission ID', value=f'`{submission_id}`', inline=True)
            embed.add_field(name='User', value=f'<@{user.id}> (`{user.id}`)', inline=True)
            embed.add_field(name='Problem', value=f'{label} `{potd_id}` (Season `{season_id}`)', inline=False)
            if subproblem is not None:
                embed.add_field(name='Subproblem', value=f'`{subproblem[1]}` (`{subproblem[2]}` marks)', inline=True)
            embed.add_field(name='Content', value=preview, inline=False)
            if files:
                embed.add_field(name='Attachments', value=f'`{len(files)}` mirrored file(s)', inline=False)

            mention_text = f'<@&{ping_role_id}>' if ping_role_id is not None else None
            try:
                control_view = self._build_manual_review_view(submission_id, 'pending')
                mirrored_message = await shared.send_with_auto_publish(
                    channel,
                    content=mention_text,
                    embed=embed,
                    files=files,
                    view=control_view,
                    logger=self.logger,
                    log_prefix='MANUAL SUBMISSION',
                )
                self._register_manual_review_view(submission_id, mirrored_message.id, 'pending')

                thread = None
                thread_id = None
                try:
                    sub_label = None if subproblem is None else str(subproblem[1])
                    thread_name = self._build_review_thread_name(
                        submission_id=submission_id,
                        potd_id=potd_id,
                        status='pending',
                        decision=None,
                    )
                    if sub_label is not None:
                        thread_name = f'{thread_name} - {sub_label}'
                        thread_name = thread_name[:100]
                    thread = await mirrored_message.create_thread(name=thread_name)
                    thread_id = thread.id
                except (discord.Forbidden, discord.HTTPException) as e:
                    self.logger.warning(f'[MANUAL SUBMISSION] Could not create thread in guild {server_id}: {e}')

                if thread is not None and source_message is not None:
                    try:
                        await source_message.forward(thread, fail_if_not_exists=False)
                    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
                        self.logger.warning(
                            f'[MANUAL SUBMISSION] Could not forward source message for submission '
                            f'{submission_id} in guild {server_id}: {e}'
                        )

                if thread is not None:
                    try:
                        thread_action_view = self._build_manual_review_view(submission_id, 'pending')
                        await shared.send_with_auto_publish(
                            thread,
                            'Review actions:',
                            view=thread_action_view,
                            logger=self.logger,
                            log_prefix='MANUAL SUBMISSION',
                        )
                    except (discord.Forbidden, discord.HTTPException) as e:
                        self.logger.warning(
                            f'[MANUAL SUBMISSION] Could not post review action buttons in thread '
                            f'{thread.id} for submission {submission_id}: {e}'
                        )

                cursor.execute(
                    'INSERT INTO manual_submission_messages '
                    '(submission_id, server_id, channel_id, message_id, thread_id, control_message_id) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (submission_id, server_id, channel.id, mirrored_message.id, thread_id, mirrored_message.id),
                )
                self.bot.db.commit()
            except Exception as e:
                self.logger.warning(f'[MANUAL SUBMISSION] Failed in guild {server_id}: {e}')

    async def review_manual_submission(
            self,
            submission_id: int,
            is_correct: bool,
            reviewer_id: int,
            require_claim: bool = False,
            reviewer_note: str | None = None,
    ):
        cursor = self.bot.db.cursor()
        cursor.execute(
            'SELECT user_id, potd_id, subproblem_id, season_id, content, status, claimed_by '
            'FROM manual_submissions WHERE id = ?',
            (submission_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return False, f'No manual submission with ID `{submission_id}`.'

        user_id, potd_id, subproblem_id, season_id, content, status, claimed_by = row
        if status == 'reviewed':
            return False, f'Submission `{submission_id}` has already been reviewed.'

        if require_claim and claimed_by != reviewer_id:
            if claimed_by is None:
                return False, f'Claim submission `{submission_id}` first.'
            return False, f'Submission `{submission_id}` is claimed by <@{claimed_by}>.'

        if claimed_by is not None and claimed_by != reviewer_id:
            return False, f'Submission `{submission_id}` is claimed by <@{claimed_by}>.'

        if claimed_by is None:
            cursor.execute(
                'UPDATE manual_submissions SET status = ?, claimed_by = ?, claimed_at = ? WHERE id = ?',
                ('claimed', reviewer_id, datetime.utcnow(), submission_id),
            )
            self.bot.db.commit()
            claimed_by = reviewer_id
            await self._notify_manual_submission_claimed(submission_id, reviewer_id)

        num_attempts = 0
        solved_now = False
        if subproblem_id is None:
            parsed_int = self._try_parse_submission_int(content)
            cursor.execute(
                'INSERT INTO attempts (user_id, potd_id, official, submission, submit_time) VALUES (?, ?, ?, ?, ?)',
                (user_id, potd_id, True, parsed_int, datetime.utcnow()),
            )

            cursor.execute('SELECT count(1) from attempts where attempts.potd_id = ? and attempts.user_id = ?',
                           (potd_id, user_id))
            num_attempts = cursor.fetchall()[0][0]

            cursor.execute('SELECT exists (select 1 from solves where problem_id = ? and solves.user = ?)',
                           (potd_id, user_id))
            already_solved = bool(cursor.fetchall()[0][0])
            if is_correct and not already_solved:
                cursor.execute('INSERT into solves (user, problem_id, num_attempts, official) VALUES (?, ?, ?, ?)',
                               (user_id, potd_id, num_attempts, True))
                solved_now = True
        else:
            cursor.execute(
                'SELECT count(1) FROM manual_submissions WHERE user_id = ? AND potd_id = ? AND subproblem_id = ? '
                'AND status = ?',
                (user_id, potd_id, subproblem_id, 'reviewed'),
            )
            num_attempts = cursor.fetchone()[0] + 1

        cursor.execute(
            'UPDATE manual_submissions SET status = ?, reviewer_id = ?, reviewed_at = ?, decision = ?, '
            'claimed_by = COALESCE(claimed_by, ?), claimed_at = COALESCE(claimed_at, ?) WHERE id = ?',
            ('reviewed', reviewer_id, datetime.utcnow(), is_correct, reviewer_id, datetime.utcnow(), submission_id),
        )

        cursor.execute(
            'SELECT server_id, channel_id, message_id FROM manual_submission_messages WHERE submission_id = ?',
            (submission_id,),
        )
        mirrored_messages = cursor.fetchall()
        self.bot.db.commit()

        if solved_now and subproblem_id is None:
            # Put a ranking entry in for them if missing.
            cursor.execute('INSERT or IGNORE into rankings (season_id, user_id) VALUES (?, ?)', (season_id, user_id))
            self.bot.db.commit()

            self.refresh(season_id, potd_id)

            cursor.execute('SELECT server_id, solved_role_id from config where solved_role_id is not null')
            servers = self._filter_allowed_servers(cursor.fetchall())
            for server_id, role_id in servers:
                guild = self.bot.get_guild(server_id)
                if guild is None:
                    continue
                member = guild.get_member(user_id)
                if member is None:
                    continue
                role = guild.get_role(role_id)
                if role is None:
                    continue
                try:
                    await member.add_roles(role, reason='Solved POTD (manual review)')
                except Exception as e:
                    self.logger.warning(f'[MANUAL REVIEW] Failed to add solved role in guild {server_id}: {e}')

        completion_emoji = '✔️'
        outcome_emoji = '✅' if is_correct else '❌'
        for server_id, channel_id, message_id in mirrored_messages:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                continue
            try:
                mirrored_message = await channel.fetch_message(message_id)
                await mirrored_message.add_reaction(completion_emoji)
                await mirrored_message.add_reaction(outcome_emoji)
            except Exception:
                continue

        result_word = 'correct' if is_correct else 'incorrect'
        note_sent_suffix = ' Reviewer note sent.' if (reviewer_note or '').strip() else ''
        await self._post_action_note_to_submission_threads(
            submission_id,
            f'Reviewed by <@{reviewer_id}>: **{result_word}**.{note_sent_suffix}',
        )
        await self._sync_manual_submission_review_messages(submission_id)
        await self._notify_manual_submission_result(
            user_id=user_id,
            submission_id=submission_id,
            potd_id=potd_id,
            is_correct=is_correct,
            num_attempts=num_attempts,
            solved_now=solved_now,
            subproblem_id=subproblem_id,
            reviewer_note=reviewer_note,
        )

        if is_correct and subproblem_id is not None:
            return True, f'Marked submission `{submission_id}` as correct.'
        if is_correct:
            if solved_now:
                return True, f'Marked submission `{submission_id}` as correct. Solve recorded.'
            return True, f'Marked submission `{submission_id}` as correct. User had already solved.'
        return True, f'Marked submission `{submission_id}` as incorrect.'

    def _pending_prompt_text(self, subproblems: list):
        lines = ['Choose the subproblem from the selector below (or reply with label/number).']
        for idx, row in enumerate(subproblems, start=1):
            mode = 'manual review' if bool(row[5]) else 'auto integer'
            lines.append(f'`{idx}` / `{row[1]}` - `{row[3]}` marks - {mode}')
        return '\n'.join(lines)

    async def _submit_pending_subproblem_answer(
            self,
            user: discord.User,
            answer_payload: dict,
            selected_subproblem: tuple,
    ):
        return await self.process_subproblem_submission(
            user=user,
            potd_id=answer_payload['potd_id'],
            season_id=answer_payload['season_id'],
            subproblem_id=selected_subproblem[0],
            content=answer_payload['content'],
            attachments=answer_payload.get('attachments', []),
            dm_channel_id=answer_payload['dm_channel_id'],
            dm_message_id=answer_payload['dm_message_id'],
        )

    @app_commands.command(name='submit', description='Submit an answer with interactive subproblem selection.')
    async def submit_slash(self, interaction: discord.Interaction):
        if interaction.guild is not None:
            await interaction.response.send_message('Use this command in DM with the bot.', ephemeral=True)
            return

        cursor = self.bot.db.cursor()
        cursor.execute(
            'SELECT answer, problems.id, seasons.id, problems.manual_marking from seasons left join problems '
            'where seasons.running = ? and problems.id = seasons.latest_potd',
            (True,),
        )
        current = cursor.fetchone()
        if current is None:
            await interaction.response.send_message('There is no current problem running.', ephemeral=True)
            return

        _, potd_id, season_id, _ = current
        subproblems = self._get_subproblems_for_problem(potd_id)
        if not subproblems:
            await interaction.response.send_message(
                'No subproblems are configured for the current problem. Send your answer as a DM message instead.',
                ephemeral=True,
            )
            return

        if interaction.user is None:
            await interaction.response.send_message('Unable to identify user.', ephemeral=True)
            return

        view = SubproblemSubmitView(self, interaction.user.id, potd_id, season_id, subproblems)
        await interaction.response.send_message(
            'Select a subproblem, then fill in the answer form.',
            view=view,
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not await self.bot.is_allowed_dm_user(message.author.id):
            return
        if message.guild is not None or message.author.id == self.bot.user.id \
                or (message.content and message.content[0] == self.bot.config['prefix']) \
                or message.author.id in self.bot.blacklist:
            return

        if self.bot.posting_problem:
            await message.channel.send('The new problem is being posted. Please wait until the bot '
                                       'status changes to submit your answer. ')
            return

        cursor = self.bot.db.cursor()

        cursor.execute('SELECT answer, problems.id, seasons.id, problems.manual_marking from seasons left join problems '
                       'where seasons.running = ? and problems.id = seasons.latest_potd', (True,))
        current = cursor.fetchone()

        cursor.execute('''INSERT OR IGNORE INTO users (discord_id, nickname, anonymous) VALUES (?, ?, ?)''',
                       (message.author.id, message.author.display_name, True))
        self.bot.db.commit()

        if current is None:
            await message.channel.send(
                f'There is no current {shared.config_otd_label(self.bot.config)} to check answers against. ')
            return

        correct_answer, potd_id, season_id, manual_marking = current
        manual_marking = bool(manual_marking)
        subproblems = self._get_subproblems_for_problem(potd_id)

        if subproblems:
            if self.bot.config['cooldown']:
                if message.author.id in self.cooldowns and self.cooldowns[message.author.id] > datetime.utcnow():
                    await message.channel.send(
                        f"You're on cooldown! Send another answer in "
                        f"{(self.cooldowns[message.author.id] - datetime.utcnow()).total_seconds():.2f} seconds. "
                    )
                    return

            pending = self.pending_subproblem_prompts.get(message.author.id)
            if pending is not None:
                # Expire pending prompts after 10 minutes.
                if pending['expires_at'] < datetime.utcnow() or pending['potd_id'] != potd_id:
                    self.pending_subproblem_prompts.pop(message.author.id, None)
                    pending = None

            if pending is not None:
                selected = self._parse_subproblem_choice(message.content, subproblems)
                if selected is None:
                    await message.channel.send(
                        'I did not recognise that subproblem choice.\n' + self._pending_prompt_text(subproblems)
                    )
                    return

                response_text = await self._submit_pending_subproblem_answer(message.author, pending, selected)
                self.pending_subproblem_prompts.pop(message.author.id, None)
                await message.channel.send(response_text)
                return

            self.pending_subproblem_prompts[message.author.id] = {
                'potd_id': potd_id,
                'season_id': season_id,
                'content': message.content,
                'attachments': list(message.attachments),
                'dm_channel_id': message.channel.id,
                'dm_message_id': message.id,
                'expires_at': datetime.utcnow() + dt.timedelta(minutes=10),
            }
            preview = message.content or '[No text content]'
            if len(preview) > 300:
                preview = preview[:300] + '\n...[truncated]'

            prompt_embed = discord.Embed(
                title='Select Subproblem',
                description=self._pending_prompt_text(subproblems),
                colour=discord.Color.blurple(),
                timestamp=discord.utils.utcnow(),
            )
            prompt_embed.add_field(name='Submission Preview', value=preview, inline=False)
            if message.attachments:
                prompt_embed.add_field(
                    name='Attachments',
                    value=f'`{len(message.attachments)}` file(s) will be included.',
                    inline=False,
                )
            prompt_view = PendingSubproblemView(self, message.author.id, potd_id, subproblems)
            await message.channel.send(embed=prompt_embed, view=prompt_view)
            return

        cursor.execute('SELECT exists (select 1 from solves where problem_id = ? and solves.user = ?)',
                       (potd_id, message.author.id))
        if cursor.fetchall()[0][0]:
            await message.channel.send(
                f'You have already solved this {shared.config_otd_label(self.bot.config, lowercase=True)}!'
            )
            return

        if self.bot.config['cooldown']:
            if message.author.id in self.cooldowns and self.cooldowns[message.author.id] > datetime.utcnow():
                await message.channel.send(
                    f"You're on cooldown! Send another answer in "
                    f"{(self.cooldowns[message.author.id] - datetime.utcnow()).total_seconds():.2f} seconds. "
                )
                return

        if manual_marking:
            cursor.execute('SELECT count() from manual_submissions where user_id = ? and potd_id = ?',
                           (message.author.id, potd_id))
            manual_attempts = cursor.fetchall()[0][0]
            if self.bot.config['cooldown']:
                cool_down = 10 if manual_attempts < 5 else 1800 if manual_attempts == 5 else 1000000
                self.cooldowns[message.author.id] = datetime.utcnow() + dt.timedelta(seconds=cool_down)

            submission_id = await self.create_manual_submission(message, potd_id, season_id)
            await self.mirror_manual_submission(message, submission_id, potd_id, season_id)
            await message.channel.send(
                f'Thank you! Your submission was sent for manual review (ID `{submission_id}`). '
                f'You will be notified when a marker claims and reviews it.'
            )
            return

        s = message.content
        if not s or not (s[1:].isdecimal() if s[0] in ('-', '+') else s.isdecimal()):
            await message.channel.send('Please provide an integer answer! ')
            return
        answer = int(s)

        if not -9223372036854775808 <= answer <= 9223372036854775807:
            await message.channel.send('Your answer is not a 64 bit signed integer (between -2^63 and 2^63 - 1). '
                                       'Please try again. ')
            return

        if self.bot.config['cooldown']:
            cursor.execute('SELECT count() from attempts where user_id = ? and potd_id = ?',
                           (message.author.id, potd_id))
            num_attempts = cursor.fetchall()[0][0]
            cool_down = 10 if num_attempts < 5 else 1800 if num_attempts == 5 else 1000000
            self.cooldowns[message.author.id] = datetime.utcnow() + dt.timedelta(seconds=cool_down)

        cursor.execute('INSERT or IGNORE into rankings (season_id, user_id) VALUES (?, ?)',
                       (season_id, message.author.id,))
        self.bot.db.commit()

        cursor.execute('INSERT into attempts (user_id, potd_id, official, submission, submit_time) '
                       'VALUES (?, ?, ?, ?, ?)',
                       (message.author.id, potd_id, True, answer, datetime.utcnow()))
        self.bot.db.commit()

        cursor.execute('SELECT count(1) from attempts where attempts.potd_id = ? and attempts.user_id = ?',
                       (potd_id, message.author.id))
        num_attempts = cursor.fetchall()[0][0]
        answer_is_correct = answer == correct_answer

        await self.forward_auto_submission(
            user=message.author,
            potd_id=potd_id,
            season_id=season_id,
            answer=answer,
            attempt_number=num_attempts,
            is_correct=answer_is_correct,
        )

        if answer_is_correct:
            cursor.execute('INSERT into solves (user, problem_id, num_attempts, official) VALUES (?, ?, ?, ?)',
                           (message.author.id, potd_id, num_attempts, True))
            self.bot.db.commit()

            self.refresh(season_id, potd_id)

            if random.random() < 0.05:
                await message.channel.send(
                    f'Correct answer, good shooting Question Hunter! Attempts: `{num_attempts}`')
            else:
                await message.channel.send(f'Thank you! You solved the problem after {num_attempts} attempts. ')

            cursor.execute('SELECT server_id, solved_role_id from config where solved_role_id is not null')
            servers = self._filter_allowed_servers(cursor.fetchall())
            for server in servers:
                guild: discord.Guild = self.bot.get_guild(server[0])
                if guild is None:
                    continue
                member: discord.Member = guild.get_member(message.author.id)
                if member is None:
                    continue
                solved_role: discord.Role = guild.get_role(server[1])
                if solved_role is None:
                    continue
                try:
                    await member.add_roles(solved_role, reason='Solved POTD')
                except Exception as e:
                    self.logger.warning(e)

            self.logger.info(
                f'User {message.author.id} just solved {shared.config_otd_label(self.bot.config)} {potd_id}. '
            )
        else:
            await message.channel.send(f'You did not solve this problem! Number of attempts: `{num_attempts}`. ')
            self.refresh(season_id, potd_id)
            self.logger.info(
                f'User {message.author.id} submitted incorrect answer {answer} '
                f'for {shared.config_otd_label(self.bot.config)} {potd_id}. '
            )

    @commands.command()
    async def score(self, ctx, season: int = None):
        cursor = self.bot.db.cursor()
        if season is None:
            cursor.execute('SELECT id, name from seasons where running = ?', (True,))
            running_seasons = cursor.fetchall()
            if len(running_seasons) == 0:
                await ctx.send('No current running season. Please specify a season. ')
                return
            else:
                season = running_seasons[0][0]
                szn_name = running_seasons[0][1]
        else:
            cursor.execute('SELECT id, name from seasons where id = ?', (season,))
            selected_seasons = cursor.fetchall()
            if len(selected_seasons) == 0:
                await ctx.send(f'No season with id {season}. Please specify a valid season. ')
                return
            else:
                season = selected_seasons[0][0]
                szn_name = selected_seasons[0][1]

        cursor.execute('SELECT rank, score from rankings where season_id = ? and user_id = ?', (season, ctx.author.id))
        rank = cursor.fetchall()
        if len(rank) == 0:
            await ctx.send('You are not ranked in this season!')
        else:
            embed = discord.Embed(title=f'{szn_name} ranking for {ctx.author.name}')
            if rank[0][0] <= 3:
                colours = [0xc9b037, 0xd7d7d7, 0xad8a56]  # gold, silver, bronze
                embed.colour = discord.Color(colours[rank[0][0] - 1])
            else:
                embed.colour = discord.Color(0xffffff)
            embed.add_field(name='Rank', value=rank[0][0])
            embed.add_field(name='Score', value=f'{rank[0][1]:.2f}')
            await ctx.send(embed=embed)

    @commands.command()
    async def rank(self, ctx, season: int = None):
        cursor = self.bot.db.cursor()
        if season is None:
            cursor.execute('SELECT id, name from seasons where running = ?', (True,))
            running_seasons = cursor.fetchall()
            if len(running_seasons) == 0:
                await ctx.send('No current running season. Please specify a season. ')
                return
            else:
                season = running_seasons[0][0]
                szn_name = running_seasons[0][1]
        else:
            cursor.execute('SELECT id, name from seasons where id = ?', (season,))
            selected_seasons = cursor.fetchall()
            if len(selected_seasons) == 0:
                await ctx.send(f'No season with id {season}. Please specify a valid season. ')
                return
            else:
                season = selected_seasons[0][0]
                szn_name = selected_seasons[0][1]

        cursor.execute('SELECT rank, score, user_id from rankings where season_id = ? order by rank', (season,))
        rankings = cursor.fetchall()

        if len(rankings) <= 20:
            # If there are less than 20 rankings, we don't need a whole menu (in fact dpymenus will throw us an error)
            embed = discord.Embed(title=f'{szn_name} rankings')
            scores = '\n'.join([f'`{rank}`. {score:.2f} [<@!{user_id}>]' for (rank, score, user_id) in rankings])
            embed.description = scores
            await ctx.send(embed=embed)
        else:
            pages = []
            for i in range(len(rankings) // 20 + 1):
                page = discord.Embed(title=f'{szn_name} rankings - Page {i + 1}')
                scores = '\n'.join(
                    [f'`{rank}`. {score:.2f} [<@!{user_id}>]' for (rank, score, user_id) in rankings[20 * i:20 * i + 20]])
                page.description = scores
                pages.append(page)
            await self.bot.get_cog('MenuManager').new_menu(ctx, pages)

    @commands.command()
    async def fetch(self, ctx, *, problem: shared.POTD):
        if not await problem.ensure_public(ctx):
            return

        potd_id = problem.id
        cursor = self.bot.db.cursor()

        # Calculate the otd prefix
        if ctx.guild is None:
            otd_prefix = self.bot.config["otd_prefix"]
        else:
            cursor.execute('SELECT otd_prefix from config WHERE server_id = ?', (ctx.guild.id,))
            result = cursor.fetchall()
            if len(result) == 0:
                otd_prefix = self.bot.config["otd_prefix"]
            else:
                otd_prefix = result[0][0]
        otd_label = shared.format_otd_label(otd_prefix)

        cursor.execute('SELECT date from problems where id = ?', (potd_id,))
        potd_date = cursor.fetchall()[0][0]

        # Display the potd to the user
        cursor.execute('''SELECT image FROM images WHERE potd_id = ?''', (potd_id,))
        images = cursor.fetchall()
        if len(images) == 0:
            await ctx.send(f'{otd_label} {potd_id} of {potd_date} has no picture attached. ')
        else:
            await ctx.send(f'{otd_label} {potd_id} of {potd_date}',
                           file=discord.File(io.BytesIO(images[0][0]),
                                             filename=f'POTD-{potd_id}-0.png'))
            for i in range(1, len(images)):
                await ctx.send(file=discord.File(io.BytesIO(images[i][0]), filename=f'POTD-{potd_id}-{i}.png'))

        # Log this stuff
        self.logger.info(
            f'User {ctx.author.id} requested {shared.config_otd_label(self.bot.config)} '
            f'with date {potd_date} and number {potd_id}. '
        )

    @app_commands.command(name='fetch', description='Fetch a public problem or one of its subproblems.')
    @app_commands.describe(
        problem_id='Problem ID to fetch',
        subproblem='Optional subproblem ID, label, or index (for example: 2, A, q2)',
    )
    async def fetch_slash(self, interaction: discord.Interaction, problem_id: int, subproblem: str = None):
        try:
            problem = shared.POTD(problem_id, self.bot.db)
        except Exception:
            await interaction.response.send_message(f'No problem with ID `{problem_id}`.', ephemeral=True)
            return

        if not problem.public:
            await interaction.response.send_message('This problem is not public!', ephemeral=True)
            return

        cursor = self.bot.db.cursor()
        if interaction.guild is None:
            otd_prefix = self.bot.config["otd_prefix"]
        else:
            cursor.execute('SELECT otd_prefix from config WHERE server_id = ?', (interaction.guild.id,))
            row = cursor.fetchone()
            otd_prefix = self.bot.config["otd_prefix"] if row is None else row[0]
        otd_label = shared.format_otd_label(otd_prefix)

        await interaction.response.defer(thinking=True)

        if subproblem is not None and subproblem.strip():
            resolved = self._resolve_subproblem_fetch(problem.id, subproblem)
            if resolved is None:
                await interaction.followup.send(
                    f'No subproblem matching `{subproblem}` for {otd_label} `{problem.id}`.'
                )
                return

            subproblem_id, sub_label, sub_statement, marks, manual_marking = resolved
            cursor.execute('SELECT image FROM subproblem_images WHERE subproblem_id = ?', (subproblem_id,))
            images = cursor.fetchall()

            mode = 'Manual review' if bool(manual_marking) else 'Auto integer check'
            embed = discord.Embed(
                title=f'{otd_label} {problem.id} - Subproblem {sub_label}',
                description=sub_statement,
            )
            embed.add_field(name='Marks', value=f'`{marks}`', inline=True)
            embed.add_field(name='Marking', value=f'`{mode}`', inline=True)

            if len(images) == 0:
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(
                    embed=embed,
                    file=discord.File(io.BytesIO(images[0][0]), filename=f'subproblem-{subproblem_id}-0.png'),
                )
                for i in range(1, len(images)):
                    await interaction.followup.send(
                        file=discord.File(io.BytesIO(images[i][0]), filename=f'subproblem-{subproblem_id}-{i}.png')
                    )

            self.logger.info(
                f'User {interaction.user.id} requested {shared.config_otd_label(self.bot.config)} '
                f'{problem.id} subproblem {sub_label} ({subproblem_id}).'
            )
            return

        cursor.execute('SELECT date from problems where id = ?', (problem.id,))
        potd_date = cursor.fetchall()[0][0]
        cursor.execute('SELECT image FROM images WHERE potd_id = ?', (problem.id,))
        images = cursor.fetchall()

        if len(images) == 0:
            await interaction.followup.send(f'{otd_label} {problem.id} of {potd_date} has no picture attached. ')
        else:
            await interaction.followup.send(
                f'{otd_label} {problem.id} of {potd_date}',
                file=discord.File(io.BytesIO(images[0][0]), filename=f'POTD-{problem.id}-0.png'),
            )
            for i in range(1, len(images)):
                await interaction.followup.send(
                    file=discord.File(io.BytesIO(images[i][0]), filename=f'POTD-{problem.id}-{i}.png')
                )

        self.logger.info(
            f'User {interaction.user.id} requested {shared.config_otd_label(self.bot.config)} '
            f'with date {potd_date} and number {problem.id}. '
        )

    @commands.command()
    async def check(self, ctx, problem: shared.POTD, answer: int):
        if not await problem.ensure_public(ctx):
            return

        cursor = self.bot.db.cursor()
        potd_id = problem.id

        # Check that it's not part of a currently running season.
        cursor.execute('SELECT name from seasons where latest_potd = ? and running = ?', (potd_id, True))
        seasons = cursor.fetchall()
        if len(seasons) > 0:
            await ctx.send(
                f'This {shared.config_otd_label(self.bot.config, lowercase=True)} is part of {seasons[0][0]}. '
                f'Please just DM your answer for this {shared.config_otd_label(self.bot.config)} to me. '
            )
            return

        # Get the correct answer
        cursor.execute('SELECT answer from problems where id = ?', (potd_id,))
        correct_answer = cursor.fetchall()[0][0]
        answer_is_correct = correct_answer == answer

        # See whether they've solved it before
        cursor.execute('SELECT exists (select * from solves where solves.user = ? and solves.problem_id = ?)',
                       (ctx.author.id, potd_id))
        solved_before = cursor.fetchall()[0][0]

        # Make sure the user is registered
        cursor.execute('''INSERT OR IGNORE INTO users (discord_id, nickname, anonymous) VALUES (?, ?, ?)''',
                       (ctx.author.id, ctx.author.display_name, True))

        # Record an attempt even if they've solved before
        cursor.execute('INSERT INTO attempts (user_id, potd_id, official, submission, submit_time) VALUES (?,?,?,?,?)',
                       (ctx.author.id, potd_id, False, answer, datetime.now()))

        # Get the number of both official and unofficial attempts
        cursor.execute('SELECT COUNT(1) from attempts WHERE user_id = ? and potd_id = ? and official = ?',
                       (ctx.author.id, potd_id, True))
        official_attempts = cursor.fetchall()[0][0]
        cursor.execute('SELECT COUNT(1) from attempts WHERE user_id = ? and potd_id = ? and official = ?',
                       (ctx.author.id, potd_id, False))
        unofficial_attempts = cursor.fetchall()[0][0]

        if answer_is_correct:
            if not solved_before:
                # Record that they solved it.
                cursor.execute('INSERT INTO solves (user, problem_id, num_attempts, official) VALUES (?, ?, ?, ?)',
                               (ctx.author.id, potd_id, official_attempts + unofficial_attempts, False))
                await ctx.send(
                    f'Nice job! You solved {shared.config_otd_label(self.bot.config)} `{potd_id}` '
                    f'after `{official_attempts + unofficial_attempts}` '
                    f'attempts (`{official_attempts}` official and `{unofficial_attempts}` unofficial). ')
            else:
                # Don't need to record that they solved it.
                await ctx.send(
                    f'Nice job! However you solved this {shared.config_otd_label(self.bot.config)} already. '
                )

            # Log this stuff
            self.logger.info(
                f'[Unofficial] User {ctx.author.id} solved {shared.config_otd_label(self.bot.config)} {potd_id}'
            )
        else:
            await ctx.send(f"Sorry! That's the wrong answer. You've had `{official_attempts + unofficial_attempts}` "
                           f"attempts (`{official_attempts}` official and `{unofficial_attempts}` unofficial). ")

            # Log this stuff
            self.logger.info(
                f'[Unofficial] User {ctx.author.id} submitted wrong answer {answer} for '
                f'{shared.config_otd_label(self.bot.config)} {potd_id}. '
            )

        # Delete the message if it's in a guild
        if ctx.guild is not None:
            await ctx.message.delete()

        # Still should refresh the embed
        await self.update_embed(potd_id)

        self.bot.db.commit()

    @commands.command(brief='Some information about the bot. ')
    async def info(self, ctx):
        embed = discord.Embed(description='OpenPOTD is a bot that posts short answer questions once a day for you '
                                          'to solve. OpenPOTD is open-source, and you can find our GitHub repository '
                                          'at https://github.com/IcosahedralDice/OpenPOTD. \n'
                                          'Have a bug report? Want to propose some problems? Join the OpenPOTD '
                                          'development server at https://discord.gg/ub2Y8b8zpt. \n'
                                          'Get the OpenPOTD manual with the `manual` command. ')
        await ctx.send(embed=embed)

    @commands.command(brief='Download the OpenPOTD manual. ')
    async def manual(self, ctx):
        await ctx.send('OpenPOTD Manual: ', file=discord.File('openpotd-manual.pdf'))


async def setup(bot: openpotd.OpenPOTD):
    await bot.add_cog(Interface(bot))
