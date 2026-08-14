"""A simple popup-window chat interface.

Replaces terminal print()/input() calls with an actual graphical window -
a running message log plus real Yes/No dialog boxes, using tkinter
(built into Python, no extra install needed). This is a stand-in for
what a real VS Code webview panel would eventually do - same idea
(show messages, ask yes/no, get an answer back), just running as its
own desktop window instead of living inside VS Code.
"""

import tkinter as tk
from tkinter import scrolledtext, messagebox


class ChatUI:
    def __init__(self, title="Code Impact Analyzer"):
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("700x500")

        self.log = scrolledtext.ScrolledText(self.root, wrap=tk.WORD, state="disabled")
        self.log.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    def show_message(self, text):
        """Append a message to the chat log, like a chat bubble appearing."""
        self.log.configure(state="normal")
        self.log.insert(tk.END, text + "\n\n")
        self.log.configure(state="disabled")
        self.log.see(tk.END)  # auto-scroll to the bottom
        self.root.update()  # redraw the window right now, don't wait

    def ask_yes_no(self, question):
        """Show the question in the log, then pop up a real Yes/No dialog.
        Blocks until the user clicks one, then returns True or False -
        no more parsing typed text like "yes"/"Yes"/"YES".
        """
        self.show_message(f"❓ {question}")
        answer = messagebox.askyesno("Confirm", question)
        self.show_message("You answered: " + ("Yes" if answer else "No"))
        return answer

    def close(self):
        self.root.destroy()

    def run(self):
        """Keep the window open, responsive, and scrollable after the
        script's own work is done, until the user closes it themselves."""
        self.root.mainloop()
