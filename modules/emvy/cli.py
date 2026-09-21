#!/usr/bin/env python3

# Electronic Cats
# `bombercat emvy …` — EMVy Controller swiss-army commands (EMVyBomberCat
# firmware). SKELETON ONLY (Fase 0): the group exists so the package imports and
# subsequent phases can hang subcommands off it, but it is NOT yet registered in
# core/cli.py and has no operative verbs. See
# docs/IMPLEMENTATION_PLAN_EMVyBomberCat_CLI.md §6 (Fases 1+) — under the Opción B
# decision the operative surface migrates to the canonical +OK/-ERR framing, so
# it will reuse DeviceLink/device_session rather than a parallel serial client.
# Distributed as-is; no warranty is given.

import click


@click.group("emvy")
def emvy() -> None:
    """EMVy Controller swiss-army commands (requires the EMVyBomberCat firmware)."""
