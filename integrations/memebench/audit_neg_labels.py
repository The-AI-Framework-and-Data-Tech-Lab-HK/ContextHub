"""Independent audit of the auto-assigned bipolar labels in the human-check
samples. NOT a re-run of _label_edge (that would be self-confirming) — instead it
applies SECOND-OPINION signals that the original value-mapping labeler did not
use, and flags only edges where the second opinion disagrees:

  1. semantic_offtopic negatives -> suspect FALSE-NEGATIVE if the dependent text
     actually contains a cascade-target's AFTER value or the entity NAME tokens
     (the original mapping used only BEFORE values, so an entity restated with
     its new value / alias would slip into the negatives).
  2. positive edges -> suspect FALSE-POSITIVE if the dependent maps to a target
     ONLY via a value string that is also a common English word / very short
     (spurious substring hit), i.e. weak evidence.
  3. predeclaration negatives -> suspect MISCLASS if, despite the leading "if",
     the text also asserts a present value (contains " is " / " are " before the
     "if"), which would make it a current-value fact that should stale.

Everything not flagged is presumed correctly labeled. The flagged set is what a
human actually needs to eyeball; the error rate = confirmed-wrong / total-sample.

Usage:
    CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.audit_neg_labels \
        --samples integrations/memebench/runs/neg_edge_sample_hop1.json \
                  integrations/memebench/runs/neg_edge_sample_hop2.json
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter

from integrations.memebench.loader import (
    DEFAULT_DATA_PATH,
    extract_cascade_cases,
    load_episodes,
)


def _norm(s) -> str:
    return " ".join(str(s or "").casefold().split())


# value strings this short/common are unreliable substring evidence
_WEAK_VALUE = re.compile(r"^\w{1,3}$")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", nargs="+", required=True)
    ap.add_argument("--data", default=str(DEFAULT_DATA_PATH))
    args = ap.parse_args()

    episodes = load_episodes(args.data)
    cases = {c.episode_id: c for c in extract_cascade_cases(episodes, hop=None)}

    edges = []
    for path in args.samples:
        edges.extend(json.load(open(path))["edges"])

    flags = []  # (reason, edge)
    for e in edges:
        case = cases.get(e["episode_id"])
        if case is None:
            flags.append(("no_case_found", e))
            continue
        targets = {t.target for t in case.edges}
        dtext = _norm(e["dependent_text"])

        # (1) semantic negative that might really be a target restated by after/name
        if e["polarity"] == "negative" and e["neg_class"] == "semantic_offtopic":
            hits = []
            for name in targets:
                ent = case.entities.get(name)
                after = _norm(ent.after) if ent and ent.after not in (None, "deleted") else ""
                # entity-name tokens, e.g. "regular_appointment" -> "appointment"
                name_tok = name.split("_")[-1]
                if after and len(after) > 3 and after in dtext:
                    hits.append(f"{name}=after('{ent.after}')")
                elif name_tok and len(name_tok) > 3 and name_tok in dtext:
                    hits.append(f"{name}~name('{name_tok}')")
            if hits:
                flags.append((f"SUSPECT_FALSE_NEG via {', '.join(hits)}", e))

        # (2) positive edge resting on weak (very short) value evidence
        if e["polarity"] == "positive":
            strong = False
            for name in (e["dependent_entities"] or []):
                ent = case.entities.get(name)
                bv = _norm(ent.before) if ent else ""
                if bv and not _WEAK_VALUE.match(bv):
                    strong = True
            if not strong and e["dependent_entities"]:
                flags.append(("SUSPECT_FALSE_POS weak_value_evidence", e))
            elif not e["dependent_entities"]:
                flags.append(("SUSPECT_FALSE_POS no_entity_mapped", e))

        # (3) predeclaration that also asserts a present value
        if e["neg_class"] == "predeclaration":
            head = dtext.split(" if ")[0] if " if " in dtext else ""
            # a leading clause before the "if" that states a value ("... is X, if...")
            if re.search(r"\b(is|are|take|go to|drive)\b", head) and len(head) > 12:
                flags.append(("SUSPECT_MISCLASS predecl_has_present_value", e))

    print("=" * 64)
    print(f"Audited {len(edges)} sampled edges")
    print("=" * 64)
    reasons = Counter(r.split(" ")[0] for r, _ in flags)
    print(f"flagged for human eyeball: {len(flags)}")
    for k, v in reasons.items():
        print(f"  {k}: {v}")
    print(f"presumed-correct (unflagged): {len(edges) - len(flags)}")
    print()
    for reason, e in flags:
        print(f"--- [{reason}]")
        print(f"    ep={e['episode_id']} auto={e['polarity']}/{e['neg_class'] or 'pos'}")
        print(f"    下游: {e['dependent_text'][:140]}")
        print(f"    上游: {e['dependency_text'][:110]}")
        print(f"    mapped_ents={e['dependent_entities']}")
        print()


if __name__ == "__main__":
    main()
