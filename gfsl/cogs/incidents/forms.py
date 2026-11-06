"""Formulare im Ticket: Meldung (Einsender) und Aussage (Beschuldigte/Hinzugefügte).

Absenden ist endgültig: Der Eintrag erscheint im Ticket, der Absender wird entfernt (beim Einsender kommen die Beschuldigten dazu).
"""
import discord

from gfsl import storage

from . import data as D
from .publish import URL_RE, download
from .ticket import lock, now, pending, submit_report, submit_statement


def entry_embed(guild: discord.Guild, t: dict, uid: str, kind: str, fields: dict, links: list[str],
                too_big: list[str]) -> discord.Embed:
    member = guild.get_member(int(uid))
    name = member.display_name if member else uid
    report = kind == "report"
    embed = discord.Embed(color=0x3B82F6 if report else 0x8B5CF6,
                          title=f"{'📝 Meldung' if report else '🗣️ Aussage'} von {name}"[:256],
                          description=fields["text"][:4000])
    if report:
        embed.add_field(name="Beschuldigt", value=", ".join(f"<@{u}>" for u in fields["accused"]), inline=False)
    if links:
        embed.add_field(name="🎥 Videos & Links", value="\n".join(f"• {u}" for u in links)[:1024], inline=False)
    if too_big:
        embed.add_field(name="⚠️ Nicht angehängt (zu groß)", value="\n".join(f"• {n}" for n in too_big)[:1024]
                        + "\nBitte als YouTube-Link nachreichen.", inline=False)
    embed.set_footer(text="Eingetragen")
    embed.timestamp = discord.utils.utcnow()  # Discord zeigt die Uhrzeit in der Zeitzone des Lesers
    return embed


class EntryForm(discord.ui.Modal):
    def __init__(self, t: dict, uid: str, kind: str):
        report = kind == "report"
        super().__init__(title=f"Rennvorfall {D.number(t)} – {'Meldung' if report else 'Aussage'}", timeout=1800)
        self.tid, self.uid, self.kind = t["id"], uid, kind
        prev = t["entries"].get(uid, {}).get("fields", {})
        self.accused = None
        if report:
            self.accused = discord.ui.UserSelect(min_values=1, max_values=10, required=True,
                                                 default_values=[discord.Object(int(u)) for u in prev.get("accused", [])])
            self.add_item(discord.ui.Label(text="Beschuldigte", description="Wen betrifft der Vorfall? Mehrere möglich",
                                           component=self.accused))
        self.text = discord.ui.TextInput(style=discord.TextStyle.long, max_length=2000, default=prev.get("text"),
                                         placeholder="Was ist passiert? Gerne mit Runde/Kurve, falls du es weißt."
                                         if report else "Wie hast du die Situation erlebt?")
        self.links = discord.ui.TextInput(style=discord.TextStyle.long, required=False, max_length=500, default=prev.get("links"),
                                          placeholder="https://youtu.be/… (einer pro Zeile)")
        self.files = discord.ui.FileUpload(required=False, min_values=0, max_values=5)
        self.add_item(discord.ui.Label(text="Vorfallbeschreibung" if report else "Deine Aussage", component=self.text))
        self.add_item(discord.ui.Label(text="Video-Links (YouTube o. ä.)", component=self.links))
        self.add_item(discord.ui.Label(text="Video / Screenshots (optional)", component=self.files,
                                       description="Große Videos bitte als Link"))
        self.add_item(discord.ui.TextDisplay("⚠️ **Absenden ist endgültig** – danach kannst du nichts mehr ändern und verlässt das Ticket."))

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        d = D.get(guild.id)
        t = d["tickets"].get(self.tid)
        report = self.kind == "report"
        def allowed():
            return t and ((report and t["phase"] == "reporter") or (not report and t["phase"] == "statements" and self.uid in pending(t)))

        if not allowed():
            return await interaction.response.send_message("Das Formular kann in diesem Schritt nicht mehr gespeichert werden.", ephemeral=True)

        fields = {"text": self.text.value.strip(), "links": self.links.value.strip()}
        if report:
            accused = [u for u in self.accused.values if not u.bot and str(u.id) != self.uid and guild.get_member(u.id)]
            if not accused:
                return await interaction.response.send_message("❌ Bitte mindestens einen Beschuldigten (nicht dich selbst) auswählen.",
                                                               ephemeral=True)
            fields["accused"] = [str(u.id) for u in accused]
        links = list(dict.fromkeys(u.rstrip(".,!?") for u in URL_RE.findall(fields["links"])))
        if fields["links"] and not links:
            return await interaction.response.send_message("❌ Bei den Video-Links bitte vollständige Links angeben (https://…).",
                                                           ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = interaction.channel
        async with lock(guild.id, self.tid):
            if not allowed():  # doppelt abgeschickt
                return await interaction.followup.send("Das wurde schon abgeschickt.", ephemeral=True)
            old = t["entries"].get(self.uid)
            uploads = list(self.files.values)
            if not uploads and old:  # nichts Neues hochgeladen: bisherige Dateien übernehmen
                try:
                    uploads = (await channel.fetch_message(old["message_id"])).attachments
                except discord.HTTPException:
                    uploads = []
            files, too_big = await download(uploads, guild.filesize_limit)
            embed = entry_embed(guild, t, self.uid, self.kind, fields, links, [a.filename for a in too_big])
            msg = None
            if old:  # Karte an derselben Stelle aktualisieren statt neu posten
                try:
                    msg = await channel.get_partial_message(old["message_id"]).edit(embed=embed, attachments=files)
                except discord.NotFound:
                    msg = None
            if msg is None:
                msg = await channel.send(embed=embed, files=files, allowed_mentions=discord.AllowedMentions.none())
            t["entries"][self.uid] = {"message_id": msg.id, "at": now(), "fields": fields, "links": links}
            if report:
                await submit_report(channel, t)
            else:
                await submit_statement(channel, d, t, self.uid)
            storage.save()

        text = "✅ Abgeschickt. Du wirst jetzt aus dem Ticket entfernt – die Entscheidung der Stewards wird im Ergebnis-Kanal veröffentlicht."
        if too_big:
            text += (f"\n⚠️ Zu groß zum Anhängen (max. {guild.filesize_limit // 1_048_576} MB): "
                     + ", ".join(a.filename for a in too_big) + " – bitte als YouTube-Link angeben.")
        await interaction.followup.send(text, ephemeral=True)
