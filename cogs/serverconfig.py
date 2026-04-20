import re
from typing import Union

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import has_permissions

import openpotd


def in_guild(ctx: commands.Context):
    return ctx.guild is not None


class PotwRoleToggleButton(discord.ui.Button):
    def __init__(self, cog: "ServerConfig"):
        super().__init__(
            label='Toggle PoTW Role',
            style=discord.ButtonStyle.secondary,
            emoji='🎯',
            custom_id='potw_role_toggle',
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_potw_role_toggle(interaction)


class PotwRoleToggleView(discord.ui.View):
    def __init__(self, cog: "ServerConfig"):
        super().__init__(timeout=None)
        self.add_item(PotwRoleToggleButton(cog))


class ServerConfig(commands.Cog):
    def __init__(self, bot: openpotd.OpenPOTD):
        self.bot = bot
        self._persistent_potw_toggle_view = PotwRoleToggleView(self)

    async def cog_load(self):
        self.bot.add_view(self._persistent_potw_toggle_view)

    def _is_authorised_user(self, user_id: int) -> bool:
        authorised = self.bot.config.get('authorised') or []
        if isinstance(authorised, int):
            return user_id == authorised
        return user_id in set(authorised)

    @staticmethod
    def _has_manage_guild(user: discord.abc.User, guild: discord.Guild | None):
        if guild is None or not isinstance(user, discord.Member):
            return False
        return user.guild_permissions.manage_guild

    def _get_guild_potw_role(self, guild: discord.Guild):
        cursor = self.bot.db.cursor()
        cursor.execute('SELECT ping_role_id FROM config WHERE server_id = ?', (guild.id,))
        row = cursor.fetchone()
        if row is None:
            return None, 'No config found for this server. Run `/init` first.'

        role_id = row[0]
        if role_id is None:
            return None, 'PoTW role is not configured. Set it with `/ping_role` first.'

        role = guild.get_role(role_id)
        if role is None:
            return None, f'Configured PoTW role `{role_id}` was not found in this server.'

        return role, None

    async def handle_potw_role_toggle(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user

        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                'This button can only be used inside a server.',
                ephemeral=True,
            )
            return

        role, error = self._get_guild_potw_role(guild)
        if role is None:
            await interaction.response.send_message(error, ephemeral=True)
            return

        try:
            if role in member.roles:
                await member.remove_roles(role, reason='Self-toggle PoTW role')
                await interaction.response.send_message(f'Removed role {role.mention}.', ephemeral=True)
            else:
                await member.add_roles(role, reason='Self-toggle PoTW role')
                await interaction.response.send_message(f'Added role {role.mention}.', ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                'I cannot manage that role. Check role hierarchy and permissions.',
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(f'Failed to update role: `{e}`', ephemeral=True)

    async def cog_check(self, ctx: commands.Context):
        if self._is_authorised_user(ctx.author.id):
            return True
        raise commands.CheckFailure('You are not authorised to use this command.')

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user is not None and self._is_authorised_user(interaction.user.id):
            return True
        if interaction.response.is_done():
            await interaction.followup.send('You are not authorised to use this command.', ephemeral=True)
        else:
            await interaction.response.send_message('You are not authorised to use this command.', ephemeral=True)
        return False

    @commands.check(in_guild)
    @commands.command(brief='Prints configuration for this server')
    async def config(self, ctx: commands.Context):
        cursor = self.bot.db.cursor()
        server_id = ctx.guild.id

        cursor.execute(
            'SELECT potd_channel, subproblem_thread_channel_id, submission_channel_id, submission_ping_role_id, '
            'ping_role_id, solved_role_id, '
            'otd_prefix, command_prefix, bronze_role_id, silver_role_id, gold_role_id, auto_publish_news '
            'FROM config WHERE server_id = ?',
            (server_id,),
        )
        result = cursor.fetchall()

        if len(result) == 0:
            await ctx.send('No config found! Use init to initialise your server\'s configuration. ')
        else:
            embed = discord.Embed()
            result = result[0]
            embed.description = f'`1. potd_channel:` {result[0]} [<#{result[0]}>]\n' \
                                f'`2. subproblem_thread_channel_id:` {result[1]} [<#{result[1]}>]\n' \
                                f'`3. submission_channel_id:` {result[2]} [<#{result[2]}>]\n' \
                                f'`4. submission_ping_role_id:` {result[3]} [<@&{result[3]}>]\n' \
                                f'`5. ping_role_id:` {result[4]} [<@&{result[4]}>]\n' \
                                f'`6. solved_role_id:` {result[5]} [<@&{result[5]}>]\n' \
                                f'`7. otd_prefix:` {result[6]}\n' \
                                f'`8. command_prefix:` {result[7]}\n' \
                                f'`9. Bronze Role:` [<@&{result[8]}>]\n' \
                                f'`10. Silver Role:` [<@&{result[9]}>]\n' \
                                f'`11. Gold Role:` [<@&{result[10]}>]\n' \
                                f'`12. auto_publish_news:` {bool(result[11])}'
            await ctx.send(embed=embed)

    @app_commands.command(name='config', description='Show this server configuration.')
    async def config_slash(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return

        cursor = self.bot.db.cursor()
        cursor.execute(
            'SELECT potd_channel, subproblem_thread_channel_id, submission_channel_id, submission_ping_role_id, '
            'ping_role_id, solved_role_id, '
            'otd_prefix, command_prefix, bronze_role_id, silver_role_id, gold_role_id, auto_publish_news '
            'FROM config WHERE server_id = ?',
            (interaction.guild.id,),
        )
        result = cursor.fetchone()
        if result is None:
            await interaction.response.send_message(
                "No config found! Use /init to initialise your server's configuration.",
                ephemeral=True,
            )
            return

        embed = discord.Embed()
        embed.description = f'`1. potd_channel:` {result[0]} [<#{result[0]}>]\n' \
                            f'`2. subproblem_thread_channel_id:` {result[1]} [<#{result[1]}>]\n' \
                            f'`3. submission_channel_id:` {result[2]} [<#{result[2]}>]\n' \
                            f'`4. submission_ping_role_id:` {result[3]} [<@&{result[3]}>]\n' \
                            f'`5. ping_role_id:` {result[4]} [<@&{result[4]}>]\n' \
                            f'`6. solved_role_id:` {result[5]} [<@&{result[5]}>]\n' \
                            f'`7. otd_prefix:` {result[6]}\n' \
                            f'`8. command_prefix:` {result[7]}\n' \
                            f'`9. Bronze Role:` [<@&{result[8]}>]\n' \
                            f'`10. Silver Role:` [<@&{result[9]}>]\n' \
                            f'`11. Gold Role:` [<@&{result[10]}>]\n' \
                            f'`12. auto_publish_news:` {bool(result[11])}'
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.check(in_guild)
    @has_permissions(manage_guild=True)
    @commands.command(brief='Initialises the configuration (note this overwrites previous configuration)', name='init')
    async def init_cfg(self, ctx: commands.Context):
        cursor = self.bot.db.cursor()
        cursor.execute('SELECT exists (select * from config where server_id = ?)', (ctx.guild.id,))

        guild: discord.Guild = ctx.guild
        channels = guild.text_channels

        # "Infer" the qotd posting channel
        for channel in channels:
            if bool(re.match('^.*-of-the-day$', channel.name)):
                qotd_channel_id = channel.id
                break
        else:
            qotd_channel_id = None

        # "Infer" the submission channel
        for channel in channels:
            if bool(re.match('^.*-(submissions|answers)$', channel.name)):
                submission_channel_id = channel.id
                break
        else:
            submission_channel_id = None

        # "Infer" the qotd role to ping
        for role in guild.roles:
            if bool(re.match('^.*-of-the-day$', role.name)) or bool(re.match('^.otd$', role.name)):
                ping_role_id = role.id
                break
        else:
            ping_role_id = None

        # "Infer" the solved role
        for role in guild.roles:
            if bool(re.match('^.*-solved$', role.name)):
                solved_role_id = role.id
                break
        else:
            solved_role_id = None

        otd_prefix = self.bot.config['otd_prefix']
        command_prefix = self.bot.config['prefix']

        if cursor.fetchall()[0][0]:
            # Just overwrite the entry
            cursor.execute(
                'UPDATE config SET potd_channel = ?, subproblem_thread_channel_id = ?, submission_channel_id = ?, '
                'submission_ping_role_id = ?, ping_role_id = ?, solved_role_id = ?, otd_prefix = ?, '
                'command_prefix = ? WHERE server_id = ?',
                (
                    qotd_channel_id,
                    None,
                    submission_channel_id,
                    None,
                    ping_role_id,
                    solved_role_id,
                    otd_prefix,
                    command_prefix,
                    guild.id,
                ),
            )
        else:
            # Make a new entry
            cursor.execute(
                'INSERT INTO config (potd_channel, subproblem_thread_channel_id, submission_channel_id, '
                'submission_ping_role_id, ping_role_id, solved_role_id, otd_prefix, command_prefix, server_id) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    qotd_channel_id,
                    None,
                    submission_channel_id,
                    None,
                    ping_role_id,
                    solved_role_id,
                    otd_prefix,
                    command_prefix,
                    guild.id,
                ),
            )

        self.bot.db.commit()
        await ctx.send('Server configuration initialised.')

    @app_commands.command(name='init', description='Initialise this server configuration (overwrites existing).')
    async def init_cfg_slash(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
        if not self._has_manage_guild(interaction.user, guild):
            await interaction.response.send_message('You need Manage Server permission.', ephemeral=True)
            return

        cursor = self.bot.db.cursor()
        cursor.execute('SELECT exists (select * from config where server_id = ?)', (guild.id,))
        channels = guild.text_channels

        for channel in channels:
            if bool(re.match('^.*-of-the-day$', channel.name)):
                qotd_channel_id = channel.id
                break
        else:
            qotd_channel_id = None

        for channel in channels:
            if bool(re.match('^.*-(submissions|answers)$', channel.name)):
                submission_channel_id = channel.id
                break
        else:
            submission_channel_id = None

        for role in guild.roles:
            if bool(re.match('^.*-of-the-day$', role.name)) or bool(re.match('^.otd$', role.name)):
                ping_role_id = role.id
                break
        else:
            ping_role_id = None

        for role in guild.roles:
            if bool(re.match('^.*-solved$', role.name)):
                solved_role_id = role.id
                break
        else:
            solved_role_id = None

        otd_prefix = self.bot.config['otd_prefix']
        command_prefix = self.bot.config['prefix']

        if cursor.fetchall()[0][0]:
            cursor.execute(
                'UPDATE config SET potd_channel = ?, subproblem_thread_channel_id = ?, submission_channel_id = ?, '
                'submission_ping_role_id = ?, ping_role_id = ?, solved_role_id = ?, otd_prefix = ?, '
                'command_prefix = ? WHERE server_id = ?',
                (
                    qotd_channel_id,
                    None,
                    submission_channel_id,
                    None,
                    ping_role_id,
                    solved_role_id,
                    otd_prefix,
                    command_prefix,
                    guild.id,
                ),
            )
        else:
            cursor.execute(
                'INSERT INTO config (potd_channel, subproblem_thread_channel_id, submission_channel_id, '
                'submission_ping_role_id, ping_role_id, solved_role_id, otd_prefix, command_prefix, server_id) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    qotd_channel_id,
                    None,
                    submission_channel_id,
                    None,
                    ping_role_id,
                    solved_role_id,
                    otd_prefix,
                    command_prefix,
                    guild.id,
                ),
            )

        self.bot.db.commit()
        await interaction.response.send_message('Server configuration initialised.', ephemeral=True)

    @commands.check(in_guild)
    @has_permissions(manage_guild=True)
    @commands.command(brief='Sets the potd channel')
    async def potd_channel(self, ctx, new: discord.TextChannel):
        if not new.guild.id == ctx.guild.id:
            await ctx.send("Please select a channel in **this** server! ")
        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET potd_channel = ? WHERE server_id = ?', (new.id, ctx.guild.id))
        self.bot.db.commit()
        await ctx.send('Set successfully!')

    @app_commands.command(name='potd_channel', description='Set the channel where PoTW is posted.')
    async def potd_channel_slash(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
        if not self._has_manage_guild(interaction.user, interaction.guild):
            await interaction.response.send_message('You need Manage Server permission.', ephemeral=True)
            return
        if channel.guild.id != interaction.guild.id:
            await interaction.response.send_message('Please select a channel in this server.', ephemeral=True)
            return

        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET potd_channel = ? WHERE server_id = ?', (channel.id, interaction.guild.id))
        self.bot.db.commit()
        await interaction.response.send_message('Set successfully!', ephemeral=True)

    @commands.check(in_guild)
    @has_permissions(manage_guild=True)
    @commands.command(brief='Sets the role to ping')
    async def ping_role(self, ctx, new: discord.Role):
        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET ping_role_id = ? WHERE server_id = ?', (new.id, ctx.guild.id))
        self.bot.db.commit()
        await ctx.send('Set successfully!')

    @app_commands.command(name='ping_role', description='Set the role pinged when posting PoTW.')
    async def ping_role_slash(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
        if not self._has_manage_guild(interaction.user, interaction.guild):
            await interaction.response.send_message('You need Manage Server permission.', ephemeral=True)
            return
        if role.guild.id != interaction.guild.id:
            await interaction.response.send_message('Please select a role in this server.', ephemeral=True)
            return

        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET ping_role_id = ? WHERE server_id = ?', (role.id, interaction.guild.id))
        self.bot.db.commit()
        await interaction.response.send_message('Set successfully!', ephemeral=True)

    @commands.check(in_guild)
    @has_permissions(manage_guild=True)
    @commands.command(
        name='potw_role_button',
        brief='Send a button that lets members toggle the configured PoTW role.',
    )
    async def potw_role_button(self, ctx: commands.Context, *, message: str = None):
        role, error = self._get_guild_potw_role(ctx.guild)
        if role is None:
            await ctx.send(error)
            return

        content = (message or '').strip()
        if not content:
            content = f'Click the button to toggle {role.mention}.'

        await ctx.send(content, view=PotwRoleToggleView(self))

    @app_commands.command(
        name='potw_role_button',
        description='Send a button that lets members toggle the configured PoTW role.',
    )
    @app_commands.describe(message='Optional message shown above the toggle button.')
    async def potw_role_button_slash(self, interaction: discord.Interaction, message: str = None):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
        if not self._has_manage_guild(interaction.user, guild):
            await interaction.response.send_message('You need Manage Server permission.', ephemeral=True)
            return

        role, error = self._get_guild_potw_role(guild)
        if role is None:
            await interaction.response.send_message(error, ephemeral=True)
            return

        content = (message or '').strip()
        if not content:
            content = f'Click the button to toggle {role.mention}.'

        await interaction.response.send_message(content, view=PotwRoleToggleView(self))

    @commands.check(in_guild)
    @has_permissions(manage_guild=True)
    @commands.command(brief='Sets the role contestants get after solving the POTD')
    async def solved_role(self, ctx, new: discord.Role):
        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET solved_role_id = ? WHERE server_id = ?', (new.id, ctx.guild.id))
        self.bot.db.commit()
        await ctx.send('Set successfully!')

    @app_commands.command(name='solved_role', description='Set the role given to members who solved PoTW.')
    async def solved_role_slash(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
        if not self._has_manage_guild(interaction.user, interaction.guild):
            await interaction.response.send_message('You need Manage Server permission.', ephemeral=True)
            return
        if role.guild.id != interaction.guild.id:
            await interaction.response.send_message('Please select a role in this server.', ephemeral=True)
            return

        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET solved_role_id = ? WHERE server_id = ?', (role.id, interaction.guild.id))
        self.bot.db.commit()
        await interaction.response.send_message('Set successfully!', ephemeral=True)

    @commands.check(in_guild)
    @has_permissions(manage_guild=True)
    @commands.command(brief='Sets the channel where DM submissions are mirrored')
    async def submission_channel(self, ctx, new: discord.TextChannel):
        if not new.guild.id == ctx.guild.id:
            await ctx.send("Please select a channel in **this** server! ")
            return
        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET submission_channel_id = ? WHERE server_id = ?', (new.id, ctx.guild.id))
        self.bot.db.commit()
        await ctx.send('Set successfully!')

    @commands.check(in_guild)
    @has_permissions(manage_guild=True)
    @commands.command(brief='Sets the channel where subproblems are posted as threads')
    async def subproblem_thread_channel(self, ctx, new: Union[discord.TextChannel, discord.ForumChannel]):
        if new.guild.id != ctx.guild.id:
            await ctx.send("Please select a channel in **this** server! ")
            return
        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET subproblem_thread_channel_id = ? WHERE server_id = ?', (new.id, ctx.guild.id))
        self.bot.db.commit()
        await ctx.send('Set successfully!')

    @app_commands.command(
        name='subproblem_thread_channel',
        description='Set the text/forum channel where posted subproblems are created as threads.',
    )
    async def subproblem_thread_channel_slash(
            self,
            interaction: discord.Interaction,
            channel: Union[discord.TextChannel, discord.ForumChannel],
    ):
        if interaction.guild is None:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
        if not self._has_manage_guild(interaction.user, interaction.guild):
            await interaction.response.send_message('You need Manage Server permission.', ephemeral=True)
            return
        if channel.guild.id != interaction.guild.id:
            await interaction.response.send_message('Please select a channel in this server.', ephemeral=True)
            return
        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET subproblem_thread_channel_id = ? WHERE server_id = ?',
                       (channel.id, interaction.guild.id))
        self.bot.db.commit()
        await interaction.response.send_message('Set successfully!', ephemeral=True)

    @commands.check(in_guild)
    @has_permissions(manage_guild=True)
    @commands.command(brief='Toggle auto-publishing messages in announcement channels')
    async def auto_publish_news(self, ctx, enabled: bool):
        cursor = self.bot.db.cursor()
        cursor.execute(
            'UPDATE config SET auto_publish_news = ? WHERE server_id = ?',
            (bool(enabled), ctx.guild.id),
        )
        self.bot.db.commit()
        state = 'enabled' if enabled else 'disabled'
        await ctx.send(f'Auto-publish in announcement channels is now {state}.')

    @app_commands.command(
        name='auto_publish_news',
        description='Enable or disable auto-publishing in announcement channels for this server.',
    )
    async def auto_publish_news_slash(self, interaction: discord.Interaction, enabled: bool):
        if interaction.guild is None:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
        if not self._has_manage_guild(interaction.user, interaction.guild):
            await interaction.response.send_message('You need Manage Server permission.', ephemeral=True)
            return

        cursor = self.bot.db.cursor()
        cursor.execute(
            'UPDATE config SET auto_publish_news = ? WHERE server_id = ?',
            (bool(enabled), interaction.guild.id),
        )
        self.bot.db.commit()
        state = 'enabled' if enabled else 'disabled'
        await interaction.response.send_message(
            f'Auto-publish in announcement channels is now {state}.',
            ephemeral=True,
        )

    @app_commands.command(name='submission_channel', description='Set the channel where DM submissions are mirrored.')
    async def submission_channel_slash(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
        if not self._has_manage_guild(interaction.user, interaction.guild):
            await interaction.response.send_message('You need Manage Server permission.', ephemeral=True)
            return
        if channel.guild.id != interaction.guild.id:
            await interaction.response.send_message('Please select a channel in this server.', ephemeral=True)
            return
        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET submission_channel_id = ? WHERE server_id = ?',
                       (channel.id, interaction.guild.id))
        self.bot.db.commit()
        await interaction.response.send_message('Set successfully!', ephemeral=True)

    @commands.check(in_guild)
    @has_permissions(manage_guild=True)
    @commands.command(brief='Sets the role pinged for manual-marking submissions')
    async def submission_ping_role(self, ctx, new: discord.Role):
        if new.guild.id != ctx.guild.id:
            await ctx.send("Please select a role in **this** server! ")
            return
        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET submission_ping_role_id = ? WHERE server_id = ?', (new.id, ctx.guild.id))
        self.bot.db.commit()
        await ctx.send('Set successfully!')

    @app_commands.command(name='submission_ping_role', description='Set the role pinged for manual submissions.')
    async def submission_ping_role_slash(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
        if not self._has_manage_guild(interaction.user, interaction.guild):
            await interaction.response.send_message('You need Manage Server permission.', ephemeral=True)
            return
        if role.guild.id != interaction.guild.id:
            await interaction.response.send_message('Please select a role in this server.', ephemeral=True)
            return
        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET submission_ping_role_id = ? WHERE server_id = ?',
                       (role.id, interaction.guild.id))
        self.bot.db.commit()
        await interaction.response.send_message('Set successfully!', ephemeral=True)

    @commands.check(in_guild)
    @has_permissions(manage_guild=True)
    @commands.command(brief='Clears the role pinged for manual-marking submissions')
    async def clear_submission_ping_role(self, ctx):
        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET submission_ping_role_id = NULL WHERE server_id = ?', (ctx.guild.id,))
        self.bot.db.commit()
        await ctx.send('Set successfully!')

    @app_commands.command(name='clear_submission_ping_role', description='Clear the role pinged for manual submissions.')
    async def clear_submission_ping_role_slash(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
        if not self._has_manage_guild(interaction.user, interaction.guild):
            await interaction.response.send_message('You need Manage Server permission.', ephemeral=True)
            return
        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET submission_ping_role_id = NULL WHERE server_id = ?', (interaction.guild.id,))
        self.bot.db.commit()
        await interaction.response.send_message('Set successfully!', ephemeral=True)

    @commands.check(in_guild)
    @has_permissions(manage_guild=True)
    @commands.command(brief='Sets the OTD prefix (some people like calling it a "QOTD" rather than a "POTD")')
    async def otd_prefix(self, ctx, new):
        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET otd_prefix = ? WHERE server_id = ?', (new, ctx.guild.id))
        self.bot.db.commit()
        await ctx.send('Set successfully!')

    @app_commands.command(name='otd_prefix', description='Set the OTD prefix used in labels (for example, P or Q).')
    async def otd_prefix_slash(self, interaction: discord.Interaction, prefix: str):
        if interaction.guild is None:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
        if not self._has_manage_guild(interaction.user, interaction.guild):
            await interaction.response.send_message('You need Manage Server permission.', ephemeral=True)
            return

        value = prefix.strip()
        if not value:
            await interaction.response.send_message('Prefix cannot be empty.', ephemeral=True)
            return

        cursor = self.bot.db.cursor()
        cursor.execute('UPDATE config SET otd_prefix = ? WHERE server_id = ?', (value, interaction.guild.id))
        self.bot.db.commit()
        await interaction.response.send_message('Set successfully!', ephemeral=True)

    @commands.check(in_guild)
    @has_permissions(manage_guild=True)
    @commands.command(brief='Sets the server command prefix')
    async def command_prefix(self, ctx, new):
        cursor = self.bot.db.cursor()
        openpotd.prefixes[ctx.guild.id] = new
        cursor.execute('UPDATE config SET command_prefix = ? WHERE server_id = ?', (new, ctx.guild.id))
        self.bot.db.commit()
        await ctx.send('Set successfully!')

    @app_commands.command(name='command_prefix', description='Set the server text-command prefix.')
    async def command_prefix_slash(self, interaction: discord.Interaction, prefix: str):
        if interaction.guild is None:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
        if not self._has_manage_guild(interaction.user, interaction.guild):
            await interaction.response.send_message('You need Manage Server permission.', ephemeral=True)
            return

        value = prefix.strip()
        if not value:
            await interaction.response.send_message('Prefix cannot be empty.', ephemeral=True)
            return

        cursor = self.bot.db.cursor()
        openpotd.prefixes[interaction.guild.id] = value
        cursor.execute('UPDATE config SET command_prefix = ? WHERE server_id = ?', (value, interaction.guild.id))
        self.bot.db.commit()
        await interaction.response.send_message('Set successfully!', ephemeral=True)

    @commands.check(in_guild)
    @has_permissions(manage_guild=True)
    @commands.command(brief='Sets the prize roles. ')
    async def medal_roles(self, ctx, bronze: discord.Role, silver: discord.Role, gold: discord.Role):
        cursor = self.bot.db.cursor()
        if not (bronze.guild == ctx.guild and silver.guild == ctx.guild and gold.guild == ctx.guild):
            await ctx.send('Invalid roles!')
            return
        cursor.execute('UPDATE config SET bronze_role_id = ?, silver_role_id = ?, gold_role_id = ? WHERE server_id = ?',
                       (bronze.id, silver.id, gold.id, ctx.guild.id))
        self.bot.db.commit()
        await ctx.send('Set successfully!')

    @app_commands.command(name='medal_roles', description='Set bronze, silver, and gold roles.')
    async def medal_roles_slash(
            self,
            interaction: discord.Interaction,
            bronze: discord.Role,
            silver: discord.Role,
            gold: discord.Role):
        if interaction.guild is None:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
        if not self._has_manage_guild(interaction.user, interaction.guild):
            await interaction.response.send_message('You need Manage Server permission.', ephemeral=True)
            return
        if not (bronze.guild == interaction.guild and silver.guild == interaction.guild and gold.guild == interaction.guild):
            await interaction.response.send_message('Invalid roles.', ephemeral=True)
            return

        cursor = self.bot.db.cursor()
        cursor.execute(
            'UPDATE config SET bronze_role_id = ?, silver_role_id = ?, gold_role_id = ? WHERE server_id = ?',
            (bronze.id, silver.id, gold.id, interaction.guild.id),
        )
        self.bot.db.commit()
        await interaction.response.send_message('Set successfully!', ephemeral=True)


async def setup(bot: openpotd.OpenPOTD):
    await bot.add_cog(ServerConfig(bot))
