import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path

import discord
import schedule
from discord.ext import commands
from ruamel.yaml import YAML

# When this file is executed as a script, cogs that import "openpotd" should
# receive this module instead of loading a second copy with separate globals.
sys.modules.setdefault("openpotd", sys.modules[__name__])

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.getenv("OPENPOTD_CONFIG_DIR", str(BASE_DIR / "config"))).expanduser()
DATA_DIR = Path(os.getenv("OPENPOTD_DATA_DIR", str(BASE_DIR / "data"))).expanduser()
DEFAULT_CONFIG_PATH = BASE_DIR / "default_config.yml"
SCHEMA_PATH = BASE_DIR / "schema.sql"


def resolve_config_file(filename: str) -> Path:
    path = Path(filename)
    if not path.is_absolute():
        path = CONFIG_DIR / path
    return path


def load_config() -> dict:
    config_path = CONFIG_DIR / "config.yml"
    if not config_path.exists():
        if not DEFAULT_CONFIG_PATH.exists():
            raise FileNotFoundError(f"Missing default config: {DEFAULT_CONFIG_PATH}")
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DEFAULT_CONFIG_PATH, config_path)

    yaml = YAML(typ="safe", pure=True)
    with config_path.open("r", encoding="utf-8") as cfgfile:
        loaded = yaml.load(cfgfile) or {}

    loaded.setdefault("token", "token.txt")
    loaded.setdefault("blacklist", "blacklist.txt")
    loaded.setdefault("prefix", "%")
    loaded.setdefault("presence", "Use /help.")
    if loaded.get("presence") == "Use %help. ":
        loaded["presence"] = "Use /help."
    loaded.setdefault("base_points", 1000)
    loaded.setdefault("posting_time", None)
    loaded.setdefault("otd_prefix", "PoTW")
    loaded.setdefault("cooldown", True)
    loaded.setdefault("allowed_guild_id", None)
    loaded.setdefault("dm_cleanup_window_minutes", 1440)
    loaded.setdefault("allow_local_db_reset", False)
    loaded.setdefault("token_env_var", "DISCORD_TOKEN")
    if loaded.get("allowed_guild_id") in (None, "") and loaded.get("allowed_guild_ids") not in (None, ""):
        loaded["allowed_guild_id"] = loaded["allowed_guild_ids"]

    if loaded.get("authorised") is None:
        loaded["authorised"] = []
    if loaded.get("cogs") is None:
        loaded["cogs"] = []

    return loaded


def ensure_database_migrations(conn: sqlite3.Connection):
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(config)")
    existing = {row[1] for row in cursor.fetchall()}

    required_config_columns = {
        "subproblem_thread_channel_id": "INTEGER",
        "submission_channel_id": "INTEGER",
        "submission_ping_role_id": "INTEGER",
        "auto_publish_news": "BOOLEAN NOT NULL DEFAULT 1",
    }
    for column_name, column_type in required_config_columns.items():
        if column_name not in existing:
            cursor.execute(f"ALTER TABLE config ADD COLUMN {column_name} {column_type}")

    cursor.execute("PRAGMA table_info(problems)")
    problem_columns = {row[1] for row in cursor.fetchall()}
    if "manual_marking" not in problem_columns:
        cursor.execute("ALTER TABLE problems ADD COLUMN manual_marking BOOLEAN NOT NULL DEFAULT 0")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS manual_submissions (
            id INTEGER NOT NULL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            potd_id INTEGER NOT NULL,
            subproblem_id INTEGER,
            season_id INTEGER NOT NULL,
            content TEXT,
            submitted_at DATETIME NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            claimed_by INTEGER,
            claimed_at DATETIME,
            reviewer_id INTEGER,
            reviewed_at DATETIME,
            decision BOOLEAN,
            dm_channel_id INTEGER,
            dm_message_id INTEGER,
            FOREIGN KEY(user_id) REFERENCES users(discord_id),
            FOREIGN KEY(potd_id) REFERENCES problems(id),
            FOREIGN KEY(subproblem_id) REFERENCES subproblems(id),
            FOREIGN KEY(season_id) REFERENCES seasons(id)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS manual_submission_messages (
            id INTEGER NOT NULL PRIMARY KEY,
            submission_id INTEGER NOT NULL,
            server_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            thread_id INTEGER,
            control_message_id INTEGER,
            UNIQUE(server_id, message_id),
            FOREIGN KEY(submission_id) REFERENCES manual_submissions(id)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS subproblems (
            id INTEGER NOT NULL PRIMARY KEY,
            potd_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            statement TEXT NOT NULL,
            marks INTEGER NOT NULL DEFAULT 1,
            answer INTEGER,
            manual_marking BOOLEAN NOT NULL DEFAULT 1,
            order_index INTEGER NOT NULL DEFAULT 0,
            public BOOLEAN NOT NULL DEFAULT 1,
            UNIQUE(potd_id, label),
            FOREIGN KEY(potd_id) REFERENCES problems(id)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS subproblem_images (
            id INTEGER NOT NULL PRIMARY KEY,
            subproblem_id INTEGER NOT NULL,
            image BLOB,
            FOREIGN KEY(subproblem_id) REFERENCES subproblems(id)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS subproblem_threads (
            id INTEGER NOT NULL PRIMARY KEY,
            subproblem_id INTEGER NOT NULL,
            server_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            thread_id INTEGER,
            FOREIGN KEY(subproblem_id) REFERENCES subproblems(id)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS subproblem_attempts (
            id INTEGER NOT NULL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            potd_id INTEGER NOT NULL,
            subproblem_id INTEGER NOT NULL,
            submission INTEGER,
            submit_time DATETIME NOT NULL,
            is_correct BOOLEAN NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(discord_id),
            FOREIGN KEY(potd_id) REFERENCES problems(id),
            FOREIGN KEY(subproblem_id) REFERENCES subproblems(id)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS subproblem_solves (
            id INTEGER NOT NULL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            potd_id INTEGER NOT NULL,
            subproblem_id INTEGER NOT NULL,
            num_attempts INTEGER NOT NULL,
            official BOOLEAN NOT NULL DEFAULT 1,
            UNIQUE(user_id, subproblem_id, official),
            FOREIGN KEY(user_id) REFERENCES users(discord_id),
            FOREIGN KEY(potd_id) REFERENCES problems(id),
            FOREIGN KEY(subproblem_id) REFERENCES subproblems(id)
        )
        """
    )

    cursor.execute("PRAGMA table_info(manual_submissions)")
    manual_submission_columns = {row[1] for row in cursor.fetchall()}
    if "subproblem_id" not in manual_submission_columns:
        cursor.execute("ALTER TABLE manual_submissions ADD COLUMN subproblem_id INTEGER")
    if "claimed_by" not in manual_submission_columns:
        cursor.execute("ALTER TABLE manual_submissions ADD COLUMN claimed_by INTEGER")
    if "claimed_at" not in manual_submission_columns:
        cursor.execute("ALTER TABLE manual_submissions ADD COLUMN claimed_at DATETIME")

    cursor.execute("PRAGMA table_info(manual_submission_messages)")
    manual_message_columns = {row[1] for row in cursor.fetchall()}
    if "thread_id" not in manual_message_columns:
        cursor.execute("ALTER TABLE manual_submission_messages ADD COLUMN thread_id INTEGER")
    if "control_message_id" not in manual_message_columns:
        cursor.execute("ALTER TABLE manual_submission_messages ADD COLUMN control_message_id INTEGER")

    cursor.execute("PRAGMA table_info(subproblems)")
    subproblem_columns = {row[1] for row in cursor.fetchall()}
    if "answer" not in subproblem_columns:
        cursor.execute("ALTER TABLE subproblems ADD COLUMN answer INTEGER")
    if "manual_marking" not in subproblem_columns:
        cursor.execute("ALTER TABLE subproblems ADD COLUMN manual_marking BOOLEAN NOT NULL DEFAULT 1")

    conn.commit()


def open_database() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Missing schema file: {SCHEMA_PATH}")
    conn = sqlite3.connect(DATA_DIR / "data.db")
    with SCHEMA_PATH.open("r", encoding="utf-8") as schema:
        conn.executescript(schema.read())
    ensure_database_migrations(conn)
    conn.commit()
    return conn


config = load_config()

prefixes = {}


def get_prefix(bot, message: discord.Message):
    if message.guild is None or message.guild.id not in prefixes:
        return config['prefix']
    else:
        return prefixes[message.guild.id]


class OpenPOTD(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()

        allowed_mentions = discord.AllowedMentions.all()
        allowed_mentions.everyone = False

        super().__init__(command_prefix=get_prefix, intents=intents, allowed_mentions=allowed_mentions)
        self.config = config
        self.db = open_database()
        logging.basicConfig(level=logging.INFO, format='[%(name)s %(levelname)s] %(message)s')
        self.logger = logging.getLogger('bot')
        self.allowed_guild_ids = self._parse_allowed_guild_ids(self.config.get("allowed_guild_id"))
        try:
            with resolve_config_file(config["blacklist"]).open('r', encoding="utf-8") as blacklist:
                self.blacklist = list(map(
                    int, filter(lambda x: x.strip(), blacklist.readlines())
                ))
        except IOError:
            self.blacklist = []

        # Populate prefixes
        cursor = self.db.cursor()
        cursor.execute('SELECT server_id, command_prefix FROM config WHERE command_prefix IS NOT NULL')
        global prefixes
        prefixes = {x[0]: x[1] for x in cursor.fetchall()}

        # Set refreshing status
        self.posting_problem = False
        self.tree_synced = False
        self._dm_user_access_cache: dict[int, tuple[bool, float]] = {}

    @staticmethod
    def _parse_allowed_guild_ids(value):
        if value in (None, "", []):
            return None

        raw_values = value if isinstance(value, list) else [value]
        parsed = set()
        invalid = []

        for raw in raw_values:
            if raw in (None, ""):
                continue
            try:
                parsed.add(int(raw))
            except (TypeError, ValueError):
                invalid.append(raw)

        if invalid:
            logging.getLogger('bot').warning(
                f'Ignoring invalid allowed_guild_id values in config: {invalid!r}'
            )

        if not parsed:
            return None

        return parsed

    def is_allowed_guild_id(self, guild_id: int | None) -> bool:
        if self.allowed_guild_ids is None:
            return True
        return guild_id is not None and guild_id in self.allowed_guild_ids

    def is_allowed_guild(self, guild: discord.Guild | None) -> bool:
        return self.is_allowed_guild_id(None if guild is None else guild.id)

    async def is_allowed_dm_user(self, user_id: int) -> bool:
        if self.allowed_guild_ids is None:
            return True

        now = time.monotonic()
        cached = self._dm_user_access_cache.get(user_id)
        if cached is not None:
            allowed, expires_at = cached
            if expires_at > now:
                return allowed
            self._dm_user_access_cache.pop(user_id, None)

        for guild_id in self.allowed_guild_ids:
            guild = self.get_guild(guild_id)
            if guild is None:
                continue

            if guild.get_member(user_id) is not None:
                self._dm_user_access_cache[user_id] = (True, now + 1800)
                return True

            try:
                await guild.fetch_member(user_id)
                self._dm_user_access_cache[user_id] = (True, now + 1800)
                return True
            except discord.NotFound:
                continue
            except (discord.Forbidden, discord.HTTPException):
                continue

        self._dm_user_access_cache[user_id] = (False, now + 120)
        return False

    async def _sync_guild_app_commands(self, guild: discord.Guild):
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        self.logger.info(f'Synced {len(synced)} app command(s) for guild {guild.id}.')

    async def _clear_global_app_commands(self):
        if self.application_id is None:
            self.logger.warning('Could not clear global app commands: missing application_id.')
            return
        cleared = await self.http.bulk_upsert_global_commands(self.application_id, payload=[])
        self.logger.info(f'Cleared global app commands; {len(cleared)} command(s) remain globally.')

    async def on_guild_join(self, guild: discord.Guild):
        if not self.is_allowed_guild(guild):
            self.logger.warning(
                f'Joined unauthorised guild {guild.id}; leaving because allowed_guild_id={self.allowed_guild_ids}.'
            )
            await guild.leave()
            return

        try:
            await self._sync_guild_app_commands(guild)
        except Exception:
            self.logger.exception(f'Failed to sync app commands for guild {guild.id}.')

    async def setup_hook(self):
        for cog in self.config['cogs']:
            try:
                await self.load_extension(cog)
            except Exception:
                self.logger.exception('Failed to load cog {}.'.format(cog))
            else:
                self.logger.info('Loaded cog {}.'.format(cog))

    async def on_ready(self):
        if self.allowed_guild_ids is not None:
            for guild in list(self.guilds):
                if guild.id not in self.allowed_guild_ids:
                    self.logger.warning(
                        f'Leaving unauthorised guild {guild.id}; allowed_guild_id={self.allowed_guild_ids}.'
                    )
                    await guild.leave()

        self.logger.info('Connected to Discord')
        self.logger.info('Guilds  : {}'.format(len(self.guilds)))
        self.logger.info('Users   : {}'.format(len(set(self.get_all_members()))))
        self.logger.info('Channels: {}'.format(len(list(self.get_all_channels()))))
        await self.set_presence(self.config['presence'])

        if not self.tree_synced:
            guilds_to_sync = list(self.guilds)
            if self.allowed_guild_ids is not None:
                guilds_to_sync = [g for g in guilds_to_sync if g.id in self.allowed_guild_ids]

            for guild in guilds_to_sync:
                try:
                    await self._sync_guild_app_commands(guild)
                except Exception:
                    self.logger.exception(f'Failed to sync app commands for guild {guild.id}.')

            try:
                await self._clear_global_app_commands()
            except Exception:
                self.logger.exception('Failed to clear global app commands.')

            self.tree_synced = True

        self.logger.info(f'Schedule: {schedule.jobs}')

    async def on_message(self, message):
        if message.author.bot: return
        if message.author.id in self.blacklist: return
        if message.guild is not None:
            if not self.is_allowed_guild(message.guild):
                return
        else:
            if not await self.is_allowed_dm_user(message.author.id):
                return
        await self.process_commands(message)

    async def set_presence(self, text):
        game = discord.Game(name=text)
        await self.change_presence(activity=game)

    async def on_command_error(self, ctx: commands.Context, exception: Exception):
        if isinstance(exception, commands.CommandInvokeError):
            # all exceptions are wrapped in CommandInvokeError if they are not a subclass of CommandError
            # you can access the original exception with .original
            exception: commands.CommandInvokeError
            if isinstance(exception.original, discord.Forbidden):
                # permissions error
                try:
                    await ctx.send('Permissions error: `{}`'.format(exception))
                except discord.Forbidden:
                    # we can't send messages in that channel
                    pass
                return

            elif isinstance(exception.original, discord.HTTPException):
                try:
                    await ctx.send('Sorry, I can\'t send that.')
                except discord.Forbidden:
                    pass

                return

            # Print to log then notify developers
            try:
                lines = traceback.format_exception(type(exception),
                                                   exception,
                                                   exception.__traceback__)
            except RecursionError:
                raise exception

            self.logger.error(''.join(lines))

            return

        if isinstance(exception, commands.CheckFailure):
            await ctx.send("You are not authorised to use this command. ")
        elif isinstance(exception, commands.CommandOnCooldown):
            exception: commands.CommandOnCooldown
            await ctx.send(f'You\'re going too fast! Try again in {exception.retry_after:.2f} seconds.')

        elif isinstance(exception, commands.CommandNotFound):
            if isinstance(ctx.channel, discord.DMChannel):
                await ctx.send("Command not recognised!")

        elif isinstance(exception, commands.UserInputError):
            error = ' '.join(exception.args)
            error_data = re.findall(r'Converting to "(.*)" failed for parameter "(.*)"\.', error)
            if not error_data:
                await ctx.send('Huh? {}'.format(' '.join(exception.args)))
            else:
                if error_data[0][0][0] in 'aeiouAEIOU':
                    anindicator = 'n'
                else:
                    anindicator = ''
                await ctx.send(
                    'Huh? I thought `{1}` was supposed to be a{2} `{0}`...'.format(*error_data[0], anindicator))
        else:
            info = traceback.format_exception(type(exception), exception, exception.__traceback__, chain=False)
            self.logger.error('Unhandled command exception - {}'.format(''.join(info)))

    async def started_posting(self):
        await self.set_presence('Posting new problem')
        self.posting_problem = True

    async def finished_posting(self):
        await self.set_presence(self.config['presence'])
        self.posting_problem = False

    def reset_database_for_local_testing(self):
        db_path = DATA_DIR / "data.db"
        try:
            self.db.close()
        except Exception:
            pass

        if db_path.exists():
            db_path.unlink()

        self.db = open_database()

        cursor = self.db.cursor()
        cursor.execute('SELECT server_id, command_prefix FROM config WHERE command_prefix IS NOT NULL')
        global prefixes
        prefixes = {x[0]: x[1] for x in cursor.fetchall()}


def executor():
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == '__main__':
    token_env_var = str(config.get("token_env_var", "DISCORD_TOKEN")) or "DISCORD_TOKEN"
    token = os.getenv(token_env_var, "").strip()
    token_path = resolve_config_file(config["token"])
    if not token and token_path.exists():
        with token_path.open(encoding="utf-8") as tokfile:
            token = tokfile.readline().rstrip('\n')
    if not token:
        raise RuntimeError(
            f"Discord token is missing. Set env var {token_env_var} "
            f"or populate token file: {token_path}"
        )

    x = threading.Thread(target=executor, args=(), daemon=True)
    x.start()
    OpenPOTD().run(token)
