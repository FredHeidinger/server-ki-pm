"""Ownerformular für eng begrenzte W1-Erlaubnisse."""

from datetime import timedelta

from django import forms
from django.utils import timezone

from .forms import _stufen
from .models import ChatBindung, Projekt, WerkbankProjektbindung

AKTIONSTEXTE = {
    "auftrag_anlegen": "Aufträge anlegen und planen",
    "auftrag_aendern": "Aufträge neu planen",
    "zuweisen": "Einem erlaubten Werkbankziel zuweisen",
    "freigeben": "Geplante Aufträge freigeben",
}


class ZielName:
    def label_from_instance(self, obj):
        return f"{obj.projekt.name} · {obj.instanz.name} · {obj.arbeitsort.name}"


class ZielAuswahl(ZielName, forms.ModelMultipleChoiceField):
    pass


class StandardzielAuswahl(ZielName, forms.ModelChoiceField):
    pass


class W1ErlaubnisForm(forms.Form):
    projekt = forms.ModelChoiceField(label="Projekt", queryset=Projekt.objects.none())
    bindung = forms.ModelChoiceField(label="Verbundener Chat", queryset=ChatBindung.objects.none())
    ziele = ZielAuswahl(
        label="Erlaubte Werkbankziele",
        queryset=WerkbankProjektbindung.objects.none(),
        widget=forms.CheckboxSelectMultiple,
    )
    standard_ziel = StandardzielAuswahl(
        label="Standardziel",
        required=False,
        queryset=WerkbankProjektbindung.objects.none(),
        empty_label="Bei mehreren Zielen nachfragen",
    )
    aktionen = forms.MultipleChoiceField(
        label="Erlaubte Aktionen", choices=AKTIONSTEXTE.items(), widget=forms.CheckboxSelectMultiple
    )
    waehrung = forms.ChoiceField(
        label="Währung", choices=[("USD", "US-Dollar (USD)"), ("EUR", "Euro (EUR)")]
    )
    geld = forms.DecimalField(
        label="Geldobergrenze insgesamt",
        min_value=0,
        max_value=9_007_199_254,
        max_digits=16,
        decimal_places=6,
        help_text="Gilt gemeinsam für alle Aufträge dieser Erlaubnis.",
    )
    minuten = forms.IntegerField(
        label="Zeitobergrenze insgesamt (Minuten)", min_value=1, max_value=150_119_987_579
    )
    aufrufe = forms.IntegerField(label="KI-Aufrufe insgesamt", min_value=1, max_value=2**53 - 1)
    gueltig_bis = forms.DateTimeField(
        label="Gültig bis",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M"],
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["projekt"].queryset = Projekt.objects.filter(aktiv=True).order_by("name")
        self.fields["bindung"].queryset = ChatBindung.objects.filter(
            art__in=[ChatBindung.ART_PC, ChatBindung.ART_PROJEKT],
            widerrufen_am=None,
            gueltig_bis__gt=timezone.now(),
        ).order_by("name")
        ziele = (
            WerkbankProjektbindung.objects.filter(
                projekt__aktiv=True,
                instanz__aktiv=True,
                arbeitsort__aktiv=True,
                arbeitsort__bestaetigt=True,
            )
            .select_related("projekt", "instanz", "arbeitsort")
            .order_by("projekt__name", "instanz__name")
        )
        self.fields["ziele"].queryset = ziele
        self.fields["standard_ziel"].queryset = ziele
        self.fields["gueltig_bis"].initial = timezone.localtime() + timedelta(days=1)
        _stufen(self, standard_ziel="optional")

    def dienst_daten(self):
        d = self.cleaned_data
        return {
            "projekt": d["projekt"],
            "bindung": d["bindung"],
            "aktionen": d["aktionen"],
            "ziele": list(d["ziele"]),
            "standard_ziel": d["standard_ziel"],
            "gueltig_bis": d["gueltig_bis"],
            "budget": {
                "waehrung": d["waehrung"],
                "geld_mikro": int(d["geld"] * 1_000_000),
                "zeit_ms": d["minuten"] * 60_000,
                "aufrufe": d["aufrufe"],
            },
        }
