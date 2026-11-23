from django.db import migrations, models


def fortschritt_erhalten(apps, schema_editor):
    meldungen = apps.get_model("steuerung", "Meldung").objects.using(schema_editor.connection.alias)
    if meldungen.filter(festgefahren=True).exists() or (
        meldungen.exclude(kriterien_erfuellt=None).exclude(kriterien_erfuellt=[]).exists()
    ):
        raise RuntimeError("Gespeicherter Kriterienfortschritt würde verloren gehen.")


class Migration(migrations.Migration):
    dependencies = [("steuerung", "0024_ordnersperre")]

    operations = [
        migrations.AddField(
            model_name="meldung",
            name="kriterien_erfuellt",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="meldung",
            name="festgefahren",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(migrations.RunPython.noop, fortschritt_erhalten),
    ]
