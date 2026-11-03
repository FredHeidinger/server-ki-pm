"""Upload von bis zu fünf Dateien je Vorgang (Verbesserungsliste 3a, ``KIPM_UPLOAD_DATEIEN``).
Scanner ist der gekennzeichnete Testadapter."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from steuerung.models import Unterlage, Unterlagenversion
from tests.test_ablage import ablageordner, pdf, png  # noqa: F401  (Fixture wird genutzt)

pytestmark = pytest.mark.django_db


def _senden(client, projekt, dateien, schluessel="mehrfach-1", unterlage=None):
    url = (
        reverse(
            "hochladen_version", kwargs={"kennung": projekt.kennung, "nummer": unterlage.nummer}
        )
        if unterlage
        else reverse("hochladen", kwargs={"kennung": projekt.kennung})
    )
    daten = {
        "upload_schluessel": schluessel,
        "beschreibung": "Sammlung",
        "datei": [SimpleUploadedFile(n, i) for n, i in dateien],
    }
    if unterlage:
        daten["erwartete_nr"] = unterlage.aktuelle_nr
    return client.post(url, daten)


def test_mehrere_dateien_je_vorgang_je_datei_eine_unterlage(owner_client, projekt):
    seite = owner_client.get(reverse("hochladen", kwargs={"kennung": "synth-a"})).content.decode()
    assert "multiple" in seite and "bis zu 5 Dateien je Vorgang" in seite
    dateien = [("a.pdf", pdf("a")), ("b.png", png()), ("c.txt", b"Notiz")]
    antwort = _senden(owner_client, projekt, dateien)
    assert antwort.status_code == 302 and antwort.url.endswith("/wissen/")
    assert sorted(Unterlage.objects.values_list("titel", flat=True)) == ["a.pdf", "b.png", "c.txt"]
    # Wiederholtes Absenden (gleicher Schlüssel): nichts doppelt.
    _senden(owner_client, projekt, dateien)
    assert Unterlage.objects.count() == 3 and Unterlagenversion.objects.count() == 3


def test_mehr_als_fuenf_oder_eine_unerlaubte_datei_speichert_nichts(
    owner_client, projekt, settings
):
    sechs = [(f"n{i}.txt", f"Text {i}".encode()) for i in range(6)]
    antwort = _senden(owner_client, projekt, sechs)
    assert antwort.status_code == 400 and "Höchstens 5 Dateien" in antwort.content.decode()
    gemischt = [("ok.txt", b"gut"), ("boese.html", b"<script>")]
    antwort = _senden(owner_client, projekt, gemischt, schluessel="mehrfach-2")
    assert antwort.status_code == 400
    assert "boese.html" in antwort.content.decode()
    assert Unterlage.objects.count() == 0
    settings.KIPM_UPLOAD_DATEIEN = 2
    antwort = _senden(owner_client, projekt, sechs[:3], schluessel="mehrfach-3")
    assert antwort.status_code == 400 and "Höchstens 2 Dateien" in antwort.content.decode()


def test_neue_version_bleibt_genau_eine_datei(owner_client, projekt):
    _senden(owner_client, projekt, [("plan.txt", b"v1")])
    plan = Unterlage.objects.get()
    seite = owner_client.get(
        reverse("hochladen_version", kwargs={"kennung": "synth-a", "nummer": plan.nummer})
    ).content.decode()
    assert "multiple" not in seite and "eine Datei als neue Version" in seite
    antwort = _senden(
        owner_client,
        projekt,
        [("plan.txt", b"v2"), ("plan2.txt", b"v3")],
        schluessel="version-2",
        unterlage=plan,
    )
    assert antwort.status_code == 400 and "genau einer Datei" in antwort.content.decode()
    plan.refresh_from_db()
    assert plan.aktuelle_nr == 1
