# Agents in this Repository

## Overview

This repository demonstrates building agents using LangGraph, focusing on an email assistant that can:
- Triage incoming emails
- Draft appropriate responses
- Execute actions (calendar scheduling, etc.)
- Incorporate human feedback
- Learn from past interactions

## Environment Setup

**Recommended: Using uv (faster and more reliable)**

```bash
# Install uv if you haven't already
pip install uv

# Install the package with development dependencies
uv sync --extra dev
```

**Alternative: Using pip**

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Ensure you have a recent version of pip (required for editable installs with pyproject.toml)
python3 -m pip install --upgrade pip

# Install the package in editable mode
pip install -e .
```

The package is installed as `interrupt_workshop` with import name `email_assistant`, allowing you to import from anywhere with `from email_assistant import ...`

## Agent Implementations

### Scripts 

The repository contains several implementations with increasing complexity in `src/email_assistant`:

1. **LangGraph 101** (`langgraph_101.py`)
   - Basics of LangGraph 

2. **Basic Email Assistant** (`email_assistant.py`)
   - Core email triage and response functionality

3. **Human-in-the-Loop** (`email_assistant_hitl.py`) 
   - Adds ability for humans to review and approve actions

4. **Memory-Enabled HITL** (`email_assistant_hitl_memory.py`)
   - Adds persistent memory to learn from feedback

5. **Gmail Integration** (`email_assistant_hitl_memory_gmail.py`)
   - Connects to Gmail API for real email processing

### Notebooks

Each aspect of the agent is explained in dedicated notebooks:
- `notebooks/langgraph_101.ipynb` - LangGraph basics
- `notebooks/agent.ipynb` - Basic agent implementation
- `notebooks/evaluation.ipynb` - Agent evaluation
- `notebooks/hitl.ipynb` - Human-in-the-loop functionality
- `notebooks/memory.ipynb` - Adding memory capabilities

## Running Tests

### Testing Scripts

Test to ensure all implementations work:

```bash
# Test all implementations
python tests/run_all_tests.py --all
```

(Note: This will leave out the Gmail implementation `email_assistant_hitl_memory_gmail` from testing.)

### Testing Notebooks

Test all notebooks to ensure they run without errors:

```bash
# Run all notebook tests directly
python tests/test_notebooks.py
```


## Academy Support Agent rules

This repo is being turned into the **Academy Support Agent** for EduChess (Champion Chess Academy, Visakhapatnam). Plan and audit: `docs/agent/PLAN.md`, `docs/agent/AUDIT.md`. These rules override anything above.

### Hard rules (enforce in code, not only in prompts)

1. **Owner approval on every send and every calendar change.** Any tool that sends a message or creates/changes/deletes a calendar event must go through a HITL interrupt. Deny by default: only tools on an explicit read-only allow-list may run without approval.
2. **No invented facts.** Never state a fee, date, time, or seat count unless it came from a tool result in the same run.
3. **Sensitive messages are never drafted.** A message labelled `sensitive` goes to the owner only; it must never reach the drafting agent, whatever memory or feedback says.
4. **EduChess is read-only.** Use only the Supabase anon key. Never write, update or delete EduChess data. Never use a service-role key for EduChess.
5. **Minimal child data, synthetic data only.** Keep only what the task needs (e.g. first name). Tests, fixtures and eval data are synthetic. Never log message bodies or personal data.
6. **No chess teaching from the model.** The agent handles admin (demos, fees, schedules, events); it does not give chess lessons or advice.
7. **Secrets.** Keys live in `.env` (git-ignored) or the deploy platform's secret store. Never print, log, commit or echo keys or tokens.
8. **Licence.** Keep the MIT `LICENSE` and credit the base repo `langchain-ai/agents-from-scratch`.
9. **Dockerfile is owner-written.** Never create or edit a `Dockerfile`.

### Models

- Free tiers only, no paid keys. Gemini Flash-Lite for triage; Gemini Flash for drafting and tool use (Google AI Studio key). The eval judge is an open model on Groq's free tier (different model family from the drafter).
- Provider and model names come from env vars, never hard-coded. Check current model names in the official docs, don't guess.
- Every model call needs retry with exponential backoff (free tiers rate-limit).
- Tracing stays on LangSmith.

### Working method

- Cloud sessions cannot see `.env` and have limited network: write code and tests that run with fake/stub models; real-model tests run on the owner's laptop (Windows, uv).
- Each phase: new branch; smallest change that meets "done when"; run no-key tests before and after; clear commit message; update `docs/agent/PLAN.md` status; give the exact laptop commands to pull and run real tests.
