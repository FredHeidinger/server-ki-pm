from django import template

from ..models import Auftrag, Entscheidung

register = template.Library()

_KLASSEN = {
    Auftrag.ENTWURF: "",
    Auftrag.FREIGEGEBEN: "ok",
    Auftrag.IN_ARBEIT: "ok",
    Auftrag.ERLEDIGT: "fertig",
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


@register.simple_tag
def datei_link(zeile: str, arbeitsort) -> str:
    """„pfad@commit“ als Links auf Datei und Commit im Repository des Arbeitsorts, sonst Text."""
    from django.utils.html import escape, format_html

    repo = (getattr(arbeitsort, "repository", "") or "").rstrip("/")
    if "@" not in zeile or not repo:
        return format_html("<code>{}</code>", zeile)
    pfad, _, commit = zeile.rpartition("@")
    commit = commit.strip()
    if not commit or any(z in commit for z in "/ \\"):
        return format_html("<code>{}</code>", zeile)
    return format_html(
        '<code><a href="{}/blob/{}/{}">{}</a>@<a href="{}/commit/{}">{}</a></code>',
        repo,
        commit,
        escape(pfad.strip().replace("\\", "/")),
        pfad,
        repo,
        commit,
        commit[:12],
    )


@register.filter
def wert(daten: dict, feld: str) -> str:
    """Feld eines gespeicherten Objektstands lesbar machen (Listen mit Komma)."""
    w = (daten or {}).get(feld, "")
    if isinstance(w, list):
        return (
            ", ".join(
                str(x) if not isinstance(x, dict) else " | ".join(str(v) for v in x.values())
                for x in w
            )
            or "–"
        )
    if isinstance(w, dict):
        return ", ".join(f"{k}: {v}" for k, v in w.items()) or "–"
    if w in (None, ""):
        return "–"
    return str(w)
