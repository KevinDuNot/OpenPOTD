"""A bunch of helper functions. """
import asyncio
import re
import sqlite3
from datetime import date
from datetime import datetime

import discord
from discord.ext import commands
import logging
import io
import openpotd

try:
    import dateparser
except ImportError:
    dateparser = None

date_regex = re.compile(r'\d\d\d\d-\d\d-\d\d')

_PERMISSION_LABELS = {
    'view_channel': 'View Channel',
    'send_messages': 'Send Messages',
    'send_messages_in_threads': 'Send Messages in Threads',
    'create_public_threads': 'Create Public Threads',
    'embed_links': 'Embed Links',
    'attach_files': 'Attach Files',
    'manage_messages': 'Manage Messages',
}


def format_otd_label(prefix_or_label, lowercase: bool = False):
    label = str(prefix_or_label).strip() if prefix_or_label is not None else ''
    if not label:
        label = 'POTD'
    elif len(label) == 1:
        label = f'{label.upper()}OTD'
    return label.lower() if lowercase else label


def config_otd_label(config: dict, lowercase: bool = False):
    return format_otd_label(config.get('otd_prefix', 'P'), lowercase=lowercase)


async def send_with_auto_publish(
        channel,
        *args,
        logger: logging.Logger | None = None,
        log_prefix: str = 'AUTO PUBLISH',
        auto_publish: bool = True,
        **kwargs):
    message: discord.Message = await channel.send(*args, **kwargs)

    if auto_publish and isinstance(channel, discord.TextChannel) and channel.is_news():
        try:
            await message.publish()
        except (discord.Forbidden, discord.HTTPException) as e:
            if logger is not None:
                logger.warning(f'[{log_prefix}] Failed to auto-publish in channel {channel.id}: {e}')

    return message


def get_current_problem(conn: sqlite3.Connection):
    cursor = conn.cursor()
    cursor.execute('SELECT problems.id from seasons left join problems '
                   'on seasons.running = ? where problems.id = seasons.latest_potd and problems.id is not null',
                   (True,))
    result = cursor.fetchall()
    if len(result) == 0:
        return None
    else:
        return POTD(result[0][0], conn)


async def assign_solved_role(servers: list, user_id: int, give: bool, context: commands.Context):
    for server in servers:
        guild: discord.Guild = context.bot.get_guild(server[0])
        if guild is None:
            continue

        member: discord.Member = guild.get_member(user_id)
        if member is None:
            continue

        solved_role: discord.Role = guild.get_role(server[1])
        if solved_role is None:
            continue

        try:
            if give:
                await member.add_roles(solved_role, reason=f'Solved POTD')
            else:
                await member.remove_roles(solved_role, reason=f'Did not solve POTD')
        except Exception as e:
            logging.warning(f'[ASSIGNING SOLVED ROLE] Guild {server[0]} failed. ')


class POTD:
    """Representation of a problem of the day. """

    def __init__(self, id: int, db: sqlite3.Connection):
        cursor = db.cursor()
        cursor.execute(
            'SELECT id, date, season, statement, difficulty, weighted_solves, base_points, answer, '
            'manual_marking, public, source, stats_message_id, difficulty_rating, coolness_rating '
            'FROM problems WHERE id = ?',
            (id,),
        )
        result = cursor.fetchall()
        if len(result) == 0:
            raise Exception('No such problem! ')
        else:
            self.id = id
            self.date = result[0][1]
            self.season = result[0][2]
            self.statement = result[0][3]
            self.difficulty = result[0][4]
            self.weighted_solves = result[0][5]
            self.base_points = result[0][6]
            self.answer = result[0][7]
            self.manual_marking = bool(result[0][8])
            self.public = result[0][9]
            self.source = result[0][10]
            self.stats_message_id = result[0][11]
            self.difficulty_rating = result[0][12]
            self.coolness_rating = result[0][13]
            cursor.execute('SELECT image from images WHERE potd_id = ?', (id,))
            self.images = [x[0] for x in cursor.fetchall()]
            cursor.execute('SELECT name from seasons WHERE id = ?', (self.season,))
            self.season_name = cursor.fetchall()[0][0]
            cursor.execute('SELECT COUNT() from problems WHERE problems.season = ? AND problems.date < ?',
                           (self.season, self.date))
            self.season_order = cursor.fetchall()[0][0]
            self.logger = logging.getLogger(f'POTD {self.id}')
            self.db = db

    @classmethod
    async def convert(cls, ctx: commands.Context, argument: str):
        """Method tries to infer a user's input and parse it as a problem."""
        db: sqlite3.Connection = ctx.bot.db
        cursor = db.cursor()

        # Check if it's an ID
        if argument.isnumeric():
            cursor.execute('SELECT EXISTS (SELECT 1 from problems where id = ?)', (int(argument),))
            if not cursor.fetchall()[0][0]:
                raise discord.ext.commands.UserInputError(f'No potd with such an ID (`{argument}`)')
            else:
                return cls(int(argument), db)

        # Check if it's an date
        if dateparser is not None:
            as_datetime = dateparser.parse(argument)
        else:
            try:
                as_datetime = datetime.combine(date.fromisoformat(argument), datetime.min.time())
            except ValueError:
                as_datetime = None
        if as_datetime is not None:
            as_date = as_datetime.date()
            cursor.execute('SELECT id from problems where date = ?', (as_date,))
            result = cursor.fetchall()
            if len(result) > 0:
                return cls(result[0][0], db)
            else:
                raise discord.ext.commands.UserInputError(f'No potd with that date! (`{str(as_date)}`)')

        raise commands.UserInputError(f'Failed to parse {argument} as a valid problem. ')

    async def ensure_public(self, ctx: commands.Context):
        if not self.public:
            await ctx.send('This problem is not public!')
            return False
        return True

    def info(self):
        """Get information about a problem. """
        return [
            ('id', self.id),
            ('date', self.date),
            ('season', self.season),
            ('statement', self.statement),
            ('difficulty', self.difficulty),
            ('weighted_solves', self.weighted_solves),
            ('base_points', self.base_points),
            ('answer', self.answer),
            ('manual_marking', self.manual_marking),
            ('public', self.public),
            ('source', self.source)
        ]

    def get_subproblems(self):
        cursor = self.db.cursor()
        cursor.execute(
            'SELECT id, label, statement, marks, answer, manual_marking FROM subproblems WHERE potd_id = ? '
            'ORDER BY order_index ASC, id ASC',
            (self.id,),
        )
        return cursor.fetchall()

    @staticmethod
    def _missing_channel_permissions(bot: openpotd.OpenPOTD, channel, required_permissions: set[str]):
        if bot.user is None:
            return []
        guild = getattr(channel, 'guild', None)
        if guild is None:
            return []
        me = guild.get_member(bot.user.id)
        if me is None:
            return []

        perms = channel.permissions_for(me)
        return sorted([perm for perm in required_permissions if not getattr(perms, perm, False)])

    def _log_missing_permissions(self, bot: openpotd.OpenPOTD, channel, action: str, required_permissions: set[str]):
        missing = self._missing_channel_permissions(bot, channel, required_permissions)
        if len(missing) == 0:
            return False

        labels = ', '.join(f'`{_PERMISSION_LABELS.get(p, p)}`' for p in missing)
        self.logger.warning(
            f'Cannot {action} in channel {channel.id} (server {channel.guild.id}): missing {labels}.'
        )
        return True

    async def post_subproblems_as_threads(
            self,
            bot: openpotd.OpenPOTD,
            server_id: int,
            thread_channel_id: int,
            identification_name: str,
    ):
        thread_channel = bot.get_channel(thread_channel_id)
        if thread_channel is None:
            self.logger.warning(f'No subproblem thread channel {thread_channel_id} in server {server_id}.')
            return
        if not isinstance(thread_channel, (discord.TextChannel, discord.ForumChannel)):
            self.logger.warning(f'Subproblem thread channel {thread_channel_id} is not text/forum.')
            return

        preflight_required = {
            'view_channel',
            'send_messages',
            'embed_links',
            'send_messages_in_threads',
            'create_public_threads',
        }
        cursor = self.db.cursor()
        cursor.execute(
            'SELECT EXISTS ('
            'SELECT 1 FROM subproblem_images '
            'INNER JOIN subproblems ON subproblems.id = subproblem_images.subproblem_id '
            'WHERE subproblems.potd_id = ?'
            ')',
            (self.id,),
        )
        if bool(cursor.fetchone()[0]):
            preflight_required.add('attach_files')
        if isinstance(thread_channel, discord.TextChannel) and thread_channel.is_news():
            preflight_required.add('manage_messages')
        if self._log_missing_permissions(
                bot,
                thread_channel,
                f'post subproblem threads for {config_otd_label(bot.config)} {self.id}',
                preflight_required):
            return

        subproblems = self.get_subproblems()
        if len(subproblems) == 0:
            self.logger.info(f'No subproblems configured for {config_otd_label(bot.config)} {self.id}.')
            return

        logged_subproblems = ', '.join(
            [f'{row[0]}:{row[1]}({row[3]}m)' for row in subproblems]
        )
        self.logger.info(
            f'Posting {len(subproblems)} subproblem(s) for {config_otd_label(bot.config)} {self.id} '
            f'in server {server_id} via channel {thread_channel_id}: {logged_subproblems}'
        )
        for subproblem_id, label, statement, marks, answer, manual_marking in subproblems:
            thread: discord.Thread | None = None
            origin_message_id = 0
            week_number = self.season_order + 1
            thread_name = f'Week {week_number} {label}'
            starter_text = f'{self.season_name} - #{week_number} - Problem {label} ({marks} marks)'
            if len(thread_name) > 100:
                thread_name = thread_name[:100]

            try:
                self.logger.info(
                    f'Posting subproblem {subproblem_id} ({label}) | marks={marks} '
                    f'| mode={"manual" if bool(manual_marking) else "auto"}'
                )
                cursor.execute('SELECT image FROM subproblem_images WHERE subproblem_id = ?', (subproblem_id,))
                images = [x[0] for x in cursor.fetchall()]
                starter_file = None
                if images:
                    starter_file = discord.File(
                        io.BytesIO(images[0]),
                        filename=f'subproblem-{subproblem_id}-0.png',
                    )

                if isinstance(thread_channel, discord.TextChannel):
                    header_kwargs = {
                        'logger': self.logger,
                        'log_prefix': 'PROBLEM THREAD',
                    }
                    if starter_file is not None:
                        header_kwargs['file'] = starter_file
                    header = await send_with_auto_publish(
                        thread_channel,
                        starter_text,
                        **header_kwargs,
                    )
                    origin_message_id = header.id
                    try:
                        thread = await header.create_thread(name=thread_name)
                    except (discord.Forbidden, discord.HTTPException) as e:
                        self.logger.warning(f'Could not create thread for subproblem {subproblem_id}: {e}')
                else:
                    create_kwargs = {
                        'name': thread_name,
                        'content': starter_text,
                    }
                    if starter_file is not None:
                        create_kwargs['file'] = starter_file
                    created = await thread_channel.create_thread(**create_kwargs)
                    starter_message = None
                    if isinstance(created, discord.Thread):
                        thread = created
                        starter_message = getattr(created, 'starter_message', None)
                    else:
                        thread = getattr(created, 'thread', None)
                        starter_message = getattr(created, 'message', None)

                    if isinstance(starter_message, discord.Message):
                        origin_message_id = starter_message.id

                    if not isinstance(thread, discord.Thread):
                        self.logger.warning(
                            f'Forum thread creation returned unexpected object for subproblem {subproblem_id}.'
                        )
                        continue

                if not isinstance(thread, discord.Thread):
                    self.logger.warning(f'No thread exists for subproblem {subproblem_id}; skipping body post.')
                    continue

                if origin_message_id == 0:
                    origin_message_id = thread.id

                for i, image in enumerate(images[1:], start=1):
                    await thread.send(
                        file=discord.File(io.BytesIO(image), filename=f'subproblem-{subproblem_id}-{i}.png')
                    )

                embed = discord.Embed(
                    title=f'Subproblem {label}',
                    description=statement,
                    colour=discord.Color.blurple(),
                )
                embed.add_field(name='Marks', value=f'`{marks}`', inline=True)
                if bool(manual_marking):
                    embed.add_field(name='Marking', value='`Manual review`', inline=True)
                else:
                    embed.add_field(name='Marking', value='`Auto integer check`', inline=True)

                await thread.send(embed=embed)

                cursor.execute(
                    'INSERT INTO subproblem_threads (subproblem_id, server_id, channel_id, message_id, thread_id) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (
                        subproblem_id,
                        server_id,
                        thread_channel.id,
                        origin_message_id,
                        thread.id if isinstance(thread, discord.Thread) else None,
                    ),
                )
                self.logger.info(
                    f'Posted subproblem {subproblem_id} ({label}) for {config_otd_label(bot.config)} {self.id} '
                    f'in server {server_id}. thread={thread.id}, images={len(images)}'
                )
            except Exception:
                self.logger.exception(
                    f'Failed to post subproblem {subproblem_id} ({label}) '
                    f'for {config_otd_label(bot.config)} {self.id} in server {server_id}.'
                )

        self.db.commit()

    async def post(
            self,
            bot: openpotd.OpenPOTD,
            channel: int,
            potd_role_id: int,
            subproblem_thread_channel_id: int = None,
            auto_publish_news: bool = True,
    ):
        channel = bot.get_channel(channel)
        if channel is None:
            raise Exception('No such channel!')
        else:
            preflight_required = {
                'view_channel',
                'send_messages',
                'embed_links',
            }
            if len(self.images) > 0:
                preflight_required.add('attach_files')
            if auto_publish_news and isinstance(channel, discord.TextChannel) and channel.is_news():
                preflight_required.add('manage_messages')
            if self._log_missing_permissions(
                    bot,
                    channel,
                    f'post {config_otd_label(bot.config)} {self.id}',
                    preflight_required):
                return

            try:
                sent_messages = []

                async def send_and_track(*args, **kwargs):
                    try:
                        message: discord.Message = await channel.send(*args, **kwargs)
                    except discord.Forbidden:
                        required = {'view_channel', 'send_messages'}
                        if kwargs.get('embed') is not None:
                            required.add('embed_links')
                        if kwargs.get('file') is not None or kwargs.get('files') is not None:
                            required.add('attach_files')
                        self._log_missing_permissions(bot, channel, 'send message while posting', required)
                        raise
                    sent_messages.append(message)
                    return message

                identification_name = f'**{self.season_name} - #{self.season_order + 1}**'
                if len(self.images) == 0:
                    await send_and_track(
                        f'{identification_name} of {str(date.today())} has no picture attached. ')
                else:
                    await send_and_track(f'{identification_name} [{str(date.today())}]',
                                         file=discord.File(io.BytesIO(self.images[0]),
                                                           filename=f'POTD-{self.id}-0.png'))
                    for i in range(1, len(self.images)):
                        await send_and_track(
                            file=discord.File(io.BytesIO(self.images[i]), filename=f'POTD-{self.id}-{i}.png'))

                cta_parts = ['DM your answers to me!']
                if potd_role_id is not None:
                    cta_parts.append(f'<@&{potd_role_id}>')
                else:
                    logging.warning(f'Config variable ping_role_id is not set! [Server {channel.guild.id}]')
                if subproblem_thread_channel_id is not None:
                    cta_parts.append(f'Discussion: <#{subproblem_thread_channel_id}>')
                await send_and_track(' '.join(cta_parts))

                if subproblem_thread_channel_id is not None:
                    await self.post_subproblems_as_threads(
                        bot=bot,
                        server_id=channel.guild.id,
                        thread_channel_id=subproblem_thread_channel_id,
                        identification_name=identification_name,
                    )

                # Construct embed and send
                embed = discord.Embed(title=f'{config_otd_label(bot.config)} {self.id} Stats')
                embed.add_field(name='Difficulty', value=self.difficulty)
                embed.add_field(name='Weighted Solves', value='0')
                embed.add_field(name='Base Points', value='0')
                embed.add_field(name='Solves (official)', value='0')
                embed.add_field(name='Solves (unofficial)', value='0')
                stats_message: discord.Message = await send_and_track(embed=embed)
                self.add_stats_message(stats_message.id, channel.guild.id, stats_message.channel.id)

                # Publish after all sends in forward order with a short delay between each publish.
                if auto_publish_news and isinstance(channel, discord.TextChannel) and channel.is_news():
                    for idx, message in enumerate(sent_messages):
                        try:
                            await message.publish()
                        except (discord.Forbidden, discord.HTTPException) as e:
                            self.logger.warning(f'Failed to auto-publish message in {channel.id}: {e}')
                        if idx < len(sent_messages) - 1:
                            await asyncio.sleep(1)
            except Exception:
                self.logger.exception(
                    f'Failed while posting {config_otd_label(bot.config)} {self.id} to channel {channel.id}.'
                )

    def add_stats_message(self, message_id: int, server_id: int, channel_id: int):
        cursor = self.db.cursor()
        cursor.execute('INSERT INTO stats_messages (potd_id, message_id, server_id, channel_id) VALUES (?, ?, ?, ?)',
                       (self.id, message_id, server_id, channel_id))
        self.db.commit()

    def build_embed(self, db: sqlite3.Connection, full_stats: bool, prefix: str = 'P'):
        cursor = db.cursor()
        cursor.execute('SELECT count(1) from solves where problem_id = ? and official = ?', (self.id, True))
        official_solves = cursor.fetchall()[0][0]
        cursor.execute('SELECT count(1) from solves where problem_id = ? and official = ?', (self.id, False))
        unofficial_solves = cursor.fetchall()[0][0]

        embed = discord.Embed(title=f'{format_otd_label(prefix)} {self.id} Stats')

        if full_stats:
            embed.add_field(name='Date', value=self.date)
            embed.add_field(name='Season', value=self.season)

        embed.add_field(name='Difficulty', value=self.difficulty)
        embed.add_field(name='Weighted Solves', value=f'{self.weighted_solves:.2f}')
        embed.add_field(name='Base Points', value=f'{self.base_points:.2f}')
        embed.add_field(name='Solves (official)', value=official_solves)
        embed.add_field(name='Solves (unofficial)', value=unofficial_solves)
        return embed
