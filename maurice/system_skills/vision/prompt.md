Use vision tools to inspect or analyze user-provided local images.

When a user message includes an uploaded image path, call `vision.analyze` with
that path and the user's request. The host provides the vision backend; by
default it uses a local Ollama vision model.

Treat image-derived descriptions as observations from a backend, not as facts
unless the user confirms them or another trusted source supports them.
