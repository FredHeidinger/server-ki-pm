from django.urls import path

from . import api, views

urlpatterns = [
    path("anmelden/", views.Anmelden.as_view(), name="anmelden"),
    path("abmelden/", views.Abmelden.as_view(), name="abmelden"),
    # Persönliche Ebene
    path("", views.posteingang, name="posteingang"),
    path("meine/", views.meine, name="meine"),
    # Projektübergreifend
    path("auftraege/", views.auftraege, name="auftraege"),
    path("auftraege/wunsch/", views.wunsch_neu, name="wunsch_neu"),
    path("auftraege/<int:auftrag_id>/", views.auftrag_detail, name="auftrag_detail"),
    path("auftraege/<int:auftrag_id>/panel/", views.auftrag_panel, name="auftrag_panel"),
    path(
        "auftraege/<int:auftrag_id>/bearbeiten/",
        views.auftrag_bearbeiten,
        name="auftrag_bearbeiten",
    ),
    path(
        "auftraege/<int:auftrag_id>/aktion/<slug:aktion>/",
        views.auftrag_aktion,
        name="auftrag_aktion",
    ),
    path("auftraege/<int:auftrag_id>/teilen/", views.auftrag_teilen, name="auftrag_teilen"),
    path("auftraege/<int:auftrag_id>/ergebnis/", views.eigenes_ergebnis, name="eigenes_ergebnis"),
    path("auftraege/<int:auftrag_id>/antwort/", views.antwort, name="antwort"),
    path(
        "auftraege/<int:auftrag_id>/vorschlag/<int:meldung_id>/<slug:wahl>/",
        views.vorschlag_bewerten,
        name="vorschlag_bewerten",
    ),
    path("pruefung/", views.pruefung, name="pruefung"),
    path("pruefung/<int:ergebnis_id>/", views.pruefung_detail, name="pruefung_detail"),
    path("entscheidungen/", views.entscheidungen, name="entscheidungen"),
    path("entscheidungen/neu/", views.entscheidung_neu, name="entscheidung_neu"),
    path(
        "entscheidungen/<int:entscheidung_id>/",
        views.entscheidung_detail,
        name="entscheidung_detail",
    ),
    path(
        "entscheidungen/<int:entscheidung_id>/aktion/<slug:aktion>/",
        views.entscheidung_aktion,
        name="entscheidung_aktion",
    ),
    path("verlauf/", views.verlauf, name="verlauf"),
    # Projekte als Arbeitsbereich
    path("projekte/", views.projekte, name="projekte"),
    path("projekt/neu/", views.projekt_neu, name="projekt_neu"),
    path("projekt/<slug:kennung>/", views.projekt_auftraege, name="projekt_detail"),
    path("projekt/<slug:kennung>/pruefung/", views.projekt_pruefung, name="projekt_pruefung"),
    path(
        "projekt/<slug:kennung>/entscheidungen/",
        views.projekt_entscheidungen,
        name="projekt_entscheidungen",
    ),
    path("projekt/<slug:kennung>/verlauf/", views.projekt_verlauf, name="projekt_verlauf"),
    path(
        "projekt/<slug:kennung>/einstellungen/",
        views.projekt_einstellungen,
        name="projekt_einstellungen",
    ),
    path("projekt/<slug:kennung>/modul/", views.modul_neu, name="modul_neu"),
    path("projekt/<slug:kennung>/arbeitsort/", views.arbeitsort_neu, name="arbeitsort_neu"),
    # Verbindungen
    path("verbindungen/", views.verbindungen, name="verbindungen"),
    path("verbindungen/zugang/neu/", views.zugang_neu, name="zugang_neu"),
    path(
        "verbindungen/zugang/<int:bindung_id>/widerrufen/",
        views.bindung_widerrufen,
        name="bindung_widerrufen",
    ),
    path(
        "verbindungen/zugang/<int:bindung_id>/pruefen/",
        views.zugang_pruefen,
        name="zugang_pruefen",
    ),
    path("verbindungen/<slug:art>/", views.verbindung, name="verbindung"),
    # HTTP-Schnittstelle für Zugänge
    path("api/projekt/", api.projekt_lesen, name="api_projekt"),
    path("api/auftraege/", api.meine_auftraege, name="api_auftraege"),
    path("api/auftraege/anlegen/", api.auftrag_anlegen, name="api_auftrag_anlegen"),
    path("api/auftraege/<int:auftrag_id>/", api.auftrag_lesen, name="api_auftrag"),
    path("api/auftraege/<int:auftrag_id>/uebernehmen/", api.uebernehmen, name="api_uebernehmen"),
    path("api/auftraege/<int:auftrag_id>/fortschritt/", api.fortschritt, name="api_fortschritt"),
    path("api/auftraege/<int:auftrag_id>/hindernis/", api.hindernis, name="api_hindernis"),
    path("api/auftraege/<int:auftrag_id>/rueckfrage/", api.rueckfrage, name="api_rueckfrage"),
    path("api/auftraege/<int:auftrag_id>/fehler/", api.fehler, name="api_fehler"),
    path("api/auftraege/<int:auftrag_id>/vorschlag/", api.vorschlag, name="api_vorschlag"),
    path("api/auftraege/<int:auftrag_id>/pruefbericht/", api.pruefbericht, name="api_pruefbericht"),
    path("api/auftraege/<int:auftrag_id>/ergebnis/", api.ergebnis, name="api_ergebnis"),
    path("api/entscheidungen/", api.entscheidungen, name="api_entscheidungen"),
    path(
        "api/entscheidungen/vorschlagen/",
        api.entscheidung_vorschlagen,
        name="api_entscheidung_vorschlagen",
    ),
    path(
        "api/entscheidungen/<int:entscheidung_id>/bestaetigen/",
        api.entscheidung_bestaetigen,
        name="api_entscheidung_bestaetigen",
    ),
]
