"""Nummern je Projekt: NEX-12 statt A-171. Die Primärschlüssel bleiben, die alte Nummer gilt weiter."""

import re
import unicodedata

from django.db import migrations, models

# Mit Fred festgelegt; übrige Projekte bekommen ihr Kürzel aus dem Namen.
FESTGELEGT = {
    "nexus": "NEX",
    "server-ki-pm": "KIPM",
    "ai-live": "AIL",
    "aig": "AIG",
    "antihack": "AH",
    "claudemastersetup": "CMS",
    "fahrzeug-diagnose": "FDG",
    "fahrzeug-simulator": "FSM",
    "hardwareloader": "HWL",
    "leitstand": "LST",
    "ui-optimizer": "UIO",
    "vehiclelogger": "VLG",
}


def _gebildet(name: str, belegt: set[str]) -> str:
    """Eingefroren wie jede Migration; die Regel für neue Projekte steht in dienst.kuerzel_vorschlag."""
    flach = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper()
    woerter = re.findall(r"[A-Z0-9]+", flach)
    zeichen = "".join(woerter) or "P"
    if not zeichen[0].isalpha():
        zeichen = "P" + zeichen
    vorn = "".join(w[0] for w in woerter)[:4] if len(woerter) > 1 else zeichen[:3]
    for kandidat in (vorn, *(zeichen[:n] for n in range(3, min(len(zeichen), 8) + 1))):
        if len(kandidat) >= 2 and kandidat[0].isalpha() and kandidat not in belegt:
            return kandidat
    n = 2
    while f"{zeichen[:5]}{n}" in belegt:
        n += 1
    return f"{zeichen[:5]}{n}"


def nummern_vergeben(apps, schema_editor):
    Projekt = apps.get_model("steuerung", "Projekt")
    Auftrag = apps.get_model("steuerung", "Auftrag")
    alle = list(Projekt.objects.order_by("id"))
    belegt = {FESTGELEGT[p.kennung] for p in alle if p.kennung in FESTGELEGT}
    for p in alle:
        kuerzel = FESTGELEGT.get(p.kennung)
        if kuerzel is None:
            kuerzel = _gebildet(p.name or p.kennung, belegt)
            belegt.add(kuerzel)
        zaehler = 0
        for a in Auftrag.objects.filter(projekt=p).order_by("id").only("pk"):
            zaehler += 1
            # update statt save: aktualisiert_am ändert sich nicht durch die neue Nummer.
            Auftrag.objects.filter(pk=a.pk).update(projekt_nummer=zaehler)
        Projekt.objects.filter(pk=p.pk).update(kuerzel=kuerzel, naechste_nummer=zaehler + 1)


class Migration(migrations.Migration):
    dependencies = [
        ("steuerung", "0014_meldung_sitzungsende"),
    ]

    operations = [
        migrations.AddField(
            model_name="projekt",
            name="kuerzel",
            field=models.CharField(default="", max_length=8, verbose_name="Kürzel"),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="projekt",
            name="naechste_nummer",
            field=models.PositiveIntegerField(default=1, verbose_name="Nächste Auftragsnummer"),
        ),
        migrations.AddField(
            model_name="auftrag",
            name="projekt_nummer",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(nummern_vergeben, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="projekt",
            name="kuerzel",
            field=models.CharField(max_length=8, unique=True, verbose_name="Kürzel"),
        ),
        migrations.AddConstraint(
            model_name="auftrag",
            constraint=models.UniqueConstraint(
                fields=("projekt", "projekt_nummer"), name="auftrag_nummer_je_projekt"
            ),
        ),
    ]
