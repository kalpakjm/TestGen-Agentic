# Prompt and interaction design

| Prompt | File | Used by | Status |
| --- | --- | --- | --- |
| Test generation | `prompts/test_generation.txt` | Test generation agent | Baseline (Ollama backend) |
| Test repair | `prompts/test_repair.txt` | Test generation agent, after failed runs | Baseline (Ollama backend) |
| Runtime migration | `prompts/runtime_migration.txt` | Migration agent | Planned |

## Design choices
- **Narrow task:** one function per call, with its source, inferred schema and the helpers that already exist in the test file. This keeps the context small enough for a 7B local model.
- **Grounded context:** the prompt includes retrieved guidance (for example "Testing window functions") instead of relying on the model's memory.
- **Constrained output:** only test functions, no imports, names must start with `test_<function>`. The agent rejects answers that fail this check and uses the template instead.
- **Verification over trust:** the model is never the judge of its own output. Generated tests are executed; failures go back to the model (`test_repair.txt`) or are quarantined.
- **Low temperature (0.1)** for stable code generation.
