"""A minimal LSP client.

Starts the pylsp language server as a background process and talks to it.
The Language Server Protocol is just JSON messages sent back and forth,
each one prefixed with a "Content-Length" header (like a tiny web request).
"""

import json
import select
import subprocess
import sys
from pathlib import Path

READ_TIMEOUT_SECONDS = 30


class LspClient:
    def __init__(self, project_folder):
        self.project_folder = Path(project_folder)
        self._next_id = 0
        # The server pushes these on its own (unasked), one per file:
        # a list of things it thinks are actually wrong with that file,
        # e.g. "undefined name 'compute_perimeter'". Keyed by file URI.
        self.diagnostics = {}
        # documentSymbol on a big external file (e.g. builtins.pyi has
        # ~900 symbols) is expensive - cache the raw result per file so we
        # only ever fetch it once, no matter how many things resolve there.
        self._document_symbols_cache = {}
        self._file_versions = {}  # uri -> version number, required by didChange
        # Start the language server as a separate process. We talk to it
        # through its stdin/stdout pipes.
        #
        # IMPORTANT: use sys.executable (the exact interpreter currently
        # running this script), not the bare string "python3" - a plain
        # "python3" depends on PATH lookup, which can silently resolve to a
        # DIFFERENT Python than the one main.py is actually running under
        # (one that might not have pylsp installed at all). If that
        # happened, this subprocess would die instantly on startup, and
        # since stderr is thrown away below, nothing would ever show an
        # error - the code waiting to hear back from it would just hang
        # forever with no explanation. sys.executable guarantees this is
        # always the same Python that has pylsp installed and working.
        self.server = subprocess.Popen(
            [sys.executable, "-m", "pylsp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        # Handshake: tell the server who we are and where the project is
        self._request("initialize", {
            "processId": None,
            "rootUri": self.project_folder.as_uri(),
            "capabilities": {},
        })
        self._notify("initialized", {})

    # ---- the two ways of talking to the server -------------------------

    def _send(self, message):
        """Send one JSON message, framed with a Content-Length header."""
        body = json.dumps(message).encode()
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        self.server.stdin.write(header + body)
        self.server.stdin.flush()

    def _wait_readable(self):
        """Block until the server's stdout actually has something waiting to
        be read, or raise TimeoutError if it doesn't within
        READ_TIMEOUT_SECONDS. Without this, a hung/crashed pylsp process
        would make readline() block forever with no error and no
        explanation - exactly the "silent freeze" this is meant to prevent.
        """
        ready, _, _ = select.select([self.server.stdout], [], [], READ_TIMEOUT_SECONDS)
        if not ready:
            raise TimeoutError(
                f"pylsp didn't respond within {READ_TIMEOUT_SECONDS} seconds - "
                f"it may have crashed or hung."
            )

    def _read_message(self):
        """Read one JSON message from the server (header, blank line, body).

        Only check readiness ONCE, before the very first read - not before
        every individual readline()/read() call. Python's buffered stream
        can pull an entire multi-line response into its own internal buffer
        in a single underlying read, so a later readline()/read() can return
        data instantly with nothing NEW arriving at the OS level - a fresh
        select() at that point can wrongly report "nothing ready" even
        though the data is already sitting in the buffer, causing a false
        hang. One check up front is enough to catch a genuinely dead/silent
        server; it doesn't need repeating once we know bytes are flowing.
        """
        self._wait_readable()
        length = 0
        while True:
            line = self.server.stdout.readline().decode()
            if line.startswith("Content-Length:"):
                length = int(line.split(":")[1].strip())
            if line == "\r\n":  # blank line = headers finished
                break
        return json.loads(self.server.stdout.read(length).decode())

    def _notify(self, method, params):
        """Fire-and-forget message (no answer expected)."""
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method, params):
        """Ask a question and wait for the matching answer."""
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id,
                    "method": method, "params": params})
        # The server also sends unrelated chatter in between (diagnostics
        # etc.) - handle it instead of throwing it away, then keep
        # reading until the message with OUR id comes back.
        while True:
            message = self._read_message()
            if message.get("id") == self._next_id:
                return message.get("result")
            self._handle_unsolicited(message)

    def _handle_unsolicited(self, message):
        """Handle a message we didn't ask for (the server sent it on its own)."""
        if message.get("method") == "textDocument/publishDiagnostics":
            uri = message["params"]["uri"]
            self.diagnostics[uri] = message["params"]["diagnostics"]

    # ---- the useful operations ------------------------------------------

    def open_file(self, file_path):
        """Tell the server to load a file (required before asking about it)."""
        path = Path(file_path)
        uri = path.as_uri()
        self._file_versions[uri] = 1
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": "python",
                "version": 1,
                "text": path.read_text(),
            }
        })

    def update_file(self, file_path, new_text):
        """Tell the server an ALREADY-OPEN file's content has changed.

        Without this, the server keeps resolving everything against
        whatever text it was first shown at open_file time - a fresh
        tree-sitter parse on OUR side means nothing to the LSP unless we
        explicitly tell it too.
        """
        path = Path(file_path)
        uri = path.as_uri()
        self._file_versions[uri] = self._file_versions.get(uri, 1) + 1
        self._notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": self._file_versions[uri]},
            "contentChanges": [{"text": new_text}],  # full-document replace
        })
        # any cached symbol list for this file is now stale
        self._document_symbols_cache.pop(str(path), None)

    def find_definition(self, file_path, line, column):
        """THE question: 'the name at this exact spot - where is it defined?'

        line and column are 0-based (LSP convention).
        Returns (full file path, line number) or None if the server can't tell.
        The FULL path is returned (not just the filename) because
        get_symbol_kind needs to open that exact file to look the symbol up.
        """
        result = self._request("textDocument/definition", {
            "textDocument": {"uri": Path(file_path).as_uri()},
            "position": {"line": line, "character": column},
        })
        if not result:
            return None
        first = result[0]
        def_file = first["uri"].removeprefix("file://")
        def_line = first["range"]["start"]["line"] + 1  # back to 1-based
        return def_file, def_line

    # LSP SymbolKind values that mean "this executes real code when
    # referenced" - as opposed to Field (8) or Variable (13), which are
    # just stored data. Method (6) covers BOTH ordinary methods AND
    # @property methods - pylsp doesn't give properties a distinct kind.
    # Class (5) is included too: since we only ever capture a class name
    # when it's actually CALLED (e.g. Calculator()), a resolved Class kind
    # here always means "this constructed an object, running __init__" -
    # never a bare, uncalled class reference.
    EXECUTABLE_KINDS = {5, 6, 9, 12}  # Class, Method, Constructor, Function

    def get_symbol_kind(self, file_path, line):
        """What KIND of thing is defined at this line - Method, Function,
        Field, Variable, Class...? `line` is 1-based, matching
        find_definition's return convention. Returns the raw LSP
        SymbolKind integer, or None if no matching symbol is found.

        pylsp returns symbols in TWO different shapes depending on the
        file: our own project files get the rich "DocumentSymbol" shape
        (selectionRange + optional children); external/library files
        (like builtins.pyi) get the flatter "SymbolInformation" shape
        (location.range instead, no children). We handle both.
        """
        if file_path not in self._document_symbols_cache:
            self._document_symbols_cache[file_path] = self._request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": Path(file_path).as_uri()}},
            )
        result = self._document_symbols_cache[file_path]
        if not result:
            return None

        def symbol_line(symbol):
            if "selectionRange" in symbol:
                return symbol["selectionRange"]["start"]["line"]
            if "location" in symbol:
                return symbol["location"]["range"]["start"]["line"]
            return None

        def search(symbols):
            for symbol in symbols:
                if symbol_line(symbol) == line - 1:
                    return symbol["kind"]
                if "children" in symbol:
                    found = search(symbol["children"])
                    if found is not None:
                        return found
            return None

        return search(result)

    def is_undefined_name(self, file_path, line, column):
        """Did the server flag the name at this exact spot as not existing
        anywhere (a real bug), as opposed to just being unresolved for some
        other reason (external library, dynamic code, etc.)?
        """
        uri = Path(file_path).as_uri()
        for diagnostic in self.diagnostics.get(uri, []):
            start_line = diagnostic["range"]["start"]["line"]
            end_line = diagnostic["range"]["end"]["line"]
            message = diagnostic.get("message", "").lower()
            if start_line <= line <= end_line and "undefined name" in message:
                return True
        return False

    def stop(self):
        """Shut the server down politely."""
        self._request("shutdown", {})
        self._notify("exit", {})
        self.server.wait(timeout=5)
