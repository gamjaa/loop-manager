import hashlib
import subprocess
from loop_manager_internal.maintenance import maintenance_file_inventory
from loop_manager_internal.maintenance import maintenance_file_review as review


def root_results_for(repository, inventory):
    tree = subprocess.check_output([
        "git", "-C", str(repository), "ls-tree", inventory.revision,
    ]).decode("utf-8", "backslashreplace")
    results = []
    for item in maintenance_file_inventory.root_placement_inventory(inventory):
        line = next(line for line in tree.splitlines()
                    if line.split("\t", 1)[1].split("/", 1)[0] == item.path)
        results.append({
            "path": item.path,
            "category": "tool_required" if item.path in {".agents", "docs", "scripts", "AGENTS.md"} else "uncertain",
            "references": [{"path": ".", "excerpt": line}],
            "user_path": "No user entry contract is established in this fixture.",
            "ownership": "No accountable owner is established in this fixture.",
            "destination": None,
            "reason": "The tracked tree confirms presence; usage and ownership remain uncertain.",
        })
    return tuple(results)

def result_for(source, excerpt, content):
    chunk_reviews = []
    for index, start in enumerate(range(0, len(content), review.MAX_EMBEDDED_SOURCE_BYTES)):
        end = min(start + review.MAX_EMBEDDED_SOURCE_BYTES, len(content))
        rendered = content[start:end].decode("utf-8", "backslashreplace")
        middle_start = max(0, len(rendered) // 2 - 128)
        chunk_reviews.append({
            "index": index, "start": start, "end": end,
            "chunk_hash": hashlib.sha256(content[start:end]).hexdigest(),
            "opening_excerpt": rendered[:16],
            "middle_excerpt": rendered[middle_start:middle_start + 16],
            "closing_excerpt": rendered[-16:],
            "review": "This segment begins with " + rendered[:16] + " and continues its responsibility.",
        })
    return {
        "path": source.path,
        "content_hash": source.content_hash,
        "mode": source.mode,
        "source_revision": source.revision,
        "evidence": {
            "excerpts": [excerpt],
            "readability": "Names show the workflow clearly.",
            "responsibility_and_integration": "The function owns one workflow and no external calls.",
            "error_recovery": "Errors pass to the caller without hidden state.",
            "test_boundary": "The function can be tested through its return value.",
            "duplication": "No duplicate implementation was found in this file.",
            "change_context": "The narrow function keeps unrelated code out of later work.",
            "chunk_reviews": chunk_reviews,
        },
        "code_map": {
            "responsibility": "Calculate the input value for callers.",
            "entry_points": ["calculate"],
            "public_symbols": ["calculate"],
            "direct_references": [],
            "related_tests": ["tests/test_calc.py"],
        },
        "pending_references": [],
        "similar_responsibilities": [],
    }
