from django.db import migrations, models


def befunde_erhalten(apps, schema_editor):
    alias = schema_editor.connection.alias
    auftraege = apps.get_model("steuerung", "Auftrag").objects.using(alias)
    ergebnisse = apps.get_model("steuerung", "Ergebnis").objects.using(alias)
    if (
        auftraege.filter(fremdbefund_sammelissue=True).exists()
        or ergebnisse.exclude(fremde_befunde=[]).exists()
    ):
        raise RuntimeError("Gespeicherte Fremdbefunde würden verloren gehen.")


class Migration(migrations.Migration):
    dependencies = [("steuerung", "0025_festfahren")]

    operations = [
        migrations.AddField(
            model_name="auftrag",
            name="fremdbefund_sammelissue",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="ergebnis",
            name="fremde_befunde",
            field=models.JSONField(blank=True, default=list),
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
                    ("FREMDBEFUND", "Fremder Prüfbefund"),
                ],
                max_length=16,
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, befunde_erhalten),
    ]
