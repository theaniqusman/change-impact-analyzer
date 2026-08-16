"""A simple popup-window chat interface.

Replaces terminal print()/input() calls with an actual graphical window -
a running message log, real Yes/No dialog boxes, and a real typing box,
using tkinter (built into Python, no extra install needed). This is a
stand-in for what a real VS Code webview panel would eventually do - same
idea (show messages, ask yes/no, get typed text back), just running as its
own desktop window instead of living inside VS Code.

IMPORTANT: the actual analysis work (git, parsing, LSP, OpenAI calls) runs
on a background thread in main.py, NOT this one. Tkinter's window must live
on the main thread (macOS refuses to draw/respond to it otherwise), so
every method here that's called from that background thread only ever
queues a job for the GUI thread to run - it never touches a widget itself.
"""

import threading
import queue
import tkinter as tk
from tkinter import scrolledtext, messagebox

# VS Code's actual "Dark+" theme colors, not an invented palette - so this
# window reads as an extension of the editor itself instead of a separate
# app with its own look.
BG = "#1e1e1e"           # VS Code editor background
LOG_BG = "#1e1e1e"       # same - seamless, no separate panel color
TEXT_FG = "#d4d4d4"      # VS Code's default editor text color
HEADER_FG = "#569cd6"    # VS Code's keyword blue - "=== SECTION ===" headers
QUESTION_FG = "#dcdcaa"  # VS Code's function-name yellow - "❓ ..." questions
USER_FG = "#b5cea8"      # VS Code's number green - your own replies
ERROR_FG = "#f44747"     # VS Code's actual error-squiggle red
ACCENT = "#0e639c"       # VS Code's primary button blue
ACCENT_HOVER = "#1177bb"  # VS Code's primary button hover blue
ENTRY_BG = "#3c3c3c"     # VS Code's input-box background
ENTRY_BORDER = "#007acc"  # VS Code's focus-border blue
DIVIDER_FG = "#2d2d2d"   # VS Code's subtle panel-divider gray
PLACEHOLDER_FG = "#767676"  # VS Code's placeholder-text gray
FONT = ("Menlo", 13)
FONT_BOLD = ("Menlo", 13, "bold")
TITLE_BAR_BG = "#323233"  # VS Code's title-bar gray
TITLE_BAR_FG = "#cccccc"
FONT_TITLE = ("Menlo", 12)

PLACEHOLDER_TEXT = "Type a message or answer..."


class ChatUI:
    def __init__(self, title="Code Impact Analyzer"):
        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(bg=BG)

        # Size the window to fit comfortably within the actual screen,
        # not a fixed 760x560 that can end up taller than the visible
        # desktop (pushing the entry box off the bottom of the screen,
        # below the Dock, with no way to see or reach it).
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        win_w = min(760, screen_w - 80)
        win_h = min(560, screen_h - 160)  # leaves headroom for the Dock/menu bar
        x = (screen_w - win_w) // 2
        y = max(40, (screen_h - win_h) // 3)  # a bit above center, not pinned to it
        self.root.geometry(f"{win_w}x{win_h}+{x}+{y}")

        # Ask macOS to draw the NATIVE title bar (the traffic-light one,
        # which tkinter itself can't touch directly) in dark mode too, so
        # it doesn't stay a bright white strip above our own dark content.
        # This is an unsupported/private Tk API - only available on macOS,
        # and only on some Tk builds - so it's wrapped in case it's not.
        try:
            self.root.tk.call("::tk::unsupported::MacWindowStyle", "appearance", self.root, "dark")
        except tk.TclError:
            pass

        # Force the window to the front and give it focus immediately -
        # without this, macOS opens it behind whatever app (e.g. VS Code)
        # already has focus, and the user has to manually click its dock
        # icon to even see it.
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after_idle(self.root.attributes, "-topmost", False)
        self.root.focus_force()

        # ---- title bar -----------------------------------------------------
        # VS Code doesn't draw a big colored banner inside its own window -
        # just a thin, plain, dark gray strip with small centered text. Copy
        # that instead of inventing a separate "app header" look.
        title_bar = tk.Frame(self.root, bg=TITLE_BAR_BG, height=28)
        title_bar.pack(fill=tk.X, side=tk.TOP)
        title_bar.pack_propagate(False)
        tk.Label(
            title_bar, text=title, bg=TITLE_BAR_BG, fg=TITLE_BAR_FG,
            font=FONT_TITLE, anchor="center",
        ).pack(fill=tk.BOTH, expand=True)

        # ---- message log ---------------------------------------------------
        # Created here, but NOT packed yet - packed further below, AFTER the
        # entry row. tkinter's pack() geometry manager gives fixed-size
        # widgets their space based on pack ORDER, not creation order: an
        # expand=True widget packed before a fixed-size one can end up
        # greedily claiming everything, leaving the later widget with zero
        # height and never actually mapped onscreen (exactly what was
        # happening to the entry box here).
        self.log = scrolledtext.ScrolledText(
            self.root, wrap=tk.WORD, state="disabled",
            bg=LOG_BG, fg=TEXT_FG, insertbackground=TEXT_FG,
            font=FONT, relief=tk.FLAT, borderwidth=0, padx=12, pady=10,
        )

        # Color-code different kinds of messages instead of one flat color -
        # makes it much easier to scan at a glance (headers vs. questions
        # vs. your own replies vs. errors).
        self.log.tag_configure("header", foreground=HEADER_FG, font=FONT_BOLD)
        self.log.tag_configure("question", foreground=QUESTION_FG, font=FONT_BOLD)
        self.log.tag_configure("user", foreground=USER_FG, font=FONT_BOLD)
        self.log.tag_configure("error", foreground=ERROR_FG, font=FONT_BOLD)
        self.log.tag_configure("normal", foreground=TEXT_FG)
        self.log.tag_configure("divider", foreground=DIVIDER_FG)

        # A real typing box + Send button, for when we need the user to
        # type something back instead of just clicking Yes/No.
        entry_frame = tk.Frame(self.root, bg=BG)
        entry_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=12, pady=(0, 14))

        self.entry = tk.Entry(
            entry_frame, bg=ENTRY_BG, fg=PLACEHOLDER_FG, insertbackground=TEXT_FG,
            font=FONT, relief=tk.FLAT, highlightthickness=1,
            highlightbackground=ENTRY_BG, highlightcolor=ENTRY_BORDER,
        )
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=(0, 8))

        self.send_button = tk.Button(
            entry_frame, text="Send", command=self._submit_entry,
            bg=ACCENT, fg="#ffffff", activebackground=ACCENT_HOVER,
            activeforeground="#ffffff", font=FONT, relief=tk.FLAT,
            padx=14, pady=4, cursor="hand2",
        )
        self.send_button.pack(side=tk.LEFT)
        self.send_button.bind("<Enter>", lambda e: self.send_button.configure(bg=ACCENT_HOVER))
        self.send_button.bind("<Leave>", lambda e: self.send_button.configure(bg=ACCENT))

        # NOW pack the log, last - it takes whatever space is left over
        # after the title bar and entry row have already claimed theirs.
        self.log.pack(fill=tk.BOTH, expand=True, padx=12, pady=(12, 8))
        # Hide the scrollbar strip itself (a plain tk.Scrollbar can't be
        # recolored to match the dark theme - macOS renders it natively
        # regardless of color options) - the log is still fully scrollable
        # via trackpad/mouse wheel/arrow keys, it just won't show a bar.
        self.log.vbar.pack_forget()

        self.entry.bind("<Return>", lambda event: self._submit_entry())

        # A plain tk.Entry has no built-in placeholder text - fake one:
        # show grayed-out hint text when empty/unfocused, clear it (and
        # switch to normal text color) the moment the user actually types.
        self._show_placeholder()
        self.entry.bind("<FocusIn>", self._clear_placeholder)
        self.entry.bind("<FocusOut>", lambda e: self._show_placeholder())

        self._pending_text_event = None  # set only while ask_text() is waiting

        # The worker thread (main.py's actual pipeline) is never allowed to
        # touch a Tk widget directly - it drops a job in this queue instead,
        # and this timer (running on the GUI thread) drains it every 50ms.
        # This is also what keeps the window responsive continuously,
        # instead of only during the brief moments code used to call
        # root.update() by hand.
        self._jobs = queue.Queue()
        self.root.after(50, self._process_jobs)

        # Anything typed and sent while NO ask_text() is waiting lands here
        # instead of being thrown away - this is what makes the box always
        # listening: the worker thread can pull from this queue at any time
        # to pick up an unprompted request (e.g. "check sample_utils.py").
        self.user_messages = queue.Queue()

    def _show_placeholder(self):
        if not self.entry.get():
            self.entry.insert(0, PLACEHOLDER_TEXT)
            self.entry.configure(fg=PLACEHOLDER_FG)

    def _clear_placeholder(self, event=None):
        if self.entry.get() == PLACEHOLDER_TEXT:
            self.entry.delete(0, tk.END)
            self.entry.configure(fg=TEXT_FG)

    def _process_jobs(self):
        try:
            while True:
                job = self._jobs.get_nowait()
                job()
        except queue.Empty:
            pass
        self.root.after(50, self._process_jobs)

    # tag name -> a small icon to prepend, purely decorative. Questions
    # already carry their own "❓" (baked into the text by ask_yes_no /
    # ask_text), so no icon is added there to avoid doubling up.
    _ICONS = {"header": "📌 ", "user": "💬 ", "error": "⚠️  "}

    def _tag_for(self, text):
        """Pick which color/style a message should get, based on a quick
        look at how it starts - not fancy, just enough to make the log
        scannable at a glance."""
        if text.startswith("=== SOMETHING WENT WRONG") or "PIPELINE CRASHED" in text:
            return "error"
        if text.startswith("❓"):
            return "question"
        if text.startswith(("You said:", "You typed:", "You answered:")):
            return "user"
        if text.startswith("==="):
            return "header"
        return "normal"

    def _append_to_log(self, text):
        self.log.configure(state="normal")
        # A thin divider between messages (skipped before the very first
        # one) - makes the log read as distinct entries instead of one
        # unbroken wall of text.
        if float(self.log.index(tk.END)) > 2.0:
            self.log.insert(tk.END, "─" * 60 + "\n", "divider")
        tag = self._tag_for(text)
        icon = self._ICONS.get(tag, "")
        self.log.insert(tk.END, icon + text + "\n\n", tag)
        self.log.configure(state="disabled")
        self.log.see(tk.END)  # auto-scroll to the bottom

    def show_message(self, text):
        """Append a message to the chat log. Safe to call from the worker
        thread - it just queues the actual widget update for the GUI
        thread to perform.
        """
        self._jobs.put(lambda: self._append_to_log(text))

    def ask_yes_no(self, question):
        """Show the question in the log, then pop up a real Yes/No dialog.
        Safe to call from the worker thread: the dialog itself is created
        on the GUI thread, and this call blocks (on whichever thread called
        it) until the user answers, then returns a real True/False.
        """
        self.show_message(f"❓ {question}")
        done = threading.Event()
        result = {}

        def _do():
            result["answer"] = messagebox.askyesno("Confirm", question)
            done.set()

        self._jobs.put(_do)
        done.wait()
        answer = result["answer"]
        self.show_message("You answered: " + ("Yes" if answer else "No"))
        return answer

    def ask_text(self, question):
        """Show the question in the log, then wait for the user to actually
        type something into the entry box and hit Enter (or click Send).
        Safe to call from the worker thread; blocks until an answer is
        typed, then returns exactly what they typed, as a string.
        """
        self.show_message(f"❓ {question}")
        done = threading.Event()
        result = {}

        def _arm():
            self._pending_text_event = (done, result)
            self.entry.focus_set()

        self._jobs.put(_arm)
        done.wait()
        answer = result["answer"]
        self.show_message(f"You typed: {answer}")
        return answer

    def _submit_entry(self):
        """Runs on the GUI thread (Send click or Enter key). If ask_text()
        is currently waiting, the typed text answers that. Otherwise, it's
        an unprompted message - drop it in user_messages instead of
        throwing it away, so the worker thread can pick it up whenever it
        checks.
        """
        text = self.entry.get()
        self.entry.delete(0, tk.END)
        if not text or text == PLACEHOLDER_TEXT:
            return
        if self._pending_text_event is not None:
            done, result = self._pending_text_event
            result["answer"] = text
            self._pending_text_event = None
            done.set()
        else:
            self._append_to_log(f"You said: {text}")
            self.user_messages.put(text)

    def wait_for_message(self):
        """Blocks (on whichever thread calls this - meant for the worker
        thread) until the user types something and hits Send with no
        question currently pending, then returns exactly what they typed.
        """
        return self.user_messages.get()

    def try_get_message(self):
        """Non-blocking check: did the user already type something
        unprompted (before being asked)? Returns it if so, or None if
        nothing's waiting - lets the caller decide whether to actively ask
        a question instead of just sitting there silently.
        """
        try:
            return self.user_messages.get_nowait()
        except queue.Empty:
            return None

    def close(self):
        self.root.destroy()

    def run(self):
        """Keep the window open, responsive, and scrollable. Call this ONCE,
        on the main thread, after starting the background thread that does
        the actual analysis work - this is what keeps the window listening
        continuously, instead of freezing while that work runs."""
        self.root.mainloop()
