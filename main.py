import re
import subprocess
import time
from pathlib import Path

from tree_sitter import Language, Parser
import tree_sitter_python

from lsp_client import LspClient
from chat_ui import ChatUI

# The popup-window interface - replaces terminal print()/input() calls.
# A stand-in for what a real VS Code webview panel would eventually do.
ui = ChatUI()

# Folder this script lives in — so file lookups work no matter
# which directory the script is run from
HERE = Path(__file__).parent

# Make sure git is tracking this project - safe to run unconditionally,
# even if it's already a repo, since "git init" on an existing repo is a
# harmless no-op (it never touches existing history). No asking yet since
# we're a plain script right now, not an extension with a UI to ask through.
subprocess.run(["git", "init"], cwd=HERE, capture_output=True, text=True)

# The whole "project" we're indexing - every .py file found anywhere under
# this folder (including subfolders like plugins/), discovered dynamically
# instead of hand-listed. Excludes this tool's OWN scaffolding files
# (main.py, lsp_client.py) - those are the analyzer, not the project being
# analyzed.
EXCLUDED_FILES = {"main.py", "lsp_client.py"}
PROJECT_FILES = []
for f in HERE.rglob("*.py"):  # every .py file, including subfolders
    if f.name in EXCLUDED_FILES or ".git" in f.parts:
        continue  # skip our own tool's files, and anything inside .git
    PROJECT_FILES.append(f)

# ---- PASS 1: tree-sitter — find what exists (the "cities") --------------

PY_LANGUAGE = Language(tree_sitter_python.language())
parser = Parser(PY_LANGUAGE)


def find_enclosing_context(node):
    """Walk UP from a node to find what it's written inside, and grab that
    context's actual source code - in ONE pass, no separate re-search needed
    later. Always returns real code, never None, regardless of whether the
    call is inside a function or not.

    Returns (function_name, function_def_line, code):
    - If a function contains this call: its name, its line, and its full
      source text - this is also what lets multi-hop tracing continue
      ("who calls THIS function too?").
    - If nothing does (a module-level call, like everything in
      sample_code.py): function_name and function_def_line are None
      (there's genuinely nothing further to chain to - nobody "calls" a
      module-level line), but code is still the actual statement itself,
      never thrown away.
    """
    current = node
    while current.parent is not None:
        current = current.parent
        if current.type == "function_definition":
            name_node = current.child_by_field_name("name")
            if name_node is not None:
                return name_node.text.decode(), name_node.start_point[0], current.text.decode()

    # never found a function - fall back to the top-level statement this
    # call is actually part of, so we still return REAL code, not nothing
    current = node
    while current.parent is not None and current.parent.type != "module":
        current = current.parent
    return None, None, current.text.decode()


def find_calls(node, calls):
    """Walk the tree and collect every candidate reference:
    (name, line, column, enclosing_function).

    This includes real calls (parens) AND bare attribute reads (no parens -
    e.g. a @property). We no longer judge by "does it have parentheses" -
    every reference is collected here; Pass 2 (the LSP) decides which ones
    actually represent executing code, based on what they resolve to.
    """
    if node.type == "call":
        target = node.children[0]  # the thing being called

        if target.type == "identifier":
            # bare-name call, e.g. add(2, 3)
            name_node = target
        elif target.type == "attribute":
            # dotted call, e.g. calc.compute(...) or math.sqrt(...) —
            # the part we actually want is what's AFTER the dot
            name_node = target.child_by_field_name("attribute")
        else:
            name_node = None  # some other shape we're not handling yet

        if name_node is not None:
            calls.append((
                name_node.text.decode(),
                name_node.start_point[0],  # 0-based line (LSP wants this)
                name_node.start_point[1],  # 0-based column
                find_enclosing_context(node),
            ))

        # Recurse into everything EXCEPT re-visiting "target" itself as a
        # standalone node - otherwise a dotted call's target would ALSO
        # get picked up a moment later by the bare-attribute check below,
        # double-counting the exact same call.
        target_span = (target.start_byte, target.end_byte)
        for child in node.children:
            child_span = (child.start_byte, child.end_byte)
            if child_span == target_span and target.type == "attribute":
                for grandchild in target.children:
                    find_calls(grandchild, calls)
            else:
                find_calls(child, calls)
        return calls

    if node.type == "attribute":
        # a bare "object.name", no parentheses - might be a plain stored
        # value, or it might secretly be a property. We don't judge here -
        # just record it as a candidate; Pass 2 resolves and checks it.
        attr_name_node = node.child_by_field_name("attribute")
        if attr_name_node is not None:
            calls.append((
                attr_name_node.text.decode(),
                attr_name_node.start_point[0],
                attr_name_node.start_point[1],
                find_enclosing_context(node),
            ))

    for child in node.children:
        find_calls(child, calls)
    return calls


# all_calls holds (file, name, line, column, caller) — same as before, but
# now tagged with WHICH file each call site was found in, plus which
# function it lives inside (for chaining multi-hop impact later)
all_calls = []
for file in PROJECT_FILES:
    tree = parser.parse(file.read_bytes())
    for name, line, col, caller in find_calls(tree.root_node, []):
        all_calls.append((file, name, line, col, caller))

#print(f"PASS 1 - tree-sitter found {len(all_calls)} candidate references across {len(PROJECT_FILES)} files:")
for file, name, line, col, caller in all_calls:
    pass
    #print(f"  '{name}' referenced at {file.name}:{line + 1}")

# ---- PASS 2: LSP — resolve where each reference truly leads, and check ---
# ---- whether it's actually something that EXECUTES (Method/Function),  ---
# ---- as opposed to a plain stored value (Field/Variable) ----------------

lsp = LspClient(HERE)
for file in PROJECT_FILES:
    lsp.open_file(file)  # show the server every file before asking about any of them

# give the server a moment to actually run its analysis (pyflakes etc.)
# and push diagnostics in the background before we start querying it
time.sleep(1)

#print("\nPASS 2 - LSP resolved each reference and checked what kind of thing it is:")
edges = []
broken_calls = []  # references to names that genuinely don't exist ANYWHERE - real bugs
for file, name, line, col, caller in all_calls:
    definition = lsp.find_definition(file, line, col)
    if definition:
        def_file, def_line = definition
        kind = lsp.get_symbol_kind(def_file, def_line)
        if kind not in lsp.EXECUTABLE_KINDS:
            # a plain stored value (Field, Variable, ...) - not a real
            # call-like relationship, so we don't count it
            continue
        def_name = Path(def_file).name
        edges.append({"from": f"{file.name}:{line + 1}",
                      "to": f"{def_name}:{def_line}",
                      "name": name,
                      "caller": caller})  # (function_name, function_line, code) - name/line are None at module level, code never is
        #print(f"  '{name}' at {file.name}:{line + 1}  -->  defined in {def_name}, line {def_line} (kind={kind})")
    elif lsp.is_undefined_name(file, line, col):
        broken_calls.append({"file": file.name, "line": line + 1, "name": name})
        #print(f"  '{name}' at {file.name}:{line + 1}  -->  BROKEN: this name doesn't exist anywhere (likely a bug)")
    # else: unresolved AND not flagged as undefined - could be an external
    # symbol OR a plain local variable read; either way, nothing to record

# ---- THE cohesive output: one graph --------------------------------------

#print("\nTHE GRAPH (the one cohesive output both passes wrote into):")
for edge in edges:
    pass
    #print(f"  {edge['from']}  --calls '{edge['name']}'-->  {edge['to']}")

if broken_calls:
    #print("\nBROKEN REFERENCES (real bugs, distinct from external/built-in calls):")
    for broken in broken_calls:
        pass
        #print(f"  {broken['file']}:{broken['line']}  --calls '{broken['name']}'-->  NOTHING (undefined)")

# ---- THE FILTERING AGENT -------------------------------------------------- 
# Deterministic - NO LLM call here. Its whole job: given a changed symbol,
# use the graph we already built (for free) to find every call site that
# depends on it, directly or through any number of hops - so a future
# risk-analyzing agent only ever gets handed this small slice, never the
# whole codebase.


def find_blast_radius(edges, changed_location):
    """changed_location looks like 'sample_utils.py:1' - the definition
    site of whatever just got edited. Returns every edge (call site) that
    depends on it, walking backward through as many hops as it takes -
    each one carrying the actual affected code, no matter what it is.
    """
    to_check = [changed_location]
    seen_locations = set()
    affected = []

    while to_check:
        target = to_check.pop()
        if target in seen_locations:
            continue
        seen_locations.add(target)

        for edge in edges:
            if edge["to"] == target:
                caller_name, caller_line, code = edge["caller"]
                edge["code"] = code  # always real code now, never None
                if caller_name is not None:
                    # this call happens inside a real function - so someone
                    # could call THAT function too; keep the chain going
                    caller_file_name = edge["from"].split(":")[0]
                    to_check.append(f"{caller_file_name}:{caller_line + 1}")
                affected.append(edge)

    return affected


# ---- DETECT A REAL CHANGE, VIA GIT (no more hardcoded old/new snippets) ---

# If this is a brand new repo (no commits yet), make one baseline commit so
# there's something for "git diff" to compare against. Safe to do
# automatically here specifically BECAUSE there's no history yet to
# interfere with - this only fires the very first time.
has_commits = subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True, text=True
).returncode == 0

if not has_commits:
    subprocess.run(["git", "add", "-A"], cwd=HERE, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=HERE, capture_output=True, text=True)

changed_relative_paths = subprocess.run(
    ["git", "diff", "--name-only"], cwd=HERE, capture_output=True, text=True
).stdout.splitlines()

if not changed_relative_paths:
    ui.show_message("No uncommitted changes detected - edit a file, save it, and run again.")
    ui.run()
    raise SystemExit

def byte_to_point(source_bytes, byte_offset):
    """Convert a byte offset into the (row, col) point tree-sitter wants."""
    row = source_bytes.count(b"\n", 0, byte_offset)
    last_newline = source_bytes.rfind(b"\n", 0, byte_offset)
    col = byte_offset - (last_newline + 1)
    return (row, col)


from openai import OpenAI

client = OpenAI()  # created once, reused for every changed file below

ui.show_message(f"{len(changed_relative_paths)} changed file(s) detected: {', '.join(changed_relative_paths)}")

# Analyze EVERY changed file, not just the first one - each gets its own
# full pass: incremental re-parse, blast radius, risk agent, reporter agent.
for changed_relative_path in changed_relative_paths:
    CHANGED_FILE = HERE / changed_relative_path

    old_source = subprocess.run(
        ["git", "show", f"HEAD:{changed_relative_path}"], cwd=HERE, capture_output=True
    ).stdout  # the last COMMITTED version, as bytes
    new_source = CHANGED_FILE.read_bytes()  # the current, UNCOMMITTED version on disk

    old_tree = parser.parse(old_source)  # the tree we already had

    # ---- INCREMENTAL RE-PARSE ---------------------------------------------
    # Find the exact byte range that changed, by comparing old vs new
    # directly - same idea as a diff, just done ourselves: find how much
    # matches at the start (the common prefix) and at the end (the common
    # suffix); whatever's left in the middle is what actually changed.
    prefix_len = 0
    while (prefix_len < min(len(old_source), len(new_source))
           and old_source[prefix_len] == new_source[prefix_len]):
        prefix_len += 1

    max_suffix = min(len(old_source), len(new_source)) - prefix_len
    suffix_len = 0
    while (suffix_len < max_suffix
           and old_source[-1 - suffix_len] == new_source[-1 - suffix_len]):
        suffix_len += 1

    start_byte = prefix_len
    old_end_byte = len(old_source) - suffix_len
    new_end_byte = len(new_source) - suffix_len

    # Find WHICH function this change actually happened inside - MUST
    # happen BEFORE old_tree.edit() below, since querying a tree AFTER
    # .edit() returns broken/inconsistent results.
    changed_node = old_tree.root_node.descendant_for_byte_range(start_byte, start_byte)
    function_name, function_line, old_change = find_enclosing_context(changed_node)

    old_tree.edit(
        start_byte=start_byte,
        old_end_byte=old_end_byte,
        new_end_byte=new_end_byte,
        start_point=byte_to_point(old_source, start_byte),
        old_end_point=byte_to_point(old_source, old_end_byte),
        new_end_point=byte_to_point(new_source, new_end_byte),
    )

    new_tree = parser.parse(new_source, old_tree)  # the actual incremental re-parse

    changed_ranges_text = "\n".join(
        f"  bytes {r.start_byte}-{r.end_byte} (out of {len(new_source)} total bytes in the file)"
        for r in old_tree.changed_ranges(new_tree)
    )
    ui.show_message(
        f"=== INCREMENTAL RE-PARSE (real change detected in {changed_relative_path}) ===\n"
        f"Changed range (proof this was incremental, not a full re-parse):\n{changed_ranges_text}"
    )

    if function_name is None:
        ui.show_message(f"{changed_relative_path}: the change wasn't inside any function - "
                         f"nothing to trace in the call graph. Skipping to the next changed file.")
        continue

    CHANGED_LOCATION = f"{CHANGED_FILE.name}:{function_line + 1}"
    ui.show_message(f"Detected change is inside '{function_name}', at {CHANGED_LOCATION}")

    # Same lookup, but in the NEW tree, to get the function's new source text
    new_changed_node = new_tree.root_node.descendant_for_byte_range(start_byte, start_byte)
    _, _, new_change = find_enclosing_context(new_changed_node)

    affected = find_blast_radius(edges, CHANGED_LOCATION)
    affected_detail = "\n".join(
        f"{edge['from']} --calls '{edge['name']}'--> {edge['to']}\n  code: {edge['code']}"
        for edge in affected
    )
    ui.show_message(f"=== affected: {len(affected)} entries, each with its own code ===\n{affected_detail}")

    # ---- THE NEW GRAPH: resolve the edited code through the SAME LSP -----
    new_source_text = new_source.decode()
    lsp.update_file(CHANGED_FILE, new_source_text)

    new_calls = find_calls(new_tree.root_node, [])

    new_edges = []
    for name, line, col, caller in new_calls:
        definition = lsp.find_definition(CHANGED_FILE, line, col)
        if definition:
            def_file, def_line = definition
            kind = lsp.get_symbol_kind(def_file, def_line)
            if kind not in lsp.EXECUTABLE_KINDS:
                continue
            def_name = Path(def_file).name
            new_edges.append({"from": f"{CHANGED_FILE.name}:{line + 1}",
                               "to": f"{def_name}:{def_line}",
                               "name": name,
                               "caller": caller})

    new_graph_detail = "\n".join(
        f"  {edge['from']}  --calls '{edge['name']}'-->  {edge['to']}"
        for edge in new_edges
    )
    ui.show_message(f"=== NEW graph slice for the edited {CHANGED_FILE.name} ===\n{new_graph_detail}")

    # ---- THE RISK / REPORTER AGENT ----------------------------------------
    # Two paid LLM calls per changed file. Deliberately fed only the small,
    # pre-filtered graph slices built above - never the raw codebase.

    affected_lines = []
    for edge in affected:
        if edge["to"] == CHANGED_LOCATION:
            directness = "DIRECT - this call goes straight to the changed code"
        else:
            directness = "INDIRECT - depends on something that depends on the change"
        affected_lines.append(
            f"- [{directness}]\n"
            f"  {edge['from']} calls '{edge['name']}', which is defined at {edge['to']}\n"
            f"  code: {edge['code']}"
        )
    affected_summary = "\n".join(affected_lines)

    new_graph_summary = "\n".join(
        f"- {edge['from']} calls '{edge['name']}' (defined at {edge['to']})"
        for edge in new_edges
    ) or "(no calls found in the edited file itself)"

    prompt = f"""You are a risk-analysis agent for a code change impact tool.

THE CHANGE:
  Location: {CHANGED_LOCATION}
  Before: {old_change}
  After:  {new_change}

CODE AFFECTED BY THIS CHANGE (the "before" graph). Every entry below is
ALREADY LABELED DIRECT or INDIRECT for you - trust these labels exactly,
don't re-derive them yourself. DIRECT means this call site goes straight
to the changed code (regardless of what name/alias it's called under).
INDIRECT means it depends on something that itself depends on the change,
however many hops away:
{affected_summary}

THE EDITED FILE'S OWN NEW RELATIONSHIPS (the "after" graph - what the
edited file itself calls, post-change):
{new_graph_summary}

Analyze how VOLATILE this change is to the rest of the codebase - how
likely it is to break other pieces of code - and estimate how many errors
it could cause. For each affected call site, use its given DIRECT/INDIRECT
label and explain why it would or wouldn't break. End with a clear
verdict: should this change be made or not?
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    risk_analysis = response.choices[0].message.content

    ui.show_message(
        f"=== RISK AGENT REPORT ({changed_relative_path}) ===\n"
        f"(model actually used, confirmed by the API response: {response.model})\n"
        f"{risk_analysis}"
    )

    lines_summary = "\n".join(
        f"- {edge['from']} [{'DIRECT' if edge['to'] == CHANGED_LOCATION else 'INDIRECT'}]: "
        f"calls '{edge['name']}', defined at {edge['to']}"
        for edge in affected
    )

    reporter_prompt = f"""You are a reporter agent for a code change impact tool.
Your job is to take a risk analyst's findings and turn them into a final,
clear report for a developer deciding whether to make a change.

THE CHANGE:
  Location: {CHANGED_LOCATION}
  Before: {old_change}
  After:  {new_change}

THE RISK ANALYST'S FINDINGS:
{risk_analysis}

GROUND-TRUTH LIST OF AFFECTED LOCATIONS (use these EXACT file names and
line numbers - do not paraphrase, round, or approximate them):
{lines_summary}

Write the final report with exactly these sections:
1. A short, plain-English summary of the change.
2. An explicit list of EVERY affected location above - the exact file and
   line number, whether it is DIRECT or INDIRECT, and specifically HOW it
   will be affected (what error, what breaks, why).
3. A final verdict: make the change or not, and a one-sentence reason why.
"""

    reporter_response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": reporter_prompt}],
    )
    report_text = reporter_response.choices[0].message.content

    ui.show_message(
        f"=== FINAL REPORT ({changed_relative_path}) ===\n"
        f"(model actually used, confirmed by the API response: {reporter_response.model})\n"
        f"{report_text}"
    )

    # ---- STILL WANT TO MAKE THIS CHANGE? --------------------------------
    # If yes: a SEPARATE fix agent goes through and fixes every OTHER
    # affected piece of code (not just the one already-edited function).
    # If no: offer to revert the edit entirely, undoing what was changed.
    answer = ui.ask_yes_no(f"Do you still want to make this change to {CHANGED_LOCATION}?")

    if not answer:
        revert = ui.ask_yes_no(f"Revert {changed_relative_path} back to its last committed version?")
        if revert:
            subprocess.run(["git", "checkout", "--", changed_relative_path], cwd=HERE)
            ui.show_message(f"Reverted - {changed_relative_path} is back to its last committed state.")
        else:
            ui.show_message("Left as-is - no changes made.")
        continue  # move on to the next changed file, nothing left to do here

    # ---- THE FIX AGENT: update every OTHER affected piece of code --------
    # A genuinely separate agent/call. De-duplicate first, since the same
    # code can appear multiple times in `affected` (e.g. two calls on the
    # same line) - we only want to ask for, and apply, ONE fix per unique
    # piece of code, not once per edge.
    unique_fixes_needed = {}
    for edge in affected:
        location = edge["from"]
        if location not in unique_fixes_needed:
            location_file_name = location.split(":")[0]
            location_file_path = next(f for f in PROJECT_FILES if f.name == location_file_name)
            unique_fixes_needed[location] = {"file": location_file_path, "code": edge["code"]}

    fix_targets_summary = "\n\n".join(
        f"LOCATION: {location}\nCODE:\n{info['code']}"
        for location, info in unique_fixes_needed.items()
    )

    fix_prompt = f"""You are a fix agent for a code change impact tool. The
developer has decided to go ahead with a risky change, and every location
below needs to be updated so it still works correctly with it.

THE CHANGE:
  Location: {CHANGED_LOCATION}
  Before: {old_change}
  After:  {new_change}

LOCATIONS THAT NEED FIXING, WITH THEIR CURRENT CODE:
{fix_targets_summary}

For EVERY location above that genuinely needs to change to keep working,
output a block in EXACTLY this format (one block per location, nothing
else between the markers, copy the LOCATION text exactly as shown):
FIX_START <location>
<the complete corrected version of that code>
FIX_END
Skip any location that doesn't actually need a change.
"""

    fix_response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": fix_prompt}],
    )
    fix_text = fix_response.choices[0].message.content

    print(f"\n=== FIX AGENT ({changed_relative_path}) ===")
    print(f"(model actually used, confirmed by the API response: {fix_response.model})")
    print(fix_text)

    fix_blocks = re.findall(r"FIX_START (.*?)\n(.*?)FIX_END", fix_text, re.DOTALL)
    if not fix_blocks:
        print("No fixes were suggested.")
    for location, fixed_code in fix_blocks:
        location = location.strip()
        fixed_code = fixed_code.strip()
        if location not in unique_fixes_needed:
            print(f"Fix agent mentioned an unrecognized location '{location}' - skipping.")
            continue
        info = unique_fixes_needed[location]
        current_text = info["file"].read_text()
        if info["code"] not in current_text:
            print(f"Couldn't safely locate the original code for {location} - skipping.")
            continue
        answer = input(f"Apply suggested fix to {location} in {info['file'].name}? (yes/no): ").strip().lower()
        if answer == "yes":
            updated_text = current_text.replace(info["code"], fixed_code, 1)
            info["file"].write_text(updated_text)
            print(f"Applied - {info['file'].name} has been updated.")
        else:
            print(f"Skipped {location} - no changes made.")

lsp.stop()

# ---- tree-sitter + LSP combined output (the OLD graph), printed at the end -

print("\n=== tree-sitter + LSP combined output (the graph) ===")
for edge in edges:
    print(f"  {edge['from']}  --calls '{edge['name']}'-->  {edge['to']}")



