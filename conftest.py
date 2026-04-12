import os

# Disable LLM policy engine during tests so all tests use the rule-based
# fallback by default. Individual tests that want to exercise the LLM path
# should patch `src.agentic_os.ollama_policy._ENABLED` directly.
os.environ.setdefault("AGENTIC_OS_LLM_POLICY", "false")
