# -*- coding: utf-8 -*-

"""
jishaku.cog
~~~~~~~~~~~

The Jishaku debugging and diagnostics cog.

:copyright: (c) 2019 Devon (Gorialis) R
:license: MIT, see LICENSE for more details.

"""

import asyncio
import collections
import contextlib
import datetime
import inspect
import io
import itertools
import os
import os.path
import re
import sys
import time
import traceback
import typing

import aiohttp
import discord
import humanize
from discord.ext import commands

from jishaku.codeblocks import Codeblock, codeblock_converter
from jishaku.exception_handling import ReplResponseReactor
from jishaku.functools import AsyncSender
from jishaku.meta import __version__
from jishaku.models import copy_context_with
from jishaku.modules import ExtensionConverter, package_version
from jishaku.paginators import PaginatorInterface, WrappedFilePaginator, WrappedPaginator
from jishaku.help_command import MinimalEmbedPaginatorHelp, DefaultEmbedPaginatorHelp
from jishaku.repl import AsyncCodeExecutor, Scope, all_inspections, get_var_dict_from_ctx
from jishaku.shell import ShellReader
from jishaku.voice import BasicYouTubeDLSource, connected_check, playing_check, vc_check, youtube_dl

try:
    import psutil
except ImportError:
    psutil = None


__all__ = (
    "Jishaku",
    "setup"
)

ENABLED_SYMBOLS = ("true", "t", "yes", "y", "on", "1")
JISHAKU_HIDE = os.getenv("JISHAKU_HIDE", "").lower() in ENABLED_SYMBOLS
JISHAKU_RETAIN = os.getenv("JISHAKU_RETAIN", "").lower() in ENABLED_SYMBOLS
JISHAKU_NO_UNDERSCORE = os.getenv("JISHAKU_NO_UNDERSCORE", "").lower() in ENABLED_SYMBOLS
SCOPE_PREFIX = '' if JISHAKU_NO_UNDERSCORE else '_'


CommandTask = collections.namedtuple("CommandTask", "index ctx task")


class Jishaku(commands.Cog):  # pylint: disable=too-many-public-methods
    """
    The cog that includes Jishaku's Discord-facing default functionality.
    """

    load_time = datetime.datetime.utcnow()

    def __init__(self, bot: commands.Bot, scope):
        self.bot = bot
        self._scope = Scope()
        self.retain = JISHAKU_RETAIN
        self.last_result = None
        self.start_time = datetime.datetime.utcnow()
        self.tasks = collections.deque()
        self.task_count: int = 0
        self.bot.old_help_command = bot.help_command
        self.queue = []
        self.SCOPE_PREFIX: str = scope

    @property
    def scope(self):
        """
        Gets a scope for use in REPL.

        If retention is on, this is the internal stored scope,
        otherwise it is always a new Scope.
        """

        if self.retain:
            return self._scope
        return Scope()

    @contextlib.contextmanager
    def submit(self, ctx: commands.Context):
        """
        A context-manager that submits the current task to jishaku's task list
        and removes it afterwards.

        Parameters
        -----------
        ctx: commands.Context
            A Context object used to derive information about this command task.
        """

        self.task_count += 1
        cmdtask = CommandTask(self.task_count, ctx, asyncio.Task.current_task())
        self.tasks.append(cmdtask)

        try:
            yield cmdtask
        finally:
            if cmdtask in self.tasks:
                self.tasks.remove(cmdtask)

    async def cog_check(self, ctx: commands.Context):
        """
        Local check, makes all commands in this cog owner-only
        """

        if not await ctx.bot.is_owner(ctx.author):
            raise commands.NotOwner("You must own this bot to use Jishaku.")
        return True

    @commands.group(name="jishaku", aliases=["jsk"], hidden=JISHAKU_HIDE,
                    invoke_without_command=True)
    async def jsk(self, ctx: commands.Context):
        """
        The Jishaku debug and diagnostic commands.

        This command on its own gives a status brief.
        All other functionality is within its subcommands.
        """
        _start_time = time.time()
        msg = await ctx.send("> Loading...")
        _end_time = time.time()
        _ping_time = round((_end_time - _start_time)*1000, 2)

        summary = [
            f">>> Jishaku v{__version__}, discord.py `{package_version('discord.py')}`, "
            f"`Python {sys.version}` on `{sys.platform}`".replace("\n", ""),
            f"Module was loaded {humanize.naturaltime(self.load_time)}, "
            f"cog was loaded {humanize.naturaltime(self.start_time)}.",
            ""
        ]

        if psutil:
            try:
                proc = psutil.Process()

                with proc.oneshot():
                    mem = proc.memory_full_info()
                    summary.append(f"Using {humanize.naturalsize(mem.rss)} physical memory and "
                                   f"{humanize.naturalsize(mem.vms)} virtual memory, "
                                   f"{humanize.naturalsize(mem.uss)} of which unique to this process.")

                    name = proc.name()
                    pid = proc.pid
                    thread_count = proc.num_threads()

                    summary.append(f"Running on PID {pid} (`{name}`) with {thread_count} thread(s).")

                    summary.append("")  # blank line
            except:
                summary.append("Was unable to get psutil information.")
                summary.append(" ")

        cache_summary = f"{len(self.bot.guilds)} guild(s), {len(list(self.bot.get_all_channels()))} channel(s)" \
                        f" and {len(self.bot.users)} user(s)"

        if isinstance(self.bot, discord.AutoShardedClient):
            summary.append(f"This bot is automatically sharded and can see {cache_summary}.")
        elif self.bot.shard_count:
            summary.append(f"This bot is manually sharded and can see {cache_summary}.")
        else:
            summary.append(f"This bot is not sharded and can see {cache_summary}.")

        summary.append(f"Average websocket latency: {round(self.bot.latency * 1000, 2)}ms, with {_ping_time}ms delay.")

        await msg.edit(content="\n".join(summary))

    # Meta commands

    @jsk.command(name="prefixrepl")
    async def jsk_prefix_repl(self, ctx, *, toggle: typing.Union[bool, str] = None):
        """Decides if vars should be prefixed in REPLs
        e.g: _ctx vs ctx.
        provide something thats not a bool to set it to that prefix.
        cleared on reboot"""
        if toggle is None:
            await ctx.send(f"Prefix: {self.SCOPE_PREFIX}")
        else:
            if toggle is True:
                self.SCOPE_PREFIX = '_'
            elif toggle is False:
                self. SCOPE_PREFIX = ''
            else:
                self.SCOPE_PREFIX = toggle
            return await ctx.send("Done.")

    @jsk.command(name="embedhelp")
    async def jsk_help_toggle(self, ctx, minimal: bool = True):
        """Switches between jsk's embedded help command and the current help command."""
        if isinstance(self.bot.help_command, (MinimalEmbedPaginatorHelp, DefaultEmbedPaginatorHelp)):
            self.bot.help_command = self.bot.old_help_command
            return await ctx.send("Returned to the original help command.")
        else:
            if minimal:
                self.bot.old_help_command = self.bot.help_command
                self.bot.help_command = MinimalEmbedPaginatorHelp()
                return await ctx.send("Set help command to the minimal embedded help command.")
            else:
                self.bot.old_help_command = self.bot.help_command
                self.bot.help_command = DefaultEmbedPaginatorHelp()
                return await ctx.send("Set help command to the embedded help command.")

    @jsk.command(name="update")
    async def jsk_update(self, ctx):
        """Updates jsk from the github repo.

        This is basically an alias for `jsk sh` but it runs the command for you."""
        pip = 'pip'
        if psutil:
            if psutil.LINUX:
                pip = "pip3"
            elif psutil.MACOS:
                pip = "pip3"
            else:
                pip = "pip"
        cb = codeblock_converter(f'{pip} install -U git+https://github.com/dragdev-studios/jishaku@master'
                                 f'#egg=jishaku --upgrade')
        status = await ctx.invoke(self.bot.get_command('jsk sh'), argument=cb)
        if status in [0, 'done']:
            m = await ctx.send("Update successfully downloaded. applying...")
            try:
                self.bot.reload_extension('jishaku')
            except:
                await m.edit(content="It looks like an error occurred while applying the update. your jishaku version"
                                     " has been reverted to pre-update to keep it working. try updating again in a few hours.")
            else:
                await m.edit(content="Updates successfully applied. have a good time!")

    @jsk.command(name="hide")
    async def jsk_hide(self, ctx: commands.Context, *, mode: bool = None):
        """
        Toggles hiding Jishaku from the help command.
        """
        self.jsk.hidden = True if not self.jsk.hidden else False
        if mode is not None:
            self.jsk.hidden = mode
        new = {
            True: "Hidden",
            False: "Shown"
        }
        await ctx.send("Jishaku is now {}.".format(new[self.jsk.hidden]))

    @jsk.command(name="tasks")
    async def jsk_tasks(self, ctx: commands.Context):
        """
        Shows the currently running jishaku tasks.
        """

        if not self.tasks:
            return await ctx.send("No currently running tasks.")

        paginator = commands.Paginator(max_size=1985)

        for task in self.tasks:
            paginator.add_line(f"{task.index}: `{task.ctx.command.qualified_name}`, invoked at "
                               f"{task.ctx.message.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")

        interface = PaginatorInterface(ctx.bot, paginator, owner=ctx.author)
        return await interface.send_to(ctx)

    @jsk.command(name="cancel")
    async def jsk_cancel(self, ctx: commands.Context, *, index: int):
        """
        Cancels a task with the given index.

        If the index passed is -1, will cancel the last task instead.
        """

        if not self.tasks:
            return await ctx.send("No tasks to cancel.")

        if index == -1:
            task = self.tasks.pop()
        else:
            task = discord.utils.get(self.tasks, index=index)
            if task:
                self.tasks.remove(task)
            else:
                return await ctx.send("Unknown task.")

        task.task.cancel()
        return await ctx.send(f"Cancelled task {task.index}: `{task.ctx.command.qualified_name}`,"
                              f" invoked at {task.ctx.message.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")

    # Bot management commands
    @jsk.command(name="load", aliases=["reload", 'r', 'l'])
    async def jsk_load(self, ctx: commands.Context, *extensions: ExtensionConverter):
        """
        Loads or reloads the given extension names.
        If a command is passed it will reload that command's cog.

        Reports any extensions that failed to load.
        """

        paginator = WrappedPaginator(prefix='', suffix='')

        for extension in itertools.chain(*extensions):
            method, icon = (
                (self.bot.reload_extension, "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}")
                if extension in self.bot.extensions else
                (self.bot.load_extension, "\N{INBOX TRAY}")
            )

            try:
                method(extension)
            except Exception as exc:  # pylint: disable=broad-except
                traceback_data = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__, 1))

                paginator.add_line(
                    f"{icon}\N{WARNING SIGN} `{extension}`\n```py\n{traceback_data}\n```",
                    empty=False
                )
            else:
                paginator.add_line(f"{icon} `{extension}`", empty=False)

        for page in paginator.pages:
            await ctx.send(page)

    @jsk.command(name="unload")
    async def jsk_unload(self, ctx: commands.Context, *extensions: ExtensionConverter):
        """
        Unloads the given extension names.

        Reports any extensions that failed to unload.
        """

        paginator = WrappedPaginator(prefix='', suffix='')
        icon = "\N{OUTBOX TRAY}"

        for extension in itertools.chain(*extensions):
            try:
                self.bot.unload_extension(extension)
            except Exception as exc:  # pylint: disable=broad-except
                traceback_data = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, 1))

                paginator.add_line(
                    f"{icon}\N{WARNING SIGN} `{extension}`\n```py\n{traceback_data}\n```",
                    empty=False
                )
            else:
                paginator.add_line(f"{icon} `{extension}`", empty=False)

        for page in paginator.pages:
            await ctx.send(page)

    @jsk.command(name="shutdown", aliases=["logout"])
    async def jsk_shutdown(self, ctx: commands.Context):
        """
        Logs this bot out.
        """

        await ctx.send("Logging out now.")
        await ctx.bot.logout()

    # Command-invocation commands
    @jsk.command(name="su")
    async def jsk_su(self, ctx: commands.Context, target: typing.Union[discord.Member, discord.User], *, command_string: str):
        """
        Run a command as someone else.

        This will try to resolve to a Member, but will use a User if it can't find one.
        """

        if ctx.guild:
            # Try to upgrade to a Member instance
            # This used to be done by a Union converter, but doing it like this makes
            #  the command more compatible with chaining, e.g. `jsk in .. jsk su ..`
            target = ctx.guild.get_member(target.id) or target

        alt_ctx = await copy_context_with(ctx, author=target, content=ctx.prefix + command_string)

        if alt_ctx.command is None:
            if alt_ctx.invoked_with is None:
                return await ctx.send('This bot has been hard-configured to ignore this user.')
            return await ctx.send(f'Command "{alt_ctx.invoked_with}" is not found')

        return await alt_ctx.command.invoke(alt_ctx)

    @jsk.command(name="in")
    async def jsk_in(self, ctx: commands.Context, channel: discord.TextChannel, *, command_string: str):
        """
        Run a command as if it were run in a different channel.
        """

        alt_ctx = await copy_context_with(ctx, channel=channel, content=ctx.prefix + command_string)

        if alt_ctx.command is None:
            return await ctx.send(f'Command "{alt_ctx.invoked_with}" is not found')

        return await alt_ctx.command.invoke(alt_ctx)

    @jsk.command(name="sudo")
    async def jsk_sudo(self, ctx: commands.Context, *, command_string: str):
        """
        Run a command bypassing all checks and cooldowns.

        This also bypasses permission checks so this has a high possibility of making commands raise exceptions.
        """

        alt_ctx = await copy_context_with(ctx, content=ctx.prefix + command_string)

        if alt_ctx.command is None:
            return await ctx.send(f'Command "{alt_ctx.invoked_with}" is not found')

        return await alt_ctx.command.reinvoke(alt_ctx)

    @jsk.command(name="repeat")
    async def jsk_repeat(self, ctx: commands.Context, times: int, *, command_string: str):
        """
        Runs a command multiple times in a row.

        This acts like the command was invoked several times manually, so it obeys cooldowns.
        You can use this in conjunction with `jsk sudo` to bypass this.
        """

        with self.submit(ctx):  # allow repeats to be cancelled
            for _ in range(times):
                alt_ctx = await copy_context_with(ctx, content=ctx.prefix + command_string)

                if alt_ctx.command is None:
                    return await ctx.send(f'Command "{alt_ctx.invoked_with}" is not found')

                await alt_ctx.command.reinvoke(alt_ctx)

    @jsk.command(name="debug", aliases=["dbg"])
    async def jsk_debug(self, ctx: commands.Context, *, command_string: str):
        """
        Run a command timing execution and catching exceptions.
        """

        alt_ctx = await copy_context_with(ctx, content=ctx.prefix + command_string)

        if alt_ctx.command is None:
            return await ctx.send(f'Command "{alt_ctx.invoked_with}" is not found')

        start = time.perf_counter()

        async with ReplResponseReactor(ctx.message):
            with self.submit(ctx):
                await alt_ctx.command.invoke(alt_ctx)

        end = time.perf_counter()
        return await ctx.send(f"Command `{alt_ctx.command.qualified_name}` finished in {end - start:.3f}s.")

    # Filesystem commands
    __cat_line_regex = re.compile(r"(?:\.\/+)?(.+?)(?:#L?(\d+)(?:\-L?(\d+))?)?$")

    @jsk.command(name="cat")
    async def jsk_cat(self, ctx: commands.Context, argument: str):
        """
        Read out a file, using syntax highlighting if detected.

        Lines and linespans are supported by adding '#L12' or '#L12-14' etc to the end of the filename.
        """

        match = self.__cat_line_regex.search(argument)

        if not match:  # should never happen
            return await ctx.send("Couldn't parse this input.")

        path = match.group(1)

        line_span = None

        if match.group(2):
            start = int(match.group(2))
            line_span = (start, int(match.group(3) or start))

        if not os.path.exists(path) or os.path.isdir(path):
            return await ctx.send(f"`{path}`: No file by that name.")

        size = os.path.getsize(path)

        if size <= 0:
            return await ctx.send(f"`{path}`: Cowardly refusing to read a file with no size stat"
                                  f" (it may be empty, endless or inaccessible).")

        if size > 50 * (1024 ** 2):
            return await ctx.send(f"`{path}`: Cowardly refusing to read a file >50MB.")

        try:
            with open(path, "rb") as file:
                paginator = WrappedFilePaginator(file, line_span=line_span, max_size=1985)
        except UnicodeDecodeError:
            return await ctx.send(f"`{path}`: Couldn't determine the encoding of this file.")
        except ValueError as exc:
            return await ctx.send(f"`{path}`: Couldn't read this file, {exc}")

        interface = PaginatorInterface(ctx.bot, paginator, owner=ctx.author)
        await interface.send_to(ctx)

    @jsk.command(name="curl")
    async def jsk_curl(self, ctx: commands.Context, url: str):
        """
        Download and display a text file from the internet.

        This command is similar to jsk cat, but accepts a URL.
        """

        # remove embed maskers if present
        url = url.lstrip("<").rstrip(">")

        async with ReplResponseReactor(ctx.message):
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    data = await response.read()
                    hints = (
                        response.content_type,
                        url
                    )
                    code = response.status

            if not data:
                return await ctx.send(f"HTTP response was empty (status code {code}).")

            try:
                paginator = WrappedFilePaginator(io.BytesIO(data), language_hints=hints, max_size=1985)
            except UnicodeDecodeError:
                return await ctx.send(f"Couldn't determine the encoding of the response. (status code {code})")
            except ValueError as exc:
                return await ctx.send(f"Couldn't read response (status code {code}), {exc}")

            interface = PaginatorInterface(ctx.bot, paginator, owner=ctx.author)
            await interface.send_to(ctx)

    @jsk.command(name="source", aliases=["src"])
    async def jsk_source(self, ctx: commands.Context, *, command_name: str):
        """
        Displays the source code for a command.
        """

        command = self.bot.get_command(command_name)
        if not command:
            return await ctx.send(f"Couldn't find command `{command_name}`.")

        try:
            source_lines, _ = inspect.getsourcelines(command.callback)
        except (TypeError, OSError):
            return await ctx.send(f"Was unable to retrieve the source for `{command}` for some reason. Is it saved?")

        # getsourcelines for some reason returns WITH line endings
        source_lines = ''.join(source_lines).split('\n')

        paginator = WrappedPaginator(prefix='```py', suffix='```', max_size=1985)
        for line in source_lines:
            line = (line.replace('`', '\u200B`').replace('*', '\u200B*').replace('|', '\u200B|').replace('>', '\u200B>')
                    .replace('_', '\u200B_'))
            paginator.add_line(line)

        interface = PaginatorInterface(ctx.bot, paginator, owner=ctx.author)
        await interface.send_to(ctx)

    # Python evaluation/execution-related commands
    @jsk.command(name="retain")
    async def jsk_retain(self, ctx: commands.Context, *, toggle: bool = None):
        """
        Turn variable retention for REPL on or off.

        Provide no argument for current status.
        """

        if toggle is None:
            if self.retain:
                return await ctx.send("Variable retention is set to ON.")

            return await ctx.send("Variable retention is set to OFF.")

        if toggle:
            if self.retain:
                return await ctx.send("Variable retention is already set to ON.")

            self.retain = True
            self._scope = Scope()
            return await ctx.send("Variable retention is ON. Future REPL sessions will retain their scope.")

        if not self.retain:
            return await ctx.send("Variable retention is already set to OFF.")

        self.retain = False
        return await ctx.send("Variable retention is OFF. Future REPL sessions will dispose their scope when done.")

    @jsk.command(name="py", aliases=["python"])
    async def jsk_python(self, ctx: commands.Context, *, argument: codeblock_converter):
        """
        Direct evaluation of Python code.
        """

        arg_dict = get_var_dict_from_ctx(ctx, self.SCOPE_PREFIX)
        arg_dict["_"] = self.last_result

        scope = self.scope

        try:
            async with ReplResponseReactor(ctx.message):
                with self.submit(ctx):
                    executor = AsyncCodeExecutor(argument.content, scope, arg_dict=arg_dict)
                    async for send, result in AsyncSender(executor):
                        if result is None:
                            continue

                        self.last_result = result

                        if isinstance(result, discord.File):
                            send(await ctx.send(file=result))
                        elif isinstance(result, discord.Embed):
                            send(await ctx.send(embed=result))
                        elif isinstance(result, PaginatorInterface):
                            send(await result.send_to(ctx))
                        else:
                            if not isinstance(result, str):
                                # repr all non-strings
                                result = repr(result)

                            if len(result) > 2000:
                                # inconsistency here, results get wrapped in codeblocks when they are too large
                                #  but don't if they're not. probably not that bad, but noting for later review
                                paginator = WrappedPaginator(prefix='```py', suffix='```', max_size=1985)

                                paginator.add_line(result)

                                interface = PaginatorInterface(ctx.bot, paginator, owner=ctx.author)
                                send(await interface.send_to(ctx))
                            else:
                                if result.strip() == '':
                                    result = "\u200b"

                                send(await ctx.send(result.replace(self.bot.http.token, "[NO_TOKEN]") if ctx.guild else result))
        finally:
            scope.clear_intersection(arg_dict)

    @jsk.command(name="py_inspect", aliases=["pyi", "python_inspect", "pythoninspect"])
    async def jsk_python_inspect(self, ctx: commands.Context, *, argument: codeblock_converter):
        """
        Evaluation of Python code with inspect information.
        """

        arg_dict = get_var_dict_from_ctx(ctx, self.SCOPE_PREFIX)
        arg_dict["_"] = self.last_result

        scope = self.scope

        try:
            async with ReplResponseReactor(ctx.message):
                with self.submit(ctx):
                    executor = AsyncCodeExecutor(argument.content, scope, arg_dict=arg_dict)
                    async for send, result in AsyncSender(executor):
                        self.last_result = result

                        header = repr(result).replace("``", "`\u200b`").replace(self.bot.http.token, "[token omitted]")

                        if len(header) > 485:
                            header = header[0:482] + "..."

                        paginator = WrappedPaginator(prefix=f"```prolog\n=== {header} ===\n", max_size=1985)

                        for name, res in all_inspections(result):
                            paginator.add_line(f"{name:16.16} :: {res}")

                        interface = PaginatorInterface(ctx.bot, paginator, owner=ctx.author)
                        send(await interface.send_to(ctx))
        finally:
            scope.clear_intersection(arg_dict)

    # Shell-related commands
    @jsk.command(name="shell", aliases=["sh"])
    async def jsk_shell(self, ctx: commands.Context, *, argument: codeblock_converter):
        """
        Executes statements in the system shell.

        This uses the system shell as defined in $SHELL, or `/bin/bash` otherwise.
        Execution can be cancelled by closing the paginator.
        """

        async with ReplResponseReactor(ctx.message):
            with self.submit(ctx):
                paginator = WrappedPaginator(prefix="```sh", max_size=1500)
                paginator.add_line(f"$ {argument.content}\n")

                interface = PaginatorInterface(ctx.bot, paginator, owner=ctx.author)
                self.bot.loop.create_task(interface.send_to(ctx))

                with ShellReader(argument.content) as reader:
                    async for line in reader:
                        if interface.closed:
                            return
                        await interface.add_line(line)

                await interface.add_line(f"\n[status] Return code {reader.close_code}")
                return reader.close_code

    @jsk.command(name="git")
    async def jsk_git(self, ctx: commands.Context, *, argument: codeblock_converter):
        """
        Shortcut for 'jsk sh git'. Invokes the system shell.
        """

        return await ctx.invoke(self.jsk_shell, argument=Codeblock(argument.language, "git " + argument.content))

    # Voice-related commands
    @jsk.group(name="voice", aliases=["vc"])
    @commands.check(vc_check)
    async def jsk_voice(self, ctx: commands.Context):
        """
        Voice-related commands.

        If invoked without subcommand, relays current voice state.
        """

        # if using a subcommand, short out
        if ctx.invoked_subcommand is not None and ctx.invoked_subcommand is not self.jsk_voice:
            return

        # give info about the current voice client if there is one
        voice = ctx.guild.voice_client

        if not voice or not voice.is_connected():
            return await ctx.send("Not connected.")

        await ctx.send(f"Connected to {voice.channel.name}, "
                       f"{'paused' if voice.is_paused() else 'playing' if voice.is_playing() else 'idle'}.")

    @jsk_voice.command(name="clients")
    async def jsk_vc_clients(self, ctx):
        """Lists voice clients."""
        p = commands.Paginator()
        for number, client in enumerate(self.bot.voice_clients, start=1):
            p.add_line(f"{number}. {client.channel.name} ({client.channel.guild.id}, {client.channel.guild.name})")
        if len(p.pages) == 0:
            return await ctx.send("No connected voice clients.")
        for page in p.pages:
            await ctx.send(page)

    @jsk_voice.command(name="join", aliases=["connect"])
    async def jsk_vc_join(self, ctx: commands.Context, *,
                          destination: typing.Union[discord.VoiceChannel, discord.Member] = None):
        """
        Joins a voice channel, or moves to it if already connected.

        Passing a voice channel uses that voice channel.
        Passing a member will use that member's current voice channel.
        Passing nothing will use the author's voice channel.
        """

        destination = destination or ctx.author

        if isinstance(destination, discord.Member):
            if destination.voice and destination.voice.channel:
                destination = destination.voice.channel
            else:
                return await ctx.send("Member has no voice channel.")

        voice = ctx.guild.voice_client

        if voice:
            await voice.move_to(destination)
        else:
            await destination.connect(reconnect=True)

        await ctx.send(f"Connected to {destination.name}.")

    @jsk_voice.command(name="disconnect", aliases=["dc"])
    @commands.check(connected_check)
    async def jsk_vc_disconnect(self, ctx: commands.Context):
        """
        Disconnects from the voice channel in this guild, if there is one.
        """

        voice = ctx.guild.voice_client

        await voice.disconnect()
        await ctx.send(f"Disconnected from {voice.channel.name}.")

    @jsk_voice.command(name="stop")
    @commands.check(playing_check)
    async def jsk_vc_stop(self, ctx: commands.Context):
        """
        Stops running an audio source, if there is one.
        """

        voice = ctx.guild.voice_client

        voice.stop()
        await ctx.send(f"Stopped playing audio in {voice.channel.name}.")

    @jsk_voice.command(name="pause")
    @commands.check(playing_check)
    async def jsk_vc_pause(self, ctx: commands.Context):
        """
        Pauses a running audio source, if there is one.
        """

        voice = ctx.guild.voice_client

        if voice.is_paused():
            return await ctx.send("Audio is already paused.")

        voice.pause()
        await ctx.send(f"Paused audio in {voice.channel.name}.")

    @jsk_voice.command(name="resume")
    @commands.check(playing_check)
    async def jsk_vc_resume(self, ctx: commands.Context):
        """
        Resumes a running audio source, if there is one.
        """

        voice = ctx.guild.voice_client

        if not voice.is_paused():
            return await ctx.send("Audio is not paused.")

        voice.resume()
        await ctx.send(f"Resumed audio in {voice.channel.name}.")

    @jsk_voice.command(name="volume")
    @commands.check(playing_check)
    async def jsk_vc_volume(self, ctx: commands.Context, *, percentage: float):
        """
        Adjusts the volume of an audio source if it is supported.
        """

        volume = max(0.0, min(1.0, percentage / 100))

        source = ctx.guild.voice_client.source

        if not isinstance(source, discord.PCMVolumeTransformer):
            return await ctx.send("This source doesn't support adjusting volume or "
                                  "the interface to do so is not exposed.")

        source.volume = volume

        await ctx.send(f"Volume set to {volume * 100:.2f}%")

    @jsk_voice.command(name="play", aliases=["play_local"])
    @commands.check(connected_check)
    async def jsk_vc_play(self, ctx: commands.Context, *, uri: str):
        """
        Plays audio direct from a URI.

        Can be either a local file or an audio resource on the internet.
        """

        voice = ctx.guild.voice_client

        if voice.is_playing():
            voice.stop()

        # remove embed maskers if present
        uri = uri.lstrip("<").rstrip(">")

        voice.play(discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(uri)))
        await ctx.send(f"Playing in {voice.channel.name}.")

    @jsk_voice.command(name="youtube_dl", aliases=["youtubedl", "ytdl", "yt"])
    @commands.check(connected_check)
    async def jsk_vc_youtube_dl(self, ctx: commands.Context, *, url: str):
        """
        Plays audio from youtube_dl-compatible sources.
        """

        if not youtube_dl:
            return await ctx.send("youtube_dl is not installed.")

        voice = ctx.guild.voice_client

        if voice.is_playing():
            voice.stop()

        # remove embed maskers if present
        url = url.lstrip("<").rstrip(">")
        # remove radio mode, if present
        _url = url.split('&')
        if len(_url) > 1:
            if _url[-1].startswith('index='):
                url = _url[0]  # removes list= too, if present

        voice.play(discord.PCMVolumeTransformer(BasicYouTubeDLSource(url)))
        await ctx.send(f"Playing in {voice.channel.name}.")


def setup(bot: commands.Bot):
    """
    Adds the Jishaku cog to the bot.
    """

    bot.add_cog(Jishaku(bot=bot, scope=SCOPE_PREFIX))
