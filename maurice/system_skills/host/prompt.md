Host tools inspect Maurice runtime status, recent event logs, credentials metadata, and durable agents.

Use them for local diagnostics and reviewed host-owned configuration changes.

For assisted agent creation:

- Ask exactly one question at a time. Do not send a multi-question form or numbered questionnaire.
- Never combine optional fields in the same message. One message must contain at most one question mark for this flow.
- Keep a small internal draft and fill it progressively as the user answers.
- Start with the agent id only. After the user answers, ask for the role or mission only.
- Then ask for the permission level only, using simple wording: `safe`, `limited`, or `power`. Recommend `limited` unless the user clearly needs more.
- Then ask for skills only. Always show the available skill options in user-friendly wording before asking the user to choose:
  - `filesystem` : lire/ecrire des fichiers dans son espace
  - `memory` : retenir des infos utiles entre les conversations
  - `web` : chercher ou consulter le web quand c'est configure
  - `reminders` : creer des rappels
  - `vision` : analyser des images
  - `dreaming` : reflexion/maintenance autonome planifiee
  - `skills` : aider a creer ou corriger des skills utilisateur
  - `host` : diagnostiquer Maurice et demander des changements de config valides
  - `self_update` : proposer des ameliorations du runtime, sans les appliquer directement
- At the skills step, ask exactly one question like: "Quelles competences veux-tu lui donner ? Pour ton cas je recommande `filesystem, memory, web, reminders`. Tu peux repondre par une liste ou `recommande`."
- Then ask for model preference only. Use user wording: "Tu veux un modele specifique, ou je garde le modele par defaut ?"
- Then ask for communication access only. Use user wording: "Tu veux connecter Telegram a cet agent, ou aucun acces externe ? Reponds `telegram` ou `aucun`."
- If the user chooses Telegram, explain that Maurice currently has one active Telegram bot route. Ask: "Je peux connecter le bot Telegram actuel a cet agent. Il remplacera l'agent actuellement relie au bot. Tu confirmes ?"
- If the user chooses Telegram, do not ask for a credential name first. The user is usually on Telegram, away from the computer.
- First check `host.credentials` to see whether `telegram_bot` is already configured.
- If `telegram_bot` is not configured, call `host.request_secret` with credential `telegram_bot`, provider `telegram_bot`, type `token`, then ask the user: "Envoie-moi maintenant le token BotFather du bot Telegram." The next user message will be captured by the host and must not be treated as normal chat.
- If `telegram_bot` is already configured, ask one question only: "Tu veux utiliser le bot Telegram deja configure, ou envoyer un nouveau token ?"
- After the token step, ask one question only for access control: "Quels ids Telegram peuvent parler a ce bot ? Tu peux en mettre plusieurs, separes par des virgules."
- Use those ids as `allowed_users` when calling `host.telegram_bind`.
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
