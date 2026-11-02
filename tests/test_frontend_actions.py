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
