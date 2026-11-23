from django.db import migrations, models


def ruecknahme_pruefen(apps, schema_editor):
    alias = schema_editor.connection.alias
    auftraege = apps.get_model("steuerung", "Auftrag").objects.using(alias)
    revisionen = apps.get_model("steuerung", "Revision").objects.using(alias)
    meldungen = apps.get_model("steuerung", "Meldung").objects.using(alias)
    if (
        auftraege.filter(models.Q(typ="integration") | models.Q(nur_mensch=True)).exists()
        or revisionen.exclude(kriterienregeln=[]).exists()
        or meldungen.filter(art="LIEFERPLAN").exists()
    ):
        raise RuntimeError(
            "Vor dem Rückweg die gespeicherten Auftragsregeln sichern und abgleichen."
        )


class Migration(migrations.Migration):
    dependencies = [("steuerung", "0021_lieferautomatik")]

    operations = [
        migrations.AddField(
            model_name="auftrag",
            name="nur_mensch",
            field=models.BooleanField("Nur im begleiteten Chat", default=False),
        ),
        migrations.AddField(
            model_name="revision",
            name="kriterienregeln",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name="auftrag",
            name="typ",
            field=models.CharField(
                "Art",
                choices=[
                    ("auftrag", "Auftrag"),
                    ("issue", "Issue"),
                    ("integration", "Integration"),
                ],
                default="auftrag",
                help_text="Issue: Fehler oder Problem",
                max_length=11,
            ),
        ),
        migrations.AlterField(
            model_name="meldung",
            name="art",
            field=models.CharField(
                choices=[
                    ("FORTSCHRITT", "Fortschritt"),
                    ("HINDERNIS", "Hindernis"),
                    ("RUECKFRAGE", "Rückfrage"),
                    ("FEHLER", "Fehler"),
                    ("VORSCHLAG", "Vorschlag"),
                    ("PRUEFBERICHT", "Prüfbericht"),
                    ("ANTWORT", "Antwort"),
                    ("SITZUNGSENDE", "Chat beendet"),
                    ("LIEFERPLAN", "Lieferbedarf geplant"),
                ],
                max_length=16,
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, ruecknahme_pruefen),
    ]
