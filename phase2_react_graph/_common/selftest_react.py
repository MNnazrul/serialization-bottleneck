"""
Offline self-test for the ReAct harness — no API key, no network.

Drives `run_episode` with a scripted mock `call_turn` (each entry is one model
turn's text) and checks that the loop, the structural tools, the Finish
parsing, the step cap, and the reused Experiment-1 scoring all behave.

  python phase2_react_graph/_common/selftest_react.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import react_graph_harness as rh

LOG = logging.getLogger("selftest_react")
logging.basicConfig(level=logging.CRITICAL)

# Two small graphs used across the cases.
G_TRI = "\n".join(["GRAPH (n=5, m=5):", "0 1", "0 2", "1 2", "1 3", "3 4"])   # 1 triangle {0,1,2}
G_DEG = "\n".join(["GRAPH (n=4, m=3):", "0 1", "0 2", "2 3"])                 # deg(0) = 2


def _row(edge_list: str, **props):
    return {
        "object_id": "graph_selftest_000",
        "tier": "simple",
        "family": "erdos_renyi",
        "num_nodes": edge_list.count("\n"),
        "num_edges": 0,
        "edge_list": edge_list,
        "properties": props,
    }


def mock_call(turns: list[str]):
    """Return a `call_turn` that yields the scripted turns in order, then repeats
    the last one (so a runaway loop still terminates on the step cap)."""
    seq = list(turns)

    async def call_turn(client, model, messages, temperature, max_tokens,
                        extra_body=None, reasoning_effort=None):
        text = seq.pop(0) if seq else turns[-1]
        return text, {"prompt_tokens": 40, "completion_tokens": 12}, "stop"

    return call_turn


def episode(edge_list, prop, turns, *, props, max_steps=8):
    ep = asyncio.run(rh.run_episode(
        client=None, model="mock", row=_row(edge_list, **props), prop=prop,
        temperature=0.0, max_tokens=512, max_steps=max_steps,
        extra_body=None, reasoning_effort=None, think_suffix="",
        call_turn=mock_call(turns), log=LOG,
    ))
    rec = rh.make_react_record(_row(edge_list, **props), prop, ep, "mock", "mock", 0.0, 512)
    return ep, rec


CHECKS: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    CHECKS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def main() -> int:
    # --- unit: parse_edge_list / execute_tool -------------------------------
    n, m, adj = rh.parse_edge_list(G_TRI)
    check("parse_edge_list header n", n == 5)
    check("parse_edge_list header m", m == 5)
    check("adjacency symmetric", adj[0] == {1, 2} and adj[3] == {1, 4})
    check("Degree tool", rh.execute_tool("degree", "0", n, adj, m) == "Node 0 has degree 2.")
    check("Neighbors tool", rh.execute_tool("neighbors", "1", n, adj, m) == "Node 1 has neighbors: [0, 2, 3].")
    check("HasEdge present", "is present" in rh.execute_tool("hasedge", "1, 2", n, adj, m))
    check("HasEdge absent", "not present" in rh.execute_tool("hasedge", "2, 3", n, adj, m))
    check("EdgeCount tool", rh.execute_tool("edgecount", "", n, adj, m) == "The graph has 5 edges.")
    check("Nodes tool", rh.execute_tool("nodes", "", n, adj, m).startswith("Nodes: [0, 1, 2, 3, 4]"))
    check("out-of-range node", "not in the graph" in rh.execute_tool("degree", "99", n, adj, m))
    check("parse_action last action wins",
          rh.parse_action("Thought 1: x\nAction 1: Neighbors[0]\nsomething\nAction 2: Finish[1]") == ("finish", "1"))
    check("parse_action unknown -> None", rh.parse_action("Action 1: Frobnicate[0]")[0] is None)

    # --- (a) full episode: every tool then a correct Finish ----------------
    ep, rec = episode(
        G_TRI, "triangle_count",
        [
            "Thought 1: start at 0.\nAction 1: Neighbors[0]",
            "Thought 2: check the pair.\nAction 2: HasEdge[1, 2]",
            "Thought 3: one triangle, node 1 next.\nAction 3: Neighbors[1]",
            "Thought 4: test (2,3).\nAction 4: HasEdge[2, 3]",
            "Thought 5: total is 1.\nAction 5: Finish[1]",
        ],
        props={"triangle_count": 1},
    )
    check("(a) finished", ep["finished"] and rec["react_finished"])
    check("(a) parse_success", rec["parse_success"] is True)
    check("(a) correct", rec["correct"] is True)
    check("(a) steps counted", rec["react_steps"] == 5)
    check("(a) failure_type None", rec["failure_type"] is None)
    check("(a) observation fed back", "Node 0 has neighbors: [1, 2]." in ep["transcript"])
    check("(a) method tag", rec["method"] == "react")

    # --- (b) step cap hit with no Finish ---------------------------------
    ep, rec = episode(
        G_TRI, "triangle_count",
        ["Thought 1: keep looking.\nAction 1: Nodes[]"], props={"triangle_count": 1}, max_steps=6,
    )
    check("(b) not finished", ep["finished"] is False)
    check("(b) failure_type no_finish", rec["failure_type"] == "no_finish")
    check("(b) correct False", rec["correct"] is False)
    check("(b) steps == cap", rec["react_steps"] == 6)

    # --- (c) invalid action, then recovery ------------------------------
    ep, rec = episode(
        G_DEG, "degree_of_node_0",
        [
            "Thought 1: try this.\nAction 1: Frobnicate[0]",
            "Thought 2: ok, real one.\nAction 2: Degree[0]",
            "Thought 3: it is 2.\nAction 3: Finish[2]",
        ],
        props={"degree_of_node_0": 2},
    )
    check("(c) recovered & correct", rec["correct"] is True and ep["finished"])
    check("(c) invalid logged", ep["tool_calls"][0]["action"] == "invalid")
    check("(c) invalid observation", "Invalid action" in ep["tool_calls"][0]["observation"])

    # --- (d) boolean + scalar Finish go through the reused metrics -------
    _, rec_b = episode(G_TRI, "is_bipartite",
                       ["Thought 1: it has an odd cycle.\nAction 1: Finish[false]"],
                       props={"is_bipartite": False})
    check("(d) boolean Finish scored", rec_b["parse_success"] and rec_b["correct"] is True)

    _, rec_s = episode(G_TRI, "avg_clustering",
                       ["Thought 1: estimate.\nAction 1: Finish[0.7333]"],
                       props={"avg_clustering": 0.7333})
    check("(d) scalar Finish scored", rec_s["parse_success"] and rec_s["correct_1pct"] is True)
    check("(d) scalar rel-error field", rec_s["relative_error"] == 0.0)

    # --- (e) unparseable Finish -> parse_failure, not silently correct ---
    _, rec_x = episode(G_TRI, "triangle_count",
                       ["Thought 1: dunno.\nAction 1: Finish[a couple]"],
                       props={"triangle_count": 1})
    check("(e) unparseable Finish flagged", rec_x["parse_success"] is False)
    check("(e) unparseable Finish -> parse_failure", rec_x["failure_type"] == "parse_failure")
    check("(e) unparseable Finish incorrect", rec_x["correct"] is False)

    passed = sum(1 for _, ok in CHECKS if ok)
    print(f"\n{passed}/{len(CHECKS)} checks passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
