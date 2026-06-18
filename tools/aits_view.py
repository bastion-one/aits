#!/usr/bin/env python3
"""Render the AITS ledger as one self-contained HTML page (stdlib only).

Fetches sessions, lineage nodes, DUT content, observation times, and frontier
verification verdicts from a running ledger, and writes a single static HTML
file with everything embedded -- no server, no JS dependencies, shareable.
The page opens with an interactive canvas of the DAG (pan, zoom, click a node
for its content; sessions are swim-lanes, dashed blue edges are handoffs),
followed by the full per-node detail cards.

    python3 tools/aits_view.py                  # all sessions -> aits-view.html, opened
    python3 tools/aits_view.py <session_uuid>   # just one session
    python3 tools/aits_view.py --url http://other:8000 --out /tmp/x.html --no-open
"""

import argparse
import html
import json
import subprocess
import sys
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

STYLE = """
body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, sans-serif; margin: 0;
       background: #f5f4f0; color: #222; }
header { background: #1c1b18; color: #f5f4f0; padding: 14px 28px; }
header h1 { font-size: 17px; margin: 0; }
header .sub { color: #b8b4a8; font-size: 12px; }
main { max-width: 980px; margin: 0 auto; padding: 18px 28px 80px; }
.toc { background: #fff; border: 1px solid #ddd8cc; border-radius: 8px; padding: 10px 16px; }
.toc a { color: #1a5fb4; text-decoration: none; }
.session { margin-top: 28px; }
.session > h2 { font-size: 15px; border-bottom: 2px solid #1c1b18; padding-bottom: 6px; }
.session .meta { color: #777; font-size: 12px; margin-bottom: 12px; }
.badge { display: inline-block; border-radius: 10px; padding: 1px 9px; font-size: 11px;
         font-weight: 600; vertical-align: 1px; }
.ok   { background: #d3e8d3; color: #1a5e1a; }
.bad  { background: #f3d2d2; color: #8b1a1a; }
.tag  { background: #e8e4d8; color: #555; }
.merge{ background: #dbe7f3; color: #1a4a7a; }
.node { background: #fff; border: 1px solid #ddd8cc; border-left: 4px solid #b8b4a8;
        border-radius: 6px; margin: 10px 0; padding: 10px 14px; }
.node.has-merge { border-left-color: #1a5fb4; }
.node h3 { font-size: 13.5px; margin: 0 0 2px; }
.node .when { color: #999; font-size: 11.5px; font-weight: 400; float: right; }
.node .ids { color: #999; font-size: 11px; word-break: break-all; }
.node .ids a { color: #1a5fb4; text-decoration: none; }
details { margin: 7px 0 0; }
summary { cursor: pointer; font-size: 12px; color: #555; font-weight: 600; }
pre { background: #faf9f5; border: 1px solid #eee8da; border-radius: 4px; padding: 9px 11px;
      white-space: pre-wrap; word-break: break-word; font-size: 12px; margin: 6px 0 0;
      max-height: 420px; overflow: auto; }
#graph-wrap { display: flex; gap: 12px; margin-top: 16px; height: 540px; position: relative; }
#graph-wrap.full { position: fixed; inset: 0; height: auto; margin: 0; padding: 12px;
                   background: #f5f4f0; z-index: 1000; }
#graph { flex: 1; background: #fff; border: 1px solid #ddd8cc; border-radius: 8px;
         cursor: grab; min-width: 0; }
#graph.panning { cursor: grabbing; }
#expand { position: absolute; top: 10px; right: 342px; z-index: 1; font-size: 12px;
          background: #fff; border: 1px solid #ddd8cc; border-radius: 6px; padding: 4px 10px;
          cursor: pointer; color: #555; }
#expand:hover { background: #faf9f5; }
#graph-wrap.full #expand { top: 22px; right: 354px; }
#panel { width: 320px; background: #fff; border: 1px solid #ddd8cc; border-radius: 8px;
         padding: 12px 14px; overflow-y: auto; font-size: 12px; }
#panel h4 { margin: 0 0 4px; font-size: 13px; }
#panel .field { color: #999; font-size: 11px; margin-top: 8px; text-transform: uppercase;
                letter-spacing: 0.04em; }
#panel pre { max-height: 160px; }
#panel a { color: #1a5fb4; }
.hint { color: #999; font-size: 11px; margin-top: 4px; }
"""

GRAPH_JS = """
const G = window.GRAPH;
const canvas = document.getElementById('graph');
const panel = document.getElementById('panel');
const ctx = canvas.getContext('2d');
const dpr = window.devicePixelRatio || 1;
const NW = 150, NH = 42;
let scale = 1, ox = 20, oy = 20, selected = null;
let dragging = false, moved = false, lastX = 0, lastY = 0;

function resize() {
  const r = canvas.getBoundingClientRect();
  canvas.width = r.width * dpr; canvas.height = r.height * dpr;
  draw();
}

function fit() {
  const r = canvas.getBoundingClientRect();
  let maxX = 0, maxY = 0;
  for (const n of G.nodes) { maxX = Math.max(maxX, n.x + NW); maxY = Math.max(maxY, n.y + NH); }
  scale = Math.min(1, (r.width - 40) / (maxX + 40), (r.height - 40) / (maxY + 40));
  ox = 20; oy = 20;
  draw();
}

function edgePath(a, b) {
  const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2;
  const mx = (x1 + x2) / 2;
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.bezierCurveTo(mx, y1, mx, y2, x2, y2);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(x2, y2);
  ctx.lineTo(x2 - 7, y2 - 4);
  ctx.lineTo(x2 - 7, y2 + 4);
  ctx.fill();
}

function draw() {
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.setTransform(dpr * scale, 0, 0, dpr * scale, dpr * ox, dpr * oy);
  ctx.font = '11px -apple-system, sans-serif';
  for (const lane of G.lanes) {
    ctx.fillStyle = '#b8b4a8';
    ctx.fillText(lane.label, 4, lane.y - 10);
    ctx.strokeStyle = '#eee8da'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, lane.y - 24); ctx.lineTo(lane.maxX + 60, lane.y - 24); ctx.stroke();
  }
  const byId = G.byId;
  for (const e of G.edges) {
    const a = byId[e.from], b = byId[e.to];
    if (!a || !b) continue;
    if (e.kind === 'derived') {
      ctx.strokeStyle = ctx.fillStyle = '#1a5fb4'; ctx.lineWidth = 1.6; ctx.setLineDash([6, 4]);
    } else {
      ctx.strokeStyle = ctx.fillStyle = '#b8b4a8'; ctx.lineWidth = 1.4; ctx.setLineDash([]);
    }
    edgePath(b, a);  // draw from parent (b) to child (a)
  }
  ctx.setLineDash([]);
  for (const n of G.nodes) {
    ctx.fillStyle = n.id === selected ? '#fdf6e3' : '#fff';
    ctx.strokeStyle = n.id === selected ? '#b58900' : (n.merge ? '#1a5fb4' : '#b8b4a8');
    ctx.lineWidth = n.id === selected ? 2.5 : (n.merge ? 2 : 1.2);
    ctx.beginPath();
    ctx.roundRect(n.x, n.y, NW, NH, 7);
    ctx.fill(); ctx.stroke();
    ctx.fillStyle = '#222';
    ctx.fillText(n.title, n.x + 8, n.y + 17);
    ctx.fillStyle = '#999';
    ctx.fillText(n.sub, n.x + 8, n.y + 32);
  }
}

function toWorld(evt) {
  const r = canvas.getBoundingClientRect();
  return [(evt.clientX - r.left - ox) / scale, (evt.clientY - r.top - oy) / scale];
}

canvas.addEventListener('mousedown', e => { dragging = true; moved = false; lastX = e.clientX; lastY = e.clientY; canvas.classList.add('panning'); });
window.addEventListener('mousemove', e => {
  if (!dragging) return;
  if (Math.abs(e.clientX - lastX) + Math.abs(e.clientY - lastY) > 2) moved = true;
  ox += e.clientX - lastX; oy += e.clientY - lastY;
  lastX = e.clientX; lastY = e.clientY;
  draw();
});
window.addEventListener('mouseup', e => {
  canvas.classList.remove('panning');
  if (dragging && !moved) {
    const [wx, wy] = toWorld(e);
    selected = null;
    for (const n of G.nodes) {
      if (wx >= n.x && wx <= n.x + NW && wy >= n.y && wy <= n.y + NH) { selected = n.id; break; }
    }
    showPanel(); draw();
  }
  dragging = false;
});
canvas.addEventListener('wheel', e => {
  e.preventDefault();
  const r = canvas.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
  const ns = Math.min(4, Math.max(0.08, scale * factor));
  ox = mx - (mx - ox) * (ns / scale);
  oy = my - (my - oy) * (ns / scale);
  scale = ns;
  draw();
}, { passive: false });

function field(label, value) {
  const f = document.createElement('div'); f.className = 'field'; f.textContent = label;
  panel.appendChild(f);
  const v = document.createElement('div'); v.textContent = value;
  panel.appendChild(v);
}

function showPanel() {
  panel.innerHTML = '';
  const n = G.byId[selected];
  if (!n) { panel.innerHTML = '<div class="hint">Click a node to inspect it. Drag to pan, scroll to zoom. Dashed blue edges are cross-session handoffs.</div>'; return; }
  const h = document.createElement('h4'); h.textContent = n.title; panel.appendChild(h);
  field('actor', n.actor);
  field('occurred / recorded', n.occurred + ' / ' + (n.recorded || '?'));
  field('node cid', n.id);
  if (n.span) field('span', n.span);
  if (n.agent) field('agent / config', n.agent + ' / ' + n.config.slice(0, 12));
  if (n.input) {
    const f = document.createElement('div'); f.className = 'field'; f.textContent = 'input';
    panel.appendChild(f);
    const p = document.createElement('pre'); p.textContent = n.input; panel.appendChild(p);
  }
  if (n.output) {
    const f = document.createElement('div'); f.className = 'field'; f.textContent = 'output';
    panel.appendChild(f);
    const p = document.createElement('pre'); p.textContent = n.output; panel.appendChild(p);
  }
  const link = document.createElement('a');
  link.href = '#' + n.id; link.textContent = 'jump to detail card';
  panel.appendChild(document.createElement('div')).appendChild(link);
}

const wrap = document.getElementById('graph-wrap');
const expandBtn = document.getElementById('expand');
function setFull(on) {
  wrap.classList.toggle('full', on);
  expandBtn.textContent = on ? '\\u2715 exit full screen (esc)' : '\\u26f6 full screen';
  resize(); fit();
}
expandBtn.addEventListener('click', () => setFull(!wrap.classList.contains('full')));
window.addEventListener('keydown', e => { if (e.key === 'Escape') setFull(false); });
canvas.addEventListener('dblclick', e => {
  const [wx, wy] = toWorld(e);
  const hit = G.nodes.some(n => wx >= n.x && wx <= n.x + NW && wy >= n.y && wy <= n.y + NH);
  if (!hit) setFull(!wrap.classList.contains('full'));
});

G.byId = {};
for (const n of G.nodes) G.byId[n.id] = n;
window.addEventListener('resize', resize);
resize(); fit(); showPanel();
"""


def fetch(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return json.loads(resp.read())


def when(iso: str | None) -> str:
    if not iso:
        return "?"
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%H:%M:%S")


def esc(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
    return html.escape(text)


def load(base: str, only_session: str | None) -> dict:
    sessions = fetch(base, "/lineage/?limit=500")
    if only_session:
        sessions = [s for s in sessions if s["session_uuid"] == only_session]
        if not sessions:
            sys.exit(f"session not found in ledger: {only_session}")
    agents = {a["cid"]: a for a in fetch(base, "/agents/")}
    recorded = {}
    for entry in fetch(base, "/commits/?limit=100000"):
        recorded.setdefault(entry["cid"], entry["recorded_at"])
    duts, verdicts = {}, {}
    for summary in sessions:
        detail = fetch(base, f"/lineage/{summary['session_uuid']}/")
        summary["detail"] = detail["nodes"]
        for node in detail["nodes"]:
            if node["dut"] and node["dut"] not in duts:
                duts[node["dut"]] = fetch(base, f"/duts/{node['dut']}/")
        for cid in detail["frontier"]:
            verdicts[cid] = fetch(base, f"/verify/{cid}/")
        summary["frontier_cids"] = detail["frontier"]
    return {
        "sessions": sessions,
        "agents": agents,
        "recorded": recorded,
        "duts": duts,
        "verdicts": verdicts,
    }


def build_graph(data: dict) -> dict:
    """Layered DAG layout, computed here so the page needs no JS libraries.

    x = causal depth (longest ``prev`` path within the session), y = lane;
    sessions stack vertically as swim-lanes. Cross-session ``derived_from``
    edges connect lanes.
    """
    cell_w, cell_h, lane_gap = 180, 64, 70
    nodes, edges, lanes = [], [], []
    y_base = 40
    known = {n["cid"] for s in data["sessions"] for n in s["detail"]}
    for s in data["sessions"]:
        by_cid = {n["cid"]: n for n in s["detail"]}
        depth: dict[str, int] = {}

        def node_depth(cid: str, by_cid=by_cid, depth=depth) -> int:
            if cid not in depth:
                parents = [p for p in by_cid[cid]["prev"] if p in by_cid]
                depth[cid] = 1 + max((node_depth(p) for p in parents), default=-1)
            return depth[cid]

        lanes_at_depth: dict[int, int] = {}
        max_lane = 0
        for node in s["detail"]:
            d = node_depth(node["cid"])
            lane = lanes_at_depth.get(d, 0)
            lanes_at_depth[d] = lane + 1
            max_lane = max(max_lane, lane)
            dut = data["duts"].get(node["dut"] or "")
            agent = data["agents"].get(dut["agent_cid"], {}) if dut else {}
            nodes.append(
                {
                    "id": node["cid"],
                    "x": 40 + d * cell_w,
                    "y": y_base + lane * cell_h,
                    "title": node["transformation"][:24],
                    "sub": f'{node["actor_id"][:16]} · {when(node["occurred_at"])}',
                    "actor": node["actor_id"],
                    "occurred": when(node["occurred_at"]),
                    "recorded": when(data["recorded"].get(node["cid"])),
                    "merge": bool(node["derived_from"]),
                    "span": dut["span_id"] if dut else "",
                    "agent": agent.get("name", ""),
                    "config": dut["config_cid"] if dut else "",
                    "input": dut["input_context"][:4000] if dut else "",
                    "output": dut["agent_output"][:4000] if dut else "",
                }
            )
            for p in node["prev"]:
                edges.append({"from": node["cid"], "to": p, "kind": "prev"})
            for d_cid in node["derived_from"]:
                if d_cid in known:
                    edges.append({"from": node["cid"], "to": d_cid, "kind": "derived"})
        max_depth = max(depth.values(), default=0)
        lanes.append(
            {
                "label": f"session {s['session_uuid']}",
                "y": y_base,
                "maxX": 40 + (max_depth + 1) * cell_w,
            }
        )
        y_base += (max_lane + 1) * cell_h + lane_gap
    return {"nodes": nodes, "edges": edges, "lanes": lanes}


def render_node(node: dict, data: dict) -> list[str]:
    out = [f'<div class="node{" has-merge" if node["derived_from"] else ""}" id="{node["cid"]}">']
    badges = ""
    if node["derived_from"]:
        badges += f' <span class="badge merge">merge ×{len(node["derived_from"])}</span>'
    out.append(
        f'<h3>{esc(node["transformation"])}{badges}'
        f'<span class="when">{esc(node["actor_id"])} · {when(node["occurred_at"])} '
        f'(recorded {when(data["recorded"].get(node["cid"]))})</span></h3>'
    )
    links = [f'node <code>{node["cid"][:12]}</code>']
    links += [f'prev <a href="#{p}">{p[:12]}</a>' for p in node["prev"]]
    links += [f'derived_from <a href="#{d}">{d[:12]}</a>' for d in node["derived_from"]]
    out.append(f'<div class="ids">{" · ".join(links)}</div>')
    dut = data["duts"].get(node["dut"] or "")
    if dut:
        agent = data["agents"].get(dut["agent_cid"], {})
        out.append(
            f'<div class="ids">span <code>{esc(dut["span_id"])}</code> · '
            f'agent {esc(agent.get("name", dut["agent_cid"][:12]))} · '
            f'config <code>{dut["config_cid"][:12]}</code>'
            + (f' · artifacts ×{len(dut["artifact_cids"])}' if dut["artifact_cids"] else "")
            + "</div>"
        )
        out.append(
            f"<details><summary>input</summary><pre>{esc(dut['input_context'])}</pre></details>"
        )
        out.append(
            f"<details open><summary>output</summary><pre>{esc(dut['agent_output'])}</pre></details>"
        )
    out.append("</div>")
    return out


def render(data: dict, base: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>AITS ledger</title>",
        f"<style>{STYLE}</style></head><body>",
        f"<header><h1>AITS ledger</h1><div class='sub'>{base} · snapshot {now} · "
        f"{len(data['sessions'])} session(s)</div></header><main>",
        "<div id='graph-wrap'><canvas id='graph'></canvas>"
        "<button id='expand'>⛶ full screen</button><div id='panel'></div></div>",
        "<div class='hint'>drag to pan · scroll to zoom · click a node · "
        "double-click empty space (or the button) for full screen, esc to exit · "
        "dashed blue edges are cross-session handoffs · blue-bordered nodes are merges</div>",
        "<div class='toc' style='margin-top:16px'><strong>Sessions</strong> (most recent first)<br>",
    ]
    for s in data["sessions"]:
        ok = all(data["verdicts"][c]["valid"] for c in s["frontier_cids"])
        out.append(
            f'<a href="#s-{s["session_uuid"]}">{s["session_uuid"]}</a> '
            f'— {s["nodes"]} nodes, last {when(s["last_occurred_at"])} '
            f'<span class="badge {"ok" if ok else "bad"}">{"verified" if ok else "TAMPERED"}</span><br>'
        )
    out.append("</div>")
    for s in data["sessions"]:
        ok = all(data["verdicts"][c]["valid"] for c in s["frontier_cids"])
        verdict = (
            '<span class="badge ok">verified</span>'
            if ok
            else '<span class="badge bad">VERIFICATION FAILED</span>'
        )
        out.append(f'<div class="session" id="s-{s["session_uuid"]}">')
        out.append(f'<h2>session {s["session_uuid"]} {verdict}</h2>')
        out.append(
            f'<div class="meta">{s["nodes"]} nodes · {when(s["started_at"])} → '
            f'{when(s["last_occurred_at"])} · frontier {", ".join(c[:12] for c in s["frontier_cids"])}'
        )
        if not ok:
            failures = {
                data["verdicts"][c]["property_violated"]
                for c in s["frontier_cids"]
                if not data["verdicts"][c]["valid"]
            }
            out.append(f' · <strong>{", ".join(sorted(failures))}</strong>')
        out.append("</div>")
        for node in s["detail"]:
            out.extend(render_node(node, data))
        out.append("</div>")
    graph_json = json.dumps(build_graph(data)).replace("</", "<\\/")
    out.append(f"</main><script>window.GRAPH = {graph_json};{GRAPH_JS}</script></body></html>")
    return "".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", nargs="?", help="render only this session uuid")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="ledger base URL")
    parser.add_argument("--out", default="aits-view.html", help="output HTML path")
    parser.add_argument("--no-open", action="store_true", help="do not open in a browser")
    args = parser.parse_args()

    data = load(args.url, args.session)
    out = Path(args.out)
    out.write_text(render(data, args.url))
    total = sum(s["nodes"] for s in data["sessions"])
    print(f"wrote {out} ({len(data['sessions'])} sessions, {total} nodes)")
    if not args.no_open:
        if sys.platform == "darwin":
            subprocess.run(["open", str(out)], check=False)
        else:
            webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
