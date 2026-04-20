import logging

import discord
from discord.ext import commands

import openpotd


class Fun(commands.Cog):
    def __init__(self, bot: openpotd.OpenPOTD):
        self.bot = bot
        self.logger = logging.getLogger("fun")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or self.bot.user is None:
            return

        if message.guild is not None:
            if not self.bot.is_allowed_guild(message.guild):
                return
        else:
            if not await self.bot.is_allowed_dm_user(message.author.id):
                return

        if self.bot.user not in message.mentions:
            return

        if not bool(self.bot.config.get("fun_reply_on_mention", False)):
            return

        reply_text = str(self.bot.config.get("fun_mention_reply", "hello")).strip() or "hello"
        try:
            await message.reply(reply_text, mention_author=False)
        except discord.Forbidden:
            try:
                await message.channel.send(reply_text)
            except discord.Forbidden:
                self.logger.warning(
                    f"Cannot send mention reply in channel {message.channel.id} (missing permissions)."
                )
        except discord.HTTPException as e:
            self.logger.warning(f"Failed to send mention reply in channel {message.channel.id}: {e}")


async def setup(bot: openpotd.OpenPOTD):
    await bot.add_cog(Fun(bot))
