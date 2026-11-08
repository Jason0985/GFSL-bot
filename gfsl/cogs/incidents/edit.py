"""Entscheidung nachträglich ändern – über den Button oben im Archiv.

Die Nachricht im Ergebnis-Kanal wird nur bearbeitet (nicht neu gepostet, kein neuer Ping). Fehlen Clips, weil sie
z. B. versehentlich gelöscht wurden, können sie hier als Link oder Datei nachgereicht werden.
Jede Änderung steht mit altem Stand im Archiv-Thread und im Verlauf.
"""
import logging

import discord

from gfsl import storage

from . import data as D
from .publish import URL_RE, download, internal_embed, summary_embed, too_big_text
from .ticket import add_log, is_staff, lock, now

log = logging.getLogger("gfsl.incidents")
NONE = discord.AllowedMentions.none()


class EditButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rvx:edit:(?P<t>\d+)"):
    def __init__(self, tid: str):
        super().__init__(discord.ui.Button(label="Entscheidung bearbeiten", style=discord.ButtonStyle.secondary,
                                           emoji="✏️", custom_id=f"rvx:edit:{tid}"))
        self.tid = tid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["t"])

    async def callback(self, interaction: discord.Interaction):
        d = D.get(interaction.guild_id)
        t = d["tickets"].get(self.tid)
        if not is_staff(interaction.user, d["settings"]):
            return await interaction.response.send_message("Das können nur Ligaleitung und Stewards.", ephemeral=True)
        if t is None or t["phase"] != "closed":
            return await interaction.response.send_message("Zu diesem Vorfall gibt es keine Entscheidung zum Bearbeiten.",
                                                           ephemeral=True)
        await interaction.response.send_modal(EditModal(t))


def archive_view(t: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(EditButton(t["id"]))
    return view


async def resolve(guild: discord.Guild, channel_id: int):
    """Kanal oder Thread holen – archivierte Threads stehen nicht im Cache."""
    channel = guild.get_channel_or_thread(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel


class Reopened:
    """Archivierten Thread kurz öffnen (sonst lässt Discord nichts bearbeiten) und danach wieder schließen."""

    def __init__(self, channel):
        self.channel, self.state = channel, None

    async def __aenter__(self):
        if isinstance(self.channel, discord.Thread) and (self.channel.archived or self.channel.locked):
            self.state = (self.channel.archived, self.channel.locked)
            self.channel = await self.channel.edit(archived=False)
        return self.channel

    async def __aexit__(self, *exc):
        if self.state:
            try:
                await self.channel.edit(archived=self.state[0], locked=self.state[1])
            except discord.HTTPException:
                pass


class EditModal(discord.ui.Modal):
    def __init__(self, t: dict):
        super().__init__(title=f"Rennvorfall {D.number(t)} – Entscheidung ändern", timeout=1800)
        self.tid = t["id"]
        self.decision = discord.ui.TextInput(style=discord.TextStyle.long, max_length=3000, default=t["decision"])
        self.penalty = discord.ui.TextInput(style=discord.TextStyle.long, max_length=1000, required=False,
                                            default=t["penalty"], placeholder="leer = keine Strafe")
        urls = "\n".join(url for _, url in t.get("result_links") or [])
        self.links = discord.ui.TextInput(style=discord.TextStyle.long, max_length=4000, required=False,
                                          default=urls[:4000] or None, placeholder="https://youtu.be/… (einer pro Zeile)")
        self.files = discord.ui.FileUpload(required=False, min_values=0, max_values=5)
        self.add_item(discord.ui.Label(text="Entscheidung", component=self.decision))
        self.add_item(discord.ui.Label(text="Strafe(n)", component=self.penalty))
        self.add_item(discord.ui.Label(text="Video-Links", component=self.links,
                                       description="Fehlende Clips einfach als Link ergänzen, falsche löschen"))
        self.add_item(discord.ui.Label(text="Clips / Dateien nachreichen (optional)", component=self.files,
                                       description="Werden an die Entscheidung angehängt"))

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        d = D.get(guild.id)
        t = d["tickets"].get(self.tid)
        if t is None or t["phase"] != "closed":
            return await interaction.response.send_message("Zu diesem Vorfall gibt es keine Entscheidung.", ephemeral=True)
        decision = self.decision.value.strip()
        if not decision:
            return await interaction.response.send_message("❌ Die Entscheidung darf nicht leer sein.", ephemeral=True)
        urls = list(dict.fromkeys(u.rstrip(".,!?") for u in URL_RE.findall(self.links.value)))
        if self.links.value.strip() and not urls:
            return await interaction.response.send_message("❌ Bei den Video-Links bitte vollständige Links angeben (https://…).",
                                                           ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        uploads = list(self.files.values)

        async with lock(guild.id, self.tid):
            old = {"decision": t["decision"], "penalty": t["penalty"]}
            owner = {url: uid for uid, url in t.get("result_links") or []}  # wer den Clip ursprünglich eingereicht hat
            t["decision"] = decision
            t["penalty"] = self.penalty.value.strip() or None
            t["result_links"] = [[owner.get(u), u] for u in urls]
            t["edited_by"], t["edited_at"] = str(interaction.user.id), now()
            changed = [label for key, label in (("decision", "Entscheidung"), ("penalty", "Strafe"))
                       if old[key] != t[key]]
            if urls != list(owner):
                changed.append("Links")
            if uploads:
                changed.append(f"{len(uploads)} Datei(en) nachgereicht")
            add_log(t, f"Geändert von <@{interaction.user.id}>: {', '.join(changed) or 'nichts'}")
            storage.save()

            notes = [await self.update_result(guild, t, uploads), await self.update_archive(guild, t, old, uploads)]
            storage.save()
        await interaction.followup.send("\n".join(n for n in notes if n), ephemeral=True)

    async def update_result(self, guild: discord.Guild, t: dict, uploads: list[discord.Attachment]) -> str:
        """Die bestehende Nachricht im Ergebnis-Kanal bearbeiten – Dateien werden an genau diese angehängt."""
        if not t.get("result_message"):
            return "⚠️ Die Entscheidung im Ergebnis-Kanal ist nicht bekannt (älterer Vorfall) – dort bitte von Hand anpassen."
        channel = await resolve(guild, t["result_message"][0])
        if channel is None:
            return "⚠️ Ergebnis-Kanal nicht gefunden – Entscheidung dort nicht aktualisiert."
        files, too_big = await download(uploads, guild.filesize_limit)
        t["result_too_big"] = [*(t.get("result_too_big") or []), *too_big_text(too_big)]
        try:
            async with Reopened(channel) as ch:
                msg = await ch.fetch_message(t["result_message"][1])
                room = 10 - len(msg.attachments)
                extra = [f.filename for f in files[room:]]
                if extra:  # passt nicht mehr an die Nachricht -> als „zu groß“ vermerkt, liegt aber im Archiv
                    t["result_too_big"] += [f"{n} (max. 10 Dateien pro Nachricht – siehe Archiv)" for n in extra]
                kwargs = {"embed": summary_embed(t), "allowed_mentions": NONE}
                if files[:room]:
                    kwargs["attachments"] = [*msg.attachments, *files[:room]]
                await msg.edit(**kwargs)
        except discord.HTTPException as e:
            log.warning("Entscheidung #%s nicht aktualisiert: %s", t["id"], e)
            return f"⚠️ Entscheidung im Ergebnis-Kanal nicht aktualisiert ({e.text or e})."
        return f"✅ Entscheidung aktualisiert: {msg.jump_url}"

    async def update_archive(self, guild: discord.Guild, t: dict, old: dict, uploads: list[discord.Attachment]) -> str:
        """Übersicht oben im Archiv aktualisieren und die Änderung (mit altem Stand) in den Thread schreiben."""
        if not t.get("archive_message"):
            return "⚠️ Archiv-Eintrag nicht gefunden."
        try:
            channel = await resolve(guild, t["archive_message"][0])
            if channel is not None:
                async with Reopened(channel) as ch:
                    await ch.get_partial_message(t["archive_message"][1]).edit(embed=internal_embed(t), view=archive_view(t))
            thread = await resolve(guild, t.get("archive_thread_id") or 0)
            if thread is not None:
                async with Reopened(thread) as th:
                    text = [f"✏️ **Entscheidung geändert** von <@{t['edited_by']}> · <t:{t['edited_at']}:f>",
                            "**Vorher – Entscheidung:**", old["decision"][:700],
                            f"**Vorher – Strafe(n):** {(old['penalty'] or '—')[:500]}"]
                    files, _ = await download(uploads, guild.filesize_limit)
                    await th.send("\n".join(text)[:2000], files=files[:10], allowed_mentions=NONE)
        except discord.HTTPException as e:
            log.warning("Archiv #%s nicht aktualisiert: %s", t["id"], e)
            return f"⚠️ Archiv nicht aktualisiert ({e.text or e})."
        return "🗄️ Archiv aktualisiert."
