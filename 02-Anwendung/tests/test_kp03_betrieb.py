"""KIPM-35 (KP03): sicherer Betrieb und Verwaltung, serverseitig.

Vor dem Code aus dem Auftrag und den Fällen B15, X58 D4, X59 und X57 U6 abgeleitet:
- B15/D4: Meldet die Werkbank nach einer Wiederherstellung des Servers eine Ausführung, die KI-PM
  nicht kennt, hält KI-PM das als Konflikt fest, zeigt ihn Fred in „Wartet auf dich“ und lässt diese
  Werkbank nichts übernehmen, bis Fred ihn geklärt hat. Erledigte Arbeit startet so nicht neu.
- X59: Termin und Meilenstein lassen Freigabe und gebundenen Kontext eines Werkbank-Auftrags
  unberührt; Archivieren geht nicht, solange eine Werkbank-Ausführung läuft.
- U6: Keine Seite lädt sich selbst neu oder fragt im Ruhezustand den Server ab.
"""

import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import akte, dienst, werkbank
from steuerung.models import Auftrag, WerkbankAusfuehrung, WerkbankKonflikt
from tests import werkbank_hilfen as h
from tests.conftest import arbeitspaket

pytestmark = pytest.mark.django_db


def _abgleich(client, inst, *kennungen):
    return client.get(
        h.BASIS + "abgleich/",
        {"ausfuehrungen": ",".join(kennungen)},
        **h.kopf(inst),
    )


def _freigegeben(akteur, projekt, pb, titel="Nach Wiederherstellung"):
    a = arbeitspaket(akteur, projekt, titel=titel, umfang=f"{titel[:4].lower()}/**")
    werkbank.zuweisen(akteur, a, pb, freigeben=True)
    a.refresh_from_db()
    return a


# --- B15 / D4: Wiederherstellung --------------------------------------------------------------


def test_unbekannte_ausfuehrung_wird_konflikt_und_sperrt_die_uebernahme(
    client, akteur, projekt, w_instanz, w_pbindung
):
    a = _freigegeben(akteur, projekt, w_pbindung)
    verloren = str(uuid.uuid4())
    antwort = _abgleich(client, w_instanz, verloren).json()
    assert [k["ausfuehrung_id"] for k in antwort["konflikte"]] == [verloren]
    konflikt = WerkbankKonflikt.objects.get(instanz=w_instanz, ausfuehrung_id=verloren)
    assert konflikt.geklaert_am is None
    n = h.uebernahme_nachricht(h.vor_quelle(w_instanz, w_pbindung, a))
    fehler = h.senden(client, w_instanz, "uebernehmen/", n)
    assert fehler.status_code == 409
    assert fehler.json()["fehler"]["code"] == "WIEDERHERSTELLUNG_UNGEKLAERT"
    assert not WerkbankAusfuehrung.objects.filter(auftrag=a).exists()
    # Dieselbe Meldung noch einmal legt keinen zweiten Konflikt an.
    _abgleich(client, w_instanz, verloren)
    assert WerkbankKonflikt.objects.filter(instanz=w_instanz).count() == 1


def test_erledigte_arbeit_startet_nach_wiederherstellung_nicht_neu(
    client, akteur, projekt, w_instanz, w_pbindung, w_ausfuehrung
):
    """Nachgestellt: Die Sicherung stammt von vor der Übernahme. Die Ausführung fehlt danach,
    der Auftrag steht wieder auf Freigegeben; die Werkbank kennt ihre Ausführung noch."""
    kennung = str(w_ausfuehrung.ausfuehrung_id)
    a = w_ausfuehrung.auftrag
    WerkbankAusfuehrung.objects.filter(pk=w_ausfuehrung.pk).update(zustand="abgeschlossen")
    w_ausfuehrung.operationen.all().delete()
    w_ausfuehrung.ereignisse.all().delete()
    WerkbankAusfuehrung.objects.filter(pk=w_ausfuehrung.pk).delete()
    Auftrag.objects.filter(pk=a.pk).update(
        zustand=Auftrag.FREIGEGEBEN, bearbeiter_bindung=None, bearbeiter_name=""
    )
    _abgleich(client, w_instanz, kennung)
    a.refresh_from_db()
    n = h.uebernahme_nachricht(h.vor_quelle(w_instanz, w_pbindung, a))
    assert h.senden(client, w_instanz, "uebernehmen/", n).status_code == 409
    assert not WerkbankAusfuehrung.objects.filter(auftrag=a).exists()


def test_fred_sieht_den_konflikt_und_klaert_ihn(
    client, owner_client, akteur, projekt, w_instanz, w_pbindung
):
    a = _freigegeben(akteur, projekt, w_pbindung)
    _abgleich(client, w_instanz, str(uuid.uuid4()))
    start = owner_client.get(reverse("projekt_detail", kwargs={"kennung": projekt.kennung}))
    assert "unbekannte Ausführung" in start.content.decode()
    verwaltung = owner_client.get(reverse("verbindungen")).content.decode()
    assert "unbekannte Ausführung" in verwaltung and "Als geklärt markieren" in verwaltung
    url = reverse("werkbank_pflegen", kwargs={"instanz_id": w_instanz.pk, "wahl": "konflikte"})
    owner_client.post(url, {"bestaetigt": "1"})
    assert not WerkbankKonflikt.objects.filter(geklaert_am__isnull=True).exists()
    n = h.uebernahme_nachricht(h.vor_quelle(w_instanz, w_pbindung, a))
    assert h.senden(client, w_instanz, "uebernehmen/", n).status_code == 200


def test_klaeren_ohne_bestaetigung_aendert_nichts(owner_client, client, w_instanz):
    _abgleich(client, w_instanz, str(uuid.uuid4()))
    url = reverse("werkbank_pflegen", kwargs={"instanz_id": w_instanz.pk, "wahl": "konflikte"})
    owner_client.post(url, {})
    assert WerkbankKonflikt.objects.filter(geklaert_am__isnull=True).count() == 1


def test_nur_fred_klaert(akteur, bindung, client, w_instanz):
    _abgleich(client, w_instanz, str(uuid.uuid4()))
    with pytest.raises(dienst.KeinZugriff):
        werkbank.konflikte_klaeren(dienst.Akteur.aus_bindung(bindung), w_instanz)


# --- X59: Metadaten ohne Rechtewirkung, Archiv ------------------------------------------------


def test_termin_und_meilenstein_lassen_freigabe_und_kontext_unberuehrt(akteur, projekt, w_pbindung):
    a = _freigegeben(akteur, projekt, w_pbindung, titel="Metadaten")
    vorher = (a.zustand, a.revision_nr, a.zuweisung.kontext_sha256, a.zuweisung.version)
    m = akte.meilenstein_anlegen(akteur, projekt, name="M", datum=timezone.localdate())
    dienst.faellig_setzen(akteur, a, timezone.localdate() + timedelta(days=3))
    dienst.meilenstein_zuordnen(akteur, a, m)
    a.refresh_from_db()
    a.zuweisung.refresh_from_db()
    assert (a.zustand, a.revision_nr, a.zuweisung.kontext_sha256, a.zuweisung.version) == vorher


def test_archivieren_geht_nicht_waehrend_eine_werkbank_arbeitet(akteur, projekt, w_ausfuehrung):
    Auftrag.objects.filter(pk=w_ausfuehrung.auftrag_id).update(zustand=Auftrag.ENTWURF)
    with pytest.raises(dienst.DienstFehler):
        dienst.projekt_archivieren(akteur, projekt)
    projekt.refresh_from_db()
    assert projekt.aktiv


# --- U6: kein Selbstneuladen, keine Ruheabfragen ------------------------------------------------


def test_keine_seite_laedt_sich_selbst_neu(owner_client, projekt):
    for url in (reverse("start"), reverse("projekt_detail", kwargs={"kennung": projekt.kennung})):
        html = owner_client.get(url).content.decode()
        assert 'http-equiv="refresh"' not in html.lower()
    vorlagen = Path(werkbank.__file__).resolve().parent / "templates"
    for datei in vorlagen.rglob("*.html"):
        text = datei.read_text("utf-8")
        assert not re.search(r"setInterval|EventSource|WebSocket|http-equiv=.refresh", text), datei
