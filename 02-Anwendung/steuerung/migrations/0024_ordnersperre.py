import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("steuerung", "0023_auftragskriterien")]

    operations = [
        migrations.CreateModel(
            name="Ordnersperre",
            fields=[
                ("pfad", models.CharField(max_length=320, primary_key=True, serialize=False)),
                ("ablauf", models.DateTimeField()),
                (
                    "auftrag",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to="steuerung.auftrag"
                    ),
                ),
            ],
        ),
    ]
