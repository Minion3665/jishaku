# -*- coding: utf-8 -*-

"""
jishaku.modules
~~~~~~~~~~~~~~

Functions for managing and searching modules.

:copyright: (c) 2019 Devon (Gorialis) R
:license: MIT, see LICENSE for more details.

"""

import pathlib
import typing

import pkg_resources
from discord.ext import commands

__all__ = ('find_extensions_in', 'resolve_extensions', 'package_version', 'ExtensionConverter')


def find_extensions_in(path: typing.Union[str, pathlib.Path, commands.command]) -> list:
    """
    Tries to find things that look like bot extensions in a directory.
    """
    if not isinstance(path, pathlib.Path):
        path = pathlib.Path(path)

    if not path.is_dir():
        return []

    extension_names = []

    # Find extensions directly in this folder
    for subpath in path.glob('*.py'):
        parts = subpath.with_suffix('').parts
        if parts[0] == '.':
            parts = parts[1:]

        extension_names.append('.'.join(parts))

    # Find extensions as subfolder modules
    for subpath in path.glob('*/__init__.py'):
        parts = subpath.parent.parts
        if parts[0] == '.':
            parts = parts[1:]

        extension_names.append('.'.join(parts))

    return extension_names


def resolve_extensions(bot: commands.Bot, name: str) -> list:
    """
    Tries to resolve extension queries into a list of extension names.
    """

    if name.endswith('.*'):
        module_parts = name[:-2].split('.')
        path = pathlib.Path(module_parts.pop(0))

        for part in module_parts:
            path = path / part

        return find_extensions_in(path)

    if name == '~':
        return list(bot.extensions.keys())

    return [name]


def package_version(package_name: str) -> typing.Optional[str]:
    """
    Returns package version as a string, or None if it couldn't be found.
    """

    try:
        return pkg_resources.get_distribution(package_name).version
    except (pkg_resources.DistributionNotFound, AttributeError):
        return None


class ExtensionConverter(commands.Converter):  # pylint: disable=too-few-public-methods
    """
    A converter interface for resolve_extensions to match extensions from users.
    """

    async def convert(self, ctx: commands.Context, argument) -> list:
        return resolve_extensions(ctx.bot, argument)
