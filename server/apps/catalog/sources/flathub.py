"""Flathub: the AppStream catalogue, one ``<component>`` per app, with names,
summaries, descriptions, categories, licences and cached 128px icons."""
import gzip
import io
import xml.etree.ElementTree as ET

SOURCE = "flatpak"
PLATFORM = "linux"
URL = "https://dl.flathub.org/repo/appstream/x86_64/appstream.xml.gz"
ICON_BASE = "https://dl.flathub.org/repo/appstream/x86_64/icons/128x128/"
_APP_TYPES = {"desktop", "desktop-application", "console-application"}
_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def _text(el, tag):
    """The untranslated child text (no xml:lang)."""
    for child in el.findall(tag):
        if _LANG not in child.attrib:
            return " ".join("".join(child.itertext()).split())
    return ""


def _description(el):
    desc = next((d for d in el.findall("description") if _LANG not in d.attrib), None)
    if desc is None:
        return ""
    parts = []
    for node in desc:
        if _LANG in node.attrib:
            continue
        if node.tag == "p":
            parts.append(" ".join("".join(node.itertext()).split()))
        elif node.tag in ("ul", "ol"):
            parts += ["- " + " ".join("".join(li.itertext()).split())
                      for li in node.findall("li") if _LANG not in li.attrib]
    return "\n".join(p for p in parts if p)


def parse(data: bytes, release: str = ""):
    with gzip.open(io.BytesIO(data)) as stream:
        for _event, el in ET.iterparse(stream, events=("end",)):
            if el.tag != "component":
                continue
            if el.get("type") in _APP_TYPES:
                pid = (el.findtext("id") or "").strip()
                name = _text(el, "name")
                if pid and name:
                    icon = next((i.text for i in el.findall("icon")
                                 if i.get("type") == "cached" and i.get("width") == "128" and i.text), "")
                    rel = el.find("releases/release")
                    developer = _text(el, "developer_name") or _text(el, "developer/name")
                    yield {
                        "package_id": pid, "name": name, "summary": _text(el, "summary")[:300],
                        "description": _description(el)[:8000],
                        "version": (rel.get("version") if rel is not None else "") or "",
                        "publisher": developer[:200],
                        "homepage": next((u.text.strip() for u in el.findall("url")
                                          if u.get("type") == "homepage" and u.text), "")[:500],
                        "license": (el.findtext("project_license") or "")[:200],
                        "categories": [c.text for c in el.findall("categories/category") if c.text][:8],
                        "icon_url": ICON_BASE + icon if icon and "/" not in icon else "",
                    }
            el.clear()
