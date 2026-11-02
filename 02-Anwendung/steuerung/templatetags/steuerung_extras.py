from django import template

from ..models import Auftrag, Entscheidung

register = template.Library()

_KLASSEN = {
    Auftrag.ENTWURF: "",
    Auftrag.FREIGEGEBEN: "ok",
    Auftrag.IN_ARBEIT: "ok",
    Auftrag.ZUR_PRUEFUNG: "warn",
    Auftrag.NACHARBEIT: "warn",
    Auftrag.ABGENOMMEN: "fertig",
    Auftrag.BLOCKIERT: "bad",
    Auftrag.KLAERUNG_ERFORDERLICH: "bad",
    Auftrag.PAUSIERT: "",
    Auftrag.ABGEBROCHEN: "aus",
    Entscheidung.VORSCHLAG: "warn",
    Entscheidung.ENTSCHIEDEN: "ok",
    Entscheidung.ZURUECKGEZOGEN: "aus",
}
_WORTE = dict(Auftrag.ZUSTAENDE) | dict(Entscheidung.ZUSTAENDE)


@register.filter
def zustand(wert: str) -> str:
    """Zustand als Wort mit Klasse: <span class="status ok">Freigegeben</span>."""
    from django.utils.html import format_html

    return format_html(
        '<span class="status {}">{}</span>', _KLASSEN.get(wert, ""), _WORTE.get(wert, wert)
    )


@register.filter
def zeilenliste(text: str) -> list[str]:
    return [z.strip() for z in (text or "").splitlines() if z.strip()]


@register.simple_tag(takes_context=True)
def filterlink(context, **neu) -> str:
    """Aktuelle GET-Parameter mit Änderungen neu zusammensetzen."""
    params = context["request"].GET.copy()
    for k, v in neu.items():
        if v in ("", None):
            params.pop(k, None)
        else:
            params[k] = v
    return "?" + params.urlencode() if params else "?"
