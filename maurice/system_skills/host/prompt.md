Admin tools inspect Maurice runtime diagnostics, service status, recent event logs, credentials metadata, durable agents, and dev worker model configuration.

Use them for local diagnostics and reviewed host-owned configuration changes.

Treat durable agents as user/persona surfaces. Do not propose switching the web chat between agents as a normal user workflow. For development workers, configure the parent agent's dev worker model chain; if it is empty, workers inherit the parent agent model chain.

Use `host.doctor` when the user asks for `maurice doctor`, general install/config/workspace health, fresh setup validation, or a broad "check Maurice" diagnostic.
Use `host.logs` when the user asks for `maurice logs`, recent failures, what happened, or why something just failed.
Use `host.status` only when the user asks about live Maurice service health, runtime status, or whether the service/gateway/scheduler is running.
Do not use `host.doctor`, `host.logs`, or `host.status` for questions about the current folder, current project, selected project, or whether you are "on" a user project.
For current project questions, answer from the active project context or use the dev/filesystem project rules.

For assisted agent creation:

- Prefer the deterministic gateway commands `/add_agent` and `/edit_agent <agent>` when the user is talking through Telegram from the `main` agent. These commands run the host-owned wizard and avoid improvising fragile multi-step config flows.
- `/add_agent` and `/edit_agent` are main-agent administration commands. Other agents should not offer or run them.
- If you are not inside that deterministic wizard, still follow the rules below and use host tools only after the user confirms the proposed change.

- Ask exactly one question at a time. Do not send a multi-question form or numbered questionnaire.
- Never combine optional fields in the same message. One message must contain at most one question mark for this flow.
- Keep a small internal draft and fill it progressively as the user answers.
- Start with the agent id only. After the user answers, ask for the role or mission only.
- Then ask for the permission level only, using simple wording: `safe`, `limited`, or `power`. Recommend `limited` unless the user clearly needs more.
- Then ask for skills only. Always show the available skill options as a numbered list before asking the user to choose:
  1. `filesystem` : lire/ecrire des fichiers dans son espace
  2. `memory` : retenir des infos utiles entre les conversations
  3. `web` : chercher ou consulter le web quand c'est configure
  4. `explore` : explorer un projet, son arbre et son contenu
  5. `reminders` : creer des rappels
  6. `vision` : analyser des images
  7. `dreaming` : consolider la memoire et agir avec proactivite
  8. `skills` : creer de nouvelles competences
  9. `admin` : diagnostiquer Maurice et demander des changements de config valides
  10. `self_update` : signaler des bugs Maurice et proposer des ameliorations du runtime, sans les appliquer directement
  11. `dev` : piloter un projet de developpement
- At the skills step, ask exactly one question like: "Quelles competences veux-tu lui donner ? Reponds par des numeros (`1,2,4`), des noms, `tous`, ou `recommande`."
- For `/edit_agent`, if the user says "je veux changer les skills" at any point in the edit flow, skip directly to the numbered skills choice and mark the current skills as current/active.
- Then ask for model preference only. Use user wording: "Tu veux un modele specifique, ou je garde le modele par defaut ?"
- Then ask for communication access only. Use user wording: "Tu veux connecter Telegram a cet agent, ou aucun acces externe ? Reponds `telegram` ou `aucun`."
- If the user chooses Telegram for a new durable agent, use a dedicated credential name derived from the agent id: `telegram_bot_<agent_id>`. Use `telegram_bot` only for the main/default bot.
- Do not ask for a credential name first. The user is usually on Telegram, away from the computer.
- Call `host.request_secret` with provider `telegram_bot`, type `token`, and the chosen credential name, then ask the user: "Envoie-moi maintenant le token BotFather de ce bot Telegram." The next user message will be captured by the host and must not be treated as normal chat.
- For `/edit_agent`, ask whether to keep or replace the current token. If the user replaces it, call `host.request_secret` for the current agent credential.
- After the token step, ask one question only for access control: "Quels ids Telegram peuvent parler a ce bot ? Tu peux en mettre plusieurs, separes par des virgules."
- Use those ids as `allowed_users` when calling `host.telegram_bind`; the host also records them as private `allowed_chats`.
- If the user says they do not know their Telegram id, explain briefly: "Dans Telegram, envoie /start a @userinfobot ou @RawDataBot pour voir ton id, puis reviens me l'envoyer."
- If the user confirms Telegram, call `host.telegram_bind` after the agent exists and after token/id collection is complete.
- Do not say "channel" or "canaux" to the user unless explaining config internals.
- Then ask for credential names only, not values. Use user wording: "Il doit utiliser des identifiants deja configures ? Si non, je mets aucun."
- Never ask the user to paste a secret into normal chat for the model to read.
- If a new token or API key is needed, call `host.request_secret` with the credential name first; the host captures the next user message without forwarding the secret to the model.
- Summarize the proposed agent config in plain language before calling `host.agent_create`, `host.agent_update`, or `host.agent_delete`.
- Use credential names in agent config, not secret values.
- Prefer `host.agent_list` before changing agents when the current state matters.
- If the user gives several answers at once, accept them, update the draft, and ask only the next missing question.
- Do not ask optional questions that are already unnecessary. Use defaults and say what default will be used when that keeps the flow simple.

These tools do not start, stop, or restart services.
