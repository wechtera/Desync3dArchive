"""Every handler named in markup must be registered, because a name in a string breaks silently."""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
NAMED = re.compile(r'data-(?:click|change|input)=["\']([A-Za-z_][A-Za-z0-9_]*)["\']')
REGISTERED = re.compile(r"registerActions\(\{(.*?)\}\);", re.DOTALL)


def sources():
    # actions.js only names these in its own comment.
    scripts = (path for path in sorted((WEB / "static" / "js").rglob("*.js")) if path.name != "actions.js")
    return [WEB / "index.html", *scripts]


def registered_names():
    names = set()
    for path in sources():
        for block in REGISTERED.findall(path.read_text(encoding="utf-8")):
            names.update(re.findall(r"(?:^|[{,\s])([A-Za-z_][A-Za-z0-9_]*)\s*(?=[,:])", f"{block.rstrip()},"))
    return names


def test_every_named_handler_is_registered():
    known = registered_names()
    missing = {
        f"{path.name}: {name}"
        for path in sources()
        for name in NAMED.findall(path.read_text(encoding="utf-8"))
        if name not in known
    }
    assert not missing, sorted(missing)


def test_the_dispatcher_only_reads_the_map():
    source = (WEB / "static" / "js" / "actions.js").read_text(encoding="utf-8")
    assert "ACTIONS.get(" in source
    assert "window[" not in source


def scripts():
    return sorted((WEB / "static" / "js").rglob("*.js"))


def test_every_action_called_from_a_view_is_registered():
    known = registered_names()
    called = {
        name for path in scripts() if path.name != "actions.js"
        for name in re.findall(r"\bactions\.([A-Za-z_][A-Za-z0-9_]*)\b(?!['\"])", path.read_text(encoding="utf-8"))
    }
    assert called <= known, sorted(called - known)


def test_views_add_nothing_to_window():
    allowed = {
        "location", "scrollTo", "scrollY", "open", "addEventListener", "removeEventListener", "confirm", "alert",
        "getComputedStyle", "setTimeout", "innerWidth", "innerHeight", "devicePixelRatio", "getSelection",
    }
    used = {
        name for path in scripts() for name in re.findall(r"\bwindow\.([A-Za-z_][A-Za-z0-9_]*)", path.read_text(encoding="utf-8"))
    }
    assert used <= allowed, sorted(used - allowed)


def test_app_requests_go_through_api_js():
    pages = {"api.js", "auth_callback.js", "login.js"}
    direct = [path.name for path in scripts() if path.name not in pages and re.search(r"(?<![A-Za-z.])fetch\(", path.read_text(encoding="utf-8"))]
    assert not direct

