---
name: Project Overview
description: Maurice est un runtime d'agent IA contract-first (réécriture de Jarvis), architecture host/kernel/skills, Python 3.12+
type: project
---

Maurice est un runtime d'agent IA contract-first, réécriture propre de "Jarvis".

**Why:** Remplacer les chemins de fonctionnalités codés en dur par des contrats clairs, en gardant le comportement produit validé par Jarvis.

**How to apply:** Toute nouvelle capacité doit être un skill (pas dans le kernel), respecter les contrats typés Pydantic, et passer par le système de permissions.

## Architecture

- **host** : CLI (`maurice`), gateway HTTP/Telegram, onboarding, agent_wizard, channels, service, dashboard
- **kernel** : boucle AgentLoop.run_turn(), permissions (profils safe/limited/power), sessions, events, scheduler, approvals
- **skills** : filesystem, memory, dreaming, dev, reminders, web, vision, self_update, host, skills authoring

## Points d'entrée

- `maurice/kernel/loop.py` — AgentLoop (boucle principale)
- `maurice/host/cli.py` — CLI `maurice`
- `maurice/host/gateway.py` — gateway HTTP/Telegram
- `maurice/host/commands.py` — CommandRegistry (slash commands)

## Stack

- Python 3.12+, Pydantic v2, PyYAML, rich, textual
- Tests : pytest, 189 tests attendus (branche master en cours de travail)

## État branche master (2026-04-27)

Beaucoup de changements en cours : nouveau skill `dev` (commandes /project, /plan, /dev, /review, /decision), agent_wizard, model_catalog, web_ui.html, commands.py.
