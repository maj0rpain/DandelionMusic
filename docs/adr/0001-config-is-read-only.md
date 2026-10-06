# Config is read-only; runtime state lives in the database

`config.Config` loads `.env` and never writes it back. It has no `save()`.
Any setting the bot changes at runtime is stored in the database
(`musicbot/settings.py`).

The bot used to write `.env` for `d!guild_whitelist`. That one write-back
path caused five separate bugs: `BOT_TOKEN` leaked into the committed
`.env.sample`; `EMBED_COLOR` changed on every save; a tuple-valued setting
made the next startup fail; writes went to the wrong directory; and a
removal silently did not persist. The whitelist moved into the database
(`WhitelistedGuild`), and the write-back path was deleted.

Considered: fixing the writer instead. Rejected because `.env` belongs to
the operator, and a round-trip writer has to preserve comments, ordering,
quoting and types it never parsed.
