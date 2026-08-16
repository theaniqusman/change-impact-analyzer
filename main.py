import os
import sys
import traceback
from pathlib import Path

# If this script isn't already running under the project's own virtual
# environment (.venv - it has tree_sitter/openai installed, and a modern,
# working Tk for the popup window), relaunch itself using that venv's
# Python instead - so you can just run "python3 main.py" (or hit VS Code's
# Run button) normally, without remembering to type ".venv/bin/python3"
# every time.
#
# IMPORTANT: this checks sys.prefix, NOT sys.executable. .venv/bin/python3
# is a symlink chain that ultimately points at the same real interpreter
# file as a bare "python3" call - comparing resolved executable paths
# can't tell those two apart, since they resolve to the identical file.
# sys.prefix is different: it only equals the .venv folder when the venv
# was actually activated as the "front door" (which is what actually makes
# its installed packages importable) - so it's the only reliable way to
# tell "am I really running inside .venv" from "am I just the same
# underlying Python binary, invoked directly."
_venv_dir = (Path(__file__).parent / ".venv").resolve()
if Path(sys.prefix).resolve() != _venv_dir:
    _venv_python = _venv_dir / "bin" / "python3"
    if _venv_python.exists():
        os.execv(str(_venv_python), [str(_venv_python), __file__] + sys.argv[1:])

import re
import subprocess
import threading
import time

from tree_sitter import Language, Parser
import tree_sitter_python

from lsp_client import LspClient
from chat_ui import ChatUI

from openai import OpenAI

# The popup-window interface - replaces terminal print()/input() calls.
# A stand-in for what a real VS Code webview panel would eventually do.
ui = ChatUI()

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


def byte_to_point(source_bytes, byte_offset):
    """Convert a byte offset into the (row, col) point tree-sitter wants."""
    row = source_bytes.count(b"\n", 0, byte_offset)
    last_newline = source_bytes.rfind(b"\n", 0, byte_offset)
    col = byte_offset - (last_newline + 1)
    return (row, col)


def run_pipeline():
    """The whole analysis pipeline - runs on a BACKGROUND thread (started
    at the bottom of this file), never on the main thread. main.py used to
    run all of this directly on the main thread, which is also the thread
    tkinter's window lives on - so every LSP call, every OpenAI API call,
    every parse, froze the window solid for its whole duration (no clicks,
    no typing, nothing). Moving all the actual work here, onto its own
    thread, is what lets the window's event loop (started via ui.run() on
    the main thread, below) stay responsive the entire time this runs.
    """
    # Folder this script lives in — so file lookups work no matter
    # which directory the script is run from
    HERE = Path(__file__).parent

    ui.show_message("Starting up...")
    ui.show_message("Remember to save your file (Cmd+S) - I can only see a change once it's actually saved.")

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
    EXCLUDED_DIRS = {".git", ".venv"}
    PROJECT_FILES = []
    for f in HERE.rglob("*.py"):  # every .py file, including subfolders
        if f.name in EXCLUDED_FILES or EXCLUDED_DIRS & set(f.parts):
            continue  # skip our own tool's files, and anything inside .git/.venv
        PROJECT_FILES.append(f)

    ui.show_message(f"Parsing {len(PROJECT_FILES)} project file(s) with tree-sitter...")

    # all_calls holds (file, name, line, column, caller) — same as before, but
    # now tagged with WHICH file each call site was found in, plus which
    # function it lives inside (for chaining multi-hop impact later)
    all_calls = []
    for file in PROJECT_FILES:
        tree = parser.parse(file.read_bytes())
        for name, line, col, caller in find_calls(tree.root_node, []):
            all_calls.append((file, name, line, col, caller))

    # ---- PASS 2: LSP — resolve where each reference truly leads, and check ---
    # ---- whether it's actually something that EXECUTES (Method/Function),  ---
    # ---- as opposed to a plain stored value (Field/Variable) ----------------

    ui.show_message("Starting the language server (pylsp)...")
    lsp = LspClient(HERE)
    for file in PROJECT_FILES:
        lsp.open_file(file)  # show the server every file before asking about any of them

    # give the server a moment to actually run its analysis (pyflakes etc.)
    # and push diagnostics in the background before we start querying it
    time.sleep(1)

    ui.show_message(f"Resolving {len(all_calls)} call site(s) via the language server...")
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
        elif lsp.is_undefined_name(file, line, col):
            broken_calls.append({"file": file.name, "line": line + 1, "name": name})
        # else: unresolved AND not flagged as undefined - could be an external
        # symbol OR a plain local variable read; either way, nothing to record

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
        lsp.stop()
        return

    client = OpenAI()  # created once, reused for every changed file below

    ui.show_message(f"{len(changed_relative_paths)} changed file(s) detected: {', '.join(changed_relative_paths)}")

    def analyze_change(changed_relative_path):
        """One full pass over ONE changed file: incremental re-parse, blast
        radius, risk agent, reporter agent, and (if confirmed) the fix
        agent. Pulled out into its own function so it can be called both
        for the files detected automatically at startup below, AND later,
        on demand, whenever the user types a fresh request into the popup
        (see the always-listening loop at the bottom of run_pipeline).
        """
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

        ui.show_message(f"Looking at the change you made in {changed_relative_path}...")

        if function_name is None:
            ui.show_message(f"That change isn't inside any function in {changed_relative_path}, "
                             f"so there's nothing else in the codebase that could be affected by it.")
            return

        ui.show_message(f"Found it - this is inside your '{function_name}' function.")

        CHANGED_LOCATION = f"{CHANGED_FILE.name}:{function_line + 1}"

        # Same lookup, but in the NEW tree, to get the function's new source text
        new_changed_node = new_tree.root_node.descendant_for_byte_range(start_byte, start_byte)
        _, _, new_change = find_enclosing_context(new_changed_node)

        affected = find_blast_radius(edges, CHANGED_LOCATION)
        if affected:
            ui.show_message(f"Found {len(affected)} other place(s) in your code that use this - checking each one now.")

            MAX_SHOWN = 10
            shown_lines = []
            for edge in affected[:MAX_SHOWN]:
                directness = "directly affected" if edge["to"] == CHANGED_LOCATION else "indirectly affected"
                shown_lines.append(f"[{directness}] {edge['from']}\n{edge['code']}")
            affected_preview = "\n\n".join(shown_lines)
            if len(affected) > MAX_SHOWN:
                affected_preview += f"\n\n...and {len(affected) - MAX_SHOWN} more piece(s) of code affected."
            ui.show_message(affected_preview)
        else:
            ui.show_message("Nothing else in your code calls this, so this change is self-contained.")

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

        new_graph_summary = "\n".join(
            f"- {edge['from']} calls '{edge['name']}' (defined at {edge['to']})"
            for edge in new_edges
        ) or "(no calls found in the edited file itself)"

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

        prompt = f"""You're analyzing how risky a code change is, so another
step can turn your notes into a short, friendly, plain-English explanation
for a developer later - so write like you're thinking it through in normal
sentences, not a formal technical report.

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

Talk through how risky this change is to the rest of the codebase - how
likely it is to break other things, and roughly how many places could be
affected. For each affected call site, use its given DIRECT/INDIRECT label
and explain, in plain language, why it would or wouldn't break.

Keep it brief - a handful of sentences, not a long writeup. Be definitive:
say what WILL happen, not what "might" or "could" happen - you have the
actual call graph, you're not guessing. End with your honest take: should
this change be made or not?
"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            timeout=30,
        )
        risk_analysis = response.choices[0].message.content

        lines_summary = "\n".join(
            f"- {edge['from']} [{'DIRECT' if edge['to'] == CHANGED_LOCATION else 'INDIRECT'}]: "
            f"calls '{edge['name']}', defined at {edge['to']}"
            for edge in affected
        )

        reporter_prompt = f"""You are explaining a code change's impact to a
developer, in the style of a helpful assistant having a normal conversation
with them - like ChatGPT would - NOT a rigid technical report.

THE CHANGE:
  Location: {CHANGED_LOCATION}
  Before: {old_change}
  After:  {new_change}

THE RISK ANALYST'S FINDINGS:
{risk_analysis}

GROUND-TRUTH LIST OF AFFECTED LOCATIONS (accurate - use this to make sure
you don't miss or misstate anything, but don't just repeat it verbatim):
{lines_summary}

Write your answer as normal, flowing paragraphs - the way a person talks,
not a numbered/bulleted technical report, and not bold section labels like
"**Summary:**" or "**Affected:**". Refer to code by what it actually does
(e.g. "the `add` function in sample_utils.py", or plain-English descriptions
like "the code that adds two numbers together in your calculator") instead
of raw notation like "sample_utils.py:8" - the reader shouldn't need to
decode file:line syntax to understand you.

Keep the whole thing short - a few sentences is enough, not a long
writeup. Be definitive, not hedgy: don't say things "might" or "could"
break - you already know the actual call graph, so say plainly what WILL
happen if this change goes through. Cover: what the change actually does,
which other parts of the code would be affected and specifically how
(what would break, if anything), and end with a clear, one-line
recommendation on whether to go ahead with it.
"""

        reporter_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": reporter_prompt}],
            timeout=30,
        )
        report_text = reporter_response.choices[0].message.content

        ui.show_message(f"=== Here's what I found ({changed_relative_path}) ===\n{report_text}")

        # ---- STILL WANT TO MAKE THIS CHANGE? --------------------------------
        # If yes: a SEPARATE fix agent goes through and fixes every OTHER
        # affected piece of code (not just the one already-edited function).
        # If no: offer to revert the edit entirely, undoing what was changed.
        location_description = (
            f"'{function_name}'" if function_name else changed_relative_path
        )
        answer = ui.ask_yes_no(f"So - do you still want to go ahead with this change to {location_description}?")

        if not answer:
            revert = ui.ask_yes_no(
                f"No problem - want me to undo the change and put {changed_relative_path} "
                f"back the way it was?"
            )
            if revert:
                subprocess.run(["git", "checkout", "--", changed_relative_path], cwd=HERE)
                ui.show_message(f"Done - {changed_relative_path} is back to how it was before.")
            else:
                ui.show_message("Okay, leaving it as-is - no changes made.")
            return  # nothing left to do for this change

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
            timeout=30,
        )
        fix_text = fix_response.choices[0].message.content

        # FIX_START/FIX_END is an internal format for us to parse reliably -
        # not something to show the user as-is, so don't dump fix_text
        # itself into the popup; just narrate what's about to happen.
        fix_blocks = re.findall(r"FIX_START (.*?)\n(.*?)FIX_END", fix_text, re.DOTALL)
        if not fix_blocks:
            ui.show_message("I checked the other places that use this code, and nothing else actually needs to change.")
        else:
            ui.show_message(f"I found {len(fix_blocks)} other spot(s) that would need updating to keep working with this change.")
        for location, fixed_code in fix_blocks:
            location = location.strip()
            fixed_code = fixed_code.strip()
            if location not in unique_fixes_needed:
                continue
            info = unique_fixes_needed[location]
            current_text = info["file"].read_text()
            if info["code"] not in current_text:
                ui.show_message(f"I couldn't safely re-locate the original code in {info['file'].name}, so I'm skipping that one to be safe.")
                continue
            answer = ui.ask_yes_no(f"Want me to update {info['file'].name} so it still works with this change?")
            if answer:
                updated_text = current_text.replace(info["code"], fixed_code, 1)
                info["file"].write_text(updated_text)
                ui.show_message(f"Done - {info['file'].name} has been updated.")
            else:
                ui.show_message(f"Okay, leaving {info['file'].name} as-is.")

    # ---- ALWAYS LISTENING: handle whatever the user types next, on demand ---
    # Don't auto-run the full analysis on every detected file up front -
    # that's what used to make you wait through a whole batch (each file
    # costing 2-3 sequential OpenAI calls) before you could ask about
    # anything. Instead, each time around: check if the user ALREADY told
    # us which change they mean (typed something unprompted) - if so, use
    # that directly. If they didn't, and there's actually something
    # uncommitted to ask about, actively ask which one instead of just
    # sitting there silently. Either way, hand the answer to a small
    # interpreting agent that matches it against whatever's CURRENTLY
    # uncommitted (re-checked fresh via git each time), then runs
    # analyze_change() on whichever file it matches.
    while True:
        current_changed = subprocess.run(
            ["git", "diff", "--name-only"], cwd=HERE, capture_output=True, text=True
        ).stdout.splitlines()

        message = ui.try_get_message()
        if message is None:
            if not current_changed:
                # nothing uncommitted yet to ask about - just wait quietly
                # until the user has an edit to tell us about
                message = ui.wait_for_message()
                current_changed = subprocess.run(
                    ["git", "diff", "--name-only"], cwd=HERE, capture_output=True, text=True
                ).stdout.splitlines()
            else:
                message = ui.ask_text(
                    "Which uncommitted change are you asking about? "
                    "(describe the file, or what you changed)"
                )

        if not current_changed:
            ui.show_message("No uncommitted changes right now to match that against.")
            continue

        file_list_text = "\n".join(f"- {path}" for path in current_changed)
        interpret_prompt = f"""A developer typed a message describing a change
they want analyzed, in their own words.

CURRENTLY UNCOMMITTED FILES:
{file_list_text}

WHAT THE DEVELOPER SAID:
{message}

Reply with ONLY the exact file path from the list above that best matches
what they said - nothing else, no explanation, no extra punctuation.
"""
        interpret_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": interpret_prompt}],
        )
        matched_path = interpret_response.choices[0].message.content.strip()

        if matched_path not in current_changed:
            ui.show_message(
                f"Couldn't confidently match \"{message}\" to one of the "
                f"currently uncommitted files (got back: '{matched_path}')."
            )
            continue

        ui.show_message(f"Matched your message to '{matched_path}' - analyzing it now.")
        analyze_change(matched_path)


def run_pipeline_safe():
    """Wraps run_pipeline() so a crash is actually visible - both printed
    to the terminal (in case the popup itself isn't reachable for some
    reason) AND shown in the popup window - instead of silently killing
    the background thread with no explanation anywhere, which is what
    happened before this wrapper existed.
    """
    try:
        run_pipeline()
    except Exception:
        error_text = traceback.format_exc()
        print("\n=== PIPELINE CRASHED ===\n" + error_text, file=sys.stderr)
        ui.show_message("=== SOMETHING WENT WRONG - the background thread crashed ===\n" + error_text)


# The actual analysis runs on its own thread, so the window (mainloop,
# below) is never blocked by it and stays responsive - clickable, typeable -
# for the whole time this pipeline is working.
threading.Thread(target=run_pipeline_safe, daemon=True).start()
ui.run()
