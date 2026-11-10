"""The system diagram as an SVG for the web page: the model canvas.layered draws, in the map's look, with the ids the
page lights things by."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from system_diff import architecture, pictures
from system_diff.compare import compare

NS = "{http://www.w3.org/2000/svg}"


def ids(svg: str) -> dict:
    root = ET.fromstring(svg)
    return {el.get("data-id"): el for el in root.iter() if el.get("data-id")}


def texts(el) -> str:
    return " ".join(t.text or "" for t in el.iter(NS + "text"))


MODEL = {
    "columns": [["actors", "Who uses it"], ["ui", "What people use"], ["empty", "Nothing here"],
                ["core", "Inside the system"], ["outside", "Data and outside"]],
    "nodes": [
        {"id": "a:people", "col": "actors", "type": "actor", "title": "People", "sub": "person"},
        {"id": "web", "col": "ui", "type": "component", "title": "Web <app> & co", "sub": "2 files", "status": "added",
         "lines": [{"text": "GET /x", "status": "added"}, {"text": "b"}, {"text": "c"}]},
        {"id": "g:Backend", "col": "core", "type": "group", "title": "Backend", "sub": "2 components",
         "status": "changed", "agent": True,
         "items": [{"id": "core", "title": "Core", "status": "changed", "agent": True},
                   {"id": "store", "title": "Store", "owner": 2}]},
        {"id": "x:pg", "col": "outside", "type": "store", "title": "PostgreSQL", "sub": "1 tables", "owner": 3},
        {"id": "x:mail", "col": "outside", "type": "host", "title": "Mail", "sub": "used by 1"},
    ],
    "edges": [
        {"from": "a:people", "to": "web", "kind": "call", "label": "browser"},
        {"from": "web", "to": "g:Backend", "to_item": "core", "kind": "call", "label": "HTTPS", "status": "added"},
        {"from": "g:Backend", "to": "x:pg", "from_item": "store", "kind": "data", "label": "SQL"},
        {"from": "g:Backend", "to": "x:mail", "kind": "call", "label": "SMTP", "status": "removed"},
        {"from": "x:pg", "to": "web", "kind": "call"},                 # backwards
        {"from": "x:pg", "to": "x:mail", "kind": "network"},           # within a column
        {"from": "web", "to": "nowhere"},                              # dangling: left out
    ],
}


def test_a_hand_made_model_draws_every_box_row_and_flow_with_its_id():
    svg = pictures.architecture_svg(MODEL, label="Shop & co")
    found = ids(svg)
    for n in MODEL["nodes"]:
        assert f"b:{n['id']}" in found
    assert {"b:core", "b:store"} <= set(found)
    for e in MODEL["edges"][:-1]:
        assert f"e:{e['from']}>{e['to']}" in found
    assert "e:web>nowhere" not in found
    root = ET.fromstring(svg)
    assert root.get("viewBox") and root.get("width") is None and root.get("height") is None
    assert root.get("aria-label") == "Shop & co"
    assert "NOTHING HERE" not in svg                                    # an empty column takes no room
    assert "Web <app> & co" in texts(found["b:web"])                     # escaped, and read back whole
    for word in ("browser", "HTTPS", "SQL", "SMTP"):
        assert word in texts(found["e:" + next(f"{e['from']}>{e['to']}" for e in MODEL["edges"]
                                               if e.get("label") == word)])


def test_status_owner_and_agent_show_in_classes_marks_and_colours():
    svg = pictures.architecture_svg(MODEL)
    found = ids(svg)
    web = found["b:web"]
    assert "s-added" in web.get("class") and texts(web).startswith("+ ")
    assert "o2" in found["b:store"].get("class") and "o3" in found["b:x:pg"].get("class")
    assert pictures.OWNERS[2][0] in svg and pictures.OWNERS[3][0] in svg      # the agents' colours, light
    assert pictures.OWNERS[2][1] in svg                                       # and dark
    assert "agent" in found["b:core"].get("class") and "● " in texts(found["b:core"])
    assert "agent" in found["b:g:Backend"].get("class")
    removed = found["e:g:Backend>x:mail"]
    assert "s-removed" in removed.get("class")
    assert removed.find(NS + "path").get("stroke-dasharray")                  # dashed, with or without the CSS
    assert found["e:x:pg>x:mail"].find(NS + "path").get("stroke-dasharray")  # the network, dashed too
    assert not found["e:a:people>web"].find(NS + "path").get("stroke-dasharray")
    for cls in (".lit", ".picked", ".dim", "prefers-color-scheme: dark"):
        assert cls in svg


def test_a_flow_meets_a_container_on_its_components_row():
    svg = pictures.architecture_svg(MODEL)
    found = ids(svg)
    path = found["e:web>g:Backend"].find(NS + "path").get("d")
    end_y = float(path.split(",")[-1])
    row = found["b:core"].find(NS + "rect")
    group_y = float(found["b:g:Backend"].get("transform").split(",")[1].rstrip(")"))
    top = group_y + float(row.get("y"))
    assert top <= end_y <= top + float(row.get("height"))


def test_the_system_and_a_container_from_a_real_project(repo):
    c = compare(repo, "HEAD~1", "HEAD")
    for comp in c.cfg["components"]:
        if comp["id"] in ("core", "store"):
            comp["group"] = "Backend"
    m = architecture.system(c, agent="store")
    svg = pictures.architecture_svg(m)
    found = ids(svg)
    for n in m["nodes"]:
        assert "b:" + n["id"] in found
        for it in n.get("items") or []:
            assert "b:" + it["id"] in found
    for e in m["edges"]:
        assert f"e:{e['from']}>{e['to']}" in found
        if e.get("label"):
            assert e["label"] in texts(found[f"e:{e['from']}>{e['to']}"])
    assert any(n.get("status") == "added" for n in m["nodes"]) and "s-added" in svg
    inner = architecture.container(c, "core")
    found = ids(pictures.architecture_svg(inner, label=inner["title"]))
    assert {"b:" + n["id"] for n in inner["nodes"]} <= set(found)
    assert {f"e:{e['from']}>{e['to']}" for e in inner["edges"]} <= set(found)
