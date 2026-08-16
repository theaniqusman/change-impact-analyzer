# Code Impact Analyzer

Analyzes how a code change will ripple through your codebase before you make
it, using tree-sitter and a language server for accurate tracing, then LLM
agents to explain the risk and fix anything that would break.

## Architectural Diagram

<img width="1336" height="940" alt="architectural-diagram-v3" src="https://github.com/user-attachments/assets/e8645982-8f00-4040-9427-4a4fded1c8e3" />

## How it works

1. **Parses** every Python file in the project with tree-sitter, and
   resolves what calls what using a language server (`pylsp`).
2. **Detects** whatever you've actually edited and saved, via git.
3. **Traces the blast radius** - every other piece of code that depends on
   what you changed, directly or through any number of hops - deterministically,
   with no API cost.
4. **Asks an LLM** to explain the risk in plain English, and give you a
   clear recommendation.
5. If you decide to go ahead with the change, a **fix agent** can update
   every other affected piece of code so it still works.

Everything happens in a popup window - it asks you which change you mean
(if there's more than one), shows you what's affected, gives you its
verdict, and asks for confirmation before touching anything.

## Setup

Clone the repository:

```bash
git clone https://github.com/theaniqusman/changeimpactanalyzer.git
cd changeimpactanalyzer
```

Create a virtual environment and install the required packages:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Set your OpenAI API key as an environment variable (the tool uses
`gpt-4o-mini`):

```bash
export OPENAI_API_KEY="your-key-here"
```

## Usage

1. **Run it once first**, before making any changes - open `main.py` in
   VS Code and click the Run button.

   The very first run just establishes a baseline - it commits the current
   state of the project to git so there's something to compare future
   edits against. It'll tell you there are no uncommitted changes yet, and
   that's expected.
2. Now edit a Python file inside this project (or point it at your own
   project by changing the file discovery in `main.py`), and **save it**.
   The tool can only see changes that are actually written to disk.
3. Click the Run button again.
4. A popup window opens. It'll ask which change you mean (if there's more
   than one uncommitted file), then walk you through what's affected and
   give you its recommendation.
5. Answer its yes/no questions in the window - it can revert the change,
   or fix every other affected piece of code for you.

You can also just type into the popup at any time (e.g. "check the change
I made in `sample_utils.py`") - you don't have to wait to be asked.

## Requirements

- Python 3.9+
- An OpenAI API key
- `pylsp` (installed automatically via `requirements.txt`)

## License

MIT - see [LICENSE](LICENSE).
